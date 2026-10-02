"""
config.py — Centralised settings loader.

All configuration is read from environment variables (via .env).
Never hardcode secrets in this file.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        logger.warning("Invalid integer for %s=%r — using default %d", name, raw, default)
        return default


def _env_list(name: str) -> Optional[List[str]]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return [x.strip().upper() for x in raw.split(",") if x.strip()]


class Settings:
    """
    Application-wide configuration.

    Extend this class to add new data sources, timeframes, or strategy
    parameters without touching any other module.
    """

    # ── Telegram ─────────────────────────────────────────────────────────────
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    CHAT_ID: str = os.getenv("CHAT_ID", "")
    TELEGRAM_COMMANDS_ENABLED: bool = _env_bool("TELEGRAM_COMMANDS_ENABLED", True)
    TELEGRAM_MIN_SEND_INTERVAL: float = 1.0   # seconds between messages to one chat

    # ── Optional endpoint secrets (recommended) ──────────────────────────────
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")
    ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "")

    # ── Twelve Data ──────────────────────────────────────────────────────────
    TWELVE_DATA_API_KEY: str = os.getenv("TWELVE_DATA_API_KEY", "")
    TWELVE_DATA_BASE_URL: str = "https://api.twelvedata.com"
    # Free Basic plan = 800 credits/day, 8/min. Leave a small margin.
    DAILY_CREDIT_BUDGET: int = _env_int("DAILY_CREDIT_BUDGET", 780)

    # ── Application metadata ─────────────────────────────────────────────────
    APP_TITLE: str = "BTC/Gold-Parth 5min"
    APP_VERSION: str = "3.0.0"

    # ── Scanner behaviour ────────────────────────────────────────────────────
    TIMEFRAME: str = "5min"           # Twelve Data interval code
    TIMEFRAME_DISPLAY: str = "5 MIN"
    TIMEFRAME_MINUTES: int = 5
    # Scans are aligned to candle closes: run SCAN_DELAY_SECONDS after each
    # 5-minute boundary (gives the provider time to finalise the bar).
    SCAN_DELAY_SECONDS: int = _env_int("SCAN_DELAY_SECONDS", 8)
    # If the just-closed bar is not yet in the response, refetch once after this delay.
    STALE_RETRY_SECONDS: int = _env_int("STALE_RETRY_SECONDS", 20)
    SCAN_INTERVAL: int = TIMEFRAME_MINUTES * 60   # kept for /scanner/status compat
    OUTPUT_SIZE: int = _env_int("OUTPUT_SIZE", 700)   # ~58h — enough for previous-day levels
    # Signals on candles that closed longer ago than this are logged, never sent.
    MAX_SIGNAL_AGE_SECONDS: int = _env_int("MAX_SIGNAL_AGE_SECONDS", 600)
    STATE_DB_PATH: str = os.getenv("STATE_DB_PATH", "data/state.db")

    # ── Legacy Supertrend defaults (used by test message) ───────────────────
    ATR_PERIOD: int = 10
    ATR_MULTIPLIER: float = 3.0

    # ── Assets to monitor ────────────────────────────────────────────────────
    # market: "crypto" = 24/7, "fx" = skip Saturday and Sunday before 21:00 UTC.
    ASSETS: List[Dict[str, Any]] = [
        {"name": "Bitcoin", "symbol": "BTC/USD", "market": "crypto", "decimals": 2},
        {"name": "Gold",    "symbol": "XAU/USD", "market": "fx",     "decimals": 2},
    ]

    # ── Strategies to run on every asset ────────────────────────────────────
    # "id" is stable (used for dedup + config). "enabled" is the default; override
    # with ENABLED_STRATEGIES / DISABLED_STRATEGIES (comma-separated ids) and tune
    # parameters with STRATEGY_PARAMS='{"RSI2_MEAN_REVERSION": {"oversold": 5}}'.
    STRATEGIES: List[Dict[str, Any]] = [
        # Retained original strategies (math unchanged)
        {"id": "SUPERTREND_10_3", "type": "supertrend", "name": "Supertrend (10,3)",
         "enabled": True, "params": {"atr_period": 10, "multiplier": 3.0}},
        {"id": "SUPERTREND_20_3", "type": "supertrend", "name": "Supertrend (20,3)",
         "enabled": True, "params": {"atr_period": 20, "multiplier": 3.0}},
        {"id": "EMA5_CROSS", "type": "ema", "name": "EMA 5",
         "enabled": True, "params": {"period": 5}},
        # New strategies
        {"id": "FIB_GOLDEN_ZONE", "type": "fib_golden_zone", "name": "Fibonacci Golden Zone",
         "enabled": True, "params": {}},
        {"id": "POS_5EMA", "type": "pos_5ema", "name": "5 EMA Alert-Candle",
         "enabled": True, "params": {}},
        {"id": "RSI2_MEAN_REVERSION", "type": "rsi2", "name": "RSI(2) Mean Reversion",
         "enabled": True, "params": {}},
        {"id": "LIQUIDITY_SWEEP", "type": "liquidity_sweep", "name": "Liquidity Sweep",
         "enabled": True, "params": {}},
    ]

    def __init__(self) -> None:
        self._apply_strategy_overrides()

    def _apply_strategy_overrides(self) -> None:
        enabled = _env_list("ENABLED_STRATEGIES")
        disabled = _env_list("DISABLED_STRATEGIES") or []
        overrides: Dict[str, Dict[str, Any]] = {}
        raw = os.getenv("STRATEGY_PARAMS", "").strip()
        if raw:
            try:
                overrides = {k.upper(): v for k, v in json.loads(raw).items()}
            except (ValueError, AttributeError) as exc:
                logger.error("STRATEGY_PARAMS is not valid JSON (%s) — ignored.", exc)
        strategies = []
        for s in self.STRATEGIES:
            s = {**s, "params": dict(s.get("params", {}))}
            if enabled is not None:
                s["enabled"] = s["id"] in enabled
            if s["id"] in disabled:
                s["enabled"] = False
            s["params"].update(overrides.get(s["id"], {}))
            strategies.append(s)
        self.STRATEGIES = strategies

    @property
    def enabled_strategies(self) -> List[Dict[str, Any]]:
        return [s for s in self.STRATEGIES if s.get("enabled", True)]

    def validate(self) -> None:
        """Raise RuntimeError for any missing required secret."""
        missing = []
        if not self.BOT_TOKEN:
            missing.append("BOT_TOKEN")
        if not self.CHAT_ID:
            missing.append("CHAT_ID")
        if not self.TWELVE_DATA_API_KEY:
            missing.append("TWELVE_DATA_API_KEY")
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}. "
                "Please set them in your .env file or the Render dashboard."
            )
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", self.BOT_TOKEN):
            logger.warning("BOT_TOKEN does not look like a Telegram bot token (digits:secret).")
        if not re.fullmatch(r"-?\d+|@\w+", self.CHAT_ID):
            logger.warning("CHAT_ID should be a numeric chat id or @channelname.")
        if not self.WEBHOOK_SECRET:
            logger.warning("WEBHOOK_SECRET not set — POST /webhook accepts unauthenticated requests.")
        if not self.ADMIN_TOKEN:
            logger.warning("ADMIN_TOKEN not set — GET /scanner/test-alert is unauthenticated.")


settings = Settings()
