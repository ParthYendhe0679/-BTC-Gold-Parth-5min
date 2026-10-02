"""Test fixtures. Deterministic, offline: no test touches a real network service."""

import os
import sys

# Must be set BEFORE any app import (settings are read at import time;
# load_dotenv never overrides variables that already exist).
os.environ["BOT_TOKEN"] = "123456:" + "A" * 30
os.environ["CHAT_ID"] = "1"
os.environ["TWELVE_DATA_API_KEY"] = "testkey123"
os.environ["STATE_DB_PATH"] = ":memory:"
os.environ["TELEGRAM_COMMANDS_ENABLED"] = "false"
for k in ("WEBHOOK_SECRET", "ADMIN_TOKEN", "ENABLED_STRATEGIES", "DISABLED_STRATEGIES", "STRATEGY_PARAMS"):
    os.environ.pop(k, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import List, Sequence, Tuple  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

T0 = pd.Timestamp("2026-09-01 00:00", tz="UTC")


def make_df(bars: Sequence[Tuple[float, float, float, float]], start: pd.Timestamp = T0) -> pd.DataFrame:
    """bars = [(open, high, low, close), ...] at 5-minute spacing (UTC open times)."""
    return pd.DataFrame({
        "datetime": [start + pd.Timedelta(minutes=5 * i) for i in range(len(bars))],
        "open": [b[0] for b in bars], "high": [b[1] for b in bars],
        "low": [b[2] for b in bars], "close": [b[3] for b in bars],
        "volume": [np.nan] * len(bars),
    })


def flat(n: int, price: float = 100.0, half: float = 0.5) -> List[Tuple[float, float, float, float]]:
    return [(price, price + half, price - half, price)] * n


def random_walk(n: int = 1500, seed: int = 7, start: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    c = start * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    o = np.concatenate([[start], c[:-1]])
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.0015, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.0015, n)))
    return make_df(list(zip(o, h, l, c)))


@pytest.fixture
def rw_df():
    return random_walk()
