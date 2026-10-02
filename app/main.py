"""
main.py — FastAPI application entry point.

Responsibilities
────────────────
• Configure structured logging
• Validate all required secrets at startup (fail fast)
• Start the background signal scanner + Telegram command poller (lifespan),
  cancel them cleanly on shutdown
• Mount all routers and exception handlers
• Expose /, /health, /scanner/status and the authenticated test endpoint
"""

import asyncio
import hmac
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app.data_fetcher import credits, fetch_status
from app.models import HealthResponse, ScannerStatus
from app.scanner import get_last_signals, get_scanner, run_scanner, scanner_state
from app.telegram import delivery_status, send_test_alert
from app.telegram_commands import command_state, poll_commands
from app.webhook import router as webhook_router

# Force UTF-8 output — prevents UnicodeEncodeError on Windows cp1252 consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

try:
    settings.validate()
except RuntimeError as exc:
    logger.critical("Startup failed — %s", exc)
    sys.exit(1)

_tasks: dict = {}


def _on_task_done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        scanner_state["last_error"] = f"{task.get_name()} crashed: {type(exc).__name__}"
        logger.critical("Background task %s crashed", task.get_name(), exc_info=exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the scanner on app startup; cancel it cleanly on shutdown."""
    _tasks["scanner"] = asyncio.create_task(run_scanner(), name="signal_scanner")
    _tasks["commands"] = asyncio.create_task(poll_commands(), name="telegram_commands")
    for t in _tasks.values():
        t.add_done_callback(_on_task_done)
    logger.info("Background tasks started: %s", ", ".join(_tasks))
    try:
        yield
    finally:
        for t in _tasks.values():
            t.cancel()
        await asyncio.gather(*_tasks.values(), return_exceptions=True)
        logger.info("Background tasks cancelled — shutdown complete.")


app = FastAPI(
    title=settings.APP_TITLE,
    version=settings.APP_VERSION,
    description=(
        "BTC/Gold-Parth 5min — multi-strategy signal scanner for BTC/USD and XAU/USD "
        "on the 5-minute chart, with Telegram delivery. Signal generation only — "
        "no order execution."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)
app.include_router(webhook_router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return a clean 422 response when Pydantic validation fails (no secret echo)."""
    errors = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]
    logger.warning("Validation error on %s — %s", request.url.path, errors)
    return JSONResponse(status_code=422, content={"status": "error", "message": "Invalid payload",
                                                  "details": errors})


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all — prevents raw tracebacks reaching the client."""
    logger.exception("Unhandled exception on %s", request.url.path)
    return JSONResponse(status_code=500, content={"status": "error", "message": "Internal server error"})


def _age_s(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()


@app.get("/", response_model=HealthResponse, tags=["Health"])
async def root() -> HealthResponse:
    """Lightweight liveness check (process is up)."""
    return HealthResponse()


@app.get("/health", tags=["Health"])
async def health() -> JSONResponse:
    """
    Readiness/health. Never calls external providers (always fast).
    503 only when the scanner itself is dead or stalled — provider or Telegram
    problems report "degraded" with 200 so Render does not restart-loop.
    """
    task = _tasks.get("scanner")
    task_alive = task is not None and not task.done()
    loop_age = _age_s(scanner_state.get("last_loop_at"))
    stall_limit = settings.TIMEFRAME_MINUTES * 60 * 2 + 300
    stalled = loop_age is not None and loop_age > stall_limit
    starting = scanner_state.get("last_loop_at") is None and task_alive

    asset_errors = {s: v.get("last_error") for s, v in fetch_status.items() if v.get("last_error")}
    degraded = bool(asset_errors) or delivery_status.get("last_status") in ("FAILED", "UNCERTAIN")
    if not task_alive or stalled:
        status, code = "down", 503
    elif starting:
        status, code = "starting", 200
    else:
        status, code = ("degraded" if degraded else "ok"), 200

    sc = get_scanner()
    body = {
        "status": status,
        "version": settings.APP_VERSION,
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "scanner": {
            "task_alive": task_alive,
            "running": scanner_state["running"],
            "started_at": scanner_state.get("started_at"),
            "scan_count": scanner_state["scan_count"],
            "last_loop_at": scanner_state.get("last_loop_at"),
            "last_successful_scan_at": scanner_state.get("last_successful_scan_at"),
            "next_scan_at": scanner_state.get("next_scan_at"),
            "last_error": scanner_state.get("last_error"),
        },
        "market_data": {
            "provider": "twelvedata",
            "status": "degraded" if asset_errors else "ok",
            "assets": {s: {"latest_closed_candle": v.get("latest_closed_candle"),
                           "skipped_reason": v.get("skipped_reason")}
                       for s, v in scanner_state["assets"].items()},
            "errors": asset_errors,
            "credits_used_today_this_process": credits.used,
            "daily_credit_budget": settings.DAILY_CREDIT_BUDGET,
        },
        "telegram": {
            "configured": bool(settings.BOT_TOKEN and settings.CHAT_ID),
            **{k: delivery_status[k] for k in ("last_status", "last_at", "last_error", "sent", "failed")},
            "commands_enabled": command_state["enabled"],
            "commands_last_error": command_state["last_error"],
        },
        "signals": sc.store.stats() if sc else {},
    }
    return JSONResponse(body, status_code=code)


@app.get("/scanner/status", tags=["Scanner"])
async def scanner_status() -> JSONResponse:
    """Scanner state including per-strategy status and the last signals."""
    status = ScannerStatus(
        running=scanner_state.get("running", False),
        assets=[{"name": a["name"], "symbol": a["symbol"]} for a in settings.ASSETS],
        scan_interval_seconds=settings.SCAN_INTERVAL,
        last_signals=get_last_signals(),
    )
    body = status.model_dump()
    body["strategies"] = scanner_state["strategies"]
    body["enabled_strategies"] = [s["id"] for s in settings.enabled_strategies]
    sc = get_scanner()
    body["recent_signals"] = sc.store.recent(20) if sc else []
    return JSONResponse(content=body)


@app.get("/scanner/test-alert", tags=["Scanner"])
async def test_alert(token: str = "") -> JSONResponse:
    """
    Send a Telegram connectivity test (explicitly labelled NOT a signal).
    Requires ?token=<ADMIN_TOKEN> when ADMIN_TOKEN is configured.
    """
    if settings.ADMIN_TOKEN and not hmac.compare_digest(token.encode(), settings.ADMIN_TOKEN.encode()):
        return JSONResponse({"success": False, "message": "unauthorized"}, status_code=401)
    success = await run_in_threadpool(send_test_alert)
    if success:
        return JSONResponse({"success": True, "message": "Test alert sent successfully."}, status_code=200)
    return JSONResponse({"success": False, "message": "Telegram notification failed. Check BOT_TOKEN and CHAT_ID."},
                        status_code=502)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True, log_level="info")
