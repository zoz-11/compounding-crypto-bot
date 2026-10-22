"""
Strategy C: Regime-switching combo (mean-reversion + trend, one equity pool).

Kaufman's Efficiency Ratio classifies each bar as "trending" or "ranging".
- Ranging  -> Strategy A's RSI mean-reversion DCA rules
- Trending -> Strategy B's EMA trend-following rules

One position at a time, sized off ONE shared compounding equity pool.
Whichever ruleset opened a position manages its exit.
"""
import math
from common import summarize


def efficiency_ratio(closes, i, lookback=48):
    if i < lookback:
        return None
    window = closes[i - lookback:i + 1]
    net_change = abs(window[-1] - window[0])
    path_length = sum(abs(window[k] - window[k - 1]) for k in range(1, len(window)))
    return 0.0 if path_length == 0 else net_change / path_length


def run_combo(df, params, fee=0.001, start_equity=30.0, cb_dd=0.35,
              er_lookback=48, er_threshold=0.3,
              rsi_entry=32, safety_drop=0.03, first_frac=0.5,
              position_frac=0.9):
    tp, sl = params["tp"], params["sl"]
    atr_mult, hard_sl = params["atr_mult"], params["hard_sl"]

    cash, coins, mode = start_equity, 0.0, None
    entries, highest_close, halted = [], None, False
    equity_curve, trades = [], []

    closes = df["close"].values
    rsi_vals = df["rsi"].values
    ema_fast, ema_slow = df["ema_fast"].values, df["ema_slow"].values
    ema_regime, atr_vals = df["ema_regime"].values, df["atr"].values
    n = len(df)

    for i in range(1, n):
        price = closes[i]
        equity = cash + coins * price
        equity_curve.append(equity)

        if halted:
            continue

        if equity <= start_equity * (1 - cb_dd) and coins > 0:
            proceeds = coins * price * (1 - fee)
            cost_basis = sum(u for _, u in entries) if entries else 0.0
            cash += proceeds
            trades.append({"type": "circuit_breaker_exit", "price": price,
                           "pnl": proceeds - cost_basis, "equity_after": cash})
            coins, entries, halted, mode = 0.0, [], True, None
            continue

        if coins == 0:
            er = efficiency_ratio(closes, i, er_lookback)
            if er is None:
                continue
            regime = "trend" if er >= er_threshold else "meanrev"

            if regime == "meanrev":
                if not math.isnan(rsi_vals[i]) and not math.isnan(rsi_vals[i - 1]):
                    if rsi_vals[i - 1] >= rsi_entry > rsi_vals[i]:
                        usd = equity * first_frac
                        coins += (usd * (1 - fee)) / price
                        cash -= usd
                        entries, mode = [(price, usd)], "meanrev"
            else:
                bull = price > ema_regime[i] if not math.isnan(ema_regime[i]) else False
                crossed_up = ema_fast[i - 1] <= ema_slow[i - 1] and ema_fast[i] > ema_slow[i]
                if bull and crossed_up:
                    usd = equity * position_frac
                    coins += (usd * (1 - fee)) / price
                    cash -= usd
                    entries, highest_close, mode = [(price, usd)], price, "trend"

        elif mode == "meanrev":
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
                trades.append({"type": f"meanrev_{reason}", "price": price,
                               "pnl": proceeds - cost_basis, "equity_after": cash})
                coins, entries, mode = 0.0, [], None

        elif mode == "trend":
            entry_price = entries[0][0]
            highest_close = max(highest_close, price)
            atr_now = atr_vals[i]
            trail = highest_close - atr_mult * atr_now if not math.isnan(atr_now) else None
            crossed_down = ema_fast[i - 1] >= ema_slow[i - 1] and ema_fast[i] < ema_slow[i]
            hit_hard_sl = price <= entry_price * (1 - hard_sl)
            hit_trail = trail is not None and price <= trail

            if crossed_down or hit_hard_sl or hit_trail:
                proceeds = coins * price * (1 - fee)
                cost_basis = sum(u for _, u in entries)
                cash += proceeds
                reason = "hard_stop" if hit_hard_sl else ("trail_stop" if hit_trail else "ema_cross_exit")
                trades.append({"type": f"trend_{reason}", "price": price,
                               "pnl": proceeds - cost_basis, "equity_after": cash})
                coins, entries, mode, highest_close = 0.0, [], None, None

    final_equity = cash + coins * closes[-1]
    return summarize(trades, equity_curve, start_equity, final_equity, halted)
