"""Candle-close validation, malformed data, and indicator correctness."""

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.candles import expected_latest_closed_open, validate_candles
from app.strategies.base import atr, ema, rsi
from app.strategy import _wilder_atr, calculate_ema_strategy

TF = pd.Timedelta(minutes=5)


def row(ts, o=100, h=101, l=99, c=100.5, v=None):
    return {"datetime": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def test_forming_candle_is_dropped():
    now = datetime(2026, 10, 2, 18, 57, 32, tzinfo=timezone.utc)
    rows = [row("2026-10-02 18:45:00"), row("2026-10-02 18:50:00"), row("2026-10-02 18:55:00")]
    df, rep = validate_candles(rows, TF, now)
    assert rep.forming == 1
    assert df["datetime"].iloc[-1] == pd.Timestamp("2026-10-02 18:50", tz="UTC")


def test_provider_returning_only_closed_candles_keeps_latest():
    now = datetime(2026, 10, 2, 19, 0, 5, tzinfo=timezone.utc)
    rows = [row("2026-10-02 18:50:00"), row("2026-10-02 18:55:00")]
    df, rep = validate_candles(rows, TF, now)
    assert rep.forming == 0 and len(df) == 2   # must NOT blindly discard the last row


def test_exact_close_boundary_counts_as_closed():
    now = datetime(2026, 10, 2, 19, 0, 0, tzinfo=timezone.utc)
    df, _ = validate_candles([row("2026-10-02 18:55:00")], TF, now)
    assert len(df) == 1


def test_future_dated_and_exchange_tz_candles_rejected():
    # Real observation: XAU/USD without timezone=UTC came back 10h ahead.
    now = datetime(2026, 10, 2, 18, 57, tzinfo=timezone.utc)
    rows = [row("2026-10-03 04:50:00"), row("2026-10-02 18:45:00")]
    df, rep = validate_candles(rows, TF, now)
    assert rep.future == 1 and len(df) == 1


def test_duplicates_malformed_inconsistent():
    now = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)
    rows = [row("2026-10-02 18:45:00", c=100.1), row("2026-10-02 18:45:00", c=100.9),
            {"datetime": "garbage", "open": 1, "high": 1, "low": 1, "close": 1},
            row("2026-10-02 18:50:00", o="nan"), row("2026-10-02 18:55:00", h=98, l=99),
            row("2026-10-02 19:00:00", o=0, h=0, l=0, c=0), row("2026-10-02 19:20:00")]
    df, rep = validate_candles(rows, TF, now)
    assert rep.duplicates == 1 and df["close"].iloc[0] == 100.9      # last occurrence wins
    assert rep.malformed == 3 and rep.inconsistent == 1
    assert rep.gaps == 1 and len(df) == 2


def test_empty_input():
    df, rep = validate_candles([], TF, datetime.now(timezone.utc))
    assert df.empty and rep.closed == 0


def test_expected_latest_closed_open():
    now = datetime(2026, 10, 2, 19, 0, 8, tzinfo=timezone.utc)
    assert expected_latest_closed_open(now, TF) == pd.Timestamp("2026-10-02 18:55", tz="UTC")


def test_atr_matches_original_after_warmup(rw_df):
    h, l, c = (rw_df[k].to_numpy() for k in ("high", "low", "close"))
    new, old = atr(h, l, c, 10), _wilder_atr(h, l, c, 10)
    assert np.isnan(new[:9]).all()
    np.testing.assert_allclose(new[9:], old[9:], rtol=1e-12)


def test_ema_matches_original_strategy(rw_df):
    np.testing.assert_allclose(ema(rw_df["close"].to_numpy(), 5), calculate_ema_strategy(rw_df, 5)["ema"].to_numpy())


def test_rsi_bounds_and_extremes():
    up = np.arange(1, 50, dtype=float)
    r = rsi(up, 2)
    assert np.isnan(r[:2]).all() and r[-1] == 100.0
    r2 = rsi(up[::-1].copy(), 2)
    assert r2[-1] == 0.0
    flat = rsi(np.full(30, 5.0), 2)
    assert flat[-1] == 50.0
