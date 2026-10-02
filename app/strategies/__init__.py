"""
Strategy registry + shared evaluation engine.

Every enabled strategy runs on the SAME validated candle DataFrame and shares
one indicator cache per scan. One strategy raising never stops the others.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

from app.strategies.base import BUY, SELL, MarketContext, Signal, Strategy
from app.strategies.fib_golden_zone import FibGoldenZoneStrategy
from app.strategies.legacy import EmaCrossStrategy, SupertrendStrategy
from app.strategies.liquidity_sweep import LiquiditySweepStrategy
from app.strategies.pos_5ema import Pos5EmaStrategy
from app.strategies.rsi2 import Rsi2Strategy

logger = logging.getLogger(__name__)

REGISTRY: Dict[str, Type[Strategy]] = {
    "supertrend": SupertrendStrategy,
    "ema": EmaCrossStrategy,
    "fib_golden_zone": FibGoldenZoneStrategy,
    "pos_5ema": Pos5EmaStrategy,
    "rsi2": Rsi2Strategy,
    "liquidity_sweep": LiquiditySweepStrategy,
}


def build_strategy(cfg: Dict[str, Any]) -> Strategy:
    cls = REGISTRY[cfg["type"]]
    return cls(params=cfg.get("params"), name=cfg.get("name"), strategy_id=cfg.get("id"))


def build_strategies(configs: List[Dict[str, Any]]) -> List[Strategy]:
    out = []
    for cfg in configs:
        try:
            out.append(build_strategy(cfg))
        except Exception as exc:  # noqa: BLE001
            logger.error("Cannot build strategy %s: %s", cfg.get("id"), exc)
    return out


@dataclass
class StrategyResult:
    strategy_id: str
    status: str      # SIGNAL (on the latest candle) | NEUTRAL | INSUFFICIENT_DATA | ERROR
    # signals: every signal in the window (the scanner filters by cursor)
    signals: List[Signal] = field(default_factory=list)
    error: Optional[str] = None


def evaluate_all(ctx: MarketContext, strategies: List[Strategy]) -> List[StrategyResult]:
    results = []
    for s in strategies:
        if len(ctx) < s.min_bars:
            results.append(StrategyResult(s.id, "INSUFFICIENT_DATA",
                                          error=f"{len(ctx)} closed bars < {s.min_bars} required"))
            continue
        try:
            sigs = s.generate(ctx)
            on_latest = any(x.bar_index == len(ctx) - 1 for x in sigs)
            results.append(StrategyResult(s.id, "SIGNAL" if on_latest else "NEUTRAL", sigs))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Strategy %s failed on %s", s.id, ctx.symbol)
            results.append(StrategyResult(s.id, "ERROR", error=f"{type(exc).__name__}: {exc}"[:300]))
    return results


def annotate_agreement(signals: List[Signal]) -> None:
    """Mark (without overriding) other strategies that fired on the same candle."""
    for s in signals:
        s.agreement = [
            f"{o.strategy_id} {o.direction}" for o in signals
            if o is not s and o.symbol == s.symbol and o.candle_time == s.candle_time
        ]


__all__ = ["BUY", "SELL", "MarketContext", "Signal", "Strategy", "REGISTRY", "StrategyResult",
           "build_strategies", "build_strategy", "evaluate_all", "annotate_agreement"]
