"""
models.py — Pydantic models for all request/response and internal data shapes.
"""

from typing import Literal, Optional

from pydantic import BaseModel, field_validator


# ── Webhook (TradingView → FastAPI) ──────────────────────────────────────────

class WebhookPayload(BaseModel):
    """
    JSON body sent by a TradingView alert webhook.

    Example:
        {
            "action":    "BUY",
            "symbol":    "BTCUSDT",
            "timeframe": "5 Minutes",
            "indicator": "Supertrend (10,3)",
            "entry":     "118250",
            "time":      "2024-01-15T10:35:00Z"
        }
    """

    action: Literal["BUY", "SELL"]
    symbol: str
    timeframe: str
    indicator: str
    entry: str
    time: str

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("symbol", "timeframe", "indicator", "entry", "time", mode="before")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


class WebhookSuccessResponse(BaseModel):
    status: str = "success"
    message: str = "Telegram notification sent"
    action: str
    symbol: str


class WebhookErrorResponse(BaseModel):
    status: str = "error"
    message: str = "Telegram notification failed"


class HealthResponse(BaseModel):
    status: str = "running"
    project: str = "BTC/Gold-Parth 5min"


# ── Scanner internal data models ─────────────────────────────────────────────

class Candle(BaseModel):
    """A single OHLCV candlestick bar returned by Twelve Data."""

    datetime: str
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None


class ScanResult(BaseModel):
    """
    Result produced by the scanner for a single asset when a signal fires.
    """

    asset_name: str
    symbol: str
    action: Literal["BUY", "SELL"]
    price: float
    signal_time: str


class ScannerStatus(BaseModel):
    """Returned by GET /scanner/status for operational visibility."""

    running: bool
    assets: list
    scan_interval_seconds: int
    last_signals: dict
