"""Strategy unit tests: rules, edge cases, warm-up, and no look-ahead."""

import numpy as np
import pandas as pd
import pytest

from app.config import settings
from app.strategies import (MarketContext, Strategy, annotate_agreement, build_strategies,
                            build_strategy, evaluate_all)
from app.strategies.fib_golden_zone import FibGoldenZoneStrategy
from app.strategies.legacy import SupertrendStrategy
from app.strategies.liquidity_sweep import LiquiditySweepStrategy
from app.strategies.pos_5ema import Pos5EmaStrategy
from app.strategies.rsi2 import Rsi2Strategy
from app.strategy import calculate_supertrend
from tests.conftest import flat, make_df, random_walk


def ctx_of(bars):
    return MarketContext("BTC/USD", make_df(bars))


def sig_bars(sigs):
    return [(s.bar_index, s.direction) for s in sigs]


# ── no look-ahead: prefix invariance for EVERY configured strategy ──────────
@pytest.mark.parametrize("cfg", settings.STRATEGIES, ids=lambda c: c["id"])
@pytest.mark.parametrize("seed", [1, 7])
def test_no_lookahead_prefix_invariance(cfg, seed):
    df = random_walk(1200, seed=seed)
    strat = build_strategy(cfg)
    full = [(s.bar_index, s.direction, s.entry, s.stop_loss, tuple(s.targets), s.setup_key)
            for s in strat.generate(MarketContext("X", df))]
    for k in (700, 950, 1100):
        part = [(s.bar_index, s.direction, s.entry, s.stop_loss, tuple(s.targets), s.setup_key)
                for s in build_strategy(cfg).generate(MarketContext("X", df.iloc[:k]))]
        assert part == [x for x in full if x[0] < k], f"{cfg['id']} uses future data (k={k})"


@pytest.mark.parametrize("cfg", settings.STRATEGIES, ids=lambda c: c["id"])
def test_insufficient_history_is_neutral(cfg):
    strat = build_strategy(cfg)
    ctx = MarketContext("X", random_walk(max(strat.min_bars - 1, 5)))
    res = evaluate_all(ctx, [strat])[0]
    assert res.status == "INSUFFICIENT_DATA" and res.signals == []


def test_signals_have_common_schema(rw_df):
    for res in evaluate_all(MarketContext("BTC/USD", rw_df), build_strategies(settings.STRATEGIES)):
        for s in res.signals[:3]:
            d = s.to_dict()
            for k in ("strategy_id", "strategy_version", "symbol", "timeframe", "signal_direction",
                      "candle_timestamp", "entry_reference", "stop_loss", "target_levels", "risk_reward",
                      "signal_reason", "indicator_values", "confirmation_status"):
                assert k in d
            assert d["signal_direction"] in ("BUY", "SELL")
            if s.stop_loss is not None:
                assert (s.stop_loss < s.entry) if s.direction == "BUY" else (s.stop_loss > s.entry)


# ── retained strategies ─────────────────────────────────────────────────────
def test_supertrend_flips_match_original_math(rw_df):
    for period in (10, 20):
        strat = SupertrendStrategy({"atr_period": period, "multiplier": 3.0})
        sigs = strat.generate(MarketContext("X", rw_df))
        d = calculate_supertrend(rw_df, atr_period=period, multiplier=3.0)["direction"].to_numpy()
        flips = [i for i in range(strat.min_bars, len(d)) if d[i] != d[i - 1] and d[i] and d[i - 1]]
        assert [s.bar_index for s in sigs] == flips
        for s in sigs:
            assert s.direction == ("BUY" if d[s.bar_index] == 1 else "SELL")


def test_retained_strategy_parameters_unchanged():
    by_id = {s["id"]: s for s in settings.STRATEGIES}
    assert by_id["SUPERTREND_10_3"]["params"] == {"atr_period": 10, "multiplier": 3.0}
    assert by_id["SUPERTREND_20_3"]["params"] == {"atr_period": 20, "multiplier": 3.0}
    assert by_id["EMA5_CROSS"]["params"] == {"period": 5}


# ── Fibonacci golden zone ───────────────────────────────────────────────────
FIB_P = {"pivot_left": 2, "pivot_right": 2, "atr_period": 5, "min_impulse_atr": 2.0, "min_rr": 0.5}


def fib_bars(tail):
    bars = flat(70)
    bars.append((100, 100.5, 98.0, 99.0))                      # 70: impulse low
    c = 99.0
    for k in range(71, 81):                                     # 71..80 rally to 110
        o, c = c, 99.0 + (k - 70) * 1.1
        bars.append((o, c + (0.0 if k == 80 else 0.2), o - 0.2, c))
    bars += [(109.9, 109.5, 108.0, 108.5),
             (108.5, 109.0, 107.0, 107.5),                      # 82: confirms pivot high @80
             (107.5, 107.8, 105.0, 105.5),
             (105.5, 105.8, 103.5, 103.8)]                      # 84: touches zone (<=104)
    return bars + tail


def test_fib_buy_on_confirmed_zone_reaction():
    bars = fib_bars([(103.8, 106.0, 103.6, 105.9)])             # 85: bullish, close > prev high
    sigs = FibGoldenZoneStrategy(FIB_P).generate(ctx_of(bars))
    assert sig_bars(sigs) == [(85, "BUY")]
    s = sigs[0]
    assert s.targets[0] == pytest.approx(110.0)
    assert s.indicators["fib_50"] == pytest.approx(104.0)
    assert s.indicators["fib_618"] == pytest.approx(110 - 0.618 * 12)
    assert s.stop_loss < s.indicators["fib_786"]


def test_fib_touch_without_confirmation_no_signal():
    bars = fib_bars([(103.8, 104.0, 103.6, 103.7)] * 6)         # sits in zone, never confirms
    assert FibGoldenZoneStrategy(FIB_P).generate(ctx_of(bars)) == []


def test_fib_structure_break_invalidates():
    bars = fib_bars([(103.8, 103.9, 97.5, 98.2), (98.2, 106.0, 98.1, 105.9)])
    assert FibGoldenZoneStrategy(FIB_P).generate(ctx_of(bars)) == []


def test_fib_narrow_impulse_ignored():
    p = {**FIB_P, "min_impulse_atr": 50.0}
    bars = fib_bars([(103.8, 106.0, 103.6, 105.9)])
    assert FibGoldenZoneStrategy(p).generate(ctx_of(bars)) == []


# ── 5 EMA alert candle ──────────────────────────────────────────────────────
POS_P = {"ema_period": 5, "atr_period": 5}


def pos_bars(tail):
    return flat(40) + [(100, 102.2, 99.9, 102.0),               # 40
                       (102.0, 104.0, 102.5, 103.8)] + tail      # 41: alert (low > EMA5)


def test_pos_sell_on_alert_low_break():
    sigs = Pos5EmaStrategy(POS_P).generate(ctx_of(pos_bars([(103.5, 103.8, 102.0, 102.2)])))
    assert sig_bars(sigs) == [(42, "SELL")]
    s = sigs[0]
    assert s.stop_loss == 104.0 and s.indicators["trigger"] == 102.5 and s.entry == 102.2
    assert s.confirmation_status == "CONFIRMED_TRIGGER"


def test_pos_no_break_no_signal_and_expiry():
    bars = pos_bars([(103.8, 104.5, 102.6, 103.5),               # 42: no break → new alert
                     (103.5, 103.6, 103.0, 103.2),               # 43: doesn't break 102.6
                     (103.2, 103.3, 102.0, 102.1)])              # 44: breaks 42's low — but 42 expired at 43
    sigs = Pos5EmaStrategy(POS_P).generate(ctx_of(bars))
    assert all(i not in (42, 43) for i, _ in sig_bars(sigs))
    key42 = f"alert:{make_df(bars)['datetime'][42].isoformat()}"
    assert all(s.setup_key != key42 for s in sigs)        # alert 42 expired after one candle


def test_pos_failed_breakout_closing_beyond_stop_skipped():
    sigs = Pos5EmaStrategy(POS_P).generate(ctx_of(pos_bars([(103.5, 104.5, 102.0, 104.2)])))
    assert sigs == []


# ── RSI(2) ──────────────────────────────────────────────────────────────────
RSI_P = {"trend_filter": False, "atr_period": 5}


def rsi_bars(tail):
    return flat(40) + [(100, 100.2, 98.8, 99.0), (99, 99.1, 97.8, 98.0), (98, 98.1, 96.8, 97.0)] + tail


def test_rsi2_buy_requires_confirmation():
    sigs = Rsi2Strategy(RSI_P).generate(ctx_of(rsi_bars([(97.0, 97.9, 96.9, 97.8)])))
    assert sig_bars(sigs) == [(43, "BUY")]
    assert sigs[0].indicators["rsi_prev"] < 10
    assert sigs[0].stop_loss < sigs[0].entry and sigs[0].targets == []


def test_rsi2_extreme_alone_no_signal():
    assert Rsi2Strategy(RSI_P).generate(ctx_of(rsi_bars([(97.0, 97.1, 96.0, 96.5)]))) == []


def test_rsi2_trend_filter_blocks_counter_trend():
    p = {**RSI_P, "trend_filter": True, "trend_ema": 20}
    assert Rsi2Strategy(p).generate(ctx_of(rsi_bars([(97.0, 97.9, 96.9, 97.8)]))) == []


def test_rsi2_exit_rule():
    ctx = ctx_of(rsi_bars([(97.0, 97.9, 96.9, 97.8), (97.8, 99.8, 97.7, 99.7)]))
    st = Rsi2Strategy(RSI_P)
    assert st.exit_signal(ctx, 44, "BUY", 43) is not None


# ── Liquidity sweep ─────────────────────────────────────────────────────────
LIQ_P = {"pivot_left": 2, "pivot_right": 2, "atr_period": 5, "use_sessions": False, "use_prev_day": False}


def liq_bars(tail):
    return flat(45) + [(100, 100.4, 98.0, 99.8)] + flat(5) + tail   # swing low 98.0 @45


def test_liquidity_sweep_same_candle_reclaim_buy():
    sigs = LiquiditySweepStrategy(LIQ_P).generate(ctx_of(liq_bars([(99.5, 99.6, 97.6, 98.8)])))
    assert sig_bars(sigs) == [(51, "BUY")]
    s = sigs[0]
    assert s.indicators["level_price"] == 98.0 and s.indicators["sweep_extreme"] == 97.6
    assert s.stop_loss < 97.6 and s.targets[0] > s.entry
    assert s.indicators["penetration"] == pytest.approx(0.4)


def test_liquidity_cross_without_reclaim_is_breakout():
    bars = liq_bars([(99.0, 99.1, 97.6, 97.7), (97.7, 97.8, 97.3, 97.5), (97.5, 99.0, 97.4, 98.6)])
    assert LiquiditySweepStrategy(LIQ_P).generate(ctx_of(bars)) == []


def test_liquidity_two_candle_reclaim():
    bars = liq_bars([(99.0, 99.1, 97.6, 97.8), (97.8, 98.7, 97.7, 98.5)])
    assert sig_bars(LiquiditySweepStrategy(LIQ_P).generate(ctx_of(bars))) == [(52, "BUY")]


def test_liquidity_no_rejection_wick_no_signal():
    bars = liq_bars([(99.9, 99.95, 97.9, 98.05)])     # tiny lower wick vs range
    assert LiquiditySweepStrategy(LIQ_P).generate(ctx_of(bars)) == []


def test_liquidity_prev_day_levels_created_across_weekend_gap():
    day1 = make_df(flat(288, 100.0))                     # complete UTC day
    gap_start = day1["datetime"].iloc[-1] + pd.Timedelta(days=2, minutes=5)
    day2 = make_df(flat(20, 100.0) + [(100, 100.4, 99.0, 100.2)], start=gap_start)
    ctx = MarketContext("XAU/USD", pd.concat([day1, day2], ignore_index=True))
    p = {**LIQ_P, "use_prev_day": True, "use_swing": False, "min_wick_ratio": 0.3}
    sigs = LiquiditySweepStrategy(p).generate(ctx)
    assert sigs and sigs[-1].indicators["level_source"] == "PREV_DAY"


# ── engine robustness ───────────────────────────────────────────────────────
class Boom(Strategy):
    id = "BOOM"

    @property
    def min_bars(self):
        return 1

    def generate(self, ctx):
        raise ValueError("kaboom")


def test_failing_strategy_does_not_block_others(rw_df):
    res = evaluate_all(MarketContext("X", rw_df), [Boom(), SupertrendStrategy()])
    assert res[0].status == "ERROR" and "kaboom" in res[0].error
    assert res[1].status in ("SIGNAL", "NEUTRAL")


def test_two_strategies_same_candle_agreement_annotated():
    ctx = ctx_of(liq_bars([(99.5, 99.6, 97.6, 98.8)]))
    a = LiquiditySweepStrategy(LIQ_P).generate(ctx)[0]
    b = LiquiditySweepStrategy(LIQ_P, strategy_id="OTHER").generate(ctx)[0]
    annotate_agreement([a, b])
    assert a.agreement == ["OTHER BUY"] and b.agreement == ["LIQUIDITY_SWEEP BUY"]
