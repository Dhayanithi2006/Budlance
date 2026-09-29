"""FastAPI application factory with lifecycle management."""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from fastapi import FastAPI
from budlance import __version__
from budlance.config import get_settings
from budlance.bot.bot import build_bot_application
from budlance.api.routes import router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown lifecycles."""
    settings = get_settings()
    logger.info("Initializing Budlance backend (env=%s, port=%d)", settings.app_env, settings.port)

    # Initialize Telegram bot application if token is available
    bot_app = build_bot_application()
    app.state.bot_app = bot_app

    if bot_app:
        try:
            await bot_app.initialize()
            await bot_app.start()
            logger.info("Telegram bot application started.")
        except Exception as exc:
            logger.warning(
                "Could not connect to Telegram API during startup (%s: %s). Running in offline/degraded mode.",
                type(exc).__name__,
                exc,
            )
    else:
        logger.info("Running without Telegram bot integration (token empty or unconfigured).")

    yield

    # Clean shutdown
    if getattr(app.state, "bot_app", None):
        logger.info("Shutting down Telegram bot application...")
        try:
            if getattr(app.state.bot_app, "running", False):
                await app.state.bot_app.stop()
            await app.state.bot_app.shutdown()
            logger.info("Telegram bot application shutdown complete.")
        except Exception as exc:
            logger.warning("Error during Telegram bot shutdown: %s", exc)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    settings = get_settings()

    app = FastAPI(
        title="Budlance API",
        description="Reverse-budget AI travel agent backend gateway",
        version=__version__,
        lifespan=lifespan,
    )

    # Register API routes
    app.include_router(router)

    return app


# Application instance for ASGI servers (uvicorn budlance.api.app:app)
app = create_app()
