"""
Strategy A: RSI mean-reversion DCA (parametrized).

- Entry: RSI(14) crosses below `rsi_entry` -> buy `first_frac` of equity
- Safety order: price drops `safety_drop` below entry -> deploy remaining cash
- Exit: +tp above avg entry or -sl below avg entry
- Circuit breaker: equity down `cb_dd` from window start -> liquidate & halt
"""

import math
from common import summarize


def run_meanrev(df, params, fee=0.001, start_equity=30.0,
                rsi_entry=32, safety_drop=0.03, cb_dd=0.35, first_frac=0.5):
    tp, sl = params["tp"], params["sl"]

    cash = start_equity
    coins = 0.0
    entries = []
    halted = False
    equity_curve = []
    trades = []

    rsi_vals = df["rsi"].values
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
            cost_basis = sum(u for _, u in entries)
            cash += proceeds
            trades.append({"type": "circuit_breaker_exit", "price": price,
                           "pnl": proceeds - cost_basis, "equity_after": cash})
            coins, entries, halted = 0.0, [], True
            continue

        if coins == 0:
            if not math.isnan(rsi_vals[i]) and not math.isnan(rsi_vals[i - 1]):
                if rsi_vals[i - 1] >= rsi_entry > rsi_vals[i]:
                    usd = equity * first_frac
                    coins += (usd * (1 - fee)) / price
                    cash -= usd
                    entries = [(price, usd)]
        else:
            avg_entry = sum(p * u for p, u in entries) / sum(u for _, u in entries)

            if len(entries) == 1 and price <= avg_entry * (1 - safety_drop) and cash > 0:
                usd = cash
                coins += (usd * (1 - fee)) / price
                cash -= usd
                entries.append((price, usd))
                avg_entry = sum(p * u for p, u in entries) / sum(u for _, u in entries)

            if price >= avg_entry * (1 + tp) or price <= avg_entry * (1 - sl):
                proceeds = coins * price * (1 - fee)
                cost_basis = sum(u for _, u in entries)
                cash += proceeds
                reason = "take_profit" if price >= avg_entry * (1 + tp) else "stop_loss"
                trades.append({"type": reason, "price": price,
                               "pnl": proceeds - cost_basis, "equity_after": cash})
                coins, entries = 0.0, []

    final_equity = cash + coins * closes[-1]
    return summarize(trades, equity_curve, start_equity, final_equity, halted)
