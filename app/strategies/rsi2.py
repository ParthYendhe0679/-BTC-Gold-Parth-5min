"""
rsi2.py — RSI2_MEAN_REVERSION v1.0

Short-term mean reversion based on the general approach published by Larry
Connors (RSI(2) extremes in the direction of the longer trend). Equity-market
results are NOT assumed to transfer to 5-minute BTC or Gold.

Rules (closed candles only)
───────────────────────────
Trend regime : long only if close > EMA(trend_ema); short only if close < EMA(trend_ema)
               (trend_filter, on by default).
Setup        : RSI(2) on the PREVIOUS completed candle <= oversold (long) /
               >= overbought (short).  Extreme RSI alone never triggers.
Confirmation : the current completed candle turns: close > open AND close > prior
               close (long); mirror for short.
Optional     : atr_filter → skip when ATR% (ATR/close) < min_atr_pct (dead market).
Entry        : confirmation candle close.
Exits        : (1) stop = entry ∓ stop_atr × ATR(atr_period) (intrabar),
               (2) mean-reversion exit at close when close crosses SMA(exit_sma)
                   (long: close >= SMA, short: close <= SMA),
               (3) time stop after max_hold_bars candles.
Cooldown     : no new same-direction signal within cooldown_bars.
"""

from typing import Any, Dict, List

import numpy as np

from app.strategies.base import BUY, SELL, MarketContext, Signal, Strategy


class Rsi2Strategy(Strategy):
    id = "RSI2_MEAN_REVERSION"
    name = "RSI(2) Mean Reversion"
    version = "1.0"
    use_target_exit = False
    defaults: Dict[str, Any] = {
        "rsi_period": 2, "oversold": 10.0, "overbought": 90.0,
        "trend_filter": True, "trend_ema": 200, "exit_sma": 5,
        "atr_period": 14, "stop_atr": 1.5, "max_hold_bars": 12,
        "atr_filter": False, "min_atr_pct": 0.0005, "cooldown_bars": 3,
        "allow_long": True, "allow_short": True,
    }

    @property
    def min_bars(self) -> int:
        return max(30, self.p["trend_ema"] if self.p["trend_filter"] else 0, self.p["atr_period"] * 2)

    def generate(self, ctx: MarketContext) -> List[Signal]:
        p = self.p
        n = len(ctx)
        if n < self.min_bars:
            return []
        O, C = ctx.o, ctx.c
        r = ctx.rsi(p["rsi_period"])
        atr = ctx.atr(p["atr_period"])
        sma = ctx.sma(p["exit_sma"])
        trend = ctx.ema(p["trend_ema"])
        last = {BUY: -10**9, SELL: -10**9}
        out: List[Signal] = []
        for i in range(self.min_bars, n):
            if not (np.isfinite(r[i - 1]) and np.isfinite(atr[i]) and atr[i] > 0):
                continue
            if p["atr_filter"] and atr[i] / C[i] < p["min_atr_pct"]:
                continue
            for direction in (BUY, SELL):
                if i - last[direction] <= p["cooldown_bars"]:
                    continue
                if direction == BUY:
                    ok = (p["allow_long"] and r[i - 1] <= p["oversold"] and C[i] > O[i] and C[i] > C[i - 1]
                          and (not p["trend_filter"] or C[i] > trend[i]))
                else:
                    ok = (p["allow_short"] and r[i - 1] >= p["overbought"] and C[i] < O[i] and C[i] < C[i - 1]
                          and (not p["trend_filter"] or C[i] < trend[i]))
                if not ok:
                    continue
                sign = 1 if direction == BUY else -1
                stop = C[i] - sign * p["stop_atr"] * atr[i]
                regime = (f"above EMA({p['trend_ema']}) {trend[i]:.2f}" if direction == BUY
                          else f"below EMA({p['trend_ema']}) {trend[i]:.2f}") if p["trend_filter"] else "no trend filter"
                reason = (f"RSI({p['rsi_period']}) reached {r[i - 1]:.1f} on the previous candle "
                          f"({'oversold' if direction == BUY else 'overbought'}), then this candle "
                          f"{'turned up' if direction == BUY else 'turned down'} and closed at {C[i]:.2f}; "
                          f"price {regime}. Exit when close crosses SMA({p['exit_sma']}) "
                          f"(now {sma[i]:.2f}) or after {p['max_hold_bars']} candles.")
                out.append(self._signal(
                    ctx, i, direction, C[i], stop, [], reason,
                    setup_key=f"rsi:{ctx.t[i - 1].isoformat()}",
                    setup_label=f"RSI(2) {'oversold bounce' if direction == BUY else 'overbought fade'}",
                    details=[(f"RSI({p['rsi_period']}) prev / now", f"{r[i - 1]:.1f} / {r[i]:.1f}"),
                             (f"Mean exit ref SMA({p['exit_sma']})", sma[i]),
                             ("Time stop", f"{p['max_hold_bars']} candles")],
                    indicators={"rsi_prev": float(r[i - 1]), "rsi": float(r[i]), "sma_exit": float(sma[i]),
                                "trend_ema": float(trend[i]), "atr": float(atr[i])},
                    max_hold_bars=p["max_hold_bars"],
                ))
                last[direction] = i
        return out

    def exit_signal(self, ctx, i, direction, entry_index):
        s = ctx.sma(self.p["exit_sma"])[i]
        if direction == BUY and ctx.c[i] >= s:
            return "close >= SMA exit"
        if direction == SELL and ctx.c[i] <= s:
            return "close <= SMA exit"
        return None
