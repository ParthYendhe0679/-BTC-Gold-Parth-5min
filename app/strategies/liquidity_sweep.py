"""
liquidity_sweep.py — LIQUIDITY_SWEEP v1.0

A price-action strategy: price trades beyond a previously established reference
level and then closes back through it. It does NOT observe real orders or stop
clusters — "liquidity" here means a price level where resting orders are
commonly assumed to sit.

Liquidity levels (each: stable id, source, creation time, status)
────────────────────────────────────────────────────────────────
SWING      : confirmed pivot high/low (pivot_left / pivot_right), usable from
             its confirmation bar onward.
EQUAL      : a new swing within equal_tolerance_atr × ATR of an active swing on
             the same side → merged level at the outer price (EQH / EQL).
PREV_CANDLE: previous completed candle high/low (off by default — too noisy).
SESSION    : Asia 00–07, London 07–13, New York 13–21 UTC session high/low,
             created at the session's last bar (only if the session is fully
             inside the loaded data).
PREV_DAY   : previous UTC day high/low, created at the day's last bar (only if
             the day is complete in the data and has >= 50 % of expected bars).
Status     : active → swept | broken | expired (max_level_age_bars; previous
             candle levels live 1 bar).

Bearish sweep (SELL) of an active high level P at completed bar i
─────────────────────────────────────────────────────────────────
1. high[i] > P + min_penetration_atr × ATR.
2. Reclaim: close[i] < P (same candle), OR the level was probed on the previous
   `reclaim_bars` candle(s) without a close back below and close[i] < P now.
3. Penetration (sweep extreme − P) <= max_penetration_atr × ATR — otherwise the
   move is classified a BREAKOUT and the level is marked broken.
4. Rejection: same-candle sweep needs upper-wick / range >= min_wick_ratio;
   two-candle sweep needs a bearish reclaim candle (close < open).
5. Optional filters: trend (close < EMA(trend_ema)), relative volume (only when
   the feed has reliable volume), min reward/risk in liquidity-target mode.
Sweep vs breakout: if price CLOSES beyond the level and does not close back
within `reclaim_bars` candles, the level is broken and no reversal signal fires.

Entry = close of the confirmation candle. Stop = sweep extreme ± stop_buffer_atr × ATR.
Targets: `target_r` multiples (default) or nearest opposing active levels
(target_mode="liquidity", falls back to R multiples).
When several levels are swept on one candle, one signal is sent using the most
significant level (PREV_DAY > SESSION > EQUAL > SWING > PREV_CANDLE); a
cooldown suppresses repeat signals in the same direction.
Bullish sweep = exact mirror on low levels.
"""

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from app.strategies.base import (BUY, SELL, MarketContext, Signal, Strategy,
                                 is_pivot_high, is_pivot_low)

PRIORITY = {"PREV_DAY": 5, "SESSION": 4, "EQUAL": 3, "SWING": 2, "PREV_CANDLE": 1}
SESSIONS = (("ASIA", 0, 7), ("LONDON", 7, 13), ("NEW_YORK", 13, 21))


def session_of(ts: pd.Timestamp) -> Optional[str]:
    for name, a, b in SESSIONS:
        if a <= ts.hour < b:
            return name
    return None


class LiquiditySweepStrategy(Strategy):
    id = "LIQUIDITY_SWEEP"
    name = "Liquidity Sweep"
    version = "1.0"
    defaults: Dict[str, Any] = {
        "pivot_left": 5, "pivot_right": 3, "atr_period": 14,
        "use_swing": True, "use_equal": True, "equal_tolerance_atr": 0.1,
        "use_prev_candle": False, "use_sessions": True, "use_prev_day": True,
        "min_penetration_atr": 0.05, "max_penetration_atr": 1.0,
        "min_wick_ratio": 0.3, "reclaim_bars": 1, "max_level_age_bars": 288,
        "stop_buffer_atr": 0.1, "target_mode": "r_multiple", "target_r": [1.5, 3.0],
        "min_rr": 1.5, "cooldown_bars": 6, "trend_filter": False, "trend_ema": 200,
        "volume_filter": False, "min_rel_volume": 1.2, "max_hold_bars": 36,
        "max_levels_per_side": 40,
    }

    @property
    def min_bars(self) -> int:
        return max(40, self.p["atr_period"] * 3)

    # ── level bookkeeping ────────────────────────────────────────────────────
    def _add(self, levels, kind, source, price, created, ctx, label="", formed=None):
        lid = f"{source}:{kind}:{ctx.t[formed if formed is not None else created].isoformat()}:{price:.5f}"
        levels.append({"id": lid, "kind": kind, "source": source, "label": label or source,
                       "price": float(price), "created": created, "status": "active", "probe": None})

    def _create_levels(self, ctx: MarketContext, i: int, levels: List[Dict], atr: np.ndarray):
        p = self.p
        H, L = ctx.h, ctx.l
        j = i - p["pivot_right"]
        if p["use_swing"]:
            for kind, is_piv, arr in (("high", is_pivot_high, H), ("low", is_pivot_low, L)):
                if not is_piv(arr, j, p["pivot_left"], p["pivot_right"]):
                    continue
                price = arr[j]
                merged = False
                if p["use_equal"] and np.isfinite(atr[i]):
                    for lv in levels:
                        if (lv["status"] == "active" and lv["kind"] == kind and lv["source"] in ("SWING", "EQUAL")
                                and abs(lv["price"] - price) <= p["equal_tolerance_atr"] * atr[i]):
                            outer = max(lv["price"], price) if kind == "high" else min(lv["price"], price)
                            lv["status"] = "merged"
                            self._add(levels, kind, "EQUAL", outer, i, ctx,
                                      "Equal highs" if kind == "high" else "Equal lows", formed=j)
                            merged = True
                            break
                if not merged:
                    self._add(levels, kind, "SWING", price, i, ctx,
                              "Swing high" if kind == "high" else "Swing low", formed=j)
        if p["use_prev_candle"]:
            self._add(levels, "high", "PREV_CANDLE", H[i], i, ctx, "Previous candle high")
            self._add(levels, "low", "PREV_CANDLE", L[i], i, ctx, "Previous candle low")

    def _boundary_levels(self, ctx: MarketContext, i: int, levels: List[Dict], tf: pd.Timedelta):
        """At the first bar of a new session/day, publish the completed period's
        high/low. created = i-1 (the period was fully known at that bar's close),
        so bar i itself may already sweep it. Robust to data gaps (weekends)."""
        if i < 1:
            return
        p = self.p
        t, H, L = ctx.t, ctx.h, ctx.l
        prev, cur = t[i - 1], t[i]
        first_t = t[0]
        if p["use_sessions"]:
            s = session_of(prev)
            if s and (session_of(cur) != s or cur.normalize() != prev.normalize()):
                start = prev.normalize() + pd.Timedelta(hours=next(a for nm, a, _ in SESSIONS if nm == s))
                if start >= first_t:
                    m = (t >= start) & (t <= prev)
                    if m.sum() >= 6:
                        self._add(levels, "high", "SESSION", H[m].max(), i - 1, ctx, f"{s.title().replace('_', ' ')} session high")
                        self._add(levels, "low", "SESSION", L[m].min(), i - 1, ctx, f"{s.title().replace('_', ' ')} session low")
        if p["use_prev_day"] and cur.normalize() != prev.normalize():
            start = prev.normalize()
            if start >= first_t:
                m = (t >= start) & (t <= prev)
                if m.sum() >= 0.5 * (pd.Timedelta(days=1) / tf):
                    for lv in levels:
                        if lv["source"] == "PREV_DAY" and lv["status"] == "active":
                            lv["status"] = "expired"
                    self._add(levels, "high", "PREV_DAY", H[m].max(), i - 1, ctx, "Previous day high")
                    self._add(levels, "low", "PREV_DAY", L[m].min(), i - 1, ctx, "Previous day low")

    # ── main loop ────────────────────────────────────────────────────────────
    def generate(self, ctx: MarketContext) -> List[Signal]:
        p = self.p
        n = len(ctx)
        if n < self.min_bars:
            return []
        O, H, L, C = ctx.o, ctx.h, ctx.l, ctx.c
        atr = ctx.atr(p["atr_period"])
        trend = ctx.ema(p["trend_ema"]) if p["trend_filter"] else None
        use_vol = p["volume_filter"] and ctx.has_volume
        vsma = ctx.volume_sma(20) if use_vol else None
        levels: List[Dict[str, Any]] = []
        last_sig = {BUY: -10**9, SELL: -10**9}
        out: List[Signal] = []

        for i in range(n):
            self._boundary_levels(ctx, i, levels, ctx.tf_delta)
            a = atr[i]
            if i >= self.min_bars and np.isfinite(a) and a > 0:
                cands = {BUY: [], SELL: []}
                for lv in levels:
                    if lv["status"] != "active" or lv["created"] >= i:
                        continue
                    age = i - lv["created"]
                    if age > (1 if lv["source"] == "PREV_CANDLE" else p["max_level_age_bars"]):
                        lv["status"] = "expired"
                        continue
                    res = self._evaluate_level(lv, i, O, H, L, C, a)
                    if res:
                        cands[res["direction"]].append(res)
                for direction, cs in cands.items():
                    if not cs:
                        continue
                    for c in cs:
                        c["level"]["status"] = "swept"
                    if i - last_sig[direction] <= p["cooldown_bars"]:
                        continue
                    best = max(cs, key=lambda c: (PRIORITY[c["level"]["source"]], c["level"]["created"]))
                    sig = self._build(ctx, i, best, cs, levels, a, trend, vsma)
                    if sig:
                        out.append(sig)
                        last_sig[direction] = i
                # prune
                for kind in ("high", "low"):
                    act = [lv for lv in levels if lv["status"] == "active" and lv["kind"] == kind]
                    for lv in act[:-p["max_levels_per_side"]]:
                        lv["status"] = "expired"
                levels = [lv for lv in levels if lv["status"] == "active"]
            self._create_levels(ctx, i, levels, atr)
        return out

    def _evaluate_level(self, lv, i, O, H, L, C, a) -> Optional[Dict[str, Any]]:
        p = self.p
        P = lv["price"]
        high_side = lv["kind"] == "high"
        beyond = (H[i] - P) if high_side else (P - L[i])          # how far price traded through
        closed_back = C[i] < P if high_side else C[i] > P
        closed_beyond = C[i] > P if high_side else C[i] < P
        direction = SELL if high_side else BUY
        rng = H[i] - L[i]

        probe = lv["probe"]
        if probe is not None:   # level was closed beyond earlier — waiting for reclaim
            extreme = max(probe["extreme"], H[i]) if high_side else min(probe["extreme"], L[i])
            pen = (extreme - P) if high_side else (P - extreme)
            if pen > p["max_penetration_atr"] * a:
                lv["status"] = "broken"
                return None
            if closed_back:
                bearish_ok = C[i] < O[i] if high_side else C[i] > O[i]
                if not bearish_ok:
                    lv["status"] = "swept"
                    return None
                return {"direction": direction, "level": lv, "extreme": extreme, "pen": pen,
                        "bars": i - probe["start"] + 1, "probe_start": probe["start"]}
            probe["extreme"] = extreme
            if i - probe["start"] >= p["reclaim_bars"]:
                lv["status"] = "broken"   # sustained breakout
            return None

        if beyond <= 0:
            return None
        if closed_beyond:
            if p["reclaim_bars"] <= 0:
                lv["status"] = "broken"
            else:
                lv["probe"] = {"start": i, "extreme": H[i] if high_side else L[i]}
            return None
        if beyond < p["min_penetration_atr"] * a:
            return None
        if beyond > p["max_penetration_atr"] * a:
            lv["status"] = "broken"
            return None
        wick = (H[i] - max(O[i], C[i])) if high_side else (min(O[i], C[i]) - L[i])
        if rng <= 0 or wick / rng < p["min_wick_ratio"]:
            lv["status"] = "swept"   # liquidity taken, but no rejection → no signal
            return None
        return {"direction": direction, "level": lv, "extreme": H[i] if high_side else L[i],
                "pen": beyond, "bars": 1, "probe_start": i}

    def _build(self, ctx, i, best, cands, levels, a, trend, vsma) -> Optional[Signal]:
        p = self.p
        C = ctx.c
        direction = best["direction"]
        lv = best["level"]
        if trend is not None:
            if direction == BUY and not C[i] > trend[i]:
                return None
            if direction == SELL and not C[i] < trend[i]:
                return None
        rel_vol = None
        if vsma is not None:
            base = vsma[i - 1] if i > 0 else np.nan
            rel_vol = ctx.v[i] / base if np.isfinite(base) and base > 0 else None
            if rel_vol is None or rel_vol < p["min_rel_volume"]:
                return None
        sign = 1 if direction == BUY else -1
        entry = C[i]
        stop = best["extreme"] - sign * p["stop_buffer_atr"] * a
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        targets = [entry + sign * r * risk for r in p["target_r"]]
        if p["target_mode"] == "liquidity":
            opp = "high" if direction == BUY else "low"
            lv_prices = sorted((x["price"] for x in levels if x["status"] == "active" and x["kind"] == opp
                                and (x["price"] - entry) * sign > 0), key=lambda x: abs(x - entry))
            liq = [x for x in lv_prices if abs(x - entry) / risk >= p["min_rr"]][:2]
            if liq:
                targets = liq + targets[len(liq):]
        word = "Bullish" if direction == BUY else "Bearish"
        side = "below" if direction == BUY else "above"
        back = "above" if direction == BUY else "below"
        extreme_name = "Sweep low" if direction == BUY else "Sweep high"
        also = [c["level"]["label"] for c in cands if c is not best]
        reason = (f"Price traded {best['pen']:.2f} {side} the {lv['label'].lower()} {lv['price']:.2f} "
                  f"(to {best['extreme']:.2f}, {best['pen'] / a:.2f}× ATR) and the "
                  f"{'same' if best['bars'] == 1 else 'following'} candle closed back {back} it at {entry:.2f} "
                  f"— a reclaim, not a sustained breakout."
                  + (f" Also swept: {', '.join(also)}." if also else ""))
        return self._signal(
            ctx, i, direction, entry, stop, targets, reason,
            setup_key=lv["id"],
            setup_label=f"{word} liquidity sweep",
            details=[("Level type", lv["label"]), ("Swept level", lv["price"]),
                     (extreme_name, best["extreme"]), ("Penetration", f"{best['pen']:.2f} ({best['pen'] / a:.2f}× ATR)")],
            indicators={"level_id": lv["id"], "level_source": lv["source"], "level_price": lv["price"],
                        "level_created": ctx.t[lv["created"]].isoformat(), "sweep_extreme": float(best["extreme"]),
                        "penetration": float(best["pen"]), "atr": float(a), "reclaim_bars": best["bars"],
                        "relative_volume": rel_vol},
            max_hold_bars=p["max_hold_bars"],
        )
