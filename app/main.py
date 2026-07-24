"""
main.py — FastAPI application entry point.

Responsibilities
────────────────
• Configure structured logging
• Validate all required secrets at startup (fail fast)
• Start the background signal scanner as an asyncio task (via lifespan)
• Mount all routers and exception handlers
• Expose health-check and scanner-status endpoints
"""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app.models import HealthResponse, ScannerStatus
from app.scanner import get_last_signals, run_scanner, scanner_state
from app.telegram import send_test_alert
from app.webhook import router as webhook_router

# ─────────────────────────────────────────────────────────────────────────────
# Force UTF-8 output — prevents UnicodeEncodeError on Windows cp1252 consoles
# ─────────────────────────────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ─────────────────────────────────────────────────────────────────────────────
# Logging — structured, stdout-based for cloud log aggregators
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Startup secret validation — fail immediately with a clear message
# ─────────────────────────────────────────────────────────────────────────────
try:
    settings.validate()
except RuntimeError as exc:
    logger.critical("Startup failed — %s", exc)
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan — start/stop background scanner task
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the scanner on app startup; cancel it cleanly on shutdown."""
    scanner_task = asyncio.create_task(run_scanner(), name="signal_scanner")
    logger.info("Background signal scanner task started.")
    try:
        yield
    finally:
        scanner_task.cancel()
        try:
            await scanner_task
        except asyncio.CancelledError:
            logger.info("Scanner task cancelled — shutdown complete.")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI application
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.APP_TITLE,
    version=settings.APP_VERSION,
    description=(
        "BTC/Gold-Parth 5min — Supertrend signal scanner. "
        "Monitors BTC/USD and XAU/USD on the 5-minute chart and "
        "sends instant Telegram alerts on every BUY/SELL crossover."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ─────────────────────────────────────────────────────────────────────────────
# Routers
# ─────────────────────────────────────────────────────────────────────────────
app.include_router(webhook_router)


# ─────────────────────────────────────────────────────────────────────────────
# Global exception handlers
# ─────────────────────────────────────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return a clean 422 response when Pydantic validation fails."""
    logger.warning("Validation error on %s — %s", request.url.path, exc.errors())
    return JSONResponse(
        status_code=422,
        content={
            "status":  "error",
            "message": "Invalid payload",
            "details": exc.errors(),
        },
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all — prevents raw tracebacks reaching the client."""
    logger.exception("Unhandled exception on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": "Internal server error"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/", response_model=HealthResponse, tags=["Health"])
async def health_check() -> HealthResponse:
    """Health-check endpoint — confirms the service is running."""
    return HealthResponse()


@app.get("/scanner/status", tags=["Scanner"])
async def scanner_status() -> JSONResponse:
    """
    Return the current scanner state including last signals for each asset.

    Useful for monitoring dashboards, uptime checks, and debugging.
    """
    status = ScannerStatus(
        running=scanner_state.get("running", False),
        assets=[
            {"name": a["name"], "symbol": a["symbol"]}
            for a in settings.ASSETS
        ],
        scan_interval_seconds=settings.SCAN_INTERVAL,
        last_signals=get_last_signals(),
    )
    return JSONResponse(content=status.model_dump())


@app.get("/scanner/test-alert", tags=["Scanner"])
async def test_alert() -> JSONResponse:
    """
    Send a test Telegram notification to verify bot connectivity.

    Does NOT use the scanner or Twelve Data — purely a connectivity check.
    Returns 200 on success, 502 if the Telegram API call fails.
    """
    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(None, send_test_alert)

    if success:
        return JSONResponse(
            content={"success": True, "message": "Test alert sent successfully."},
            status_code=200,
        )

    logger.error("Test alert endpoint — Telegram delivery failed.")
    return JSONResponse(
        content={"success": False, "message": "Telegram notification failed. Check BOT_TOKEN and CHAT_ID."},
        status_code=502,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dev runner
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
