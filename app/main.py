"""Application entrypoint: startup, static UI, scheduler, security headers."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import cadence as cadence_engine
from .api import public_router
from .api import router as api_router
from .config import settings
from .db import SessionLocal, init_db
from .security import ensure_bootstrap_user
from .templates_content import seed_default_cadence, seed_templates

log = logging.getLogger("wholesale_ai")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s"
)

WEB_DIR = Path(__file__).parent.parent / "web"
SCHEDULER_INTERVAL_SECONDS = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "300"))


async def scheduler_loop() -> None:
    """Background loop that advances the cadence.

    Without this, nothing is ever contacted. Runs every 5 minutes by default
    (Railway-friendly: a cron ping to /api/pipeline/send-due also works if you
    scale this service to zero).
    """
    await asyncio.sleep(20)  # let the app settle
    while True:
        try:
            if settings.app_env != "test":
                db = SessionLocal()
                try:
                    stats = cadence_engine.process_due(db)
                    if stats["steps_run"]:
                        log.info("cadence: %s", stats)
                finally:
                    db.close()
        except Exception as e:  # never let the loop die
            log.exception("scheduler error: %s", e)
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        ensure_bootstrap_user(db)
        seed_templates(db)
        seed_default_cadence(db)
        db.commit()
    finally:
        db.close()

    for name, cap in settings.capabilities().items():
        state = "LIVE" if cap["live"] else "dry-run"
        log.info("channel %-6s -> %-8s (%s)", name, state, cap["provider"])
    if not settings.telnyx_public_key:
        log.warning(
            "TELNYX_PUBLIC_KEY is not set: inbound webhooks are NOT signature-verified. "
            "Set it before going live."
        )
    if settings.admin_password in ("changeme123", "CHANGE_ME_strong_password"):
        log.warning("ADMIN_PASSWORD is still the default — change it immediately.")

    task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(
    title="Wholesale AI",
    version="2.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.app_env != "production" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.app_env != "production" else None,
)

app.include_router(public_router)
app.include_router(api_router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if settings.app_env == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/health")
def health():
    return {"ok": True, "version": "2.0"}


@app.get("/manifest.json")
def manifest():
    p = WEB_DIR / "manifest.json"
    if p.exists():
        return FileResponse(p)
    return JSONResponse({"name": "Wholesale AI", "short_name": "WholesaleAI"})


@app.get("/sell")
def sell_page():
    """Public seller landing page. Point your website at this, or iframe it."""
    p = WEB_DIR / "sell.html"
    if p.exists():
        return FileResponse(p, headers={"Cache-Control": "public, max-age=300"})
    return JSONResponse({"error": "form not built"}, status_code=500)


@app.get("/")
def index():
    p = WEB_DIR / "index.html"
    if p.exists():
        return FileResponse(p, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"error": "UI not built"}, status_code=500)


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
