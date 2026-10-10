"""Expense lifecycle handler for recording user actual spending and managing day transitions."""

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

from budlance.ai.schemas import ParsedTripIntent
from budlance.db.models import Itinerary, LedgerCategory, Trip, utc_now
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import DuplicateLedgerEntryError, LedgerRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.ledger.manager import VirtualLedgerManager
from budlance.ledger.models import LedgerSummary
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class ExpenseResult(BaseModel):
    """Result container for expense logging and day lifecycle operations."""
    model_config = ConfigDict(from_attributes=True)

    trip_id: UUID | None = None
    status: str = Field(
        description="Lifecycle status: EXPENSE_LOGGED, NO_ACTIVE_TRIP, INVALID_EXPENSE, INVALID_DAY, ERROR.",
    )
    message_text: str = Field(
        default="",
        description="Concise user-facing response text for Telegram delivery.",
    )
    ledger_summary: LedgerSummary | None = None
    error: str | None = None


def map_expense_category_to_bucket(category: str | None) -> LedgerCategory:
    """Map natural-language expense category to authoritative A/B/C/D ledger bucket.

    Authoritative Budget Model:
    Bucket A (fixed_booking): Stay / accommodation, fixed transport
    Bucket B (daily_survival): Food / meals, in-trip transport
    Bucket C (activities): Sightseeing, activities, general discretionary
    Bucket D (rescue): Emergency reserve (in-trip disruption only)
    """
    if not category:
        return "activities"
    cat = category.strip().lower()
    if cat == "stay":
        return "fixed_booking"
    if cat in ("food", "transport"):
        return "daily_survival"
    if cat in ("activities", "general"):
        return "activities"
    return "activities"


def format_currency_amount(amount: Decimal) -> str:
    """Format decimal amount cleanly without unnecessary decimal trailing zeros."""
    if amount == int(amount):
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


def _update_itinerary_day_statuses(
    itinerary: Itinerary,
    completed_day: int | None = None,
    in_progress_day: int | None = None,
) -> None:
    """Update status of days in persisted itinerary schedule."""
    if not itinerary.days:
        return

    updated_days: list[Any] = []
    for day in itinerary.days:
        if isinstance(day, dict):
            d_copy = dict(day)
            d_num = d_copy.get("day_number")
            if completed_day is not None and d_num == completed_day:
                d_copy["status"] = "COMPLETED"
            elif in_progress_day is not None and d_num == in_progress_day:
                d_copy["status"] = "IN_PROGRESS"
            updated_days.append(d_copy)
        elif hasattr(day, "day_number"):
            d_num = getattr(day, "day_number", None)
            if completed_day is not None and d_num == completed_day:
                setattr(day, "status", "COMPLETED")
            elif in_progress_day is not None and d_num == in_progress_day:
                setattr(day, "status", "IN_PROGRESS")
            updated_days.append(day)
        else:
            updated_days.append(day)
    itinerary.days = updated_days


def reoptimize_hook_stub(*args: Any, **kwargs: Any) -> None:
    """Minimal integration point for future Task 4 re-optimization.

    Strictly unimplemented in Task 3 per architectural boundaries.
    """
    pass


class ExpenseLifecycleHandler:
    """Lifecycle handler for LOG_EXPENSE actions and day progression."""

    def __init__(
        self,
        trip_repo: TripRepository | None = None,
        ledger_repo: LedgerRepository | None = None,
        itinerary_repo: ItineraryRepository | None = None,
        ledger_manager: VirtualLedgerManager | None = None,
    ) -> None:
        self.trip_repo = trip_repo or TripRepository()
        self.ledger_repo = ledger_repo or LedgerRepository()
        self.itinerary_repo = itinerary_repo or ItineraryRepository()
        self.ledger_manager = ledger_manager or VirtualLedgerManager(self.ledger_repo)
        self._processed_events: dict[UUID, set[str]] = {}

    async def handle_log_expense(
        self,
        chat_id: int,
        parsed: ParsedTripIntent,
        trip: Trip | None = None,
        event_id: str | None = None,
    ) -> ExpenseResult:
        """Handle expense logging and optional day lifecycle progression.

        Invariants:
        1. Trip must exist and trip.status == "ACTIVE".
        2. Amount must be valid positive Decimal.
        3. Day must fall within 1 <= day <= trip.duration_days.
        4. actual_amount represents money actually spent (never conflated with planned_amount).
        5. Day completion and advancement occur ONLY when parsed.day_completed is True.
        6. Event idempotency: replaying identical event_id is suppressed, while distinct
           event_ids with identical amounts/descriptions remain separate genuine purchases.
        """
        # 1. Load active trip
        active_trip = trip or self.trip_repo.get_active_trip(chat_id)
        if not active_trip or str(active_trip.status).upper() != "ACTIVE":
            logger.info("[EXPENSE_LIFECYCLE] No active trip found for chat_id=%s", chat_id)
            return ExpenseResult(
                trip_id=None,
                status="NO_ACTIVE_TRIP",
                message_text="No active trip found to log expenses against. Please start or activate a trip first.",
                error="NO_ACTIVE_TRIP",
            )

        # 2. Validate amount
        if parsed.amount is None or parsed.amount <= Decimal("0.00"):
            logger.warning("[EXPENSE_LIFECYCLE] Invalid expense amount for chat_id=%s", chat_id)
            return ExpenseResult(
                trip_id=active_trip.id,
                status="INVALID_EXPENSE",
                message_text="Please specify a valid expense amount to log (e.g., 'Spent ₹500 on lunch').",
                error="INVALID_EXPENSE",
            )

        # 3. Resolve applicable day
        resolved_day = parsed.day_number if parsed.day_number is not None else active_trip.current_day

        # 4. Validate day within trip's valid range
        if resolved_day < 1 or resolved_day > active_trip.duration_days:
            logger.warning(
                "[EXPENSE_LIFECYCLE] Day %s outside range [1, %s] for trip_id=%s",
                resolved_day, active_trip.duration_days, active_trip.id,
            )
            return ExpenseResult(
                trip_id=active_trip.id,
                status="INVALID_DAY",
                message_text=(
                    f"Day {resolved_day} is outside the valid range for this trip "
                    f"(Day 1 to Day {active_trip.duration_days}). No expense was recorded."
                ),
                error="INVALID_DAY",
            )

        # 1.5 Handle expense reversal / correction if requested
        if getattr(parsed, "reversal_target_amount", None) is not None:
            reversal_entry = self.ledger_repo.record_expense_reversal(
                trip_id=active_trip.id,
                target_amount=parsed.reversal_target_amount,
                category=parsed.expense_category,
                reason=getattr(parsed, "reversal_reason", None),
            )
            if reversal_entry:
                summary = self.ledger_manager.get_summary(active_trip.id)
                formatted_rev = format_currency_amount(parsed.reversal_target_amount)
                return ExpenseResult(
                    trip_id=active_trip.id,
                    status="EXPENSE_LOGGED",
                    message_text=f"Reversed expense of ₹{formatted_rev} for Day {resolved_day}.\n\nDay {resolved_day} is still active.",
                    ledger_summary=summary,
                )
            return ExpenseResult(
                trip_id=active_trip.id,
                status="INVALID_EXPENSE",
                message_text=f"Could not find matching recorded expense of ₹{parsed.reversal_target_amount} to reverse.",
                error="REVERSAL_TARGET_NOT_FOUND",
            )

        # 5. Map to A/B/C/D ledger bucket and check idempotency
        effective_event_id = event_id or getattr(parsed, "event_id", None)
        existing_entries = self.ledger_repo.get_ledger_entries(active_trip.id)
        processed_set = self._processed_events.setdefault(active_trip.id, set())

        # Check multi-expense vs single expense
        expense_items = parsed.expenses if getattr(parsed, "expenses", None) and len(parsed.expenses) > 1 else None

        if effective_event_id:
            # Stable incoming-message / update identifier present.
            # Replaying the SAME event identity must NOT create duplicate spending.
            is_duplicate_event = (
                effective_event_id in processed_set
                or self.ledger_repo.has_event_id(active_trip.id, effective_event_id)
                or any(f"[evt:{effective_event_id}]" in (e.description or "") or f"[evt:{effective_event_id}:" in (e.description or "") for e in existing_entries)
            )
            if is_duplicate_event:
                logger.info(
                    "[EXPENSE_LIFECYCLE] Suppressed duplicate event replay (event_id=%s) for Day %s",
                    effective_event_id, resolved_day,
                )
                formatted_amt = format_currency_amount(parsed.amount)
                msg = f"Expense of ₹{formatted_amt} for Day {resolved_day} was already recorded.\n\nDay {resolved_day} is still active."
                return ExpenseResult(
                    trip_id=active_trip.id,
                    status="EXPENSE_LOGGED",
                    message_text=msg,
                    ledger_summary=self.ledger_manager.get_summary(active_trip.id),
                )

            processed_set.add(effective_event_id)

        # 6. Record spending in Virtual Ledger
        if expense_items:
            # Multiple expenses in a single event: committed atomically in a single transaction
            batch_tuples = []
            for idx, item in enumerate(expense_items):
                b = map_expense_category_to_bucket(item.category)
                i_desc = (item.description or item.category).strip().capitalize()
                entry_desc = f"Day {resolved_day} {i_desc}"
                if effective_event_id:
                    entry_desc += f" [evt:{effective_event_id}:{idx}]"
                batch_tuples.append((b, item.amount, entry_desc, resolved_day))

            try:
                self.ledger_manager.record_spending_batch(
                    trip_id=active_trip.id,
                    items=batch_tuples,
                    source="user_reported",
                )
            except DuplicateLedgerEntryError:
                # Atomic transaction failure: storage committed zero rows
                logger.info(
                    "[EXPENSE_LIFECYCLE] Persistent store rejected duplicate multi-expense replay (event_id=%s)",
                    effective_event_id,
                )
                total_amt = sum(it.amount for it in expense_items)
                formatted_amt = format_currency_amount(total_amt)
                msg = f"Expense of ₹{formatted_amt} for Day {resolved_day} was already recorded.\n\nDay {resolved_day} is still active."
                return ExpenseResult(
                    trip_id=active_trip.id,
                    status="EXPENSE_LOGGED",
                    message_text=msg,
                    ledger_summary=self.ledger_manager.get_summary(active_trip.id),
                )
            total_amt = sum(it.amount for it in expense_items)
            formatted_amt = format_currency_amount(total_amt)
            parts_desc = " and ".join(f"₹{format_currency_amount(it.amount)} on {it.description or it.category}" for it in expense_items)
            spend_prefix = f"Recorded {parts_desc} for Day {resolved_day}."
        else:
            # Single expense path
            bucket = map_expense_category_to_bucket(parsed.expense_category)
            cat_desc = (parsed.expense_category or "expense").strip().capitalize()
            base_desc = f"Day {resolved_day} {cat_desc} expense"

            if effective_event_id:
                entry_description = f"{base_desc} [evt:{effective_event_id}]"
            else:
                entry_description = base_desc
                for e in existing_entries:
                    if (
                        e.actual_amount == parsed.amount
                        and e.category == bucket
                        and e.day_number == resolved_day
                        and e.description == entry_description
                        and (utc_now() - e.created_at).total_seconds() < 60
                    ):
                        logger.info(
                            "[EXPENSE_LIFECYCLE] Suppressed duplicate expense ₹%s for Day %s (within 60s, no event_id)",
                            parsed.amount, resolved_day,
                        )
                        formatted_amt = format_currency_amount(parsed.amount)
                        msg = f"Expense of ₹{formatted_amt} for Day {resolved_day} was already recorded.\n\nDay {resolved_day} is still active."
                        return ExpenseResult(
                            trip_id=active_trip.id,
                            status="EXPENSE_LOGGED",
                            message_text=msg,
                            ledger_summary=self.ledger_manager.get_summary(active_trip.id),
                        )

            try:
                self.ledger_manager.record_spending(
                    trip_id=active_trip.id,
                    category=bucket,
                    amount=parsed.amount,
                    description=entry_description,
                    source="user_reported",
                    actual_amount=parsed.amount,
                    day_number=resolved_day,
                )
            except DuplicateLedgerEntryError:
                logger.info(
                    "[EXPENSE_LIFECYCLE] Persistent store rejected duplicate expense replay (event_id=%s)",
                    effective_event_id,
                )
                formatted_amt = format_currency_amount(parsed.amount)
                msg = f"Expense of ₹{formatted_amt} for Day {resolved_day} was already recorded.\n\nDay {resolved_day} is still active."
                return ExpenseResult(
                    trip_id=active_trip.id,
                    status="EXPENSE_LOGGED",
                    message_text=msg,
                    ledger_summary=self.ledger_manager.get_summary(active_trip.id),
                )
            formatted_amt = format_currency_amount(parsed.amount)
            spend_prefix = f"Recorded ₹{formatted_amt} for Day {resolved_day}."

        is_day_complete = bool(getattr(parsed, "complete_day", False) or getattr(parsed, "day_completed", False))
        if not is_day_complete:
            msg = f"{spend_prefix}\n\nDay {resolved_day} is still active."
        else:
            # Explicit day completion requested
            itinerary_record = self.itinerary_repo.get_itinerary(active_trip.id)

            if resolved_day == active_trip.current_day:
                # Completing the current active day
                if resolved_day < active_trip.duration_days:
                    next_day = resolved_day + 1
                    # Advance current day pointer
                    self.trip_repo.update_current_day(active_trip.id, next_day)

                    # Update day statuses in itinerary
                    if itinerary_record:
                        _update_itinerary_day_statuses(
                            itinerary=itinerary_record,
                            completed_day=resolved_day,
                            in_progress_day=next_day,
                        )
                        self.itinerary_repo.save_itinerary(itinerary_record)

                    msg = (
                        f"Recorded ₹{formatted_amt} for Day {resolved_day}.\n\n"
                        f"Day {resolved_day} completed. Moving to Day {next_day}."
                    )
                    logger.info(
                        "[EXPENSE_LIFECYCLE] Day %s completed. Advanced to Day %s.",
                        resolved_day, next_day,
                    )
                else:
                    # Final day completion — do NOT mark trip COMPLETED in Task 3
                    if itinerary_record:
                        _update_itinerary_day_statuses(
                            itinerary=itinerary_record,
                            completed_day=resolved_day,
                        )
                        self.itinerary_repo.save_itinerary(itinerary_record)

                    msg = f"Recorded ₹{formatted_amt} for Day {resolved_day}.\n\nDay {resolved_day} completed."
                    logger.info(
                        "[EXPENSE_LIFECYCLE] Final Day %s completed. Trip remains ACTIVE.",
                        resolved_day,
                    )
            elif resolved_day < active_trip.current_day:
                # Historical day completion — mark day completed without moving current_day pointer
                if itinerary_record:
                    _update_itinerary_day_statuses(
                        itinerary=itinerary_record,
                        completed_day=resolved_day,
                    )
                    self.itinerary_repo.save_itinerary(itinerary_record)

                msg = (
                    f"Recorded ₹{formatted_amt} for Day {resolved_day}.\n\n"
                    f"Day {resolved_day} completed. Current day remains Day {active_trip.current_day}."
                )
                logger.info(
                    "[EXPENSE_LIFECYCLE] Historical Day %s completed. Current day remains %s.",
                    resolved_day, active_trip.current_day,
                )
            else:
                # Future day completion attempt — record expense but do not advance current_day
                msg = (
                    f"Recorded ₹{formatted_amt} for Day {resolved_day}.\n\n"
                    f"Day {active_trip.current_day} is still active."
                )
                logger.info(
                    "[EXPENSE_LIFECYCLE] Future Day %s expense recorded. Current day %s unchanged.",
                    resolved_day, active_trip.current_day,
                )

        try:
            summary = self.ledger_manager.get_summary(active_trip.id)
        except Exception:
            summary = None

        return ExpenseResult(
            trip_id=active_trip.id,
            status="EXPENSE_LOGGED",
            ledger_summary=summary,
            message_text=msg,
        )


async def handle_log_expense(
    chat_id: int,
    parsed: ParsedTripIntent,
    trip_repo: TripRepository | None = None,
    ledger_repo: LedgerRepository | None = None,
    itinerary_repo: ItineraryRepository | None = None,
    ledger_manager: VirtualLedgerManager | None = None,
    trip: Trip | None = None,
    event_id: str | None = None,
) -> ExpenseResult:
    """Module-level entry point equivalent to handle_log_expense(chat_id, parsed)."""
    handler = ExpenseLifecycleHandler(
        trip_repo=trip_repo,
        ledger_repo=ledger_repo,
        itinerary_repo=itinerary_repo,
        ledger_manager=ledger_manager,
    )
    return await handler.handle_log_expense(chat_id=chat_id, parsed=parsed, trip=trip, event_id=event_id)
