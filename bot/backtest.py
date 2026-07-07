"""Backtest the strategy on ~90 days of free public 1h klines.

Data source: data-api.binance.vision (Binance's official public market-data
mirror -- no API key required). Stdlib only; run: python3 backtest.py [SYMBOL]
"""

import json
import sys
import time
import urllib.request

from strategy import Position, decide, rsi, RSI_PERIOD, FEE, CIRCUIT_BREAKER

BASE_URL = "https://data-api.binance.vision/api/v3/klines"
SLIPPAGE = 0.0005  # 0.05% adverse slippage per fill, on top of fees


def fetch_klines(symbol, interval="1h", days=90):
    """Fetch klines in 1000-candle pages. Returns list of (ts, close, low, high)."""
    end = int(time.time() * 1000)
    start = end - days * 24 * 3600 * 1000
    out = []
    while start < end:
        url = f"{BASE_URL}?symbol={symbol}&interval={interval}&startTime={start}&limit=1000"
        with urllib.request.urlopen(url, timeout=20) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        for k in batch:
            out.append((k[0], float(k[4]), float(k[3]), float(k[2])))
        start = batch[-1][0] + 1
        time.sleep(0.2)  # be polite to the public endpoint
    return out


def run(symbol="BTCUSDT", start_equity=30.0):
    candles = fetch_klines(symbol)
    if len(candles) < RSI_PERIOD + 10:
        raise SystemExit(f"Not enough data returned for {symbol} ({len(candles)} candles)")

    closes = [c[1] for c in candles]
    quote = start_equity
    pos = Position()
    trades = []          # (exit_reason, pnl_quote, equity_after)
    halted = False
    peak_equity = start_equity
    max_dd = 0.0

    for i in range(RSI_PERIOD + 2, len(candles)):
        ts, close, low, high = candles[i]
        window = closes[: i + 1]
        rsi_now = rsi(window)
        rsi_prev = rsi(window[:-1])

        equity = quote + pos.qty * close
        peak_equity = max(peak_equity, equity)
        max_dd = max(max_dd, (peak_equity - equity) / peak_equity)

        if equity < start_equity * (1 - CIRCUIT_BREAKER):
            halted = True
            if pos.open:
                proceeds = pos.qty * close * (1 - FEE - SLIPPAGE)
                trades.append(("CIRCUIT_BREAKER", proceeds - pos.cost, quote + proceeds))
                quote += proceeds
                pos = Position()
            break

        action, spend = decide(pos, close, rsi_prev, rsi_now, quote)

        if action in ("BUY_BASE", "BUY_SAFETY") and spend and spend >= 5.0:
            fill = close * (1 + SLIPPAGE)
            qty = (spend / fill) / (1 + FEE)
            pos.add_fill(qty, fill)
            quote -= spend
        elif action in ("SELL_TP", "SELL_SL") and pos.open:
            fill = close * (1 - SLIPPAGE)
            proceeds = pos.qty * fill * (1 - FEE)
            trades.append((action, proceeds - pos.cost, quote + proceeds))
            quote += proceeds
            pos = Position()

    # mark-to-market any open position at the last close
    final_equity = quote + pos.qty * candles[-1][1]
    wins = [t for t in trades if t[1] > 0]

    print(f"=== Backtest: {symbol} | 1h candles | {len(candles)} candles (~90d) ===")
    print(f"Start equity:   ${start_equity:.2f}")
    print(f"Final equity:   ${final_equity:.2f}  ({(final_equity/start_equity-1)*100:+.2f}%)")
    print(f"Closed trades:  {len(trades)}  (wins: {len(wins)}, "
          f"win rate: {100*len(wins)/len(trades) if trades else 0:.0f}%)")
    print(f"Max drawdown:   {max_dd*100:.2f}%")
    print(f"Circuit breaker tripped: {halted}")
    print(f"Open position at end: {'yes' if pos.open else 'no'}")
    print("\nTrade log (compounding visible in growing equity column):")
    for n, (reason, pnl, eq) in enumerate(trades, 1):
        print(f"  #{n:>2} {reason:<16} pnl ${pnl:+.3f}   equity after: ${eq:.2f}")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT")
