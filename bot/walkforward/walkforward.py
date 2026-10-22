"""
Walk-forward validation harness.

Optimizes parameters on an in-sample (IS) window, then tests the chosen params
ONCE on the next out-of-sample (OOS) window the optimizer never saw. Rolls
forward across all history and chains the OOS results into one honest curve.

Run:  python3 walkforward.py [SYMBOL] [DAYS]
Writes ../../public/results.json for the /results dashboard page.
"""

import os
import sys
import json
import itertools
from datetime import datetime, timezone

from indicators import to_df, rsi, ema, atr
import strategy_meanrev as meanrev
import strategy_trend as trend
import strategy_combo as combo
from data_loader import load_klines

MEANREV_GRID = {"tp": [0.015, 0.02, 0.025, 0.03], "sl": [0.03, 0.04, 0.05, 0.06]}
TREND_GRID = {"atr_mult": [1.5, 2.0, 2.5, 3.0], "hard_sl": [0.04, 0.06, 0.08, 0.10]}
COMBO_GRID = {"tp": [0.02, 0.03], "sl": [0.04, 0.05], "atr_mult": [2.0, 2.5], "hard_sl": [0.06, 0.08]}

IS_DAYS, OOS_DAYS, STEP_DAYS = 45, 15, 15
CANDLES_PER_DAY = 24  # 1h candles
MIN_IS_TRADES = 3
START_EQUITY = 30.0

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "public", "results.json")


def grid(d):
    keys = list(d.keys())
    for values in itertools.product(*d.values()):
        yield dict(zip(keys, values))


def prep_meanrev_df(candles):
    df = to_df(candles)
    df["rsi"] = rsi(df["close"], 14)
    return df


def prep_trend_df(candles):
    df = to_df(candles)
    df["ema_fast"] = ema(df["close"], 9)
    df["ema_slow"] = ema(df["close"], 21)
    df["ema_regime"] = ema(df["close"], 100)
    df["atr"] = atr(df, 14)
    return df


def prep_combo_df(candles):
    df = to_df(candles)
    df["rsi"] = rsi(df["close"], 14)
    df["ema_fast"] = ema(df["close"], 9)
    df["ema_slow"] = ema(df["close"], 21)
    df["ema_regime"] = ema(df["close"], 100)
    df["atr"] = atr(df, 14)
    return df


def walk_forward(df, run_fn, param_grid):
    is_n = IS_DAYS * CANDLES_PER_DAY
    oos_n = OOS_DAYS * CANDLES_PER_DAY
    step_n = STEP_DAYS * CANDLES_PER_DAY

    folds = []
    i = 0
    while i + is_n + oos_n <= len(df):
        is_slice = df.iloc[i:i + is_n]
        oos_slice = df.iloc[i + is_n:i + is_n + oos_n]

        best = None
        for params in grid(param_grid):
            res = run_fn(is_slice, params, start_equity=START_EQUITY)
            if res["num_trades"] < MIN_IS_TRADES:
                continue
            if best is None or res["net_return_pct"] > best["res"]["net_return_pct"]:
                best = {"params": params, "res": res}

        if best is None:
            i += step_n
            continue

        oos_res = run_fn(oos_slice, best["params"], start_equity=START_EQUITY)
        folds.append({
            "is_start": is_slice.index[0], "is_end": is_slice.index[-1],
            "oos_start": oos_slice.index[0], "oos_end": oos_slice.index[-1],
            "chosen_params": best["params"],
            "is_metrics": best["res"], "oos_metrics": oos_res,
        })
        i += step_n

    return folds


def aggregate_oos(folds):
    equity = START_EQUITY
    all_trades = []
    peak, max_dd = START_EQUITY, 0.0

    for f in folds:
        m = f["oos_metrics"]
        equity *= (m["final_equity"] / START_EQUITY)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak > 0 else 0.0)
        all_trades.extend(m["trades"])

    closed = [t for t in all_trades if "pnl" in t]
    wins = [t for t in closed if t["pnl"] > 0]
    return {
        "folds": len(folds),
        "total_oos_trades": len(closed),
        "oos_win_rate": (len(wins) / len(closed) * 100) if closed else 0.0,
        "chained_net_return_pct": (equity / START_EQUITY - 1) * 100,
        "chained_max_drawdown_pct": max_dd * 100,
    }


def avg_is(folds):
    if not folds:
        return 0.0
    return sum(f["is_metrics"]["net_return_pct"] for f in folds) / len(folds)


def report(name, folds, agg):
    print(f"\n=== {name} ({len(folds)} walk-forward folds) ===")
    if not folds:
        print("  No fold ever cleared the minimum in-sample trade count.")
        print("  -> Not enough signal to trust ANY parameter choice.")
        return

    for idx, f in enumerate(folds, 1):
        print(f"  Fold {idx}: IS {f['is_start'].date()}->{f['is_end'].date()} "
              f"best={f['chosen_params']} IS_ret={f['is_metrics']['net_return_pct']:+.2f}% "
              f"({f['is_metrics']['num_trades']}t)  |  "
              f"OOS {f['oos_start'].date()}->{f['oos_end'].date()} "
              f"OOS_ret={f['oos_metrics']['net_return_pct']:+.2f}% "
              f"({f['oos_metrics']['num_trades']}t)")

    print(f"  ---- Average IS (best-fit, biased):  {avg_is(folds):+.2f}% per fold ----")
    print("  ---- Aggregated OOS (blind, chained, the number that matters) ----")
    print(f"  Folds: {agg['folds']}  |  OOS trades: {agg['total_oos_trades']}  |  "
          f"OOS win rate: {agg['oos_win_rate']:.1f}%")
    print(f"  Chained OOS return: {agg['chained_net_return_pct']:+.2f}%  |  "
          f"Max DD: {agg['chained_max_drawdown_pct']:.2f}%")

    gap = avg_is(folds) - (agg['chained_net_return_pct'] / max(agg['folds'], 1))
    if gap > 3:
        print(f"  WARNING: IS-OOS gap is large ({gap:.1f}pp/fold) -> signs of overfitting.")
    if agg['total_oos_trades'] < 15:
        print(f"  WARNING: only {agg['total_oos_trades']} OOS trades -> not enough to trust yet.")


def fold_to_dict(f):
    return {
        "is_start": str(f["is_start"].date()), "is_end": str(f["is_end"].date()),
        "oos_start": str(f["oos_start"].date()), "oos_end": str(f["oos_end"].date()),
        "params": f["chosen_params"],
        "is_return_pct": float(f["is_metrics"]["net_return_pct"]),
        "is_trades": int(f["is_metrics"]["num_trades"]),
        "oos_return_pct": float(f["oos_metrics"]["net_return_pct"]),
        "oos_trades": int(f["oos_metrics"]["num_trades"]),
    }


def export_json(results, source, symbol, n_candles, path=OUT_PATH):
    out = {
        "data_source": source,
        "symbol": symbol,
        "candles": n_candles,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settings": {"is_days": IS_DAYS, "oos_days": OOS_DAYS, "step_days": STEP_DAYS,
                     "start_equity": START_EQUITY, "fee_per_side": 0.001},
        "strategies": {
            key: {"folds": [fold_to_dict(f) for f in folds], "aggregate": agg}
            for key, (folds, agg) in results.items()
        },
    }
    path = os.path.normpath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fp:
        json.dump(out, fp, indent=2, default=float)
    print(f"\nExported dashboard data -> {path}")


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 365

    candles, source = load_klines(symbol=symbol, interval="1h", days=days)
    print(f"Loaded {len(candles)} candles for {symbol} (source={source})")
    if source == "synthetic":
        print("!!! NOT real market data. Results below are a CODE TEST only. !!!")

    results = {}
    for key, name, prep, fn, grid_ in [
        ("meanrev", "Strategy A: RSI Mean-Reversion DCA", prep_meanrev_df, meanrev.run_meanrev, MEANREV_GRID),
        ("trend", "Strategy B: EMA Trend-Following (regime-filtered)", prep_trend_df, trend.run_trend, TREND_GRID),
        ("combo", "Strategy C: Regime-Switching Combo", prep_combo_df, combo.run_combo, COMBO_GRID),
    ]:
        folds = walk_forward(prep(candles), fn, grid_)
        agg = aggregate_oos(folds)
        report(name, folds, agg)
        results[key] = (folds, agg)

    export_json(results, source, symbol, len(candles))
    print(f"\nData source used: {source}")
