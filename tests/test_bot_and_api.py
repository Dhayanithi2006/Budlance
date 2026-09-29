"""Unit and integration tests for FastAPI application and Telegram bot connection."""

from unittest.mock import AsyncMock, MagicMock
import pytest
from httpx import ASGITransport, AsyncClient
from telegram import Chat, Message, Update, User
from budlance.api.app import create_app
from budlance.bot.handlers import start_handler, text_message_handler
from budlance.bot.bot import build_bot_application


@pytest.mark.asyncio
async def test_health_endpoint():
    """Verify that the health check endpoint returns 200 OK and expected structure."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "app_env" in data
        assert "version" in data
        assert "telegram_configured" in data


@pytest.mark.asyncio
async def test_webhook_unconfigured():
    """Verify that webhook returns 503 if bot is not configured."""
    app = create_app()
    app.state.bot_app = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/webhook", json={"update_id": 12345})
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_invalid_payload():
    """Verify that webhook returns 400 on malformed payloads."""
    app = create_app()
    mock_bot_app = MagicMock()
    app.state.bot_app = mock_bot_app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Non-JSON content
        response = await client.post(
            "/webhook",
            content="not a json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_webhook_valid_update_dispatch():
    """Verify that a valid Telegram update is parsed and dispatched to process_update."""
    app = create_app()
    mock_bot_app = MagicMock()
    mock_bot_app.bot = MagicMock()
    mock_bot_app.bot.defaults = None
    mock_bot_app.process_update = AsyncMock()
    app.state.bot_app = mock_bot_app

    valid_payload = {
        "update_id": 10001,
        "message": {
            "message_id": 1,
            "date": 1441645532,
            "chat": {"id": 99999, "type": "private", "first_name": "TestUser"},
            "from": {"id": 99999, "is_bot": False, "first_name": "TestUser"},
            "text": "Hello Budlance!",
        },
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/webhook", json=valid_payload)
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert mock_bot_app.process_update.called
        dispatched_update = mock_bot_app.process_update.call_args[0][0]
        assert isinstance(dispatched_update, Update)
        assert dispatched_update.update_id == 10001


@pytest.mark.asyncio
async def test_start_command_handler():
    """Verify start_handler produces the expected welcome text."""
    update = MagicMock(spec=Update)
    message = MagicMock(spec=Message)
    message.reply_text = AsyncMock()
    update.effective_message = message
    update.effective_chat = MagicMock(spec=Chat, id=12345)
    context = MagicMock()

    await start_handler(update, context)

    assert message.reply_text.called
    reply_content = message.reply_text.call_args[0][0]
    assert "Welcome to Budlance" in reply_content
    assert "reverse-budget" in reply_content


@pytest.mark.asyncio
async def test_text_message_handler():
    """Verify text_message_handler dispatches to orchestrator and replies to user."""
    update = MagicMock(spec=Update)
    message = MagicMock(spec=Message)
    message.text = "Can you plan a trip to Goa?"
    message.reply_text = AsyncMock()
    update.effective_message = message
    update.effective_chat = MagicMock(spec=Chat, id=12345)
    update.effective_user = MagicMock(spec=User, id=12345, username="tester", first_name="Test")
    context = MagicMock()

    await text_message_handler(update, context)

    assert message.reply_text.called
    reply_content = message.reply_text.call_args[0][0]
    assert "details to plan your trip" in reply_content or "budget" in reply_content.lower()


def test_build_bot_application_with_dummy_token():
    """Verify bot application builds properly when given a token."""
    dummy_token = "123456789:ABCDEFghijklmnopqrstuvwxyz123456789"
    bot_app = build_bot_application(token=dummy_token)
    assert bot_app is not None
    assert len(bot_app.handlers[0]) >= 2  # Handlers registered at group 0
