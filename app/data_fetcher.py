"""
data_fetcher.py — Twelve Data REST API client.

Fetches OHLCV candlestick data for any symbol supported by Twelve Data.
Includes automatic retry with exponential back-off for transient failures.

Twelve Data free-plan limits (as of 2024):
  • 8 API requests / minute
  • 800 API requests / day

With 2 assets scanned every 60 s → 2 req/min, 2,880 req/day.
If you exceed the daily limit, consider reducing OUTPUT_SIZE or ASSETS count,
or upgrading to a paid Twelve Data plan.
"""

import logging
import time
from typing import List, Optional

import requests

from app.config import settings
from app.models import Candle

logger = logging.getLogger(__name__)

_TIME_SERIES_URL = f"{settings.TWELVE_DATA_BASE_URL}/time_series"


def fetch_candles(
    symbol: str,
    interval: str = "5min",
    outputsize: int = 150,
    max_retries: int = 3,
    retry_delay: float = 5.0,
) -> Optional[List[Candle]]:
    """
    Fetch historical OHLCV candles from Twelve Data.

    Parameters
    ----------
    symbol      : Twelve Data symbol, e.g. "BTC/USD", "XAU/USD", "EUR/USD"
    interval    : Bar interval — "1min", "5min", "15min", "1h", "1day", etc.
    outputsize  : Number of bars to fetch (max 5000 on paid plans, 500 on free)
    max_retries : Maximum number of retry attempts on failure
    retry_delay : Base delay in seconds between retries (multiplied by attempt#)

    Returns
    -------
    List[Candle] sorted oldest → newest, or None if all retries fail.
    """
    params = {
        "symbol":     symbol,
        "interval":   interval,
        "outputsize": outputsize,
        "apikey":     settings.TWELVE_DATA_API_KEY,
    }

    for attempt in range(1, max_retries + 1):
        try:
            headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
            resp = requests.get(_TIME_SERIES_URL, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            # ── API-level error (HTTP 200 but status == "error") ─────────────
            if data.get("status") == "error":
                code = data.get("code", "?")
                msg  = data.get("message", "Unknown error")
                logger.error(
                    "Twelve Data API error for %s (code %s): %s", symbol, code, msg
                )
                # 429 = rate limit — wait longer before retrying
                if str(code) == "429":
                    time.sleep(retry_delay * attempt * 3)
                elif attempt < max_retries:
                    time.sleep(retry_delay * attempt)
                continue

            values = data.get("values")
            if not values:
                logger.warning("Empty response for %s — no 'values' key.", symbol)
                return None

            # ── Parse — API returns newest-first; we reverse to oldest-first ──
            candles: List[Candle] = []
            for row in reversed(values):
                try:
                    candles.append(
                        Candle(
                            datetime=row["datetime"],
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=float(row["volume"]) if row.get("volume") else None,
                        )
                    )
                except (KeyError, ValueError, TypeError) as exc:
                    logger.warning("Skipping malformed candle row: %s — %s", row, exc)

            if not candles:
                logger.warning("All rows were malformed for %s.", symbol)
                return None

            logger.debug("Fetched %d candles for %s", len(candles), symbol)
            return candles

        except requests.exceptions.Timeout:
            logger.warning(
                "Timeout fetching %s (attempt %d/%d)", symbol, attempt, max_retries
            )
        except requests.exceptions.ConnectionError as exc:
            logger.warning(
                "Connection error for %s (attempt %d/%d): %s",
                symbol, attempt, max_retries, exc,
            )
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response else "?"
            logger.warning(
                "HTTP %s for %s (attempt %d/%d)", status, symbol, attempt, max_retries
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error fetching %s: %s", symbol, exc)

        if attempt < max_retries:
            sleep = retry_delay * attempt
            logger.info("Retrying %s in %.0fs...", symbol, sleep)
            time.sleep(sleep)

    logger.error("All %d fetch attempts failed for %s.", max_retries, symbol)
    return None
