"""Telegram message handlers wired to the Budlance Orchestrator.

Architectural Boundary:
- Handlers extract Telegram user/chat metadata and dispatch directly to BudlanceOrchestrator.
- Handlers contain NO SerpApi calls, budget calculations, optimization logic, or database operations.
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes
from budlance.orchestrator.orchestrator import BudlanceOrchestrator

logger = logging.getLogger(__name__)

_default_orchestrator: BudlanceOrchestrator | None = None


def get_orchestrator() -> BudlanceOrchestrator:
    """Retrieve or lazily initialize the singleton BudlanceOrchestrator."""
    global _default_orchestrator
    if _default_orchestrator is None:
        _default_orchestrator = BudlanceOrchestrator()
    return _default_orchestrator


def set_orchestrator(orchestrator: BudlanceOrchestrator | None) -> None:
    """Set custom orchestrator instance (used primarily for testing/mocking)."""
    global _default_orchestrator
    _default_orchestrator = orchestrator


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command with helpful reverse-budget onboarding."""
    if not update.effective_message:
        return

    welcome_text = (
        "👋 *Welcome to Budlance — reverse-budget AI travel agent!* ✈️\n\n"
        "Tell me your budget, number of travelers, trip duration, and departure city. "
        "Budlance will discover, calculate, and construct a complete trip that strictly fits your budget.\n\n"
        "💡 *Example:*\n"
        "`Plan a trip from Mumbai for 2 people, 3 days, with budget ₹20,000`\n\n"
        "✨ *In-Trip Rescue:* If you are currently traveling and hit bad weather, closures, or unfair fares, "
        "simply message me here (e.g. _'It's raining at the beach'_ or _'The auto driver is asking ₹500'_)."
    )
    await update.effective_message.reply_text(welcome_text, parse_mode="Markdown")
    logger.info("Handled /start command for chat_id=%s", update.effective_chat.id if update.effective_chat else "unknown")


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming user message by invoking the Budlance Orchestrator."""
    if not update.effective_message or not update.effective_message.text:
        return

    text = update.effective_message.text
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user_id = update.effective_user.id if update.effective_user else chat_id
    username = update.effective_user.username if update.effective_user else None
    first_name = update.effective_user.first_name if update.effective_user else None

    orchestrator = get_orchestrator()
    result = await orchestrator.handle_user_message(
        telegram_user_id=user_id,
        chat_id=chat_id,
        message=text,
        username=username,
        first_name=first_name,
    )

    try:
        await update.effective_message.reply_text(result.message_text, parse_mode="Markdown")
    except Exception as exc:
        logger.warning("Markdown formatting rejected by Telegram (%s). Falling back to plain text.", exc)
        await update.effective_message.reply_text(result.message_text)

    logger.info(
        "Dispatched message for chat_id=%s to Orchestrator -> Status: %s",
        chat_id,
        result.status,
    )
