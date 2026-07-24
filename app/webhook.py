"""
webhook.py — FastAPI router for the TradingView webhook endpoint.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.models import WebhookErrorResponse, WebhookPayload, WebhookSuccessResponse
from app.telegram import send_telegram_message

logger = logging.getLogger(__name__)

router = APIRouter()


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
            "time":      "{{time}}"
        }
    """
    client_host = request.client.host if request.client else "unknown"
    logger.info(
        "Webhook Received — action=%s  symbol=%s  from=%s",
        payload.action, payload.symbol, client_host,
    )

    success = send_telegram_message(payload)

    if success:
        return JSONResponse(
            content=WebhookSuccessResponse(
                action=payload.action,
                symbol=payload.symbol,
            ).model_dump(),
            status_code=200,
        )

    return JSONResponse(
        content=WebhookErrorResponse().model_dump(),
        status_code=502,
    )
