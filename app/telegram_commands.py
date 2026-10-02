"""
telegram_commands.py — Authenticated bot commands via long polling (getUpdates).

Only messages from the configured CHAT_ID are answered; everything else is
ignored silently. Commands: /start /help /status /strategies /test.
/test sends a connectivity message that is explicitly NOT a trading signal.

If the bot has a Telegram webhook configured, or another instance is polling
(HTTP 409), polling backs off — it never interferes with signal delivery.
"""

import asyncio
import html
import logging
from typing import Any, Dict, Optional

from app.config import settings
from app.telegram import api_call, build_test_alert_message, send_text
from app.utils import redact

logger = logging.getLogger(__name__)

command_state: Dict[str, Any] = {"enabled": settings.TELEGRAM_COMMANDS_ENABLED, "last_error": None}


def _strategies_text() -> str:
    lines = ["<b>Strategies</b>"]
    for s in settings.STRATEGIES:
        lines.append(f"{'✅' if s.get('enabled', True) else '⛔'} <code>{s['id']}</code> — {html.escape(s['name'])}")
    lines.append("\nToggle with ENABLED_STRATEGIES / DISABLED_STRATEGIES env vars.")
    return "\n".join(lines)


def _status_text() -> str:
    from app.scanner import get_scanner, scanner_state

    lines = [f"<b>Scanner status</b> (v{settings.APP_VERSION})",
             f"Running: {'yes' if scanner_state['running'] else 'NO'}",
             f"Last successful scan: {scanner_state.get('last_successful_scan_at') or '—'}",
             f"Next scan: {scanner_state.get('next_scan_at') or '—'}"]
    for sym, st in scanner_state["assets"].items():
        lines.append(f"• {html.escape(sym)}: last closed candle {st.get('latest_closed_candle') or '—'}"
                     + (f" ({html.escape(st['skipped_reason'])})" if st.get("skipped_reason") else "")
                     + (f" ⚠️ {html.escape(str(st['last_error']))}" if st.get("last_error") else ""))
    sc = get_scanner()
    if sc:
        stats = sc.store.stats()
        if stats:
            lines.append("Deliveries: " + ", ".join(f"{k}={v['count']}" for k, v in stats.items()))
    return "\n".join(lines)


HELP = ("<b>BTC/Gold-Parth 5min</b>\n"
        "/status — scanner health\n/strategies — enabled strategies\n"
        "/test — Telegram connectivity test (not a signal)\n/help — this message")


def handle_command(text: str) -> Optional[str]:
    cmd = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return _status_text()
    if cmd == "/strategies":
        return _strategies_text()
    if cmd == "/test":
        return build_test_alert_message()
    return None


async def poll_commands() -> None:
    if not settings.TELEGRAM_COMMANDS_ENABLED:
        return
    offset = None
    backoff = 5
    while True:
        try:
            payload: Dict[str, Any] = {"timeout": 25, "allowed_updates": ["message", "channel_post"]}
            if offset is not None:
                payload["offset"] = offset
            resp = await asyncio.to_thread(api_call, "getUpdates", payload, (5, 35))
            if resp.status_code == 409:
                command_state["last_error"] = "409 conflict (webhook set or another instance polling)"
                await asyncio.sleep(60)
                continue
            body = resp.json()
            if not body.get("ok"):
                command_state["last_error"] = redact(body.get("description", resp.status_code))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue
            backoff = 5
            for upd in body.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("channel_post") or {}
                if str(msg.get("chat", {}).get("id")) != str(settings.CHAT_ID):
                    continue   # unauthorised chat — ignore
                reply = handle_command(msg.get("text", ""))
                if reply:
                    await asyncio.to_thread(send_text, reply)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            command_state["last_error"] = redact(exc)
            logger.warning("Command polling error: %s", redact(exc))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
