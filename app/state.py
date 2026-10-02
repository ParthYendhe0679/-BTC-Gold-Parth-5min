"""
state.py — Durable signal dedup, per-strategy candle cursors and delivery log (SQLite).

stdlib sqlite3 — no new dependency. Thread-safe via one lock + one connection.

Durability caveat: on Render's free plan the filesystem is wiped on every
deploy/restart/spin-down. The scanner therefore also applies a startup baseline
(signals on candles that closed before the process started its first scan are
never sent) — so a restart can MISS a signal from the bar during the restart,
but cannot RE-SEND one. Attach a persistent disk and point STATE_DB_PATH at it
for full durability.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id      TEXT PRIMARY KEY,
    strategy_id    TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    candle_time    TEXT NOT NULL,
    direction      TEXT NOT NULL,
    setup_key      TEXT NOT NULL,
    payload        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    status         TEXT NOT NULL,          -- PENDING | SENT | FAILED | UNCERTAIN | STALE_SKIPPED
    attempts       INTEGER DEFAULT 0,
    error          TEXT,
    message_id     INTEGER,
    sent_at        TEXT,
    latency_ms     INTEGER                 -- candle close → Telegram accepted
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_setup ON signals(strategy_id, symbol, direction, setup_key);
CREATE TABLE IF NOT EXISTS cursors (
    symbol       TEXT NOT NULL,
    strategy_id  TEXT NOT NULL,
    last_candle  TEXT NOT NULL,
    PRIMARY KEY (symbol, strategy_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # ── cursors ──────────────────────────────────────────────────────────────
    def get_cursor(self, symbol: str, strategy_id: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT last_candle FROM cursors WHERE symbol=? AND strategy_id=?",
                                   (symbol, strategy_id)).fetchone()
        return row[0] if row else None

    def set_cursor(self, symbol: str, strategy_id: str, candle_iso: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO cursors VALUES (?,?,?) ON CONFLICT(symbol, strategy_id) "
                             "DO UPDATE SET last_candle=excluded.last_candle", (symbol, strategy_id, candle_iso))
            self._db.commit()

    # ── signals ──────────────────────────────────────────────────────────────
    def claim_signal(self, sig_dict: Dict[str, Any], status: str = "PENDING") -> bool:
        """Insert a signal; False if this signal (or the same setup) was already recorded."""
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO signals (signal_id, strategy_id, symbol, candle_time, direction, "
                "setup_key, payload, created_at, status) VALUES (?,?,?,?,?,?,?,?,?)",
                (sig_dict["signal_id"], sig_dict["strategy_id"], sig_dict["symbol"],
                 sig_dict["candle_timestamp"], sig_dict["signal_direction"], sig_dict["setup_key"],
                 json.dumps(sig_dict, default=str), _now(), status))
            self._db.commit()
            return cur.rowcount == 1

    def update_delivery(self, signal_id: str, status: str, attempts: int, error: Optional[str],
                        message_id: Optional[int], latency_ms: Optional[int]) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE signals SET status=?, attempts=?, error=?, message_id=?, sent_at=?, latency_ms=? "
                "WHERE signal_id=?",
                (status, attempts, error, message_id, _now() if status == "SENT" else None, latency_ms, signal_id))
            self._db.commit()

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT signal_id, strategy_id, symbol, candle_time, direction, status, attempts, error, "
                "latency_ms, created_at FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        keys = ["signal_id", "strategy_id", "symbol", "candle_time", "direction", "status", "attempts",
                "error", "latency_ms", "created_at"]
        return [dict(zip(keys, r)) for r in rows]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*), AVG(latency_ms) FROM signals GROUP BY status").fetchall()
        return {s: {"count": c, "avg_latency_ms": round(a) if a is not None else None} for s, c, a in rows}
