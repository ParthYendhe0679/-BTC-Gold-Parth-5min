"""
fib_golden_zone.py — FIB_GOLDEN_ZONE v1.0

Rules (deterministic, closed candles only)
──────────────────────────────────────────
Swings     : pivot high at bar j = high[j] > highs of the `pivot_left` bars before
             and >= highs of the `pivot_right` bars after. It is CONFIRMED (and only
             then used) at bar j + pivot_right — never earlier, so no repainting.
Impulse    : when a swing high H (index h) is confirmed, the bullish impulse anchor
             is the lowest low between the previous confirmed swing low and h
             (inclusive). Require H - Lo >= min_impulse_atr × ATR(atr_period)[h].
             Bearish mirror on a newly confirmed swing low. The newest confirmed
             impulse replaces any older active setup (one setup at a time).
Zone       : 50 % – 61.8 % retracement of the impulse (a reaction zone, not a
             guaranteed reversal).
Invalidate : (bull) any low < Lo (structure broken), any close below the 78.6 %
             level, any high > H before entry (impulse extended), or
             `max_setup_bars` bars after the swing high. Mirror for bear.
BUY entry  : on a completed bar i after confirmation:
             • price touched the zone (low <= 50 % level) within the last
               `touch_lookback` bars including i,
             • bullish confirmation candle: close > open AND close > high[i-1],
             • close >= 61.8 % level (did not close below the zone) and close < H,
             • reward/risk to Target 1 >= min_rr.
Stop       : min(lowest low since H, 78.6 % level) - stop_buffer_atr × ATR.
Targets    : fib extensions of the impulse, default [0.0, -0.272] → T1 = swing
             high retest, T2 = 127.2 % extension.
One alert per swing setup (setup_key = impulse anchor timestamps).
"""

from typing import Any, Dict, List, Optional

import numpy as np

from app.strategies.base import (BUY, SELL, MarketContext, Signal, Strategy,
                                 is_pivot_high, is_pivot_low)


class FibGoldenZoneStrategy(Strategy):
    id = "FIB_GOLDEN_ZONE"
    name = "Fibonacci Golden Zone"
    version = "1.0"
    defaults: Dict[str, Any] = {
        "pivot_left": 5, "pivot_right": 5, "atr_period": 14,
        "min_impulse_atr": 3.0, "zone_min": 0.5, "zone_max": 0.618,
        "invalidation_level": 0.786, "stop_buffer_atr": 0.1,
        "target_extensions": [0.0, -0.272], "max_setup_bars": 48,
        "touch_lookback": 2, "min_rr": 1.0, "max_hold_bars": 48,
    }

    @property
    def min_bars(self) -> int:
        return max(60, self.p["atr_period"] * 3)

    def generate(self, ctx: MarketContext) -> List[Signal]:
        p = self.p
        n = len(ctx)
        if n < self.min_bars:
            return []
        H, L, C, O = ctx.h, ctx.l, ctx.c, ctx.o
        atr = ctx.atr(p["atr_period"])
        Lft, R = p["pivot_left"], p["pivot_right"]
        last_ph: Optional[int] = None
        last_pl: Optional[int] = None
        setup: Optional[Dict[str, Any]] = None
        out: List[Signal] = []

        def update(s: Dict[str, Any], k: int) -> None:
            """Apply bar k (already closed) to setup s: invalidation + touch tracking."""
            bull = s["dir"] == BUY
            if bull:
                if L[k] < s["lo"] or C[k] < s["inv"] or H[k] > s["hi"]:
                    s["dead"] = True
                    return
                s["extreme"] = min(s["extreme"], L[k])
                if L[k] <= s["z_near"]:
                    s["last_touch"] = k
            else:
                if H[k] > s["hi"] or C[k] > s["inv"] or L[k] < s["lo"]:
                    s["dead"] = True
                    return
                s["extreme"] = max(s["extreme"], H[k])
                if H[k] >= s["z_near"]:
                    s["last_touch"] = k

        for i in range(self.min_bars, n):
            # 1) confirm pivots known at bar i
            j = i - R
            new_high = is_pivot_high(H, j, Lft, R)
            new_low = is_pivot_low(L, j, Lft, R)
            if new_high:
                start = last_pl if last_pl is not None else max(0, j - 50)
                lo_idx = start + int(np.argmin(L[start:j + 1]))
                setup = self._make(ctx, BUY, lo_idx, j, i, atr) or setup
                last_ph = j
            if new_low:
                start = last_ph if last_ph is not None else max(0, j - 50)
                hi_idx = start + int(np.argmax(H[start:j + 1]))
                setup = self._make(ctx, SELL, hi_idx, j, i, atr) or setup
                last_pl = j
            if setup is None or setup.get("dead"):
                continue
            # replay bars between the swing and its confirmation (known history)
            if not setup.get("replayed"):
                for k in range(setup["swing_idx"] + 1, i):
                    update(setup, k)
                    if setup.get("dead"):
                        break
                setup["replayed"] = True
                if setup.get("dead"):
                    continue
            update(setup, i)
            if setup.get("dead"):
                continue
            if i - setup["swing_idx"] > p["max_setup_bars"]:
                setup["dead"] = True
                continue
            sig = self._check_entry(ctx, setup, i, atr)
            if sig:
                out.append(sig)
                setup["dead"] = True   # one alert per swing setup
        return out

    def _make(self, ctx, direction, anchor_idx, swing_idx, confirm_idx, atr) -> Optional[Dict[str, Any]]:
        p = self.p
        if direction == BUY:
            lo, hi = ctx.l[anchor_idx], ctx.h[swing_idx]
        else:
            hi, lo = ctx.h[anchor_idx], ctx.l[swing_idx]
        rng = hi - lo
        a = atr[swing_idx]
        if not np.isfinite(a) or a <= 0 or rng < p["min_impulse_atr"] * a or swing_idx - anchor_idx < 2:
            return None   # narrow / ambiguous impulse → no setup
        if direction == BUY:
            z_near, z_far, inv = hi - p["zone_min"] * rng, hi - p["zone_max"] * rng, hi - p["invalidation_level"] * rng
            extreme = hi
        else:
            z_near, z_far, inv = lo + p["zone_min"] * rng, lo + p["zone_max"] * rng, lo + p["invalidation_level"] * rng
            extreme = lo
        return {
            "dir": direction, "lo": lo, "hi": hi, "rng": rng, "anchor_idx": anchor_idx,
            "swing_idx": swing_idx, "confirm_idx": confirm_idx, "z_near": z_near, "z_far": z_far,
            "inv": inv, "extreme": extreme, "last_touch": None,
            "key": f"{ctx.t[anchor_idx].isoformat()}>{ctx.t[swing_idx].isoformat()}",
        }

    def _check_entry(self, ctx: MarketContext, s: Dict[str, Any], i: int, atr: np.ndarray) -> Optional[Signal]:
        p = self.p
        if s["last_touch"] is None or i - s["last_touch"] > p["touch_lookback"]:
            return None
        O, H, L, C = ctx.o, ctx.h, ctx.l, ctx.c
        a = atr[i]
        bull = s["dir"] == BUY
        if bull:
            if not (C[i] > O[i] and C[i] > H[i - 1] and C[i] >= s["z_far"] and C[i] < s["hi"]):
                return None
            stop = min(s["extreme"], s["inv"]) - p["stop_buffer_atr"] * a
            targets = [s["hi"] - e * s["rng"] for e in p["target_extensions"]]
        else:
            if not (C[i] < O[i] and C[i] < L[i - 1] and C[i] <= s["z_far"] and C[i] > s["lo"]):
                return None
            stop = max(s["extreme"], s["inv"]) + p["stop_buffer_atr"] * a
            targets = [s["lo"] + e * s["rng"] for e in p["target_extensions"]]
        risk = abs(C[i] - stop)
        if risk <= 0 or abs(targets[0] - C[i]) / risk < p["min_rr"]:
            return None
        word = "Bullish" if bull else "Bearish"
        swing_from, swing_to = (s["lo"], s["hi"]) if bull else (s["hi"], s["lo"])
        reason = (f"{word} impulse {swing_from:.2f} → {swing_to:.2f} ({s['rng'] / atr[s['swing_idx']]:.1f}× ATR). "
                  f"Price retraced to {s['extreme']:.2f} into the 50–61.8% zone "
                  f"[{min(s['z_near'], s['z_far']):.2f}–{max(s['z_near'], s['z_far']):.2f}] and the "
                  f"{'bullish' if bull else 'bearish'} confirmation candle closed at {C[i]:.2f} "
                  f"beyond the previous candle's {'high' if bull else 'low'}. "
                  f"Setup invalid on a close beyond the 78.6% level {s['inv']:.2f}.")
        return self._signal(
            ctx, i, s["dir"], C[i], stop, targets, reason, setup_key=s["key"],
            setup_label=f"{word} retracement into Fibonacci golden zone",
            details=[("Swing", f"{swing_from:.2f} → {swing_to:.2f}"),
                     ("Golden zone", f"{min(s['z_near'], s['z_far']):.2f} – {max(s['z_near'], s['z_far']):.2f}"),
                     ("Invalidation (78.6%)", s["inv"])],
            indicators={"swing_start": swing_from, "swing_end": swing_to,
                        "fib_50": s["z_near"], "fib_618": s["z_far"], "fib_786": s["inv"],
                        "retrace_extreme": s["extreme"], "atr": float(a),
                        "swing_time": ctx.t[s["swing_idx"]].isoformat()},
            max_hold_bars=p["max_hold_bars"],
        )
