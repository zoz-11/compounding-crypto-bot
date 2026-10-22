"""
Historical kline loader for Binance Spot.

Uses Binance's PUBLIC data endpoint (data-api.binance.vision) -- no API key.
Always tries real data first. If the fetch fails, falls back to a clearly
labeled SYNTHETIC series so the code path can still be exercised. Synthetic
output is a code test, never a performance claim.
"""

import os
import time
import json
import random
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = "https://data-api.binance.vision/api/v3/klines"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cache")


def _fetch_page(symbol, interval, start_ms, limit=1000):
    url = f"{BASE_URL}?symbol={symbol}&interval={interval}&startTime={start_ms}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def fetch_real_klines(symbol="BTCUSDT", interval="1h", days=365):
    ms_per_candle = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[interval]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    cursor = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    rows = []
    while cursor < end_ms:
        page = _fetch_page(symbol, interval, cursor)
        if not page:
            break
        rows.extend(page)
        cursor = page[-1][0] + ms_per_candle
        if len(page) < 1000:
            break
        time.sleep(0.25)

    return [{"ts": r[0], "open": float(r[1]), "high": float(r[2]),
             "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
            for r in rows]


def make_synthetic_klines(days=365, interval_hours=1, seed=42, start_price=60000.0):
    """Regime-switching toy series. NOT real market data."""
    rnd = random.Random(seed)
    n = days * 24 // interval_hours
    price = start_price
    candles = []
    regime_len = 24 * 10
    regime = rnd.choice([-1, 0, 1])
    t0 = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    ms_step = interval_hours * 3_600_000

    for i in range(n):
        if i % regime_len == 0:
            regime = rnd.choice([-1, 0, 1])
        drift = {-1: -0.00025, 0: 0.0, 1: 0.00030}[regime]
        ret = drift + rnd.gauss(0, 0.006)
        new_price = max(price * (1 + ret), 1.0)
        o, c = price, new_price
        hi = max(o, c) * (1 + abs(rnd.gauss(0, 0.0015)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, 0.0015)))
        candles.append({"ts": t0 + i * ms_step, "open": o, "high": hi, "low": lo,
                        "close": c, "volume": abs(rnd.gauss(500, 150))})
        price = new_price
    return candles


def load_klines(symbol="BTCUSDT", interval="1h", days=365, cache=True):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{symbol}_{interval}_{days}d.json")

    if cache and os.path.exists(cache_path):
        age_h = (time.time() - os.path.getmtime(cache_path)) / 3600
        if age_h < 12:
            with open(cache_path) as f:
                return json.load(f), "cache"

    try:
        candles = fetch_real_klines(symbol, interval, days)
        if len(candles) < 100:
            raise RuntimeError("suspiciously few candles returned")
        with open(cache_path, "w") as f:
            json.dump(candles, f)
        return candles, "real"
    except Exception as e:
        print(f"[data_loader] Real data fetch failed: {e!r}")
        print("[data_loader] Falling back to SYNTHETIC data (code-path validation only).")
        print("[data_loader] >>> Metrics from this run are NOT real performance. <<<")
        return make_synthetic_klines(days=days), "synthetic"
