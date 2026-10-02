"""
pos_5ema.py — POS_5EMA v1.0 (5 EMA alert-candle breakout)

An original implementation INSPIRED BY the publicly described "5 EMA" concept
popularised by Power of Stocks. It is NOT claimed to reproduce any trader's
proprietary rules.

Rules (closed candles only)
───────────────────────────
Alert candle (SELL): a completed candle entirely ABOVE EMA(5): low > EMA5.
Alert candle (BUY) : a completed candle entirely BELOW EMA(5): high < EMA5.
Trigger            : the NEXT candle trades through the alert candle's low (SELL)
                     / high (BUY). If the next candle is itself a new alert candle
                     without triggering, it replaces the old one. Otherwise the
                     alert expires after exactly one candle (`trigger_expiry_bars`).
Signal timing      : emitted at the CLOSE of the trigger candle (confirmed
                     breakout). entry_reference = trigger-candle close (what a
                     signal follower can act on); the trigger level is reported too.
Skip               : if the trigger candle closed back beyond the stop (failed
                     breakout), or risk is outside [min_risk_atr, max_risk_atr] × ATR
                     (low-quality: tiny noise candles or abnormal spikes).
Stop               : alert candle high (SELL) / low (BUY).
Targets            : `target_r` multiples of risk (default 2R, 3R).
Optional filter    : trend_filter → SELL only when EMA5 < EMA(trend_ema), BUY
                     only when EMA5 > EMA(trend_ema). Off by default.
One alert per alert candle (setup_key = alert candle time).
"""

from typing import Any, Dict, List

import numpy as np

from app.strategies.base import BUY, SELL, MarketContext, Signal, Strategy


class Pos5EmaStrategy(Strategy):
    id = "POS_5EMA"
    name = "5 EMA Alert-Candle"
    version = "1.0"
    defaults: Dict[str, Any] = {
        "ema_period": 5, "atr_period": 14, "trigger_expiry_bars": 1,
        "min_risk_atr": 0.25, "max_risk_atr": 3.0, "target_r": [2.0, 3.0],
        "trend_filter": False, "trend_ema": 50, "allow_long": True, "allow_short": True,
        "max_hold_bars": 24,
    }

    @property
    def min_bars(self) -> int:
        return max(30, self.p["atr_period"] * 2, self.p["trend_ema"] if self.p["trend_filter"] else 0)

    def generate(self, ctx: MarketContext) -> List[Signal]:
        p = self.p
        n = len(ctx)
        if n < self.min_bars:
            return []
        O, H, L, C = ctx.o, ctx.h, ctx.l, ctx.c
        e5 = ctx.ema(p["ema_period"])
        atr = ctx.atr(p["atr_period"])
        trend = ctx.ema(p["trend_ema"]) if p["trend_filter"] else None
        sell_alert = buy_alert = None   # (index)
        out: List[Signal] = []
        for i in range(self.min_bars, n):
            # 1) triggers from live alerts (alerts strictly before i)
            if sell_alert is not None and i - sell_alert <= p["trigger_expiry_bars"] and L[i] < L[sell_alert]:
                s = self._build(ctx, i, SELL, sell_alert, e5, atr, trend)
                if s:
                    out.append(s)
                sell_alert = None
            if buy_alert is not None and i - buy_alert <= p["trigger_expiry_bars"] and H[i] > H[buy_alert]:
                s = self._build(ctx, i, BUY, buy_alert, e5, atr, trend)
                if s:
                    out.append(s)
                buy_alert = None
            # 2) expire
            if sell_alert is not None and i - sell_alert >= p["trigger_expiry_bars"]:
                sell_alert = None
            if buy_alert is not None and i - buy_alert >= p["trigger_expiry_bars"]:
                buy_alert = None
            # 3) new alert candles (replace older ones)
            if p["allow_short"] and L[i] > e5[i]:
                sell_alert = i
            if p["allow_long"] and H[i] < e5[i]:
                buy_alert = i
        return out

    def _build(self, ctx, i, direction, a, e5, atr, trend):
        p = self.p
        C = ctx.c
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            return None
        if trend is not None:
            if direction == SELL and not e5[i] < trend[i]:
                return None
            if direction == BUY and not e5[i] > trend[i]:
                return None
        if direction == SELL:
            trigger, stop = ctx.l[a], ctx.h[a]
            if C[i] >= stop:
                return None
        else:
            trigger, stop = ctx.h[a], ctx.l[a]
            if C[i] <= stop:
                return None
        alert_range = ctx.h[a] - ctx.l[a]
        if not (p["min_risk_atr"] * atr[i] <= alert_range <= p["max_risk_atr"] * atr[i]):
            return None
        entry = C[i]
        risk = abs(entry - stop)
        sign = 1 if direction == BUY else -1
        targets = [entry + sign * r * risk for r in p["target_r"]]
        side = "below" if direction == BUY else "above"
        brk = "high" if direction == BUY else "low"
        reason = (f"Alert candle at {ctx.t[a]:%H:%M} UTC closed entirely {side} EMA({p['ema_period']}) "
                  f"{e5[a]:.2f} (range {ctx.l[a]:.2f}–{ctx.h[a]:.2f}). The next candle broke its "
                  f"{brk} {trigger:.2f} and closed at {entry:.2f}. Stop at the alert candle's opposite extreme.")
        return self._signal(
            ctx, i, direction, entry, stop, targets, reason,
            setup_key=f"alert:{ctx.t[a].isoformat()}",
            setup_label=f"5 EMA alert-candle {'breakout' if direction == BUY else 'breakdown'}",
            details=[("Alert candle", f"{ctx.t[a]:%H:%M} UTC  H {ctx.h[a]:.2f} / L {ctx.l[a]:.2f}"),
                     ("Trigger level", trigger), (f"EMA({p['ema_period']})", e5[i])],
            indicators={"ema5": float(e5[i]), "alert_high": float(ctx.h[a]), "alert_low": float(ctx.l[a]),
                        "trigger": float(trigger), "atr": float(atr[i])},
            confirmation_status="CONFIRMED_TRIGGER",
            max_hold_bars=p["max_hold_bars"],
        )
