"""FastAPI route handlers for health checks and Telegram webhooks.

Business logic is strictly decoupled and will be managed by the Orchestrator
in subsequent implementation phases.
"""

import html
import logging
import urllib.parse
from typing import Any
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
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


# =========================================================================
# Payment and Trip Pass Endpoints (Phase 8)
# =========================================================================

from uuid import UUID
from budlance.payment.service import PaymentService

_payment_service: PaymentService | None = None


def get_payment_service() -> PaymentService:
    """Retrieve or lazily initialize the PaymentService singleton."""
    global _payment_service
    if _payment_service is None:
        _payment_service = PaymentService()
    return _payment_service


def set_payment_service(service: PaymentService | None) -> None:
    """Set custom PaymentService instance for testing."""
    global _payment_service
    _payment_service = service


@router.post("/payment/webhook/{provider}", tags=["Payment"])
async def payment_webhook(provider: str, request: Request) -> dict[str, Any]:
    """Receive payment webhook notifications from Razorpay, Stripe, or test rails."""
    raw_body = await request.body()
    try:
        import json
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        ) from exc

    sig_header = (
        request.headers.get("x-razorpay-signature")
        or request.headers.get("stripe-signature")
    )

    service = get_payment_service()
    res = await service.verify_webhook_event(
        provider=provider,
        payload=payload,
        signature=sig_header,
        raw_body=raw_body,
    )

    if not res.success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=res.error_message or res.error or "Webhook verification failed.",
        )

    return {
        "ok": True,
        "status": res.status,
        "trip_id": str(res.trip_id) if res.trip_id else None,
        "message": res.message,
    }


@router.get("/payment/pass/{trip_id}", tags=["Payment"])
async def get_pass_status(trip_id: UUID) -> dict[str, Any]:
    """Get the Trip Pass status for a trip."""
    service = get_payment_service()
    pass_rec = service.trip_pass_repo.get_by_trip_id(trip_id)
    if not pass_rec:
        return {
            "trip_id": str(trip_id),
            "status": "FREE",
            "unlocked": False,
        }
    return {
        "trip_id": str(trip_id),
        "status": pass_rec.status,
        "amount": float(pass_rec.amount),
        "currency": pass_rec.currency,
        "unlocked": pass_rec.is_unlocked,
        "provider": pass_rec.provider,
        "payment_reference": pass_rec.payment_reference,
    }


@router.post("/payment/demo_bypass", tags=["Payment"])
async def demo_bypass(request: Request) -> dict[str, Any]:
    """Demo / evaluator bypass endpoint to unlock a Trip Pass without actual payment."""
    from budlance.config import get_settings
    settings = get_settings()
    if settings.is_production:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Demo bypass is disabled in production environments.",
        )

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        ) from exc

    trip_id_raw = body.get("trip_id")
    if not trip_id_raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="trip_id is required.",
        )

    try:
        trip_id = UUID(str(trip_id_raw))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid UUID format for trip_id.",
        ) from exc

    chat_id = body.get("chat_id", 0)
    user_id = body.get("user_id")
    user_id_uuid = UUID(str(user_id)) if user_id else None

    service = get_payment_service()
    pass_rec = await service.bypass_trip_pass(
        trip_id=trip_id,
        chat_id=int(chat_id),
        user_id=user_id_uuid,
    )

    return {
        "ok": True,
        "status": pass_rec.status,
        "trip_id": str(pass_rec.trip_id),
        "unlocked": pass_rec.is_unlocked,
        "message": "Trip Pass successfully unlocked via demo bypass.",
    }


@router.get("/book/{booking_id}", response_class=HTMLResponse, tags=["Booking"])
async def booking_relay(booking_id: str):
    """Booking relay endpoint that looks up stored booking_request and returns an auto-submitting POST form HTML."""
    from budlance.db.repositories.cache_repo import CacheRepository

    repo = CacheRepository()
    booking_req = repo.get_booking_request(booking_id)
    if not booking_req or not isinstance(booking_req, dict):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Booking session expired or not found. Please search again on Google Flights.",
        )

    target_url = booking_req.get("url")
    if not target_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid booking request: target URL missing.",
        )

    post_data = booking_req.get("post_data")
    form_inputs: list[str] = []
    if isinstance(post_data, dict):
        for k, v in post_data.items():
            form_inputs.append(
                f'<input type="hidden" name="{html.escape(str(k))}" value="{html.escape(str(v))}">'
            )
    elif isinstance(post_data, str) and post_data.strip():
        parsed_qsl = urllib.parse.parse_qsl(post_data, keep_blank_values=True)
        if parsed_qsl:
            for k, v in parsed_qsl:
                form_inputs.append(
                    f'<input type="hidden" name="{html.escape(str(k))}" value="{html.escape(str(v))}">'
                )
        else:
            form_inputs.append(
                f'<input type="hidden" name="payload" value="{html.escape(post_data)}">'
            )

    inputs_html = "\n        ".join(form_inputs)
    html_page = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Redirecting to Booking...</title>
</head>
<body>
    <p>Redirecting to airline booking...</p>
    <form id="bookForm" method="POST" action="{html.escape(str(target_url))}">
        {inputs_html}
        <noscript>
            <button type="submit">Continue to Booking</button>
        </noscript>
    </form>
    <script>
        document.getElementById('bookForm').submit();
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_page)

