"""
candles.py — Candle validation layer.

Provider convention (Twelve Data docs): each bar's ``datetime`` is the bar
OPEN time. We request ``timezone=UTC`` explicitly, so a bar is closed when
``open_time + timeframe <= now`` (UTC). Whether the provider includes the
forming bar is not documented — we never assume, we check every row.

Rejected rows:
  • unparsable timestamps or prices, NaN / non-positive prices
  • inconsistent OHLC (high < low, open/close outside [low, high])
  • duplicate timestamps (last occurrence wins — providers revise forming bars)
  • future-dated bars (open_time > now + tolerance)
  • bars that have not closed yet (open_time + timeframe > now)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

COLUMNS = ["datetime", "open", "high", "low", "close", "volume"]


@dataclass
class ValidationReport:
    received: int = 0
    malformed: int = 0
    inconsistent: int = 0
    duplicates: int = 0
    future: int = 0
    forming: int = 0
    gaps: int = 0
    closed: int = 0
    latest_closed_open: Optional[pd.Timestamp] = None
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["latest_closed_open"] = (
            self.latest_closed_open.isoformat() if self.latest_closed_open is not None else None
        )
        return d


def validate_candles(
    rows: Sequence[Any],
    timeframe: pd.Timedelta,
    now: datetime,
    future_tolerance: pd.Timedelta = pd.Timedelta(seconds=60),
) -> "tuple[pd.DataFrame, ValidationReport]":
    """
    Turn raw candle rows (dicts or Candle models) into a clean DataFrame of
    CLOSED bars only, sorted oldest→newest, ``datetime`` = UTC open time.
    """
    rep = ValidationReport(received=len(rows))
    now_ts = pd.Timestamp(now)
    now_ts = now_ts.tz_localize("UTC") if now_ts.tzinfo is None else now_ts.tz_convert("UTC")

    recs = []
    for r in rows:
        get = r.get if isinstance(r, dict) else (lambda k, _r=r: getattr(_r, k, None))
        try:
            ts = pd.Timestamp(get("datetime"))
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
            o, h, l, c = (float(get(k)) for k in ("open", "high", "low", "close"))
            v = get("volume")
            v = float(v) if v not in (None, "") else np.nan
        except (TypeError, ValueError):
            rep.malformed += 1
            continue
        if not all(np.isfinite(x) and x > 0 for x in (o, h, l, c)):
            rep.malformed += 1
            continue
        if h < l or not (l <= o <= h) or not (l <= c <= h):
            rep.inconsistent += 1
            continue
        recs.append((ts, o, h, l, c, v))

    df = pd.DataFrame(recs, columns=COLUMNS)
    if df.empty:
        rep.notes.append("no valid rows")
        return df, rep

    df = df.sort_values("datetime", kind="stable")
    before = len(df)
    df = df.drop_duplicates("datetime", keep="last")
    rep.duplicates = before - len(df)

    future_mask = df["datetime"] > now_ts + future_tolerance
    rep.future = int(future_mask.sum())
    df = df[~future_mask]

    forming_mask = df["datetime"] + timeframe > now_ts
    rep.forming = int(forming_mask.sum())
    df = df[~forming_mask].reset_index(drop=True)

    if len(df) > 1:
        rep.gaps = int((df["datetime"].diff().dropna() > timeframe).sum())
    rep.closed = len(df)
    if rep.closed:
        rep.latest_closed_open = df["datetime"].iloc[-1]
    if rep.malformed or rep.inconsistent or rep.duplicates or rep.future:
        logger.warning("Candle validation: %s", {k: v for k, v in rep.as_dict().items() if v and k != "notes"})
    return df, rep


def expected_latest_closed_open(now: datetime, timeframe: pd.Timedelta) -> pd.Timestamp:
    """Open time of the most recent bar that should be closed at ``now``."""
    now_ts = pd.Timestamp(now)
    now_ts = now_ts.tz_localize("UTC") if now_ts.tzinfo is None else now_ts.tz_convert("UTC")
    return now_ts.floor(timeframe) - timeframe
