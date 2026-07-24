"""
telegram.py — Telegram Bot API client.

Two notification pathways:
  1. Webhook alerts  — TradingView → POST /webhook → Telegram
  2. Scanner alerts  — Internal scanner → Telegram (auto-generated signals)

The core HTTP sender (_send_raw) is shared by both pathways and includes
automatic retry with exponential back-off.
"""

import logging
import time

import requests

from app.config import settings
from app.models import WebhookPayload
from app.utils import format_price, normalize_timeframe, parse_tradingview_time

logger = logging.getLogger(__name__)

_TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━"


# ─────────────────────────────────────────────────────────────────────────────
# Core HTTP sender
# ─────────────────────────────────────────────────────────────────────────────

def _send_raw(text: str, max_retries: int = 3, retry_delay: float = 3.0) -> bool:
    """
    Post a plain-text (Markdown) message to the configured Telegram chat.

    Retries up to `max_retries` times with exponential back-off.
    Returns True on success, False if all attempts fail.
    """
    url = _TELEGRAM_URL.format(token=settings.BOT_TOKEN)
    payload = {
        "chat_id": settings.CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            result = resp.json()

            if result.get("ok"):
                return True

            logger.error(
                "Telegram Error (attempt %d/%d) — ok=false: %s",
                attempt, max_retries,
                result.get("description", "unknown"),
            )

        except requests.exceptions.Timeout:
            logger.error("Telegram Error (attempt %d/%d) — Timeout", attempt, max_retries)
        except requests.exceptions.ConnectionError as exc:
            logger.error("Telegram Error (attempt %d/%d) — Connection: %s", attempt, max_retries, exc)
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response else "?"
            logger.error("Telegram Error (attempt %d/%d) — HTTP %s", attempt, max_retries, status)
        except Exception as exc:  # noqa: BLE001
            logger.error("Telegram Error (attempt %d/%d) — Unexpected: %s", attempt, max_retries, exc)

        if attempt < max_retries:
            sleep = retry_delay * attempt
            logger.info("Retrying Telegram in %.0fs...", sleep)
            time.sleep(sleep)

    logger.error("Telegram Error — All %d attempts failed.", max_retries)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Pathway 1 — TradingView webhook alerts
# ─────────────────────────────────────────────────────────────────────────────

def build_webhook_message(payload: WebhookPayload) -> str:
    """Build the Telegram message for a TradingView webhook signal."""
    action = payload.action.upper()
    symbol = payload.symbol
    timeframe = normalize_timeframe(payload.timeframe)
    indicator = payload.indicator
    entry_price = format_price(payload.entry)
    signal_time = parse_tradingview_time(payload.time)

    if action == "BUY":
        header, trend_icon, trend_label, footer_emoji = (
            "🟢 *BUY SIGNAL*", "📈", "Bullish", "✅"
        )
    else:
        header, trend_icon, trend_label, footer_emoji = (
            "🔴 *SELL SIGNAL*", "📉", "Bearish", "⚠️"
        )

    return (
        f"{header}\n\n"
        f"{_DIVIDER}\n\n"
        f"🪙 *Coin*        : `{symbol}`\n\n"
        f"⏱ *Timeframe* : `{timeframe}`\n\n"
        f"{trend_icon} *Indicator*  : `{indicator}`\n\n"
        f"💰 *Entry Price* : `{entry_price}`\n\n"
        f"📊 *Trend*       : `{trend_label}`\n\n"
        f"🕒 *Time*         : `{signal_time}`\n\n"
        f"{_DIVIDER}\n\n"
        f"{footer_emoji} *Strategy Triggered Successfully*\n\n"
        f"_Powered by TradingView_"
    )


def send_telegram_message(payload: WebhookPayload) -> bool:
    """Send a Telegram notification for a TradingView webhook alert."""
    text = build_webhook_message(payload)
    success = _send_raw(text)
    if success:
        logger.info("Telegram Sent Successfully")
    else:
        logger.error("Telegram Error")
    return success


# ─────────────────────────────────────────────────────────────────────────────
# Pathway 2 — Internal scanner alerts
# ─────────────────────────────────────────────────────────────────────────────

def build_scanner_message(
    asset_name: str,
    symbol: str,
    action: str,
    price: float,
    raw_time: str,
    strategy_name: str = "Supertrend (10,3)",
) -> str:
    """Build the Telegram message for a scanner-generated signal."""
    formatted_price = format_price(str(price))
    signal_time = parse_tradingview_time(raw_time)

    if action == "BUY":
        header, trend_icon, trend_label, footer_emoji = (
            "🟢 *BUY SIGNAL*", "📈", "Bullish", "✅"
        )
    else:
        header, trend_icon, trend_label, footer_emoji = (
            "🔴 *SELL SIGNAL*", "📉", "Bearish", "⚠️"
        )

    return (
        f"{header}\n\n"
        f"{_DIVIDER}\n\n"
        f"🪙 *Asset*       : `{asset_name} ({symbol})`\n\n"
        f"⏱ *Timeframe* : `{settings.TIMEFRAME_DISPLAY}`\n\n"
        f"{trend_icon} *Strategy*   : `{strategy_name}`\n\n"
        f"💰 *Entry Price* : `{formatted_price}`\n\n"
        f"📊 *Trend*       : `{trend_label}`\n\n"
        f"🕒 *Signal Time* : `{signal_time}`\n\n"
        f"{_DIVIDER}\n\n"
        f"{footer_emoji} *Strategy Triggered Successfully*\n\n"
        f"_Powered by BTC/Gold-Parth 5M_"
    )


def send_scanner_alert(
    asset_name: str,
    symbol: str,
    action: str,
    price: float,
    raw_time: str,
    strategy_name: str = "Supertrend (10,3)",
) -> bool:
    """Send a Telegram notification for a scanner-detected signal change."""
    text = build_scanner_message(
        asset_name=asset_name,
        symbol=symbol,
        action=action,
        price=price,
        raw_time=raw_time,
        strategy_name=strategy_name,
    )
    return _send_raw(text)


# ─────────────────────────────────────────────────────────────────────────────
# Pathway 3 — Test alert  (GET /scanner/test-alert)
# ─────────────────────────────────────────────────────────────────────────────

def build_test_alert_message() -> str:
    """
    Build a fixed-format test Telegram message with the current server time.
    Used exclusively by GET /scanner/test-alert — no scanner or API calls.
    """
    from datetime import datetime, timedelta, timezone

    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(timezone.utc).astimezone(IST)
    current_time = now.strftime("%I:%M %p IST").lstrip("0")
    indicator = f"Supertrend ({settings.ATR_PERIOD},{int(settings.ATR_MULTIPLIER)})"

    return (
        f"🧪 *TEST ALERT*\n\n"
        f"{_DIVIDER}\n\n"
        f"🪙 *Asset*       : `Bitcoin (BTC/USD)`\n\n"
        f"⏱ *Timeframe* : `{settings.TIMEFRAME_DISPLAY}`\n\n"
        f"📈 *Indicator*  : `{indicator}`\n\n"
        f"💰 *Entry Price* : `TEST`\n\n"
        f"📊 *Trend*       : `Bullish`\n\n"
        f"🕒 *Time*         : `{current_time}`\n\n"
        f"{_DIVIDER}\n\n"
        f"✅ *Telegram Connection Successful*\n\n"
        f"_Powered by BTC/Gold-Parth 5min_"
    )


def send_test_alert() -> bool:
    """
    Send a test Telegram message via the existing bot configuration.
    Returns True on success, False on any failure.
    """
    text = build_test_alert_message()
    success = _send_raw(text)
    if success:
        logger.info("Test alert sent successfully.")
    else:
        logger.error("Test alert failed — check BOT_TOKEN and CHAT_ID in .env")
    return success

