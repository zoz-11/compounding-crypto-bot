"""
Strategy B: EMA trend-following with regime filter.

- Regime filter: only long while price > EMA(regime)
- Entry: EMA(fast) crosses above EMA(slow) while in the bull regime
- Exit: EMA(fast) crosses below EMA(slow), OR hard stop `hard_sl` below entry,
  OR close below (highest close since entry - atr_mult*ATR)
- Circuit breaker: equity down 35% from window start -> liquidate & halt
"""

import math
from common import summarize


def run_trend(df, params, fee=0.001, start_equity=30.0,
              position_frac=0.9, cb_dd=0.35):
    atr_mult, hard_sl = params["atr_mult"], params["hard_sl"]

    cash = start_equity
    coins = 0.0
    entry_price = None
    usd_deployed = 0.0
    highest_close = None
    halted = False
    equity_curve = []
    trades = []

    ema_fast = df["ema_fast"].values
    ema_slow = df["ema_slow"].values
    ema_regime = df["ema_regime"].values
    atr_vals = df["atr"].values
    closes = df["close"].values
    n = len(df)

    for i in range(1, n):
        price = closes[i]
        equity = cash + coins * price
        equity_curve.append(equity)

        if halted:
            continue

        if equity <= start_equity * (1 - cb_dd) and coins > 0:
            proceeds = coins * price * (1 - fee)
            cash += proceeds
            trades.append({"type": "circuit_breaker_exit", "price": price,
                           "pnl": proceeds - usd_deployed, "equity_after": cash})
            coins, halted = 0.0, True
            continue

        bull = price > ema_regime[i] if not math.isnan(ema_regime[i]) else False

        if coins == 0:
            crossed_up = ema_fast[i - 1] <= ema_slow[i - 1] and ema_fast[i] > ema_slow[i]
            if bull and crossed_up:
                usd = equity * position_frac
                coins += (usd * (1 - fee)) / price
                cash -= usd
                entry_price, usd_deployed, highest_close = price, usd, price
        else:
            highest_close = max(highest_close, price)
            atr_now = atr_vals[i]
            trail = highest_close - atr_mult * atr_now if not math.isnan(atr_now) else None
            crossed_down = ema_fast[i - 1] >= ema_slow[i - 1] and ema_fast[i] < ema_slow[i]
            hit_hard_sl = price <= entry_price * (1 - hard_sl)
            hit_trail = trail is not None and price <= trail

            if crossed_down or hit_hard_sl or hit_trail:
                proceeds = coins * price * (1 - fee)
                cash += proceeds
                reason = "hard_stop" if hit_hard_sl else ("trail_stop" if hit_trail else "ema_cross_exit")
                trades.append({"type": reason, "price": price,
                               "pnl": proceeds - usd_deployed, "equity_after": cash})
                coins, entry_price, usd_deployed = 0.0, None, 0.0

    final_equity = cash + coins * closes[-1]
    return summarize(trades, equity_curve, start_equity, final_equity, halted)
