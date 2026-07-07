"""Binance Spot compounding bot -- RSI mean-reversion DCA.

Safety defaults:
  * DRY_RUN defaults to True (Binance Spot TESTNET). Set DRY_RUN=false only
    after paper-trading successfully.
  * Secrets come from environment variables only, never hardcoded:
      BINANCE_API_KEY, BINANCE_API_SECRET   (Spot Trading permission ONLY,
      withdrawals disabled)
  * Optional: SYMBOL (default BTCUSDT), DRY_RUN (default true)

Run: pip install -r requirements.txt && python3 bot.py
"""

import logging
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN

from binance.client import Client
from binance.exceptions import BinanceAPIException

from strategy import Position, decide, rsi, RSI_PERIOD, CIRCUIT_BREAKER

SYMBOL = os.environ.get("SYMBOL", "BTCUSDT")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
POLL_SECONDS = 60  # check once per minute; decisions use closed 1h candles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("trades.log")],
)
log = logging.getLogger("bot")


def get_client():
    key = os.environ.get("BINANCE_API_KEY")
    secret = os.environ.get("BINANCE_API_SECRET")
    if not key or not secret:
        raise SystemExit("Set BINANCE_API_KEY and BINANCE_API_SECRET env vars first.")
    return Client(key, secret, testnet=DRY_RUN)


def get_filters(client):
    """Fetch LOT_SIZE step and MIN_NOTIONAL for the symbol."""
    info = client.get_symbol_info(SYMBOL)
    step, min_notional = Decimal("0.00001"), Decimal("5")
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step = Decimal(f["stepSize"])
        elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
            min_notional = Decimal(f.get("minNotional", f.get("notional", "5")))
    return step, min_notional


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


def quote_balance(client):
    """Free balance of the quote asset (USDT pairs only, e.g. BTCUSDT)."""
    bal = safe_call(client.get_asset_balance, asset="USDT")
    return float(bal["free"]) if bal else 0.0


def closed_hourly_closes(client, limit=RSI_PERIOD + 60):
    kl = safe_call(client.get_klines, symbol=SYMBOL,
                   interval=Client.KLINE_INTERVAL_1HOUR, limit=limit)
    if not kl:
        return None
    return [float(k[4]) for k in kl[:-1]]  # drop the still-open candle


def main():
    client = get_client()
    step, min_notional = get_filters(client)
    log.info("Mode: %s | %s | LOT_SIZE step=%s minNotional=%s",
             "TESTNET/DRY-RUN" if DRY_RUN else "LIVE", SYMBOL, step, min_notional)

    start_equity = quote_balance(client)
    log.info("Session start equity: $%.2f (compounding: sizes are %% of current equity)",
             start_equity)
    if start_equity < float(min_notional) * 2:
        raise SystemExit(f"Need at least ${float(min_notional)*2:.0f} free USDT to run.")

    pos = Position()
    last_candle_ts = None

    while True:
        try:
            closes = closed_hourly_closes(client)
            if not closes:
                time.sleep(POLL_SECONDS)
                continue

            ticker = safe_call(client.get_symbol_ticker, symbol=SYMBOL)
            price = float(ticker["price"]) if ticker else closes[-1]
            quote = quote_balance(client)
            equity = quote + pos.qty * price

            if equity < start_equity * (1 - CIRCUIT_BREAKER):
                log.error("CIRCUIT BREAKER: equity $%.2f < %.0f%% of start. Liquidating & halting.",
                          equity, (1 - CIRCUIT_BREAKER) * 100)
                if pos.open:
                    market_sell(client, pos.qty, step, min_notional, price)
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
            elif action in ("SELL_TP", "SELL_SL") and pos.open:
                proceeds = market_sell(client, pos.qty, step, min_notional, price)
                if proceeds is not None:
                    pnl = proceeds - pos.cost
                    new_equity = quote_balance(client)
                    log.info("CLOSED %s: pnl $%+.3f | equity now $%.2f (next trade sizes off this)",
                             action, pnl, new_equity)
                    pos = Position()
            else:
                log.info("HOLD | price %.2f RSI %.1f | equity $%.2f | pos: %s",
                         price, rsi_now or 0, equity,
                         f"{pos.qty:.6f} @ {pos.avg_entry:.2f}" if pos.open else "flat")

            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            log.info("Stopped by user. Open position: %s", "yes" if pos.open else "no")
            break


if __name__ == "__main__":
    main()
