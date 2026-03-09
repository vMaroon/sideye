"""FastAPI application entry point."""

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import Config
from app.database import init_database
from web.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Sideye", version="0.1.0")

# CORS — allow browser extension to talk to the bot from github.com
# Chrome extensions use chrome-extension:// origins, Safari uses safari-web-extension://
# Since this is a local-only server, allow all origins for maximum compatibility.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Mount static files
_static_dir = Path(__file__).parent.parent / "web" / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Include routes
app.include_router(router)


@app.on_event("startup")
async def startup():
    # Validate config
    issues = Config.validate()
    if issues:
        logger.warning("Config issues: %s", issues)

    # Initialize database
    init_database()

    # Start coherence scheduler (optional, non-blocking)
    try:
        _start_scheduler()
    except Exception as e:
        logger.warning("Scheduler setup failed (non-critical): %s", e)

    logger.info("Sideye started at http://%s:%d", Config.APP_HOST, Config.APP_PORT)


def _start_scheduler():
    """Set up APScheduler for daily coherence runs."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from agents.coherence import run_coherence_cycle

    cron = Config.COHERENCE_CRON
    parts = cron.split()
    if len(parts) != 5:
        logger.warning("Invalid COHERENCE_CRON: %s", cron)
        return

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_coherence_cycle,
        "cron",
        minute=parts[0],
        hour=parts[1],
        day=parts[2],
        month=parts[3],
        day_of_week=parts[4],
        id="daily_coherence",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Coherence scheduler started: %s", cron)


# Config status endpoint (used by settings page)
@app.get("/api/config/status")
async def config_status():
    return {
        "github_token": bool(Config.GITHUB_TOKEN),
        "api_key": bool(Config.ANTHROPIC_API_KEY),
        "model": Config.CLAUDE_MODEL,
        "coherence_cron": Config.COHERENCE_CRON,
        "workspace_root": Config.WORKSPACE_ROOT,
    }
