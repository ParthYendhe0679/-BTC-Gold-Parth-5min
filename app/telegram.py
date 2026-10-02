"""
telegram.py — Telegram Bot API client.

Notification pathways:
  1. Webhook alerts  — TradingView → POST /webhook → Telegram
  2. Scanner alerts  — strategy engine → delivery queue → Telegram
  3. Connectivity test (never formatted like a trading signal)

Delivery policy (send_text):
  • HTML parse mode; every dynamic value is HTML-escaped.
  • Timeouts: 5 s connect / 10 s read.
  • 429 → wait Telegram's retry_after (capped), retry.
  • 5xx / connect failure → exponential back-off, retry (max 3 attempts).
  • 400/401/403/404 → permanent, no retry.
  • Read timeout → UNCERTAIN (Telegram may have delivered it) → NOT retried,
    to avoid duplicate alerts.
  • The bot token is never logged (URLs are redacted).
"""

import html
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests

from app.config import settings
from app.models import WebhookPayload
from app.utils import fmt_utc_ist, format_price, normalize_timeframe, parse_tradingview_time, redact

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_DIVIDER = "━━━━━━━━━━━━━━━━━━"
_session = requests.Session()

# Last delivery outcome (for /health)
delivery_status: Dict[str, Any] = {"last_status": None, "last_at": None, "last_error": None,
                                   "sent": 0, "failed": 0}


@dataclass
class DeliveryResult:
    ok: bool
    status: str                 # SENT | FAILED | UNCERTAIN
    attempts: int
    error: Optional[str] = None
    message_id: Optional[int] = None


def _esc(x: Any) -> str:
    return html.escape(str(x), quote=False)


# ─────────────────────────────────────────────────────────────────────────────
# Core HTTP sender
# ─────────────────────────────────────────────────────────────────────────────

def api_call(method: str, payload: Dict[str, Any], timeout=(5, 10)) -> requests.Response:
    return _session.post(_API.format(token=settings.BOT_TOKEN, method=method), json=payload, timeout=timeout)


def send_text(text: str, chat_id: Optional[str] = None, max_attempts: int = 3,
              base_delay: float = 1.5) -> DeliveryResult:
    payload = {"chat_id": chat_id or settings.CHAT_ID, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    error = None
    attempt = 0
    for attempt in range(1, max_attempts + 1):
        delay = base_delay * (2 ** (attempt - 1))
        try:
            resp = api_call("sendMessage", payload)
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code == 200 and body.get("ok"):
                res = DeliveryResult(True, "SENT", attempt, message_id=body.get("result", {}).get("message_id"))
                _note(res)
                return res
            desc = redact(body.get("description", f"HTTP {resp.status_code}"))
            error = f"HTTP {resp.status_code}: {desc}"
            if resp.status_code == 429:
                delay = min(float(body.get("parameters", {}).get("retry_after", 5)), 30.0)
            elif resp.status_code < 500:
                logger.error("Telegram permanent error (no retry): %s", error)
                break
        except requests.exceptions.ConnectTimeout:
            error = "connect timeout"
        except requests.exceptions.ReadTimeout:
            res = DeliveryResult(False, "UNCERTAIN", attempt,
                                 "read timeout — may have been delivered; not retried to avoid duplicates")
            logger.error("Telegram %s", res.error)
            _note(res)
            return res
        except requests.exceptions.ConnectionError as exc:
            error = f"connection error: {redact(exc)[:160]}"
        except Exception as exc:  # noqa: BLE001
            error = f"unexpected: {redact(exc)[:160]}"
        logger.warning("Telegram attempt %d/%d failed: %s", attempt, max_attempts, error)
        if attempt < max_attempts:
            time.sleep(delay)
    res = DeliveryResult(False, "FAILED", attempt, error)
    _note(res)
    return res


def _note(res: DeliveryResult) -> None:
    delivery_status["last_status"] = res.status
    delivery_status["last_at"] = datetime.now(timezone.utc).isoformat()
    delivery_status["last_error"] = res.error
    delivery_status["sent" if res.ok else "failed"] += 1


def _send_raw(text: str, max_retries: int = 3, retry_delay: float = 1.5) -> bool:
    """Backward-compatible boolean wrapper."""
    return send_text(text, max_attempts=max_retries, base_delay=retry_delay).ok


# ─────────────────────────────────────────────────────────────────────────────
# Pathway 1 — TradingView webhook alerts
# ─────────────────────────────────────────────────────────────────────────────

def build_webhook_message(payload: WebhookPayload) -> str:
    """Build the Telegram message for a TradingView webhook signal (HTML-escaped)."""
    action = payload.action.upper()
    icon = "🟢" if action == "BUY" else "🔴"
    return (
        f"{_DIVIDER}\n"
        f"📊 <b>{_esc(payload.symbol)} | {_esc(normalize_timeframe(payload.timeframe))}</b>\n"
        f"{icon} <b>{_esc(action)} — TradingView alert</b>\n"
        f"{_DIVIDER}\n\n"
        f"• Indicator: {_esc(payload.indicator)}\n"
        f"• Entry: <code>{_esc(format_price(payload.entry))}</code>\n"
        f"• Time: {_esc(parse_tradingview_time(payload.time))}\n\n"
        f"<i>Forwarded from a TradingView webhook — not generated by this bot's scanner.</i>"
    )


def send_telegram_message(payload: WebhookPayload) -> bool:
    """Send a Telegram notification for a TradingView webhook alert."""
    ok = send_text(build_webhook_message(payload)).ok
    (logger.info if ok else logger.error)("Webhook alert Telegram delivery: %s", "SENT" if ok else "FAILED")
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# Pathway 2 — Scanner signals
# ─────────────────────────────────────────────────────────────────────────────

_STATUS_TEXT = {
    "CONFIRMED_CLOSE": "Confirmed candle-close signal",
    "CONFIRMED_TRIGGER": "Confirmed — breakout trigger hit, candle closed",
}


def _px(v: Optional[float], decimals: int) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return _esc(v)


def format_signal_message(sig, decimals: int = 2, tf_minutes: int = 5) -> str:
    """Compact mobile-friendly HTML message. Only calculated fields are shown."""
    icon = "🟢" if sig.direction == "BUY" else "🔴"
    title = sig.strategy_name.upper()
    tf_label = f"{tf_minutes} MIN"
    lines = [
        _DIVIDER,
        f"📊 <b>{_esc(sig.symbol)} | {tf_label}</b>",
        f"{icon} <b>{_esc(title)} — {sig.direction}</b>",
        _DIVIDER,
        "",
    ]
    if sig.setup_label:
        lines.append(f"• Setup: {_esc(sig.setup_label)}")
    for label, value in sig.details:
        val = _px(value, decimals) if isinstance(value, (int, float)) else _esc(value)
        lines.append(f"• {_esc(label)}: {val}")
    lines.append(f"• Entry reference: <code>{_px(sig.entry, decimals)}</code>")
    if sig.stop_loss is not None:
        lines.append(f"• Stop-loss: <code>{_px(sig.stop_loss, decimals)}</code>")
    for k, t in enumerate(sig.targets, 1):
        lines.append(f"• Target {k}: <code>{_px(t, decimals)}</code>")
    rr = sig.risk_reward
    if rr is not None:
        lines.append(f"• Risk/reward (T1): 1 : {rr:.2f}")
    close_time = sig.candle_time.to_pydatetime() + timedelta(minutes=tf_minutes)
    lines.append(f"• Candle closed: {fmt_utc_ist(close_time)}")
    lines.append(f"• Status: {_esc(_STATUS_TEXT.get(sig.confirmation_status, sig.confirmation_status))}")
    if sig.agreement:
        lines.append(f"• Same candle: {_esc(', '.join(sig.agreement))}")
    lines += [
        "",
        "<b>Reason</b>",
        _esc(sig.reason),
        "",
        f"Strategy: <code>{_esc(sig.strategy_id)}</code> v{_esc(sig.strategy_version)}",
        f"Signal ID: <code>{_esc(sig.signal_id)}</code>",
        "",
        "⚠️ <i>Educational signal, not a guaranteed outcome. Verify execution conditions before trading.</i>",
        _DIVIDER,
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Pathway 3 — Connectivity test (GET /scanner/test-alert, /test command)
# ─────────────────────────────────────────────────────────────────────────────

def build_test_alert_message() -> str:
    now = datetime.now(timezone.utc)
    return (
        f"🧪 <b>CONNECTIVITY TEST — NOT A TRADING SIGNAL</b>\n"
        f"{_DIVIDER}\n"
        f"Bot → Telegram delivery is working.\n"
        f"Server time: {fmt_utc_ist(now)}\n"
        f"Version: {_esc(settings.APP_VERSION)}\n"
        f"{_DIVIDER}"
    )


def send_test_alert() -> bool:
    ok = send_text(build_test_alert_message()).ok
    (logger.info if ok else logger.error)("Test alert %s", "sent" if ok else "FAILED — check BOT_TOKEN / CHAT_ID")
    return ok
