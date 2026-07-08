"""Binance Spot compounding bot -- RSI mean-reversion DCA (crash-resilient).

Production hardening:
  * Atomic state persistence (state.py): every fill/exit/halt is saved to
    state.json, so a VPS reboot / OOM kill / systemd restart never causes
    the bot to forget an open position.
  * Startup reconciliation: on boot, state.json is cross-checked against
    actual Binance holdings. Mismatches pause the bot instead of trading
    blind (send /resume via Telegram, or delete state.json, to continue).
  * Graceful shutdown: SIGTERM/SIGINT save state, notify, and exit cleanly.
  * Symbol status check: orders are skipped if the pair is not TRADING
    (e.g. CANCEL_ONLY during exchange maintenance).
  * Persisted circuit breaker: a tripped breaker stays tripped across
    restarts -- systemd auto-restart cannot resurrect a halted bot.

Safety defaults:
  * DRY_RUN defaults to True (Binance Spot TESTNET). Set DRY_RUN=false only
    after paper-trading successfully.
  * Secrets come from environment variables only, never hardcoded:
      BINANCE_API_KEY, BINANCE_API_SECRET   (Spot Trading permission ONLY,
      withdrawals disabled)
  * Optional: SYMBOL (default BTCUSDT), DRY_RUN (default true),
    TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID for alerts and /resume.

Run: pip install -r requirements.txt && python3 bot.py
"""

import logging
import os
import signal
import sys
import time
from decimal import Decimal, ROUND_DOWN

from binance.client import Client
from binance.exceptions import BinanceAPIException

import notify
import state as statemod
from strategy import Position, decide, rsi, RSI_PERIOD, CIRCUIT_BREAKER

SYMBOL = os.environ.get("SYMBOL", "BTCUSDT")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
POLL_SECONDS = 60  # check once per minute; decisions use closed 1h candles
STATUS_REFRESH_SECONDS = 300  # re-check symbol trading status every 5 min

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("trades.log")],
)
log = logging.getLogger("bot")


# ---------------------------------------------------------------- exchange

def get_client():
    key = os.environ.get("BINANCE_API_KEY")
    secret = os.environ.get("BINANCE_API_SECRET")
    if not key or not secret:
        raise SystemExit("Set BINANCE_API_KEY and BINANCE_API_SECRET env vars first.")
    return Client(key, secret, testnet=DRY_RUN)


def get_symbol_meta(client):
    """LOT_SIZE step, minNotional, base asset, and trading status."""
    info = client.get_symbol_info(SYMBOL)
    if not info:
        raise SystemExit(f"Symbol {SYMBOL} not found on this exchange.")
    step, min_notional = Decimal("0.00001"), Decimal("5")
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step = Decimal(f["stepSize"])
        elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
            min_notional = Decimal(f.get("minNotional", f.get("notional", "5")))
    return step, min_notional, info["baseAsset"], info.get("status", "TRADING")


def symbol_is_trading(client):
    """True only if the pair is in TRADING status (not CANCEL_ONLY etc.)."""
    try:
        info = safe_call(client.get_symbol_info, SYMBOL)
        status = (info or {}).get("status", "UNKNOWN")
        if status != "TRADING":
            log.warning("Symbol %s status is %s -- orders suspended", SYMBOL, status)
            return False
        return True
    except Exception as e:
        log.warning("Could not verify symbol status (%s) -- skipping orders", e)
        return False


def round_step(qty, step):
    return float(Decimal(str(qty)).quantize(step.normalize(), rounding=ROUND_DOWN))


def safe_call(fn, *args, retries=5, **kwargs):
    """Call a Binance endpoint with backoff for rate limits / transient errors."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except BinanceAPIException as e:
            if e.code in (-1003, -1015):  # rate limit / too many orders
                wait = 2 ** attempt * 10
                log.warning("Rate limited (%s); sleeping %ss", e.code, wait)
                time.sleep(wait)
            elif e.code == -2010:  # insufficient balance
                log.error("Insufficient balance: %s -- skipping order", e.message)
                return None
            else:
                raise
        except Exception as e:  # network blips
            log.warning("Transient error: %s; retrying", e)
            time.sleep(5)
    log.error("Giving up after %d retries", retries)
    return None


def market_buy(client, spend_quote, step, min_notional, price):
    if not symbol_is_trading(client):
        return None
    if Decimal(str(spend_quote)) < min_notional:
        log.info("Buy of $%.2f below minNotional $%s -- skipped", spend_quote, min_notional)
        return None
    qty = round_step(spend_quote / price, step)
    if qty <= 0 or Decimal(str(qty * price)) < min_notional:
        log.info("Rounded qty %s fails minNotional -- skipped", qty)
        return None
    order = safe_call(client.order_market_buy, symbol=SYMBOL, quantity=qty)
    if order:
        fill = float(order["cummulativeQuoteQty"]) / float(order["executedQty"])
        log.info("BUY  %s %s @ ~%.2f ($%.2f)", order["executedQty"], SYMBOL, fill, spend_quote)
        return float(order["executedQty"]), fill
    return None


def market_sell(client, qty, step, min_notional, price):
    if not symbol_is_trading(client):
        return None
    qty = round_step(qty, step)
    if qty <= 0 or Decimal(str(qty * price)) < min_notional:
        log.info("Sell qty %s fails minNotional -- holding dust", qty)
        return None
    order = safe_call(client.order_market_sell, symbol=SYMBOL, quantity=qty)
    if order:
        proceeds = float(order["cummulativeQuoteQty"])
        log.info("SELL %s %s -> $%.2f", order["executedQty"], SYMBOL, proceeds)
        return proceeds
    return None


def free_balance(client, asset):
    bal = safe_call(client.get_asset_balance, asset=asset)
    return float(bal["free"]) if bal else 0.0


def closed_hourly_closes(client, limit=RSI_PERIOD + 60):
    kl = safe_call(client.get_klines, symbol=SYMBOL,
                   interval=Client.KLINE_INTERVAL_1HOUR, limit=limit)
    if not kl:
        return None
    return [float(k[4]) for k in kl[:-1]]  # drop the still-open candle


# ---------------------------------------------------------------- state I/O

def snapshot(pos, start_equity, paused, halted):
    return {
        "symbol": SYMBOL,
        "dry_run": DRY_RUN,
        "qty": pos.qty,
        "cost": pos.cost,
        "base_price": pos.base_price,
        "safety_used": pos.safety_used,
        "session_start_equity": start_equity,
        "paused": paused,
        "halted": halted,
    }


def restore_position(st):
    pos = Position()
    pos.qty = float(st.get("qty") or 0.0)
    pos.cost = float(st.get("cost") or 0.0)
    pos.base_price = st.get("base_price")
    pos.safety_used = bool(st.get("safety_used"))
    return pos


def reconcile(client, base_asset, step):
    """Cross-check state.json against actual Binance holdings.

    Returns (pos, start_equity_or_None, paused, halted).
    start_equity None => caller measures a fresh session start.
    """
    st, ok = statemod.load()
    coin_free = free_balance(client, base_asset)
    dust = float(step)  # anything below one LOT_SIZE step is untradeable dust
    holding = coin_free > dust

    if st is not None and ok:
        if st.get("symbol") != SYMBOL or bool(st.get("dry_run")) != DRY_RUN:
            msg = (f"State file is for {st.get('symbol')} (dry_run={st.get('dry_run')}) "
                   f"but bot is configured for {SYMBOL} (dry_run={DRY_RUN}). "
                   "PAUSED. Send /resume to trade fresh, or fix the config.")
            log.error(msg)
            notify.send("WARNING: " + msg)
            return Position(), None, True, False
        if st.get("halted"):
            log.error("State file says circuit breaker tripped previously. Staying HALTED. "
                      "Delete state.json to reset after reviewing what happened.")
            notify.send("Bot restarted but circuit breaker was tripped -- staying halted.")
            return restore_position(st), st.get("session_start_equity"), True, True
        if holding and st.get("qty", 0) > 0:
            pos = restore_position(st)
            log.info("Restored position from state.json: %.8f %s @ avg %.2f (safety_used=%s)",
                     pos.qty, base_asset, pos.avg_entry, pos.safety_used)
            if coin_free < pos.qty * 0.99:
                log.warning("Binance shows %.8f free but state says %.8f -- "
                            "trimming position to actual balance", coin_free, pos.qty)
                pos.cost *= coin_free / pos.qty
                pos.qty = coin_free
            notify.send(f"Bot restarted. Position restored: {pos.qty:.8f} {base_asset} "
                        f"@ avg ${pos.avg_entry:.2f}. Resuming.")
            return pos, st.get("session_start_equity"), bool(st.get("paused")), False
        if not holding and st.get("qty", 0) > 0:
            log.warning("state.json shows a position but Binance shows no %s -- "
                        "position was closed externally. Clearing stale state.", base_asset)
            statemod.clear()
            return Position(), None, False, False
        # state exists, flat on both sides -> reuse session equity if present
        return Position(), st.get("session_start_equity") or None, bool(st.get("paused")), False

    if not ok:  # file exists but corrupt
        if holding:
            msg = (f"Holding {coin_free} {base_asset} but state file is corrupt. "
                   "Entering PAUSED mode. Send /resume to acknowledge "
                   "(bot will treat holdings as untracked and trade only fresh capital).")
            log.error(msg)
            notify.send("WARNING: " + msg)
            return Position(), None, True, False
        statemod.clear()
        return Position(), None, False, False

    # no state file at all
    if holding:
        msg = (f"Holding {coin_free} {base_asset} but state file is missing. "
               "Entering PAUSED mode. Send /resume to acknowledge.")
        log.error(msg)
        notify.send("WARNING: " + msg)
        return Position(), None, True, False
    return Position(), None, False, False


# ---------------------------------------------------------------- main loop

class Shutdown(Exception):
    pass


def _signal_handler(signum, _frame):
    raise Shutdown(signal.Signals(signum).name)


def main():
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    client = get_client()
    step, min_notional, base_asset, sym_status = get_symbol_meta(client)
    log.info("Mode: %s | %s (%s) | LOT_SIZE step=%s minNotional=%s | telegram=%s",
             "TESTNET/DRY-RUN" if DRY_RUN else "LIVE", SYMBOL, sym_status,
             step, min_notional, "on" if notify.enabled() else "off")

    pos, start_equity, paused, halted = reconcile(client, base_asset, step)

    if start_equity is None:
        price0 = float(safe_call(client.get_symbol_ticker, symbol=SYMBOL)["price"])
        start_equity = free_balance(client, "USDT") + pos.qty * price0
        log.info("New session start equity: $%.2f (compounding: sizes are %% of this, updated live)",
                 start_equity)
        if start_equity < float(min_notional) * 2:
            raise SystemExit(f"Need at least ${float(min_notional)*2:.0f} equity to run.")

    statemod.save(snapshot(pos, start_equity, paused, halted))
    last_candle_ts = None

    try:
        while True:
            if halted:
                log.error("HALTED (circuit breaker). Delete state.json to reset. Exiting.")
                break

            if paused:
                for cmd in notify.get_commands():
                    if cmd.lower().startswith("/resume"):
                        paused = False
                        # untracked holdings stay untracked; trade fresh capital only
                        log.info("Resume acknowledged via Telegram.")
                        notify.send("Resumed. Trading with fresh capital only.")
                        statemod.save(snapshot(pos, start_equity, paused, halted))
                if paused:
                    log.info("PAUSED -- waiting for /resume (or delete state.json and restart)")
                    time.sleep(POLL_SECONDS)
                    continue

            closes = closed_hourly_closes(client)
            if not closes:
                time.sleep(POLL_SECONDS)
                continue

            ticker = safe_call(client.get_symbol_ticker, symbol=SYMBOL)
            price = float(ticker["price"]) if ticker else closes[-1]
            quote = free_balance(client, "USDT")
            equity = quote + pos.qty * price

            if equity < start_equity * (1 - CIRCUIT_BREAKER):
                log.error("CIRCUIT BREAKER: equity $%.2f < %.0f%% of start. Liquidating & halting.",
                          equity, (1 - CIRCUIT_BREAKER) * 100)
                notify.send(f"CIRCUIT BREAKER tripped at ${equity:.2f}. Liquidating and halting.")
                if pos.open:
                    market_sell(client, pos.qty, step, min_notional, price)
                    pos = Position()
                halted = True
                statemod.save(snapshot(pos, start_equity, paused, halted))
                break

            rsi_now, rsi_prev = rsi(closes), rsi(closes[:-1])
            action, spend = decide(pos, price, rsi_prev, rsi_now, quote)

            # act only once per closed candle for entries (avoids re-triggering)
            candle_ts = len(closes)
            if action == "BUY_BASE" and candle_ts == last_candle_ts:
                action = "HOLD"

            if action in ("BUY_BASE", "BUY_SAFETY"):
                fill = market_buy(client, spend, step, min_notional, price)
                if fill:
                    pos.add_fill(fill[0], fill[1])
                    if action == "BUY_SAFETY":
                        pos.safety_used = True
                    last_candle_ts = candle_ts
                    statemod.save(snapshot(pos, start_equity, paused, halted))
            elif action in ("SELL_TP", "SELL_SL") and pos.open:
                proceeds = market_sell(client, pos.qty, step, min_notional, price)
                if proceeds is not None:
                    pnl = proceeds - pos.cost
                    new_equity = free_balance(client, "USDT")
                    log.info("CLOSED %s: pnl $%+.3f | equity now $%.2f (next trade sizes off this)",
                             action, pnl, new_equity)
                    notify.send(f"{action}: pnl ${pnl:+.3f} | equity ${new_equity:.2f}")
                    pos = Position()
                    statemod.save(snapshot(pos, start_equity, paused, halted))
            else:
                log.info("HOLD | price %.2f RSI %.1f | equity $%.2f | pos: %s",
                         price, rsi_now or 0, equity,
                         f"{pos.qty:.6f} @ {pos.avg_entry:.2f}" if pos.open else "flat")

            time.sleep(POLL_SECONDS)

    except Shutdown as sig:
        statemod.save(snapshot(pos, start_equity, paused, halted))
        log.info("Shutdown requested (%s). State saved. Open position: %s",
                 sig, "yes" if pos.open else "no")
        notify.send("Bot shutting down gracefully. State saved. "
                    f"Open position: {'yes' if pos.open else 'no'}.")


if __name__ == "__main__":
    main()
