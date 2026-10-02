"""End-to-end scanner pipeline with mocked provider + Telegram: dedup, restart,
staleness, isolation, overlap, provider lag, delivery recording."""

import asyncio
from datetime import datetime, timezone

import pandas as pd
import pytest

import app.scanner as scanner_mod
from app.config import settings
from app.scanner import Scanner, market_open, next_run_time
from app.state import StateStore
from app.strategies.base import Strategy
from app.telegram import DeliveryResult

ASSET = {"name": "Bitcoin", "symbol": "BTC/USD", "market": "crypto", "decimals": 2}
GOLD = {"name": "Gold", "symbol": "XAU/USD", "market": "fx", "decimals": 2}


class FireAt(Strategy):
    """Emits a BUY on any candle whose open time is in `times`."""
    id = "FAKE"
    name = "Fake"

    def __init__(self, times, sid="FAKE"):
        super().__init__(strategy_id=sid)
        self.times = {pd.Timestamp(t, tz="UTC") for t in times}

    @property
    def min_bars(self):
        return 1

    def generate(self, ctx):
        return [self._signal(ctx, i, "BUY", ctx.c[i], ctx.c[i] - 1, [ctx.c[i] + 2], "test", f"k:{ctx.t[i]}")
                for i in range(len(ctx)) if ctx.t[i] in self.times]


def rows_until(last_open: str, n: int = 60):
    end = pd.Timestamp(last_open, tz="UTC")
    return [{"datetime": (end - pd.Timedelta(minutes=5 * k)).strftime("%Y-%m-%d %H:%M:%S"),
             "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": None} for k in range(n)][::-1]


class Clock:
    def __init__(self, iso):
        self.t = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)

    def __call__(self):
        return self.t


def run(coro):
    return asyncio.run(coro)


def make(store, strategies, clock, fetch, sent, assets=(ASSET,)):
    def sender(text):
        sent.append(text)
        return DeliveryResult(True, "SENT", 1, message_id=len(sent))
    return Scanner(store, strategies, list(assets), fetch=fetch, sender=sender, now_fn=clock)


async def scan_and_deliver(sc):
    sigs = await sc.scan_once()
    worker = asyncio.create_task(sc.delivery_worker())
    await asyncio.wait_for(sc.queue.join(), 5)
    worker.cancel()
    return sigs


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_MIN_SEND_INTERVAL", 0)
    monkeypatch.setattr(settings, "STALE_RETRY_SECONDS", 0)
    scanner_mod.scanner_state["assets"].clear()
    scanner_mod.scanner_state["strategies"].clear()


def test_baseline_then_single_alert_then_dedup_and_restart():
    store = StateStore(":memory:")
    clock = Clock("2026-10-01T10:00:08")
    data = {"last": "2026-10-01 10:00:00"}                       # provider includes forming bar
    fetch = lambda **kw: rows_until(data["last"])
    sent = []
    strat = FireAt(["2026-10-01 09:55", "2026-10-01 10:00"])
    sc = make(store, [strat], clock, fetch, sent)

    assert run(scan_and_deliver(sc)) == [] and sent == []         # startup baseline: 09:55 not sent

    clock.t = datetime(2026, 10, 1, 10, 5, 8, tzinfo=timezone.utc)
    data["last"] = "2026-10-01 10:05:00"
    sigs = run(scan_and_deliver(sc))
    assert len(sigs) == 1 and len(sent) == 1 and "BTC/USD | 5 MIN" in sent[0]
    rec = store.recent()[0]
    assert rec["status"] == "SENT" and rec["latency_ms"] is not None

    assert run(scan_and_deliver(sc)) == [] and len(sent) == 1    # same candle again → nothing

    sc2 = make(store, [strat], clock, fetch, sent)                # restart, persisted DB
    assert run(scan_and_deliver(sc2)) == [] and len(sent) == 1

    sc3 = make(StateStore(":memory:"), [strat], clock, fetch, sent)   # restart, wiped disk (Render free)
    assert run(scan_and_deliver(sc3)) == [] and len(sent) == 1        # baseline → no re-send


def test_stale_signal_logged_not_sent():
    store = StateStore(":memory:")
    store.set_cursor("BTC/USD", "FAKE", "2026-10-01T09:55:00+00:00")
    clock = Clock("2026-10-01T10:25:08")
    sent = []
    sc = make(store, [FireAt(["2026-10-01 10:00"])], clock, lambda **kw: rows_until("2026-10-01 10:25:00"), sent)
    run(scan_and_deliver(sc))
    assert sent == [] and store.recent()[0]["status"] == "STALE_SKIPPED"


def test_catch_up_after_missed_scan_within_freshness():
    store = StateStore(":memory:")
    store.set_cursor("BTC/USD", "FAKE", "2026-10-01T09:50:00+00:00")
    clock = Clock("2026-10-01T10:05:08")
    sent = []
    sc = make(store, [FireAt(["2026-10-01 09:55", "2026-10-01 10:00"])], clock,
              lambda **kw: rows_until("2026-10-01 10:05:00"), sent)
    run(scan_and_deliver(sc))
    statuses = sorted(r["status"] for r in store.recent())
    assert len(sent) == 2 and statuses == ["SENT", "SENT"]


def test_one_asset_failure_does_not_block_other():
    store = StateStore(":memory:")
    for sym in ("BTC/USD", "XAU/USD"):
        store.set_cursor(sym, "FAKE", "2026-10-01T09:55:00+00:00")
    clock = Clock("2026-10-01T10:05:08")   # Thursday → gold market open

    def fetch(symbol, **kw):
        if symbol == "XAU/USD":
            raise RuntimeError("provider down apikey=testkey123")
        return rows_until("2026-10-01 10:05:00")
    sent = []
    sc = make(store, [FireAt(["2026-10-01 10:00"])], clock, fetch, sent, assets=(ASSET, GOLD))
    run(scan_and_deliver(sc))
    assert len(sent) == 1
    assert "testkey123" not in scanner_mod.scanner_state["last_error"]


def test_two_strategies_same_candle_both_sent_with_agreement():
    store = StateStore(":memory:")
    for sid in ("A", "B"):
        store.set_cursor("BTC/USD", sid, "2026-10-01T09:55:00+00:00")
    sent = []
    sc = make(store, [FireAt(["2026-10-01 10:00"], "A"), FireAt(["2026-10-01 10:00"], "B")],
              Clock("2026-10-01T10:05:08"), lambda **kw: rows_until("2026-10-01 10:05:00"), sent)
    run(scan_and_deliver(sc))
    assert len(sent) == 2 and "Same candle: B BUY" in sent[0]


def test_provider_lag_triggers_single_refetch():
    store = StateStore(":memory:")
    calls = []

    def fetch(**kw):
        calls.append(1)
        return rows_until("2026-10-01 09:55:00" if len(calls) == 1 else "2026-10-01 10:05:00")
    sc = make(store, [FireAt([])], Clock("2026-10-01T10:05:08"), fetch, [])
    run(sc.scan_once())
    assert len(calls) == 2
    assert scanner_mod.scanner_state["assets"]["BTC/USD"]["latest_closed_candle"].startswith("2026-10-01T10:00")


def test_overlapping_scan_is_skipped():
    sc = make(StateStore(":memory:"), [FireAt([])], Clock("2026-10-01T10:05:08"),
              lambda **kw: rows_until("2026-10-01 10:05:00"), [])

    async def go():
        async with sc.lock:
            return await sc.scan_once()
    assert run(go()) == []


def test_failed_delivery_recorded():
    store = StateStore(":memory:")
    store.set_cursor("BTC/USD", "FAKE", "2026-10-01T09:55:00+00:00")
    sc = Scanner(store, [FireAt(["2026-10-01 10:00"])], [ASSET],
                 fetch=lambda **kw: rows_until("2026-10-01 10:05:00"),
                 sender=lambda t: DeliveryResult(False, "FAILED", 3, "HTTP 502"),
                 now_fn=Clock("2026-10-01T10:05:08"))
    run(scan_and_deliver(sc))
    rec = store.recent()[0]
    assert rec["status"] == "FAILED" and rec["attempts"] == 3 and rec["error"] == "HTTP 502"


def test_gold_weekend_skip_and_schedule_alignment():
    assert not market_open(GOLD, datetime(2026, 10, 3, 12, tzinfo=timezone.utc))     # Saturday
    assert market_open(GOLD, datetime(2026, 10, 4, 22, tzinfo=timezone.utc))         # Sunday reopen
    assert market_open(ASSET, datetime(2026, 10, 3, 12, tzinfo=timezone.utc))
    nxt = next_run_time(datetime(2026, 10, 1, 10, 3, 1, tzinfo=timezone.utc), 8, 5)
    assert nxt == datetime(2026, 10, 1, 10, 5, 8, tzinfo=timezone.utc)
    nxt = next_run_time(datetime(2026, 10, 1, 10, 5, 8, tzinfo=timezone.utc), 8, 5)
    assert nxt == datetime(2026, 10, 1, 10, 10, 8, tzinfo=timezone.utc)
