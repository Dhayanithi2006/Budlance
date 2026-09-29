"""FastAPI route handlers for health checks and Telegram webhooks.

Business logic is strictly decoupled and will be managed by the Orchestrator
in subsequent implementation phases.
"""

import logging
from typing import Any
from fastapi import APIRouter, HTTPException, Request, status
from telegram import Update
from budlance import __version__
from budlance.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health", tags=["System"])
async def health_check(request: Request) -> dict[str, Any]:
    """Health check endpoint reporting system status and configuration state."""
    settings = get_settings()
    bot_app = getattr(request.app.state, "bot_app", None)

    return {
        "status": "healthy",
        "app_env": settings.app_env,
        "version": __version__,
        "telegram_configured": bot_app is not None and settings.has_telegram_token,
    }


@router.post("/webhook", tags=["Telegram"])
async def telegram_webhook(request: Request) -> dict[str, bool]:
    """Ingest and dispatch incoming Telegram webhook updates to python-telegram-bot."""
    bot_app = getattr(request.app.state, "bot_app", None)
    if not bot_app:
        logger.error("Received webhook update but bot application is not configured.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram bot is not configured on this server.",
        )

    try:
        payload = await request.json()
    except Exception as exc:
        logger.warning("Invalid JSON received on webhook endpoint: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload must be a JSON object.",
        )

    # Validate and deserialize Telegram update
    try:
        update = Update.de_json(data=payload, bot=bot_app.bot)
        if update is None:
            raise ValueError("Update.de_json returned None")
    except Exception as exc:
        logger.warning("Payload could not be parsed as a Telegram Update: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed Telegram update payload.",
        ) from exc

    # Dispatch update into python-telegram-bot async queue/handler chain
    await bot_app.process_update(update)
    return {"ok": True}
