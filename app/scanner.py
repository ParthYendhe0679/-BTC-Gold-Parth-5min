"""
scanner.py — Autonomous multi-asset, multi-strategy signal scanner.

Runs as a background asyncio task inside the FastAPI process.

Lifecycle per 5-minute bar (aligned to the candle close):
  1. Wake SCAN_DELAY_SECONDS after the bar boundary (no overlapping scans — lock).
  2. Per asset (concurrently, isolated): fetch candles ONCE (Twelve Data, UTC).
  3. Validate: only CLOSED, well-formed, de-duplicated, non-future bars survive.
     If the just-closed bar is missing (provider lag) → one bounded refetch,
     only when the daily credit budget allows it.
  4. If no new closed candle since the last evaluation → nothing to do.
  5. Run every enabled strategy on the shared data + indicator cache.
  6. Keep signals on candles newer than each strategy's cursor; claim them in
     SQLite (dedup); stale ones (> MAX_SIGNAL_AGE_SECONDS) are logged, not sent.
  7. Enqueue for the delivery worker → Telegram immediately (scan never waits
     on Telegram). Delivery status + latency are recorded.

Startup baseline: a strategy with no stored cursor records the latest closed
candle WITHOUT alerting (same as the original "first scan initialisation"),
so a restart never re-sends an old signal.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from app.candles import expected_latest_closed_open, validate_candles
from app.config import settings
from app.data_fetcher import credits, fetch_candles
from app.state import StateStore
from app.strategies import (MarketContext, Signal, Strategy, annotate_agreement,
                            build_strategies, evaluate_all)
from app.telegram import format_signal_message, send_text
from app.utils import redact, utc_now

logger = logging.getLogger(__name__)

TF = pd.Timedelta(minutes=settings.TIMEFRAME_MINUTES)

# Health snapshot — exposed via /health and /scanner/status
scanner_state: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "scan_count": 0,
    "last_loop_at": None,
    "last_scan_started_at": None,
    "last_successful_scan_at": None,
    "last_scan_ist": None,
    "last_error": None,
    "assets": {},          # symbol → {latest_closed_candle, validation, skipped_reason, ...}
    "strategies": {},      # "symbol::strategy_id" → {status, last_signal, error}
}


def market_open(asset: Dict[str, Any], now: datetime) -> bool:
    """Coarse weekend filter for FX/metals (saves API credits). Crypto is 24/7."""
    if asset.get("market") != "fx":
        return True
    wd, hr = now.weekday(), now.hour      # Mon=0 … Sat=5, Sun=6
    if wd == 5 or (wd == 6 and hr < 21):
        return False
    return True


def next_run_time(now: datetime, delay_s: int, tf_minutes: int) -> datetime:
    tf_s = tf_minutes * 60
    epoch = now.timestamp()
    boundary = (epoch // tf_s) * tf_s + delay_s
    if boundary <= epoch:
        boundary += tf_s
    return datetime.fromtimestamp(boundary, tz=timezone.utc)


class Scanner:
    def __init__(
        self,
        store: StateStore,
        strategies: List[Strategy],
        assets: Optional[List[Dict[str, Any]]] = None,
        fetch: Callable[..., Any] = fetch_candles,
        sender: Callable[[str], Any] = send_text,
        now_fn: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.strategies = strategies
        self.assets = assets if assets is not None else settings.ASSETS
        self.fetch = fetch
        self.sender = sender
        self.now = now_fn
        self.queue: "asyncio.Queue" = asyncio.Queue()
        self.lock = asyncio.Lock()
        self.last_evaluated: Dict[str, pd.Timestamp] = {}

    # ── scanning ─────────────────────────────────────────────────────────────
    async def _fetch_validated(self, symbol: str):
        candles = await asyncio.to_thread(self.fetch, symbol=symbol, interval=settings.TIMEFRAME,
                                          outputsize=settings.OUTPUT_SIZE)
        if not candles:
            return None, None
        return validate_candles(candles, TF, self.now())

    def _retry_budget_ok(self) -> bool:
        now = self.now()
        bars_left = int((24 * 60 - (now.hour * 60 + now.minute)) / settings.TIMEFRAME_MINUTES)
        planned = bars_left * len(self.assets)
        return credits.remaining(settings.DAILY_CREDIT_BUDGET) - planned > 0

    async def scan_asset(self, asset: Dict[str, Any]) -> List[Signal]:
        symbol = asset["symbol"]
        st = scanner_state["assets"].setdefault(symbol, {})
        now = self.now()
        if not market_open(asset, now):
            st["skipped_reason"] = "market closed (weekend)"
            return []
        st["skipped_reason"] = None

        df, rep = await self._fetch_validated(symbol)
        expected = expected_latest_closed_open(now, TF)
        if rep is not None and (rep.latest_closed_open is None or rep.latest_closed_open < expected) \
                and self._retry_budget_ok():
            logger.info("%s: latest closed bar %s < expected %s — refetching once in %ds",
                        symbol, rep.latest_closed_open, expected, settings.STALE_RETRY_SECONDS)
            await asyncio.sleep(settings.STALE_RETRY_SECONDS)
            df2, rep2 = await self._fetch_validated(symbol)
            if rep2 is not None:
                df, rep = df2, rep2
        if df is None or df.empty:
            st["last_error"] = "no valid candles"
            logger.warning("%s: no valid closed candles this cycle.", symbol)
            return []

        latest = rep.latest_closed_open
        st.update(latest_closed_candle=latest.isoformat(), validation=rep.as_dict(),
                  last_fetch_at=now.isoformat(), last_error=None)
        if self.last_evaluated.get(symbol) == latest:
            logger.info("%s: no new closed candle (latest %s) — strategies not re-run.", symbol, latest)
            return []
        self.last_evaluated[symbol] = latest

        ctx = MarketContext(symbol, df, settings.TIMEFRAME, TF)
        results = evaluate_all(ctx, self.strategies)
        fresh: List[Signal] = []
        for res in results:
            key = f"{symbol}::{res.strategy_id}"
            sstate = scanner_state["strategies"].setdefault(key, {})
            sstate.update(status=res.status, error=res.error, evaluated_candle=latest.isoformat())
            if res.status == "ERROR":
                continue   # cursor not advanced → retried next bar (within freshness)
            cursor = self.store.get_cursor(symbol, res.strategy_id)
            self.store.set_cursor(symbol, res.strategy_id, latest.isoformat())
            if cursor is None:
                logger.info("%s [%s]: baseline at %s (no alert on startup).", symbol, res.strategy_id, latest)
                continue
            cur_ts = pd.Timestamp(cursor)
            for sig in res.signals:
                if sig.candle_time <= cur_ts:
                    continue
                age = (now - (sig.candle_time + TF).to_pydatetime()).total_seconds()
                d = sig.to_dict()
                if age > settings.MAX_SIGNAL_AGE_SECONDS:
                    self.store.claim_signal(d, status="STALE_SKIPPED")
                    logger.warning("%s [%s] %s signal on %s is %.0fs old — logged, not sent.",
                                   symbol, sig.strategy_id, sig.direction, sig.candle_time, age)
                    continue
                if self.store.claim_signal(d):
                    fresh.append(sig)
                    sstate["last_signal"] = {"direction": sig.direction, "candle": sig.candle_time.isoformat(),
                                             "signal_id": sig.signal_id}
                else:
                    logger.info("Duplicate suppressed: %s %s %s", sig.strategy_id, symbol, sig.setup_key)
        annotate_agreement(fresh)
        for sig in fresh:
            logger.info("SIGNAL %s %s %s @ %.5f (candle %s) id=%s", sig.strategy_id, symbol, sig.direction,
                        sig.entry, sig.candle_time, sig.signal_id)
            await self.queue.put((sig, asset.get("decimals", 2), datetime.now(timezone.utc)))
        logger.info("%s: evaluated candle %s — %s", symbol, latest,
                    ", ".join(f"{r.strategy_id}={r.status}" for r in results))
        return fresh

    async def scan_once(self) -> List[Signal]:
        if self.lock.locked():
            logger.warning("Previous scan still running — skipping this cycle.")
            return []
        async with self.lock:
            now = self.now()
            scanner_state["scan_count"] += 1
            scanner_state["last_scan_started_at"] = now.isoformat()
            scanner_state["last_scan_ist"] = now.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()
            results = await asyncio.gather(*(self.scan_asset(a) for a in self.assets), return_exceptions=True)
            signals: List[Signal] = []
            ok = False
            for asset, r in zip(self.assets, results):
                if isinstance(r, BaseException):
                    if isinstance(r, asyncio.CancelledError):
                        raise r
                    msg = redact(f"{asset['symbol']}: {type(r).__name__}: {r}")
                    scanner_state["last_error"] = msg
                    logger.error("Scan error %s", msg, exc_info=r)
                else:
                    signals += r
                    ok = True
            if ok:
                scanner_state["last_successful_scan_at"] = self.now().isoformat()
            return signals

    # ── delivery ─────────────────────────────────────────────────────────────
    async def delivery_worker(self) -> None:
        while True:
            sig, decimals, generated_at = await self.queue.get()
            try:
                text = format_signal_message(sig, decimals, settings.TIMEFRAME_MINUTES)
                res = await asyncio.to_thread(self.sender, text)
                done = datetime.now(timezone.utc)
                close = (sig.candle_time + TF).to_pydatetime()
                latency_ms = int((done - close).total_seconds() * 1000)
                gen_ms = int((done - generated_at).total_seconds() * 1000)
                self.store.update_delivery(sig.signal_id, res.status, res.attempts, res.error,
                                           res.message_id, latency_ms)
                logger.info("Telegram %s for %s (attempts=%d, candle-close→sent=%dms, generated→sent=%dms)",
                            res.status, sig.signal_id, res.attempts, latency_ms, gen_ms)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Delivery failed for %s", sig.signal_id)
                self.store.update_delivery(sig.signal_id, "FAILED", 0, redact(exc), None, None)
            finally:
                self.queue.task_done()
            await asyncio.sleep(settings.TELEGRAM_MIN_SEND_INTERVAL)

    # ── main loop ────────────────────────────────────────────────────────────
    async def run_forever(self) -> None:
        worker = asyncio.create_task(self.delivery_worker(), name="telegram_delivery")
        try:
            while True:
                scanner_state["last_loop_at"] = self.now().isoformat()
                try:
                    await asyncio.wait_for(self.scan_once(), timeout=240)
                except asyncio.TimeoutError:
                    scanner_state["last_error"] = "scan timed out after 240s"
                    logger.error("Scan timed out.")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    scanner_state["last_error"] = redact(exc)
                    logger.exception("Scan cycle failed")
                nxt = next_run_time(self.now(), settings.SCAN_DELAY_SECONDS, settings.TIMEFRAME_MINUTES)
                wait = max(1.0, (nxt - self.now()).total_seconds())
                scanner_state["next_scan_at"] = nxt.isoformat()
                logger.info("Next scan at %s UTC (in %.0fs).", nxt.strftime("%H:%M:%S"), wait)
                await asyncio.sleep(wait)
        finally:
            worker.cancel()


_scanner: Optional[Scanner] = None


def get_scanner() -> Optional[Scanner]:
    return _scanner


def get_last_signals() -> Dict[str, Any]:
    """Return last signal per symbol::strategy (for the status endpoint)."""
    return {k: v.get("last_signal") for k, v in scanner_state["strategies"].items()}


async def run_scanner() -> None:
    """Background entry point started by main.py's lifespan. One instance per process."""
    global _scanner
    if scanner_state["running"]:
        logger.warning("Scanner already running in this process — not starting a second one.")
        return
    store = StateStore(settings.STATE_DB_PATH)
    strategies = build_strategies(settings.enabled_strategies)
    _scanner = Scanner(store, strategies)
    scanner_state["running"] = True
    scanner_state["started_at"] = utc_now().isoformat()
    logger.info("=" * 60)
    logger.info("BTC/Gold-Parth 5min v%s — Scanner Started", settings.APP_VERSION)
    logger.info("Assets     : %s", ", ".join(a["symbol"] for a in settings.ASSETS))
    logger.info("Strategies : %s", ", ".join(s.id for s in strategies))
    logger.info("Schedule   : every %d min, %ds after candle close", settings.TIMEFRAME_MINUTES,
                settings.SCAN_DELAY_SECONDS)
    logger.info("=" * 60)
    try:
        await _scanner.run_forever()
    finally:
        scanner_state["running"] = False
        logger.info("Scanner stopped.")
