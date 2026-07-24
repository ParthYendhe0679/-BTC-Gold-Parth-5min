"""
config.py — Centralised settings loader.

All configuration is read from environment variables (via .env).
Never hardcode secrets in this file.
"""

import os
from typing import Any, Dict, List

from dotenv import load_dotenv

load_dotenv()


class Settings:
    """
    Application-wide configuration.

    Extend this class to add new data sources, timeframes, or strategy
    parameters without touching any other module.
    """

    # ── Telegram ─────────────────────────────────────────────────────────────
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    CHAT_ID: str = os.getenv("CHAT_ID", "")

    # ── Twelve Data ──────────────────────────────────────────────────────────
    TWELVE_DATA_API_KEY: str = os.getenv("TWELVE_DATA_API_KEY", "")
    TWELVE_DATA_BASE_URL: str = "https://api.twelvedata.com"

    # ── Application metadata ─────────────────────────────────────────────────
    APP_TITLE: str = "BTC/Gold-Parth 5min"
    APP_VERSION: str = "2.0.0"

    # ── Scanner behaviour ────────────────────────────────────────────────────
    SCAN_INTERVAL: int = 60            # seconds between each full scan cycle
    TIMEFRAME: str = "5min"           # Twelve Data interval code
    TIMEFRAME_DISPLAY: str = "5 Minutes"
    OUTPUT_SIZE: int = 200            # candles fetched per request (>= max ATR_PERIOD + 50)

    # ── Supertrend strategy parameters ──────────────────────────────────────
    ATR_PERIOD: int = 10
    ATR_MULTIPLIER: float = 3.0

    # ── Assets to monitor ────────────────────────────────────────────────────
    # To add more assets, simply append a dict here — no code changes needed.
    # Supported by Twelve Data free plan: BTC/USD, XAU/USD, ETH/USD, EUR/USD
    # Equities / indices may require a paid plan.
    ASSETS: List[Dict[str, Any]] = [
        {"name": "Bitcoin", "symbol": "BTC/USD"},
        {"name": "Gold",    "symbol": "XAU/USD"},
        # {"name": "Ethereum",   "symbol": "ETH/USD"},
        # {"name": "Solana",     "symbol": "SOL/USD"},
        # {"name": "EUR/USD",    "symbol": "EUR/USD"},
        # {"name": "NASDAQ",     "symbol": "QQQ"},
        # {"name": "US30",       "symbol": "DIA"},
    ]

    # ── Strategies to run on every asset ────────────────────────────────────
    # Add a new dict here to run an additional Supertrend variant.
    # The scanner loops over this list automatically — no code changes needed.
    STRATEGIES: List[Dict[str, Any]] = [
        {"name": "Supertrend (10,3)", "atr_period": 10, "multiplier": 3.0},
        {"name": "Supertrend (20,3)", "atr_period": 20, "multiplier": 3.0},
        # {"name": "Supertrend (7,3)",  "atr_period":  7, "multiplier": 3.0},
    ]

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
                "Please set them in your .env file."
            )


settings = Settings()
