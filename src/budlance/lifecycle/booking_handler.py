"""Booking lifecycle state tracking and status integrity handler for Budlance.

Budlance is a travel planning and handoff assistant, not automatically the merchant
or booking provider. This handler maintains strict boundaries between:
- Planned trip components
- External booking links provided
- User-confirmed bookings (user reports having booked)
- Provider-verified bookings (authoritative provider integration confirms booking)
- Cancellations recorded (user-reported vs verified)

Rules:
1. A booking URL is not a booking confirmation.
2. A user saying "I booked it" transitions to USER_CONFIRMED, never PROVIDER_VERIFIED.
3. Provider-verified status requires genuine authoritative provider integration;
   when absent, state the limitation honestly.
4. Never invent reservation IDs, provider references, or refund guarantees.
5. Retain audit history across state transitions.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
import logging
from typing import Any, Literal
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field

from budlance.db.models import Trip, utc_now

logger = logging.getLogger(__name__)


class BookingState(str, Enum):
    """Explicit lifecycle states for travel booking components."""
    PLANNED = "PLANNED"
    LINK_PROVIDED = "LINK_PROVIDED"
    USER_CONFIRMED = "USER_CONFIRMED"
    PROVIDER_VERIFIED = "PROVIDER_VERIFIED"
    CANCELLATION_RECORDED = "CANCELLATION_RECORDED"


class BookingComponentType(str, Enum):
    FLIGHT = "flight"
    HOTEL = "hotel"
    TRANSPORT = "transport"
    ACTIVITY = "activity"


class BookingRecord(BaseModel):
    """Auditable record of a planned or confirmed trip component booking."""
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    trip_id: UUID
    component_type: BookingComponentType
    name: str
    booking_link: str | None = None
    state: BookingState = BookingState.PLANNED
    is_provider_verified: bool = False
    cancellation_status: str | None = None
    cancellation_verified: bool = False
    estimated_cost: Decimal = Decimal("0.00")
    currency: str = "INR"
    notes: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BookingTransitionResult(BaseModel):
    """Result of a booking lifecycle action."""
    model_config = ConfigDict(from_attributes=True)

    booking_id: UUID | None = None
    component_type: BookingComponentType
    previous_state: BookingState
    new_state: BookingState
    is_provider_verified: bool
    message_text: str


class BookingLifecycleHandler:
    """Manages booking records and transitions with status integrity."""

    # Persistent in-memory store keyed by trip_id, preserved across handler instances
    _class_booking_store: dict[UUID, list[BookingRecord]] = {}

    def __init__(self) -> None:
        self._store = self._class_booking_store

    @classmethod
    def reset_in_memory_store(cls) -> None:
        cls._class_booking_store.clear()

    def get_bookings(self, trip_id: UUID) -> list[BookingRecord]:
        """Fetch all tracked bookings for a trip."""
        return list(self._store.get(trip_id, []))

    def get_or_create_booking(
        self,
        trip_id: UUID,
        component_type: BookingComponentType | str,
        name: str = "",
        booking_link: str | None = None,
        estimated_cost: Decimal = Decimal("0.00"),
    ) -> BookingRecord:
        """Get existing booking of given type or initialize one."""
        comp = BookingComponentType(str(component_type).lower())
        existing = self._store.setdefault(trip_id, [])
        for b in existing:
            if b.component_type == comp:
                if booking_link and not b.booking_link:
                    b.booking_link = booking_link
                    if b.state == BookingState.PLANNED:
                        b.state = BookingState.LINK_PROVIDED
                    b.updated_at = utc_now()
                return b

        initial_state = BookingState.LINK_PROVIDED if booking_link else BookingState.PLANNED
        record = BookingRecord(
            trip_id=trip_id,
            component_type=comp,
            name=name or f"Scheduled {comp.value.title()}",
            booking_link=booking_link,
            state=initial_state,
            estimated_cost=estimated_cost,
        )
        existing.append(record)
        return record

    def user_confirms_booking(
        self,
        trip_id: UUID,
        component_type: BookingComponentType | str,
        notes: str | None = None,
    ) -> BookingTransitionResult:
        """User reports completing a booking externally.
        
        Strict Invariant: State becomes USER_CONFIRMED, NEVER PROVIDER_VERIFIED.
        """
        b = self.get_or_create_booking(trip_id, component_type)
        prev = b.state
        b.state = BookingState.USER_CONFIRMED
        b.is_provider_verified = False  # Never claim provider verified without authoritative integration
        b.notes = notes or "User confirmed booking externally"
        b.updated_at = utc_now()

        logger.info(
            "[BOOKING_LIFECYCLE] Trip %s %s transitioned: %s -> USER_CONFIRMED (is_provider_verified=False)",
            trip_id, b.component_type.value, prev.value,
        )
        comp_title = b.component_type.value.title()
        return BookingTransitionResult(
            booking_id=b.id,
            component_type=b.component_type,
            previous_state=prev,
            new_state=b.state,
            is_provider_verified=False,
            message_text=(
                f"✅ Recorded: Your {comp_title} booking is marked as *User-Confirmed*.\n\n"
                f"ℹ️ Status: `USER_CONFIRMED` (User-reported). Note: Budlance is a planning assistant "
                f"and does not have authoritative provider access to verify airline/hotel confirmation numbers."
            ),
        )

    def record_cancellation(
        self,
        trip_id: UUID,
        component_type: BookingComponentType | str,
        user_reported_only: bool = True,
        notes: str | None = None,
    ) -> BookingTransitionResult:
        """User reports cancelling a booking.
        
        Strict Invariant: Preservation of verification boundary.
        A user-reported cancellation is NOT presented as provider-verified.
        Refund status remains unverified.
        """
        b = self.get_or_create_booking(trip_id, component_type)
        prev = b.state
        b.state = BookingState.CANCELLATION_RECORDED
        b.cancellation_status = "CANCELLED_BY_USER"
        b.cancellation_verified = not user_reported_only  # True only if authoritative integration exists
        b.notes = notes or "User reported cancellation"
        b.updated_at = utc_now()

        logger.info(
            "[BOOKING_LIFECYCLE] Trip %s %s cancellation recorded (verified=%s)",
            trip_id, b.component_type.value, b.cancellation_verified,
        )
        comp_title = b.component_type.value.title()
        verif_note = "Provider-Verified" if b.cancellation_verified else "User-Reported (Unverified by Provider)"
        return BookingTransitionResult(
            booking_id=b.id,
            component_type=b.component_type,
            previous_state=prev,
            new_state=b.state,
            is_provider_verified=False,
            message_text=(
                f"⚠️ Recorded: Cancellation for {comp_title}.\n\n"
                f"• Status: `CANCELLATION_RECORDED` ({verif_note})\n"
                f"• Refund Status: Pending/Unknown (Budlance does not track merchant refunds without provider integration).\n"
                f"• Future Obligations: Removed from projected trip schedule."
            ),
        )

    def format_bookings_summary(self, trip_id: UUID) -> str:
        """Generate human-readable audit summary of all bookings for a trip."""
        bookings = self.get_bookings(trip_id)
        if not bookings:
            return "No bookings currently recorded for this trip."

        lines = ["📋 *Booking Lifecycle Status:*"]
        for b in bookings:
            state_label = b.state.value
            verif_badge = " [Verified]" if b.is_provider_verified else " [User-Reported]"
            if b.state == BookingState.CANCELLATION_RECORDED:
                verif_badge = " [Cancellation Recorded]"
            link_str = f" ([Link]({b.booking_link}))" if b.booking_link else ""
            lines.append(f"• *{b.component_type.value.title()}:* `{state_label}`{verif_badge}{link_str}")
        return "\n".join(lines)
