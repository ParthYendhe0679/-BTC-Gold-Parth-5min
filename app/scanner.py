"""
scanner.py — Autonomous multi-asset, multi-strategy signal scanner.

Runs as a background asyncio task inside the FastAPI process.
Every SCAN_INTERVAL seconds it:
  1. Loops through every configured asset
  2. Fetches latest candles ONCE per asset via Twelve Data REST API
  3. Dynamically filters out unclosed forming candles to evaluate strictly CLOSED bars
  4. Loops through every configured strategy independently
  5. Calculates Supertrend (ATR, HL2, Upper/Lower bands, direction) for each strategy
  6. Checks for signal changes against previous closed-candle state
  7. Sends Telegram alerts strictly on confirmed trend crossovers

State key format:  "<symbol>::<strategy_name>"
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from app.config import settings
from app.data_fetcher import fetch_candles
from app.models import Candle
from app.strategy import calculate_supertrend, candles_to_dataframe, get_signal
from app.telegram import send_scanner_alert

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# In-memory signal state
# ─────────────────────────────────────────────────────────────────────────────

_last_signals: Dict[str, Optional[str]] = {}
_initialised: set = set()
_sent_alerts: set = set()

# Health snapshot — exposed via GET /scanner/status
scanner_state: Dict[str, Any] = {
    "running": False,
    "scan_count": 0,
    "last_scan_ist": None,
}


def get_last_signals() -> Dict[str, Optional[str]]:
    """Return a copy of the current signal state (for the status endpoint)."""
    return dict(_last_signals)


def get_timeframe_delta(timeframe_str: str) -> pd.Timedelta:
    """Parse timeframe string into a pandas Timedelta (e.g. '5min' -> 5 mins)."""
    tf = timeframe_str.lower().strip()
    if "min" in tf:
        mins = int(tf.replace("minutes", "").replace("minute", "").replace("min", "").strip())
        return pd.Timedelta(minutes=mins)
    elif "m" in tf and not "min" in tf:
        mins = int(tf.replace("m", "").strip())
        return pd.Timedelta(minutes=mins)
    elif "h" in tf:
        hours = int(tf.replace("hours", "").replace("hour", "").replace("h", "").strip())
        return pd.Timedelta(hours=hours)
    elif "d" in tf:
        days = int(tf.replace("days", "").replace("day", "").replace("d", "").strip())
        return pd.Timedelta(days=days)
    return pd.Timedelta(minutes=5)


# ─────────────────────────────────────────────────────────────────────────────
# Per-strategy signal check & detailed audit tracing
# ─────────────────────────────────────────────────────────────────────────────

async def _check_strategy(
    asset_name: str,
    symbol: str,
    strategy: Dict[str, Any],
    df_closed: pd.DataFrame,
    latest_api_candle_str: str,
    now_ist: datetime,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """
    Run one strategy on completed closed candles for one asset.

    Calculates ATR, HL2, Upper Band, Lower Band, Supertrend, Direction & Signal
    independently for the strategy. Logs complete end-to-end audit trace.
    """
    strategy_name = strategy["name"]
    atr_period    = strategy["atr_period"]
    multiplier    = strategy["multiplier"]
    state_key     = f"{symbol}::{strategy_name}"

    min_bars = atr_period + 10
    if len(df_closed) < min_bars:
        logger.warning(
            "%s [%s] — Only %d closed bars available; need %d. Skipping.",
            asset_name, strategy_name, len(df_closed), min_bars,
        )
        return

    # Calculate Supertrend independently for this strategy
    df_strat = calculate_supertrend(
        df_closed,
        atr_period=atr_period,
        multiplier=multiplier,
    )

    info = get_signal(df_strat)
    if info is None:
        logger.warning(
            "%s [%s] — Could not extract signal; skipping.", asset_name, strategy_name
        )
        return

    current_action = info["action"]
    current_price  = info["price"]
    raw_time       = info["datetime"]
    last_action    = _last_signals.get(state_key)
    alert_key      = f"{symbol}::{strategy_name}::{raw_time}"

    # Log strategy snapshot
    logger.info(
        "%-10s [%s] | API Latest: %s | Evaluated: %s | Close: %.2f | ST: %.2f | Dir: %-7s | Signal: %s",
        symbol,
        strategy_name,
        latest_api_candle_str,
        raw_time,
        current_price,
        info["supertrend"],
        info["direction_label"],
        current_action,
    )

    # First Scan Initialization — record state baseline without sending alert
    if state_key not in _initialised:
        _last_signals[state_key] = current_action
        _sent_alerts.add(alert_key)
        _initialised.add(state_key)
        logger.info(
            "%s [%s] — Initial baseline set to %s @ %.2f (No Telegram call on first scan)",
            symbol, strategy_name, current_action, current_price,
        )
        return

    if current_action != last_action:
        # Trend Crossover Detected!
        if alert_key in _sent_alerts:
            logger.info(
                "%s [%s] — Signal %s already sent for candle %s. Skipping.",
                symbol, strategy_name, current_action, raw_time,
            )
            return

        logger.info(
            "🚨 SIGNAL CROSSOVER DETECTED — %s %s [%s] @ %.2f (was %s). Calling Telegram...",
            current_action, symbol, strategy_name, current_price, last_action,
        )

        success = await loop.run_in_executor(
            None,
            lambda: send_scanner_alert(
                asset_name=asset_name,
                symbol=symbol,
                action=current_action,
                price=current_price,
                raw_time=raw_time,
                strategy_name=strategy_name,
            ),
        )

        if success:
            _last_signals[state_key] = current_action
            _sent_alerts.add(alert_key)
            logger.info(
                "✅ Telegram API Response: SUCCESS (200 OK) — %s %s [%s]",
                current_action, symbol, strategy_name,
            )
        else:
            logger.error(
                "❌ Telegram API Response: FAILED — %s %s [%s] (will retry next scan)",
                current_action, symbol, strategy_name,
            )
    else:
        logger.debug(
            "%s [%s] — No signal change (%s == %s). Telegram not called.",
            symbol, strategy_name, current_action, last_action,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Per-asset scan (fetch once, filter closed candles, iterate strategies)
# ─────────────────────────────────────────────────────────────────────────────

async def _scan_asset(asset: Dict[str, Any], loop: asyncio.AbstractEventLoop) -> None:
    """
    Scan one asset across ALL configured strategies using ONLY CLOSED candles.
    """
    name   = asset["name"]
    symbol = asset["symbol"]

    # Fetch candles (blocking I/O → thread pool)
    candles: Optional[List[Candle]] = await loop.run_in_executor(
        None,
        lambda: fetch_candles(
            symbol=symbol,
            interval=settings.TIMEFRAME,
            outputsize=settings.OUTPUT_SIZE,
        ),
    )

    if not candles:
        logger.warning("No candles returned for %s — skipping this cycle.", name)
        return

    # Convert to DataFrame (datetimes are converted to IST timezone)
    df_base = candles_to_dataframe(candles)

    IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(timezone.utc).astimezone(IST)

    # Dynamic Candle Close Verification — filter out forming unclosed candles
    tf_delta = get_timeframe_delta(settings.TIMEFRAME)
    latest_api_candle = df_base["datetime"].iloc[-1]
    latest_api_candle_str = latest_api_candle.strftime("%Y-%m-%d %H:%M IST")

    df_closed = df_base[df_base["datetime"] + tf_delta <= now_ist].copy()
    if df_closed.empty:
        df_closed = df_base.iloc[:-1].copy() if len(df_base) > 1 else df_base.copy()

    # Run each strategy on df_closed
    for strategy in settings.STRATEGIES:
        try:
            await _check_strategy(
                asset_name=name,
                symbol=symbol,
                strategy=strategy,
                df_closed=df_closed,
                latest_api_candle_str=latest_api_candle_str,
                now_ist=now_ist,
                loop=loop,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "%s [%s] — Unexpected error: %s",
                name, strategy.get("name", "?"), exc,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Main scanner loop
# ─────────────────────────────────────────────────────────────────────────────

async def run_scanner() -> None:
    """
    Infinite scan loop. Started as an asyncio background task by main.py.
    """
    scanner_state["running"] = True

    strategy_names = ", ".join(s["name"] for s in settings.STRATEGIES)
    asset_names    = ", ".join(f"{a['name']} ({a['symbol']})" for a in settings.ASSETS)

    logger.info("=" * 60)
    logger.info("BTC/Gold-Parth 5min — Scanner Started")
    logger.info("Assets     : %s", asset_names)
    logger.info("Strategies : %s", strategy_names)
    logger.info("Timeframe  : %s", settings.TIMEFRAME_DISPLAY)
    logger.info("Interval   : %d seconds", settings.SCAN_INTERVAL)
    logger.info("=" * 60)

    loop = asyncio.get_event_loop()

    try:
        while True:
            IST = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(timezone.utc).astimezone(IST)
            scanner_state["scan_count"] += 1
            scanner_state["last_scan_ist"] = now_ist.isoformat()

            logger.info(
                "--- Scan #%d  |  %s IST ---",
                scanner_state["scan_count"],
                now_ist.strftime("%Y-%m-%d %H:%M:%S"),
            )

            # Each asset: fetch once, filter closed candles, run all strategies
            for asset in settings.ASSETS:
                try:
                    await _scan_asset(asset, loop)
                    # Brief pause between assets to respect API rate limits
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Unexpected error scanning %s: %s", asset["name"], exc
                    )

            logger.info("Waiting %d Seconds...", settings.SCAN_INTERVAL)
            await asyncio.sleep(settings.SCAN_INTERVAL)

    except asyncio.CancelledError:
        scanner_state["running"] = False
        logger.info("Scanner stopped cleanly.")
