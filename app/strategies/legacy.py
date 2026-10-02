"""
legacy.py — The retained original strategies, adapted to the common interface.

The indicator math is UNCHANGED: both wrappers call the original functions in
app/strategy.py (calculate_supertrend / calculate_ema_strategy).

Behaviour fix (audit D5): a signal is now the direction FLIP on a specific
closed candle (direction[i] != direction[i-1]) instead of "current direction
differs from the last alert we managed to send". This makes every alert refer
to the exact candle and price where the flip happened.
"""

from typing import List

import numpy as np

from app.strategies.base import BUY, SELL, MarketContext, Signal, Strategy
from app.strategy import calculate_ema_strategy, calculate_supertrend


class SupertrendStrategy(Strategy):
    id = "SUPERTREND"
    name = "Supertrend"
    version = "2.0"   # 1.x = original state-diff alerts
    defaults = {"atr_period": 10, "multiplier": 3.0}
    use_target_exit = False

    @property
    def min_bars(self) -> int:
        return self.p["atr_period"] * 3

    def _direction(self, ctx: MarketContext):
        key = ("supertrend", self.p["atr_period"], self.p["multiplier"])
        return ctx._memo(key, lambda: calculate_supertrend(
            ctx.df, atr_period=self.p["atr_period"], multiplier=self.p["multiplier"]))

    def generate(self, ctx: MarketContext) -> List[Signal]:
        if len(ctx) < self.min_bars:
            return []
        st = self._direction(ctx)
        d = st["direction"].to_numpy()
        line = st["supertrend"].to_numpy()
        atr = st["atr"].to_numpy()
        out = []
        for i in range(self.min_bars, len(ctx)):
            if d[i] == 0 or d[i - 1] == 0 or d[i] == d[i - 1]:
                continue
            direction = BUY if d[i] == 1 else SELL
            prev_label, new_label = ("Bearish", "Bullish") if direction == BUY else ("Bullish", "Bearish")
            reason = (f"Close {ctx.c[i]:.2f} crossed the Supertrend band; trend flipped "
                      f"{prev_label} → {new_label}. Supertrend line now {line[i]:.2f} "
                      f"(ATR{self.p['atr_period']} = {atr[i]:.2f}).")
            out.append(self._signal(
                ctx, i, direction, ctx.c[i], line[i], [], reason,
                setup_key=f"flip:{ctx.t[i].isoformat()}",
                setup_label=f"Supertrend flip to {new_label.lower()}",
                details=[("Supertrend line (trailing stop)", line[i])],
                indicators={"supertrend": round(float(line[i]), 4), "atr": round(float(atr[i]), 4)},
            ))
        return out

    def exit_signal(self, ctx, i, direction, entry_index):
        d = self._direction(ctx)["direction"].to_numpy()
        want = 1 if direction == BUY else -1
        return "supertrend flip" if d[i] != want else None


class EmaCrossStrategy(Strategy):
    id = "EMA5_CROSS"
    name = "EMA 5"
    version = "2.0"
    defaults = {"period": 5}
    use_target_exit = False

    @property
    def min_bars(self) -> int:
        return self.p["period"] * 4

    def _df(self, ctx: MarketContext):
        return ctx._memo(("ema_strategy", self.p["period"]),
                         lambda: calculate_ema_strategy(ctx.df, period=self.p["period"]))

    def generate(self, ctx: MarketContext) -> List[Signal]:
        if len(ctx) < self.min_bars:
            return []
        df = self._df(ctx)
        d = df["direction"].to_numpy()
        e = df["ema"].to_numpy()
        out = []
        for i in range(self.min_bars, len(ctx)):
            if d[i] == 0 or d[i - 1] == 0 or d[i] == d[i - 1]:
                continue
            direction = BUY if d[i] == 1 else SELL
            side = "above" if direction == BUY else "below"
            out.append(self._signal(
                ctx, i, direction, ctx.c[i], None, [],
                f"Close {ctx.c[i]:.2f} closed {side} EMA({self.p['period']}) {e[i]:.2f} "
                f"after closing on the other side on the previous candle.",
                setup_key=f"flip:{ctx.t[i].isoformat()}",
                setup_label=f"Close crossed {side} EMA {self.p['period']}",
                details=[(f"EMA({self.p['period']})", e[i])],
                indicators={"ema": round(float(e[i]), 4)},
            ))
        return out

    def exit_signal(self, ctx, i, direction, entry_index):
        d = self._df(ctx)["direction"].to_numpy()
        want = 1 if direction == BUY else -1
        return "EMA side flip" if d[i] != want else None
