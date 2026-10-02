"""
fetch_history.py — Download historical 5-minute candles to CSV (data/ is git-ignored).

Sources
  twelvedata : the live bot's own provider (needs TWELVE_DATA_API_KEY). Paginates
               backwards 5000 bars per request, paced to 8 requests/minute.
               Each request costs 1 credit of the 800/day free budget — the live
               bot shares this budget, so keep --bars modest.
  binance    : public klines, no key (BTCUSDT ≈ BTC/USD proxy; PAXGUSDT is a thin
               tokenised-gold proxy, NOT representative of spot XAU/USD).

Examples
  python scripts/fetch_history.py --source binance --symbol BTCUSDT --days 180
  python scripts/fetch_history.py --source twelvedata --symbol XAU/USD --bars 15000
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def binance(symbol: str, days: int) -> pd.DataFrame:
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = end - days * 86_400_000
    rows = []
    while start < end:
        r = requests.get("https://api.binance.com/api/v3/klines", timeout=20,
                         params={"symbol": symbol, "interval": "5m", "startTime": start, "limit": 1000})
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows += batch
        start = batch[-1][0] + 300_000
        time.sleep(0.2)
    df = pd.DataFrame([{"datetime": pd.Timestamp(x[0], unit="ms", tz="UTC"), "open": float(x[1]),
                        "high": float(x[2]), "low": float(x[3]), "close": float(x[4]),
                        "volume": float(x[5])} for x in rows])
    return df


def twelvedata(symbol: str, bars: int) -> pd.DataFrame:
    from app.config import settings
    if not settings.TWELVE_DATA_API_KEY:
        raise SystemExit("TWELVE_DATA_API_KEY not set")
    frames, end_date = [], None
    while sum(len(f) for f in frames) < bars:
        params = {"symbol": symbol, "interval": "5min", "outputsize": 5000, "timezone": "UTC",
                  "apikey": settings.TWELVE_DATA_API_KEY}
        if end_date:
            params["end_date"] = end_date
        data = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=30).json()
        if data.get("status") == "error":
            print("API error:", data.get("code"), data.get("message", "")[:120])
            break
        vals = data.get("values") or []
        if not vals:
            break
        f = pd.DataFrame(vals)
        frames.append(f)
        oldest = pd.Timestamp(f["datetime"].min())
        end_date = (oldest - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {len(f)} bars back to {oldest}")
        time.sleep(8)   # stay under 8 requests/minute
    df = pd.concat(frames)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df["volume"] = pd.to_numeric(df.get("volume"), errors="coerce") if "volume" in df else float("nan")
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["binance", "twelvedata"], required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--bars", type=int, default=15000)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    df = binance(a.symbol, a.days) if a.source == "binance" else twelvedata(a.symbol, a.bars)
    df = df.drop_duplicates("datetime").sort_values("datetime")
    # never keep the forming bar
    df = df[df["datetime"] + pd.Timedelta(minutes=5) <= pd.Timestamp.now(tz="UTC")]
    out = a.out or f"data/{a.source}_{a.symbol.replace('/', '')}_5m.csv"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_csv(out, index=False)
    print(f"Saved {len(df)} bars {df['datetime'].min()} -> {df['datetime'].max()} to {out}")


if __name__ == "__main__":
    main()
