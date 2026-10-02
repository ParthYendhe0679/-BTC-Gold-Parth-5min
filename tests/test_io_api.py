"""Telegram delivery/formatting, market-data client, HTTP API, config, backtester."""

import logging

import pandas as pd
import pytest
import requests
from fastapi.testclient import TestClient

import app.data_fetcher as fetcher
import app.telegram as tg
from app.backtest import metrics, simulate
from app.config import Settings, settings
from app.strategies import MarketContext
from app.strategies.base import Strategy
from tests.conftest import make_df


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: None)


# ── Telegram ────────────────────────────────────────────────────────────────
def seq(monkeypatch, module_attr, items):
    calls = []

    def fake(*a, **k):
        calls.append((a, k))
        item = items[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(*module_attr, fake)
    return calls


def test_telegram_429_respects_retry_after_then_succeeds(monkeypatch):
    slept = []
    monkeypatch.setattr(tg.time, "sleep", lambda s: slept.append(s))
    calls = seq(monkeypatch, (tg, "api_call"), [
        Resp(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}}),
        Resp(200, {"ok": True, "result": {"message_id": 42}})])
    r = tg.send_text("hi")
    assert r.ok and r.message_id == 42 and len(calls) == 2 and slept == [7.0]


def test_telegram_permanent_error_not_retried(monkeypatch):
    calls = seq(monkeypatch, (tg, "api_call"), [Resp(400, {"ok": False, "description": "can't parse entities"})])
    r = tg.send_text("hi")
    assert not r.ok and r.status == "FAILED" and len(calls) == 1


def test_telegram_read_timeout_is_uncertain_and_not_retried(monkeypatch):
    calls = seq(monkeypatch, (tg, "api_call"), [requests.exceptions.ReadTimeout()])
    r = tg.send_text("hi")
    assert r.status == "UNCERTAIN" and len(calls) == 1


def test_telegram_connect_failures_retried_then_fail_and_token_redacted(monkeypatch, caplog):
    err = requests.exceptions.ConnectionError(
        f"Max retries exceeded with url: /bot{settings.BOT_TOKEN}/sendMessage")
    calls = seq(monkeypatch, (tg, "api_call"), [requests.exceptions.ConnectTimeout(), err, err])
    with caplog.at_level(logging.WARNING):
        r = tg.send_text("hi")
    assert r.status == "FAILED" and len(calls) == 3
    assert settings.BOT_TOKEN not in caplog.text and settings.BOT_TOKEN not in (r.error or "")


def test_signal_message_escaping_and_missing_values(rw_df):
    from app.strategies.legacy import EmaCrossStrategy
    s = EmaCrossStrategy().generate(MarketContext("XAU/USD", rw_df))[0]
    s.reason = "close <b>& stuff</b>"
    s.details.append(("weird_<key>", "a&b"))
    text = tg.format_signal_message(s, decimals=2)
    assert "&lt;b&gt;&amp; stuff&lt;/b&gt;" in text and "weird_&lt;key&gt;" in text
    assert "Stop-loss" not in text and "Target 1" not in text and "Risk/reward" not in text  # not calculated
    assert "UTC" in text and "IST" in text and "XAU/USD | 5 MIN" in text and "Educational signal" in text


def test_test_message_is_not_a_signal():
    t = tg.build_test_alert_message()
    assert "NOT A TRADING SIGNAL" in t and "Entry" not in t and "BUY" not in t


# ── Market data client ──────────────────────────────────────────────────────
def test_fetch_requests_utc_and_parses(monkeypatch):
    calls = seq(monkeypatch, (fetcher._session, "get"), [Resp(200, {"status": "ok", "values": [
        {"datetime": "2026-10-02 18:55:00", "open": "1", "high": "2", "low": "0.5", "close": "1.5"},
        {"datetime": "2026-10-02 18:50:00", "open": "1", "high": "2", "low": "0.5", "close": "1.2"}]})])
    out = fetcher.fetch_candles("XAU/USD")
    assert calls[0][1]["params"]["timezone"] == "UTC"
    assert [c.datetime for c in out] == ["2026-10-02 18:50:00", "2026-10-02 18:55:00"]


def test_fetch_permanent_api_error_not_retried(monkeypatch):
    calls = seq(monkeypatch, (fetcher._session, "get"),
                [Resp(200, {"status": "error", "code": 401, "message": "invalid apikey testkey123"})])
    assert fetcher.fetch_candles("BTC/USD") is None and len(calls) == 1
    assert "testkey123" not in fetcher.fetch_status["BTC/USD"]["last_error"]


def test_fetch_retries_5xx_and_redacts_key(monkeypatch, caplog):
    err = requests.exceptions.ConnectionError("HTTPSConnectionPool url: /time_series?apikey=testkey123&x=1")
    calls = seq(monkeypatch, (fetcher._session, "get"), [Resp(503, {}), err,
                                                         Resp(200, {"status": "ok", "values": []})])
    with caplog.at_level(logging.WARNING):
        assert fetcher.fetch_candles("BTC/USD") is None
    assert len(calls) == 3 and "testkey123" not in caplog.text


# ── HTTP API (lifespan NOT started → no background network) ─────────────────
@pytest.fixture
def client():
    import app.main as m
    m._tasks.clear()
    return TestClient(m.app), m


def test_health_reports_down_when_scanner_not_running(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "down"
    assert "testkey123" not in r.text and settings.BOT_TOKEN not in r.text
    assert c.get("/").status_code == 200


def test_health_ok_with_live_scanner_task(client):
    import asyncio
    from datetime import datetime, timezone
    c, m = client

    class Alive:
        def done(self):
            return False
    m._tasks["scanner"] = Alive()
    m.scanner_state["last_loop_at"] = datetime.now(timezone.utc).isoformat()
    fetcher.fetch_status.clear()
    tg.delivery_status["last_status"] = "SENT"
    r = c.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    m.scanner_state["last_loop_at"] = "2020-01-01T00:00:00+00:00"   # stalled loop
    assert c.get("/health").status_code == 503
    m._tasks.clear()


def test_webhook_secret_enforced(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(settings, "WEBHOOK_SECRET", "s3cret-value")
    monkeypatch.setattr("app.webhook.send_telegram_message", lambda p: True)
    body = {"action": "buy", "symbol": "BTCUSDT", "timeframe": "5m", "indicator": "x", "entry": "1", "time": "1"}
    assert c.post("/webhook", json=body).status_code == 401
    assert c.post("/webhook", json={**body, "secret": "wrong"}).status_code == 401
    assert c.post("/webhook", json={**body, "secret": "s3cret-value"}).status_code == 200


def test_test_alert_requires_admin_token(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "adm1n-token")
    monkeypatch.setattr("app.main.send_test_alert", lambda: True)
    assert c.get("/scanner/test-alert").status_code == 401
    assert c.get("/scanner/test-alert?token=adm1n-token").status_code == 200


# ── Config ──────────────────────────────────────────────────────────────────
def test_config_validation_missing_vars():
    s = Settings()
    s.BOT_TOKEN = ""
    s.TWELVE_DATA_API_KEY = ""
    with pytest.raises(RuntimeError, match="BOT_TOKEN.*TWELVE_DATA_API_KEY"):
        s.validate()


def test_strategy_toggles_and_param_overrides(monkeypatch):
    monkeypatch.setenv("DISABLED_STRATEGIES", "ema5_cross,POS_5EMA")
    monkeypatch.setenv("STRATEGY_PARAMS", '{"RSI2_MEAN_REVERSION": {"oversold": 5}}')
    s = Settings()
    ids = [x["id"] for x in s.enabled_strategies]
    assert "EMA5_CROSS" not in ids and "POS_5EMA" not in ids and "SUPERTREND_10_3" in ids
    assert next(x for x in s.STRATEGIES if x["id"] == "RSI2_MEAN_REVERSION")["params"]["oversold"] == 5
    monkeypatch.setenv("ENABLED_STRATEGIES", "LIQUIDITY_SWEEP")
    monkeypatch.delenv("DISABLED_STRATEGIES")
    assert [x["id"] for x in Settings().enabled_strategies] == ["LIQUIDITY_SWEEP"]


# ── Backtester ──────────────────────────────────────────────────────────────
class OneShot(Strategy):
    id = "ONE"

    def __init__(self, direction, stop, target):
        super().__init__()
        self.d, self.s, self.tg = direction, stop, target

    def generate(self, ctx):
        return [self._signal(ctx, 0, self.d, ctx.c[0], self.s, [self.tg], "t", "k")]


def test_backtest_stop_and_target_same_bar_counts_as_stop():
    ctx = MarketContext("X", make_df([(100, 100, 100, 100), (100, 103, 97, 100)]))
    t = simulate(ctx, OneShot("BUY", 98, 102), cost_pct=0.0)[0]
    assert t.exit == 98 and t.exit_reason.startswith("stop") and t.r_multiple == pytest.approx(-1)


def test_backtest_gap_through_stop_fills_at_open_and_costs_applied():
    ctx = MarketContext("X", make_df([(100, 100, 100, 100), (95, 96, 94, 95)]))
    t = simulate(ctx, OneShot("BUY", 98, 102), cost_pct=0.1)[0]
    assert t.exit == 95 and t.gross_pct == pytest.approx(-5) and t.net_pct == pytest.approx(-5.1)


def test_backtest_short_target_and_metrics():
    ctx = MarketContext("X", make_df([(100, 100, 100, 100), (100, 100.5, 97.5, 98)]))
    t = simulate(ctx, OneShot("SELL", 101, 98), cost_pct=0.0)
    m = metrics(t)
    assert t[0].exit == 98 and m["trades"] == 1 and m["win_rate_pct"] == 100.0 and m["sell_trades"] == 1


def test_backtest_flip_strategy_alternates_long_and_short(rw_df):
    from app.strategies.legacy import EmaCrossStrategy
    ctx = MarketContext("X", rw_df)
    trades = simulate(ctx, EmaCrossStrategy(), cost_pct=0.0)
    sides = [t.direction for t in trades]
    assert "BUY" in sides and "SELL" in sides
    assert all(a != b for a, b in zip(sides, sides[1:]))      # stop-and-reverse on every flip
