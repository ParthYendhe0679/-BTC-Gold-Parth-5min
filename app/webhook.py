"""
webhook.py — FastAPI router for the TradingView webhook endpoint.

If WEBHOOK_SECRET is set, the request must carry it as the JSON field
"secret", the query parameter ?secret=, or the X-Webhook-Secret header
(TradingView cannot set headers, so use the JSON field there).
"""

import hmac
import logging

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.config import settings
from app.models import WebhookErrorResponse, WebhookPayload, WebhookSuccessResponse
from app.telegram import send_telegram_message

logger = logging.getLogger(__name__)

router = APIRouter()


def _authorised(payload: WebhookPayload, request: Request) -> bool:
    if not settings.WEBHOOK_SECRET:
        return True
    supplied = (payload.secret or request.query_params.get("secret")
                or request.headers.get("x-webhook-secret") or "")
    return hmac.compare_digest(supplied.encode(), settings.WEBHOOK_SECRET.encode())


@router.post("/webhook", tags=["Webhook"])
async def receive_webhook(payload: WebhookPayload, request: Request) -> JSONResponse:
    """
    Receive a TradingView alert and forward it to Telegram.

    Expected JSON body::

        {
            "action":    "BUY" | "SELL",
            "symbol":    "BTCUSDT",
            "timeframe": "5 Minutes",
            "indicator": "Supertrend (10,3)",
            "entry":     "{{close}}",
            "time":      "{{time}}",
            "secret":    "<WEBHOOK_SECRET>"
        }
    """
    client_host = request.client.host if request.client else "unknown"
    if not _authorised(payload, request):
        logger.warning("Webhook rejected — bad/missing secret from %s", client_host)
        return JSONResponse({"status": "error", "message": "unauthorized"}, status_code=401)
    logger.info("Webhook received — action=%s symbol=%s from=%s", payload.action, payload.symbol, client_host)

    success = await run_in_threadpool(send_telegram_message, payload)
    if success:
        return JSONResponse(
            content=WebhookSuccessResponse(action=payload.action, symbol=payload.symbol).model_dump(),
            status_code=200,
        )
    return JSONResponse(content=WebhookErrorResponse().model_dump(), status_code=502)
