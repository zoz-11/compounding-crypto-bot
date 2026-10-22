"""Shared helpers used by all strategy backtests."""


def summarize(trades, equity_curve, start_equity, final_equity, halted):
    closed = [t for t in trades if "pnl" in t]
    wins = [t for t in closed if t["pnl"] > 0]

    peak = start_equity
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        dd = (peak - e) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    return {
        "num_trades": len(closed),
        "win_rate": (len(wins) / len(closed) * 100) if closed else 0.0,
        "net_return_pct": (final_equity / start_equity - 1) * 100,
        "max_drawdown_pct": max_dd * 100,
        "final_equity": final_equity,
        "halted": halted,
        "trades": trades,
    }
