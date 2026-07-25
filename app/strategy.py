"""
strategy.py — Supertrend & EMA indicator strategy calculations.

Implementation matches TradingView's Pine Script built-in functions:
  • Supertrend: ta.supertrend() (ATR computed via Wilder's RMA)
  • EMA       : ta.ema() (Exponential Moving Average)

References
----------
TradingView Pine Script built-in functions documentation
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
    Add Supertrend columns to a price DataFrame matching TradingView.
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
        if basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i - 1]

        if basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i - 1]

    # ── Supertrend direction ─────────────────────────────────────────────────
    supertrend = np.zeros(n)
    direction  = np.zeros(n, dtype=int)   # 0 = warm-up

    seed = atr_period - 1
    if seed >= n:
        df["atr"]        = atr
        df["supertrend"] = supertrend
        df["direction"]  = direction
        return df

    supertrend[seed] = final_upper[seed]
    direction[seed]  = -1

    for i in range(seed + 1, n):
        if direction[i - 1] == -1:
            if close[i] > final_upper[i]:
                direction[i]  = 1
                supertrend[i] = final_lower[i]
            else:
                direction[i]  = -1
                supertrend[i] = final_upper[i]
        else:
            if close[i] < final_lower[i]:
                direction[i]  = -1
                supertrend[i] = final_upper[i]
            else:
                direction[i]  = 1
                supertrend[i] = final_lower[i]

    df["atr"]         = atr
    df["final_upper"] = final_upper
    df["final_lower"] = final_lower
    df["supertrend"]  = supertrend
    df["direction"]   = direction
    return df


def get_signal(df: pd.DataFrame) -> Optional[dict]:
    """
    Extract the Supertrend signal & detailed indicator values from the last row of df.
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


# ─────────────────────────────────────────────────────────────────────────────
# EMA Strategy (Length = 5 default)
# ─────────────────────────────────────────────────────────────────────────────

def calculate_ema_strategy(
    df: pd.DataFrame,
    period: int = 5,
) -> pd.DataFrame:
    """
    Add EMA indicator and direction columns to a price DataFrame.

    Matches TradingView `ta.ema(close, period)`.

    Direction logic:
        1  (Bullish) = close > ema
       -1  (Bearish) = close < ema
    """
    df = df.copy()
    n = len(df)

    close = df["close"].values.astype(float)
    ema_series = pd.Series(close).ewm(span=period, adjust=False).mean().values

    direction = np.zeros(n, dtype=int)

    for i in range(1, n):
        if close[i] > ema_series[i]:
            direction[i] = 1   # Bullish (price above EMA)
        elif close[i] < ema_series[i]:
            direction[i] = -1  # Bearish (price below EMA)
        else:
            direction[i] = direction[i - 1]

    df["ema"]       = ema_series
    df["direction"] = direction
    return df


def get_ema_signal(df: pd.DataFrame) -> Optional[dict]:
    """
    Extract the EMA signal & detailed indicator values from the last row of df.
    """
    valid = df[df["direction"] != 0]
    if len(valid) < 2:
        logger.warning("Not enough valid bars to determine EMA signal.")
        return None

    latest    = valid.iloc[-1]
    direction = int(latest["direction"])
    price     = float(latest["close"])
    dt_str    = str(latest["datetime"])
    o_val     = float(latest["open"])
    h_val     = float(latest["high"])
    l_val     = float(latest["low"])
    c_val     = float(latest["close"])
    ema_val   = float(latest["ema"])

    action = "BUY" if direction == 1 else "SELL"
    return {
        "action": action,
        "price": price,
        "datetime": dt_str,
        "open": o_val,
        "high": h_val,
        "low": l_val,
        "close": c_val,
        "ema": ema_val,
        "direction_label": "Bullish" if direction == 1 else "Bearish",
    }
