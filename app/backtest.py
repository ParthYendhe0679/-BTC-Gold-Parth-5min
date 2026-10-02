"""
backtest.py — Event-driven backtester for the strategy engine.

Assumptions (explicit, conservative)
────────────────────────────────────
• Strategies are run ONCE over the whole history; their causality is enforced
  by the prefix-invariance tests, so no signal uses future bars.
• Entry  : at the signal's entry reference (= confirmation-candle close).
• One open position per strategy; signals while in a position are ignored,
  except a signal on the exit candle itself (stop-and-reverse).
• From the NEXT bar on:
    – gap through stop/target at the open → filled at the open;
    – stop and target both inside one bar (intrabar order unknown) → STOP
      (worst case);
    – strategy dynamic exit (e.g. Supertrend flip, RSI SMA exit) → at close;
    – time stop after max_hold_bars → at close.
• Targets: exits at Target 1 for strategies with fixed targets.
• Costs : `cost_pct` round-trip (fees + spread + slippage) deducted per trade.
• Returns are per-trade % of entry price (no compounding, no position sizing).
"""

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from app.strategies.base import BUY, MarketContext, Signal, Strategy


@dataclass
class Trade:
    strategy_id: str
    symbol: str
    direction: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    exit_reason: str
    bars_held: int
    gross_pct: float
    net_pct: float
    r_multiple: Optional[float]


def simulate(ctx: MarketContext, strategy: Strategy, cost_pct: float,
             signals: Optional[List[Signal]] = None, default_max_hold: int = 288) -> List[Trade]:
    sigs = sorted(signals if signals is not None else strategy.generate(ctx), key=lambda s: s.bar_index)
    O, H, L, C = ctx.o, ctx.h, ctx.l, ctx.c
    n = len(ctx)
    trades: List[Trade] = []
    busy_until = -1
    for s in sigs:
        i = s.bar_index
        # A new entry may happen on the candle the previous trade exited on (exit
        # and entry both at/after that close → stop-and-reverse for flip strategies).
        if i < busy_until or i >= n - 1:
            continue
        long = s.direction == BUY
        stop = s.stop_loss
        target = s.targets[0] if (strategy.use_target_exit and s.targets) else None
        max_hold = s.max_hold_bars or default_max_hold
        exit_px, reason, j = None, None, i
        for j in range(i + 1, n):
            if stop is not None:
                if long and O[j] <= stop:
                    exit_px, reason = O[j], "stop (gap)"
                elif not long and O[j] >= stop:
                    exit_px, reason = O[j], "stop (gap)"
            if exit_px is None and target is not None:
                if long and O[j] >= target:
                    exit_px, reason = O[j], "target (gap)"
                elif not long and O[j] <= target:
                    exit_px, reason = O[j], "target (gap)"
            if exit_px is None:
                hit_stop = stop is not None and (L[j] <= stop if long else H[j] >= stop)
                hit_tgt = target is not None and (H[j] >= target if long else L[j] <= target)
                if hit_stop:
                    exit_px, reason = stop, "stop" + (" (both hit — worst case)" if hit_tgt else "")
                elif hit_tgt:
                    exit_px, reason = target, "target"
            if exit_px is None:
                dyn = strategy.exit_signal(ctx, j, s.direction, i)
                if dyn:
                    exit_px, reason = C[j], dyn
            if exit_px is None and j - i >= max_hold:
                exit_px, reason = C[j], "time stop"
            if exit_px is not None:
                break
        if exit_px is None:
            exit_px, reason = C[n - 1], "end of data"
        sign = 1 if long else -1
        gross = sign * (exit_px - s.entry) / s.entry * 100
        r_mult = None
        if stop is not None and abs(s.entry - stop) > 0:
            r_mult = sign * (exit_px - s.entry) / abs(s.entry - stop)
        trades.append(Trade(strategy.id, ctx.symbol, s.direction, ctx.t[i].isoformat(), ctx.t[j].isoformat(),
                            s.entry, float(exit_px), reason, j - i, gross, gross - cost_pct,
                            None if r_mult is None else float(r_mult)))
        busy_until = j
    return trades


def metrics(trades: List[Trade], bar_minutes: int = 5) -> Dict[str, Any]:
    if not trades:
        return {"trades": 0}
    net = np.array([t.net_pct for t in trades])
    wins, losses = net[net > 0], net[net <= 0]
    equity = np.cumsum(net)
    dd = float((np.maximum.accumulate(np.concatenate([[0], equity]))[1:] - equity).max())
    gp, gl = wins.sum(), -losses.sum()
    out = {
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(net) * 100, 1),
        "avg_win_pct": round(float(wins.mean()), 4) if len(wins) else 0.0,
        "avg_loss_pct": round(float(losses.mean()), 4) if len(losses) else 0.0,
        "expectancy_pct": round(float(net.mean()), 4),
        "profit_factor": round(float(gp / gl), 2) if gl > 0 else None,
        "net_return_pct": round(float(net.sum()), 2),
        "max_drawdown_pct": round(dd, 2),
        "avg_hold_min": round(float(np.mean([t.bars_held for t in trades])) * bar_minutes, 1),
        "t_stat": round(float(net.mean() / (net.std(ddof=1) / np.sqrt(len(net)))), 2) if len(net) > 2 and net.std() > 0 else None,
    }
    for side in ("BUY", "SELL"):
        sub = np.array([t.net_pct for t in trades if t.direction == side])
        out[f"{side.lower()}_trades"] = int(len(sub))
        out[f"{side.lower()}_win_rate_pct"] = round(float((sub > 0).mean() * 100), 1) if len(sub) else None
        out[f"{side.lower()}_expectancy_pct"] = round(float(sub.mean()), 4) if len(sub) else None
    return out


def regime_split(ctx: MarketContext, trades: List[Trade], ema_n: int = 200) -> Dict[str, Any]:
    """Split trades by trend regime at entry (close vs EMA200, both computed causally)."""
    e = ctx.ema(ema_n)
    idx = {t: k for k, t in enumerate(ctx.t)}
    up, down = [], []
    for tr in trades:
        k = idx.get(pd.Timestamp(tr.entry_time))
        if k is None or k < ema_n:
            continue
        (up if ctx.c[k] > e[k] else down).append(tr)
    return {"uptrend": metrics(up), "downtrend": metrics(down)}


def split_bounds(n: int, fractions=(0.6, 0.2, 0.2)) -> Dict[str, slice]:
    a = int(n * fractions[0])
    b = int(n * (fractions[0] + fractions[1]))
    return {"train": slice(0, a), "validation": slice(a, b), "test": slice(b, n)}


def trades_in(trades: List[Trade], ctx: MarketContext, sl: slice) -> List[Trade]:
    start = ctx.t[sl.start]
    end = ctx.t[min(sl.stop, len(ctx)) - 1]
    return [t for t in trades if start <= pd.Timestamp(t.entry_time) <= end]


def to_records(trades: List[Trade]) -> List[Dict[str, Any]]:
    return [asdict(t) for t in trades]
