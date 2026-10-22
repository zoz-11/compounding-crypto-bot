"use client"

import { useEffect, useState } from "react"
import { AlertTriangle, CheckCircle2, ShieldAlert } from "lucide-react"

type Fold = {
  is_start: string
  is_end: string
  oos_start: string
  oos_end: string
  params: Record<string, number>
  is_return_pct: number
  is_trades: number
  oos_return_pct: number
  oos_trades: number
}
type Agg = {
  folds: number
  total_oos_trades: number
  oos_win_rate: number
  chained_net_return_pct: number
  chained_max_drawdown_pct: number
}
type Results = {
  data_source: string
  symbol: string
  candles: number
  generated_at: string
  settings: { is_days: number; oos_days: number; step_days: number; start_equity: number; fee_per_side: number }
  strategies: Record<string, { folds: Fold[]; aggregate: Agg }>
}

const META: Record<string, { label: string; color: string }> = {
  meanrev: { label: "A · RSI mean-reversion DCA", color: "#f87171" },
  trend: { label: "B · EMA trend-following", color: "#60a5fa" },
  combo: { label: "C · Regime-switching combo", color: "#a78bfa" },
}

const pct = (n: number) => `${n >= 0 ? "+" : ""}${n.toFixed(1)}%`

function equityCurve(folds: Fold[]) {
  let eq = 100
  return [100, ...folds.map((f) => (eq *= 1 + f.oos_return_pct / 100))]
}

function EquityChart({ data }: { data: Results["strategies"] }) {
  const W = 640, H = 220, P = 28
  const curves = Object.entries(data).map(([k, v]) => ({ k, pts: equityCurve(v.folds) }))
  const all = curves.flatMap((c) => c.pts)
  const lo = Math.min(...all, 100) - 2, hi = Math.max(...all, 100) + 2
  const n = Math.max(...curves.map((c) => c.pts.length))
  const x = (i: number) => P + (i / Math.max(n - 1, 1)) * (W - 2 * P)
  const y = (v: number) => H - P - ((v - lo) / (hi - lo)) * (H - 2 * P)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      <line x1={P} x2={W - P} y1={y(100)} y2={y(100)} stroke="#52525b" strokeDasharray="4 4" />
      <text x={W - P} y={y(100) - 4} textAnchor="end" fontSize="10" fill="#a1a1aa">start = 100</text>
      {curves.map((c) => (
        <polyline key={c.k} fill="none" stroke={META[c.k]?.color ?? "#fff"} strokeWidth="2"
          points={c.pts.map((v, i) => `${x(i)},${y(v)}`).join(" ")} />
      ))}
      <text x={P} y={y(lo + 2) + 12} fontSize="10" fill="#a1a1aa">{lo.toFixed(0)}</text>
      <text x={P} y={y(hi - 2) - 4} fontSize="10" fill="#a1a1aa">{hi.toFixed(0)}</text>
    </svg>
  )
}

function FoldBars({ folds, color }: { folds: Fold[]; color: string }) {
  const W = 320, H = 110
  const max = Math.max(...folds.flatMap((f) => [Math.abs(f.is_return_pct), Math.abs(f.oos_return_pct)]), 1)
  const mid = H / 2
  const bw = (W / Math.max(folds.length, 1)) * 0.38
  const step = W / Math.max(folds.length, 1)
  const h = (v: number) => (Math.abs(v) / max) * (mid - 4)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      <line x1="0" x2={W} y1={mid} y2={mid} stroke="#52525b" />
      {folds.map((f, i) => (
        <g key={i}>
          <rect x={i * step + 1} width={bw} fill="#71717a"
            y={f.is_return_pct >= 0 ? mid - h(f.is_return_pct) : mid} height={h(f.is_return_pct)} />
          <rect x={i * step + 1 + bw} width={bw} fill={color}
            y={f.oos_return_pct >= 0 ? mid - h(f.oos_return_pct) : mid} height={h(f.oos_return_pct)} />
        </g>
      ))}
    </svg>
  )
}

export default function ResultsPage() {
  const [data, setData] = useState<Results | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    fetch("/results.json", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("results.json not found"))))
      .then(setData)
      .catch((e) => setErr(String(e.message ?? e)))
  }, [])

  if (err)
    return (
      <main className="min-h-screen bg-zinc-950 p-8 text-zinc-100">
        <h1 className="text-2xl font-bold">Walk-forward results</h1>
        <p className="mt-4 text-zinc-400">
          No results yet. Run <code className="rounded bg-zinc-800 px-1">python3 walkforward.py</code> in{" "}
          <code className="rounded bg-zinc-800 px-1">bot/walkforward</code> and reload. ({err})
        </p>
      </main>
    )
  if (!data) return <main className="min-h-screen bg-zinc-950 p-8 text-zinc-400">Loading…</main>

  const entries = Object.entries(data.strategies)
  const anyPositive = entries.some(([, s]) => s.aggregate.chained_net_return_pct > 0)
  const real = data.data_source === "real" || data.data_source === "cache"

  return (
    <main className="min-h-screen bg-zinc-950 p-6 text-zinc-100">
      <div className="mx-auto max-w-6xl space-y-6">
        <header>
          <h1 className="text-3xl font-bold">Walk-forward results</h1>
          <p className="text-sm text-zinc-400">
            {data.symbol} · {data.candles.toLocaleString()} 1h candles · IS {data.settings.is_days}d / OOS{" "}
            {data.settings.oos_days}d · fee {(data.settings.fee_per_side * 100).toFixed(1)}%/side · generated{" "}
            {new Date(data.generated_at).toLocaleString()}
          </p>
        </header>

        <div className={`flex items-center gap-2 rounded-xl border p-3 text-sm ${real
          ? "border-emerald-700 bg-emerald-950/40 text-emerald-200"
          : "border-red-700 bg-red-950/40 text-red-200"}`}>
          {real ? <CheckCircle2 className="h-5 w-5" /> : <AlertTriangle className="h-5 w-5" />}
          {real ? "Real Binance market data." : `Data source: ${data.data_source} — a code test, not a performance claim.`}
        </div>

        <div className={`flex items-start gap-2 rounded-xl border p-4 ${anyPositive
          ? "border-amber-700 bg-amber-950/40 text-amber-200"
          : "border-red-700 bg-red-950/40 text-red-200"}`}>
          <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" />
          <p className="text-sm">
            {anyPositive
              ? "At least one strategy is positive out-of-sample, but check fold consistency and trade count before trusting it."
              : "No strategy cleared out-of-sample validation. Do not trade these live with real money."}
          </p>
        </div>

        <section className="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-5">
          <h2 className="mb-2 font-semibold">Chained out-of-sample equity (start = 100)</h2>
          <EquityChart data={data.strategies} />
          <div className="mt-2 flex flex-wrap gap-4 text-xs">
            {entries.map(([k]) => (
              <span key={k} className="flex items-center gap-1">
                <i className="inline-block h-2 w-4 rounded" style={{ background: META[k]?.color }} />
                {META[k]?.label ?? k}
              </span>
            ))}
          </div>
        </section>

        <div className="grid gap-4 md:grid-cols-3">
          {entries.map(([k, s]) => {
            const a = s.aggregate
            const avgIs = s.folds.reduce((t, f) => t + f.is_return_pct, 0) / Math.max(s.folds.length, 1)
            const gap = avgIs - a.chained_net_return_pct / Math.max(a.folds, 1)
            return (
              <section key={k} className="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-5">
                <h3 className="font-semibold" style={{ color: META[k]?.color }}>{META[k]?.label ?? k}</h3>
                <dl className="mt-3 grid grid-cols-2 gap-3 text-sm">
                  <div><dt className="text-zinc-400">Chained OOS</dt>
                    <dd className={`text-xl font-bold ${a.chained_net_return_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>{pct(a.chained_net_return_pct)}</dd></div>
                  <div><dt className="text-zinc-400">Max drawdown</dt><dd className="text-xl font-bold text-orange-300">{a.chained_max_drawdown_pct.toFixed(1)}%</dd></div>
                  <div><dt className="text-zinc-400">Win rate</dt><dd className="text-xl font-bold">{a.oos_win_rate.toFixed(0)}%</dd></div>
                  <div><dt className="text-zinc-400">OOS trades / folds</dt><dd className="text-xl font-bold">{a.total_oos_trades} / {a.folds}</dd></div>
                </dl>
                {gap > 3 && <p className="mt-3 text-xs text-amber-300">IS–OOS gap {gap.toFixed(1)}pp/fold — signs of overfitting.</p>}
                {a.total_oos_trades < 15 && <p className="mt-1 text-xs text-amber-300">Only {a.total_oos_trades} OOS trades — too few to trust.</p>}
                <p className="mb-1 mt-4 text-xs text-zinc-400">Per fold: <span className="text-zinc-500">■</span> in-sample (biased) vs <span style={{ color: META[k]?.color }}>■</span> out-of-sample</p>
                <FoldBars folds={s.folds} color={META[k]?.color ?? "#fff"} />
              </section>
            )
          })}
        </div>
      </div>
    </main>
  )
}
