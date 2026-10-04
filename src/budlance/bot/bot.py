"""Telegram bot application lifecycle and setup."""

import logging
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, filters
from budlance.config import get_settings
from budlance.bot.handlers import demo_pass_handler, pass_handler, start_handler, text_message_handler

logger = logging.getLogger(__name__)


def build_bot_application(token: str | None = None) -> Application | None:
    """Initialize and configure the python-telegram-bot Application.

    Reads token from centralized settings if not explicitly provided.
    Returns None if no token is configured, allowing degraded offline operation.
    """
    settings = get_settings()
    resolved_token = token or settings.telegram_bot_token

    if not resolved_token or not resolved_token.strip():
        logger.warning("No TELEGRAM_BOT_TOKEN provided. Telegram bot application not initialized.")
        return None

    app = ApplicationBuilder().token(resolved_token).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler(["demo_pass", "bypass"], demo_pass_handler))
    app.add_handler(CommandHandler(["pass", "trip_pass"], pass_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    logger.info("Telegram bot application initialized successfully.")
    return app
