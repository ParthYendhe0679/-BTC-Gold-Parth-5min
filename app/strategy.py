"""
strategy.py — Supertrend indicator calculation.

Implementation matches TradingView's Pine Script ta.supertrend() function:
  • ATR computed via Wilder's RMA (EWM alpha = 1/period, no bias adjustment)
  • Band adjustment uses the same look-back clamping logic as Pine Script
  • Direction flip conditions are identical to Pine Script

References
----------
Pine Script source  : ta.supertrend() built-in
TradingView article : https://www.tradingview.com/support/solutions/43000634738/
"""

import logging
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from app.models import Candle

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data conversion
# ─────────────────────────────────────────────────────────────────────────────

def candles_to_dataframe(candles: List[Candle]) -> pd.DataFrame:
    """
    Convert a list of Candle objects into a pandas DataFrame.

    The DataFrame is sorted chronologically (oldest row first) and has columns:
        datetime, open, high, low, close
    """
    df = pd.DataFrame(
        {
            "datetime": [c.datetime for c in candles],
            "open":     [c.open     for c in candles],
            "high":     [c.high     for c in candles],
            "low":      [c.low      for c in candles],
            "close":    [c.close    for c in candles],
        }
    )
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert("Asia/Kolkata")
    df.sort_values("datetime", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# ATR — Wilder's RMA
# ─────────────────────────────────────────────────────────────────────────────

def _wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """
    Compute Average True Range using Wilder's smoothing (RMA).

    This exactly mirrors TradingView's `ta.atr(length)` which internally uses
    `ta.rma(ta.tr(true), length)`.

    Seed: simple average of the first `period` True Range values.
    Subsequent values: ATR[i] = (ATR[i-1] * (period-1) + TR[i]) / period
    """
    n = len(close)

    # True Range
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i]  - close[i - 1]),
        )

    # Wilder's RMA
    atr = np.zeros(n)
    if n < period:
        return atr

    # Seed with simple mean of first `period` bars
    atr[period - 1] = np.mean(tr[:period])

    alpha = 1.0 / period
    for i in range(period, n):
        atr[i] = alpha * tr[i] + (1.0 - alpha) * atr[i - 1]

    return atr


# ─────────────────────────────────────────────────────────────────────────────
# Supertrend
# ─────────────────────────────────────────────────────────────────────────────

def calculate_supertrend(
    df: pd.DataFrame,
    atr_period: int = 10,
    multiplier: float = 3.0,
) -> pd.DataFrame:
    """
    Add Supertrend columns to a price DataFrame.

    Parameters
    ----------
    df         : DataFrame with columns [datetime, open, high, low, close]
    atr_period : ATR lookback period (TradingView default: 10)
    multiplier : Band multiplier     (TradingView default: 3.0)

    Returns
    -------
    Copy of df with additional columns:
        atr        – Wilder's ATR value
        supertrend – Supertrend line value
        direction  –  1 = Bullish  (price above supertrend → BUY zone)
                     -1 = Bearish  (price below supertrend → SELL zone)
                      0 = Not yet computed (warm-up bars)
    """
    df = df.copy()
    n = len(df)

    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    close = df["close"].values.astype(float)

    # ── ATR ─────────────────────────────────────────────────────────────────
    atr = _wilder_atr(high, low, close, atr_period)

    # ── Basic bands ──────────────────────────────────────────────────────────
    hl2         = (high + low) / 2.0
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr

    # ── Final bands — TradingView clamping logic ──────────────────────────
    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()

    for i in range(1, n):
        # Upper band tightens downward; resets if previous close broke above it
        if basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i - 1]

        # Lower band tightens upward; resets if previous close broke below it
        if basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i - 1]

    # ── Supertrend direction ─────────────────────────────────────────────────
    supertrend = np.zeros(n)
    direction  = np.zeros(n, dtype=int)   # 0 = warm-up

    # First valid bar (end of ATR warm-up period)
    seed = atr_period - 1
    if seed >= n:
        df["atr"]        = atr
        df["supertrend"] = supertrend
        df["direction"]  = direction
        return df

    # Seed assumes bearish (supertrend = upper band) — will self-correct within
    # a few bars based on actual price action
    supertrend[seed] = final_upper[seed]
    direction[seed]  = -1

    for i in range(seed + 1, n):
        if direction[i - 1] == -1:
            # ── Previously BEARISH ───────────────────────────────────────────
            if close[i] > final_upper[i]:
                # Crosses above → turns BULLISH
                direction[i]  = 1
                supertrend[i] = final_lower[i]
            else:
                # Remains BEARISH
                direction[i]  = -1
                supertrend[i] = final_upper[i]
        else:
            # ── Previously BULLISH ───────────────────────────────────────────
            if close[i] < final_lower[i]:
                # Drops below → turns BEARISH
                direction[i]  = -1
                supertrend[i] = final_upper[i]
            else:
                # Remains BULLISH
                direction[i]  = 1
                supertrend[i] = final_lower[i]

    df["atr"]         = atr
    df["final_upper"] = final_upper
    df["final_lower"] = final_lower
    df["supertrend"]  = supertrend
    df["direction"]   = direction
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Signal extraction
# ─────────────────────────────────────────────────────────────────────────────

def get_signal(df: pd.DataFrame) -> Optional[dict]:
    """
    Extract the Supertrend signal & detailed indicator values from the last row of df.

    Returns a dict with:
      action          : "BUY" | "SELL"
      price           : float (close price)
      datetime        : str
      open            : float
      high            : float
      low             : float
      close           : float
      atr             : float
      upper_band      : float
      lower_band      : float
      supertrend      : float
      direction_label : "Bullish" | "Bearish"
    """
    valid = df[df["direction"] != 0]
    if len(valid) < 2:
        logger.warning("Not enough valid bars to determine signal.")
        return None

    latest    = valid.iloc[-1]
    direction = int(latest["direction"])
    price     = float(latest["close"])
    dt_str    = str(latest["datetime"])
    o_val     = float(latest["open"])
    h_val     = float(latest["high"])
    l_val     = float(latest["low"])
    c_val     = float(latest["close"])
    atr_val   = float(latest["atr"])
    up_band   = float(latest["final_upper"])
    low_band  = float(latest["final_lower"])
    st_val    = float(latest["supertrend"])

    action = "BUY" if direction == 1 else "SELL"
    return {
        "action": action,
        "price": price,
        "datetime": dt_str,
        "open": o_val,
        "high": h_val,
        "low": l_val,
        "close": c_val,
        "hl2": (h_val + l_val) / 2.0,
        "atr": atr_val,
        "upper_band": up_band,
        "lower_band": low_band,
        "supertrend": st_val,
        "direction_label": "Bullish" if direction == 1 else "Bearish",
    }
