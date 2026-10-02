"""
data_fetcher.py — Twelve Data REST API client.

Fetches OHLCV candlestick data for any symbol supported by Twelve Data.

Twelve Data Basic (free) plan: 8 credits / minute, 800 credits / day,
1 credit per time_series request. The scanner calls this once per asset per
5-minute bar (2 × 288 = 576/day), with a daily budget guard for retries.

Reliability rules:
  • explicit ``timezone=UTC`` (the provider default is the exchange timezone)
  • bounded timeouts; retries only for transient errors (timeouts, 5xx, 429)
  • permanent errors (bad key/symbol, 4xx) are not retried
  • secrets are redacted from every log line
"""

import logging
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from app.config import settings
from app.models import Candle
from app.utils import redact

logger = logging.getLogger(__name__)

_TIME_SERIES_URL = f"{settings.TWELVE_DATA_BASE_URL}/time_series"
_session = requests.Session()


class CreditTracker:
    """Counts API credits used per UTC day (process-local)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.day: date = datetime.now(timezone.utc).date()
        self.used = 0
        self.provider_left: Optional[int] = None

    def _roll(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day, self.used = today, 0

    def add(self, n: int = 1) -> None:
        with self._lock:
            self._roll()
            self.used += n

    def remaining(self, budget: int) -> int:
        with self._lock:
            self._roll()
            return budget - self.used


credits = CreditTracker()

# Last fetch outcome per symbol (for /health)
fetch_status: Dict[str, Dict[str, Any]] = {}


def _record(symbol: str, ok: bool, error: Optional[str] = None) -> None:
    st = fetch_status.setdefault(symbol, {})
    now = datetime.now(timezone.utc).isoformat()
    if ok:
        st["last_ok_at"] = now
        st["last_error"] = None
    else:
        st["last_error_at"] = now
        st["last_error"] = error


def fetch_candles(
    symbol: str,
    interval: str = "5min",
    outputsize: int = 150,
    max_retries: int = 3,
    retry_delay: float = 2.0,
) -> Optional[List[Candle]]:
    """
    Fetch historical OHLCV candles from Twelve Data.

    Returns List[Candle] sorted oldest → newest (UTC open times), or None.
    """
    params = {
        "symbol":     symbol,
        "interval":   interval,
        "outputsize": outputsize,
        "timezone":   "UTC",
        "order":      "ASC",
        "apikey":     settings.TWELVE_DATA_API_KEY,
    }

    for attempt in range(1, max_retries + 1):
        retryable = True
        error = ""
        try:
            credits.add(1)
            resp = _session.get(_TIME_SERIES_URL, params=params, timeout=(5, 15))
            left = resp.headers.get("api-credits-left")
            if left is not None and str(left).isdigit():
                credits.provider_left = int(left)

            if resp.status_code == 429:
                error = "HTTP 429 rate limited"
            elif resp.status_code >= 500:
                error = f"HTTP {resp.status_code}"
            elif resp.status_code >= 400:
                error, retryable = f"HTTP {resp.status_code}", False
            else:
                data = resp.json()
                if data.get("status") == "error":
                    code = str(data.get("code", "?"))
                    error = f"API error {code}: {redact(data.get('message', ''))[:160]}"
                    retryable = code in ("429",) or code.startswith("5")
                else:
                    values = data.get("values") or []
                    if not values:
                        _record(symbol, False, "empty response")
                        logger.warning("Empty response for %s — no 'values'.", symbol)
                        return None
                    candles: List[Candle] = []
                    for row in values:
                        try:
                            candles.append(Candle(
                                datetime=row["datetime"],
                                open=float(row["open"]),
                                high=float(row["high"]),
                                low=float(row["low"]),
                                close=float(row["close"]),
                                volume=float(row["volume"]) if row.get("volume") else None,
                            ))
                        except (KeyError, ValueError, TypeError) as exc:
                            logger.warning("Skipping malformed candle row for %s: %s", symbol, exc)
                    candles.sort(key=lambda c: c.datetime)
                    _record(symbol, bool(candles), None if candles else "all rows malformed")
                    return candles or None

        except requests.exceptions.Timeout:
            error = "timeout"
        except requests.exceptions.ConnectionError as exc:
            error = f"connection error: {redact(exc)[:160]}"
        except ValueError:
            error = "invalid JSON"
        except Exception as exc:  # noqa: BLE001
            error = f"unexpected: {redact(exc)[:160]}"

        logger.warning("Twelve Data %s (attempt %d/%d): %s", symbol, attempt, max_retries, error)
        _record(symbol, False, error)
        if not retryable or attempt == max_retries:
            break
        sleep = min(retry_delay * (2 ** (attempt - 1)), 10.0)
        if "429" in error:
            sleep = 15.0   # per-minute limit window
        time.sleep(sleep)

    logger.error("Fetch failed for %s: %s", symbol, fetch_status.get(symbol, {}).get("last_error"))
    return None
