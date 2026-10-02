"""
base.py — Common strategy interface, signal schema and shared indicator cache.

Contract for every strategy
───────────────────────────
``generate(ctx)`` returns ALL signals over ``ctx.df`` (validated CLOSED candles,
oldest→newest). It must be causal: a signal at bar ``i`` may only use rows
``0..i``. This is enforced by the prefix-invariance tests
(``generate(df[:k])`` == signals of ``generate(df)`` with bar < k).

The live scanner keeps only signals on newly closed candles; the backtester
uses all of them. Entries are candle-close references (``entry`` = price a
signal-follower could act on at/after the close of the confirmation candle).
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

BUY, SELL = "BUY", "SELL"


@dataclass
class Signal:
    strategy_id: str
    strategy_name: str
    strategy_version: str
    symbol: str
    timeframe: str
    direction: str                      # BUY | SELL
    bar_index: int                      # row in ctx.df (internal)
    candle_time: pd.Timestamp           # UTC OPEN time of confirmation candle
    entry: float
    stop_loss: Optional[float]
    targets: List[float]
    reason: str
    setup_key: str                      # identifies the setup (dedup)
    setup_label: str = ""
    details: List[Tuple[str, Any]] = field(default_factory=list)   # extra display fields
    indicators: Dict[str, Any] = field(default_factory=dict)
    confirmation_status: str = "CONFIRMED_CLOSE"
    max_hold_bars: Optional[int] = None
    params: Dict[str, Any] = field(default_factory=dict)
    agreement: List[str] = field(default_factory=list)

    @property
    def risk_reward(self) -> Optional[float]:
        if self.stop_loss is None or not self.targets:
            return None
        risk = abs(self.entry - self.stop_loss)
        if risk <= 0:
            return None
        return abs(self.targets[0] - self.entry) / risk

    @property
    def signal_id(self) -> str:
        raw = "|".join([self.strategy_id, self.symbol, self.timeframe,
                        self.candle_time.isoformat(), self.direction, self.setup_key])
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "signal_direction": self.direction,
            "candle_timestamp": self.candle_time.isoformat(),
            "entry_reference": self.entry,
            "stop_loss": self.stop_loss,
            "target_levels": self.targets,
            "risk_reward": self.risk_reward,
            "signal_reason": self.reason,
            "indicator_values": self.indicators,
            "confirmation_status": self.confirmation_status,
            "setup_key": self.setup_key,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Indicators (causal, NaN during warm-up)
# ─────────────────────────────────────────────────────────────────────────────

def ema(x: np.ndarray, n: int) -> np.ndarray:
    """TradingView ta.ema-compatible (same as the original EMA strategy)."""
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def sma(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n).mean().to_numpy()


def rma(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder's RMA seeded with the SMA of the first n valid values (ta.rma)."""
    out = np.full(len(x), np.nan)
    valid = np.where(~np.isnan(x))[0]
    if len(valid) < n:
        return out
    start = valid[0]
    seed_end = start + n - 1
    out[seed_end] = np.mean(x[start:seed_end + 1])
    alpha = 1.0 / n
    for i in range(seed_end + 1, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    prev_c = np.concatenate([[np.nan], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    tr[0] = h[0] - l[0]
    return tr


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    return rma(true_range(h, l, c), n)


def rsi(c: np.ndarray, n: int) -> np.ndarray:
    """Wilder RSI (ta.rsi)."""
    delta = np.concatenate([[np.nan], np.diff(c)])
    gain = np.where(np.isnan(delta), np.nan, np.maximum(delta, 0.0))
    loss = np.where(np.isnan(delta), np.nan, np.maximum(-delta, 0.0))
    ag, al = rma(gain, n), rma(loss, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag / al
        out = 100.0 - 100.0 / (1.0 + rs)
    out = np.where((al == 0) & (ag > 0), 100.0, out)
    out = np.where((al == 0) & (ag == 0), 50.0, out)
    return out


def is_pivot_high(h: np.ndarray, j: int, left: int, right: int) -> bool:
    """Swing high at j: strictly above `left` bars before, >= `right` bars after.
    Only known at bar j + right (callers must respect this)."""
    if j - left < 0 or j + right >= len(h):
        return False
    return h[j] > h[j - left:j].max() and h[j] >= h[j + 1:j + right + 1].max()


def is_pivot_low(l: np.ndarray, j: int, left: int, right: int) -> bool:
    if j - left < 0 or j + right >= len(l):
        return False
    return l[j] < l[j - left:j].min() and l[j] <= l[j + 1:j + right + 1].min()


class MarketContext:
    """Validated closed candles for one symbol + a per-scan indicator cache
    shared by every strategy (each indicator is computed once per scan)."""

    def __init__(self, symbol: str, df: pd.DataFrame, timeframe: str = "5min",
                 tf_delta: pd.Timedelta = pd.Timedelta(minutes=5)) -> None:
        self.symbol = symbol
        self.df = df.reset_index(drop=True)
        self.timeframe = timeframe
        self.tf_delta = tf_delta
        self.o = self.df["open"].to_numpy(dtype=float)
        self.h = self.df["high"].to_numpy(dtype=float)
        self.l = self.df["low"].to_numpy(dtype=float)
        self.c = self.df["close"].to_numpy(dtype=float)
        vol = self.df["volume"] if "volume" in self.df else pd.Series(np.nan, index=self.df.index)
        self.v = vol.to_numpy(dtype=float)
        self.t = pd.DatetimeIndex(self.df["datetime"])
        self._cache: Dict[Tuple, Any] = {}

    def __len__(self) -> int:
        return len(self.df)

    def _memo(self, key: Tuple, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def ema(self, n: int) -> np.ndarray:
        return self._memo(("ema", n), lambda: ema(self.c, n))

    def sma(self, n: int) -> np.ndarray:
        return self._memo(("sma", n), lambda: sma(self.c, n))

    def atr(self, n: int) -> np.ndarray:
        return self._memo(("atr", n), lambda: atr(self.h, self.l, self.c, n))

    def rsi(self, n: int) -> np.ndarray:
        return self._memo(("rsi", n), lambda: rsi(self.c, n))

    def volume_sma(self, n: int) -> np.ndarray:
        return self._memo(("vsma", n), lambda: sma(self.v, n))

    @property
    def has_volume(self) -> bool:
        v = self.v[~np.isnan(self.v)]
        return len(v) > len(self.v) * 0.9 and np.any(v > 0)


class Strategy:
    """Base class. Subclasses set id/name/version/defaults and implement generate()."""

    id = "BASE"
    name = "Base"
    version = "1.0"
    defaults: Dict[str, Any] = {}
    # True → backtest exits at targets[0]; strategies with dynamic exits override exit_signal
    use_target_exit = True

    def __init__(self, params: Optional[Dict[str, Any]] = None, name: Optional[str] = None,
                 strategy_id: Optional[str] = None) -> None:
        self.p = {**self.defaults, **(params or {})}
        if name:
            self.name = name
        if strategy_id:
            self.id = strategy_id

    @property
    def min_bars(self) -> int:
        return 50

    def generate(self, ctx: MarketContext) -> List[Signal]:
        raise NotImplementedError

    def exit_signal(self, ctx: MarketContext, i: int, direction: str, entry_index: int) -> Optional[str]:
        """Backtest hook: return an exit reason to close at bar i's close, else None."""
        return None

    def _signal(self, ctx: MarketContext, i: int, direction: str, entry: float,
                stop: Optional[float], targets: List[float], reason: str, setup_key: str,
                **kw: Any) -> Signal:
        return Signal(
            strategy_id=self.id, strategy_name=self.name, strategy_version=self.version,
            symbol=ctx.symbol, timeframe=ctx.timeframe, direction=direction, bar_index=i,
            candle_time=ctx.t[i], entry=float(entry),
            stop_loss=None if stop is None else float(stop),
            targets=[float(x) for x in targets], reason=reason, setup_key=setup_key,
            params=dict(self.p), **kw,
        )
