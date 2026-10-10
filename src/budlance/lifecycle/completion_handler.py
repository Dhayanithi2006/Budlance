"""Trip completion and final actual-spend reconciliation lifecycle handler.

Implements the final lifecycle stage:
ACTIVE
  ↓
User explicitly completes trip (TRIP_COMPLETE)
  ↓
Prompt for final reconciliation (pending_action = LOG_ACTUAL_SPEND)
  ↓
User provides final actual spend OR says skip
  ↓
Authoritative ledger update (no double counting, historical rows preserved)
  ↓
Trip status = COMPLETED, is_active = False, completion_reason set
  ↓
Conversation state cleared → Ready for completely clean NEW_TRIP
"""

from decimal import Decimal
import logging
import re
from typing import Any
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field

from budlance.db.models import LedgerEntry, Trip, utc_now
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.ledger_repo import LedgerRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.ledger.manager import VirtualLedgerManager
from budlance.ledger.models import LedgerSummary

logger = logging.getLogger(__name__)


def format_currency_amount(amount: Decimal) -> str:
    """Format decimal amount cleanly without unnecessary trailing decimals."""
    if amount == int(amount):
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


def extract_reconciliation_amount(text: str) -> Decimal | None:
    """Extract a positive numeric reconciliation amount from user text.

    Supports:
    - ₹18,450 / rs 18450 / inr 18450
    - 18.5k / 18k
    - 18,450 / 18450 / 18450.00
    - 'spent 18450' / 'total 18450' / 'final spend was 18450'
    """
    clean = text.lower().strip()

    # 1. k notation e.g. 18.5k, 20k
    k_match = re.search(r"\b(\d+(?:\.\d+)?)\s*k\b", clean)
    if k_match:
        try:
            val = Decimal(str(float(k_match.group(1)) * 1000))
            if val > Decimal("0"):
                return val
        except Exception:
            pass

    # 2. Currency symbol + digits or spending phrase e.g. ₹18,450, rs. 18450, spent 18450
    m = re.search(
        r"(?:spent|spend|used|cost|paid|total|actual|amount|₹|rs\.?|inr|rupees?)?\s*(?:was|is|about|around)?\s*(?:₹|rs\.?|inr|rupees?)?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)",
        clean,
    )
    if m:
        try:
            raw_num = m.group(1).replace(",", "")
            val = Decimal(raw_num)
            if val > Decimal("0"):
                return val
        except Exception:
            pass

    # 3. Pure number e.g. 18450 or 18450.50
    m_pure = re.fullmatch(r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)[.!]?", clean)
    if m_pure:
        try:
            val = Decimal(m_pure.group(1).replace(",", ""))
            if val > Decimal("0"):
                return val
        except Exception:
            pass

    return None


def is_skip_response(text: str) -> bool:
    """Return True if user explicitly expresses skipping reconciliation."""
    clean = text.lower().strip().rstrip(".!?,")
    skip_exact = {
        "skip", "no", "skip it", "skip reconciliation", "no thanks",
        "nope", "don't want to", "dont want to", "none", "na",
        "not now", "later", "cancel", "ignore", "pass", "just close it",
        "close it", "done",
    }
    if clean in skip_exact:
        return True
    if clean.startswith("skip"):
        return True
    return False


def is_new_trip_message(text: str) -> bool:
    """Detect if text is an explicit new trip planning request."""
    clean = text.lower().strip()
    if any(p in clean for p in (
        "add ", "one more place", "places i want to visit", "place i want to visit", "to my plan",
        "move the trip", "shift the trip", "change dates", "keep everything else",
    )):
        return False
    signals = [
        "plan a trip", "plan another trip", "new trip", "start over",
        "trip to", "want to visit", "want to go", "travel to",
        "start fresh", "another trip", "recommend",
    ]
    return any(sig in clean for sig in signals)


class CompletionResult(BaseModel):
    """Result container for trip completion and reconciliation operations."""
    model_config = ConfigDict(from_attributes=True)

    trip_id: UUID | None = None
    status: str = Field(
        description="Lifecycle status: PENDING_RECONCILIATION, COMPLETED, NO_ACTIVE_TRIP, ERROR.",
    )
    message_text: str = Field(
        default="",
        description="User-facing summary text ready for Telegram delivery.",
    )
    ledger_summary: LedgerSummary | None = None
    planned_budget: Decimal | None = None
    recorded_actual_spend: Decimal | None = None
    final_variance: Decimal | None = None
    reconciliation_skipped: bool = False
    completion_reason: str | None = None
    error: str | None = None


class TripCompletionHandler:
    """Lifecycle handler for TRIP_COMPLETE, actual-spend reconciliation, and clean wrap-up."""

    def __init__(
        self,
        trip_repo: TripRepository,
        ledger_repo: LedgerRepository,
        conversation_repo: ConversationStateRepository,
        ledger_manager: VirtualLedgerManager | None = None,
    ) -> None:
        self.trip_repo = trip_repo
        self.ledger_repo = ledger_repo
        self.conversation_repo = conversation_repo
        self.ledger_manager = ledger_manager or VirtualLedgerManager(ledger_repo=ledger_repo)

    async def handle_trip_complete(
        self,
        chat_id: int,
        trip: Trip | None = None,
        completion_reason: str | None = None,
    ) -> CompletionResult:
        """Handle TRIP_COMPLETE intent.

        Verifies active trip exists, calculates recorded actual spend, sets pending
        LOG_ACTUAL_SPEND state, and returns concise reconciliation prompt.
        """
        active_trip = trip or self.trip_repo.get_active_trip(chat_id)
        if not active_trip or str(active_trip.status).upper() != "ACTIVE":
            logger.info("[COMPLETION] No active trip found for chat_id=%s", chat_id)
            return CompletionResult(
                trip_id=None,
                status="NO_ACTIVE_TRIP",
                message_text="No active trip found to complete. Please start or activate a trip first.",
                error="NO_ACTIVE_TRIP",
            )

        # Calculate recorded actual spending strictly from non-null actual_amount
        entries = self.ledger_repo.get_ledger_entries(active_trip.id)
        actual_entries = [e for e in entries if e.actual_amount is not None]
        recorded_actual = sum(e.actual_amount for e in actual_entries) if actual_entries else Decimal("0.00")

        # Set pending reconciliation state
        self.conversation_repo.save_reconciliation_state(
            chat_id=chat_id,
            trip_id=active_trip.id,
            planned_budget=active_trip.budget_total,
            completion_reason=completion_reason,
            origin=active_trip.origin,
            destination=active_trip.destination,
            people=active_trip.people_count,
            days=active_trip.duration_days,
            currency=active_trip.currency,
        )

        prompt_text = (
            f"Your planned trip budget was ₹{format_currency_amount(active_trip.budget_total)}. "
            f"Would you like to record your final actual spend? You can also say skip."
        )

        return CompletionResult(
            trip_id=active_trip.id,
            status="PENDING_RECONCILIATION",
            message_text=prompt_text,
            planned_budget=active_trip.budget_total,
            recorded_actual_spend=recorded_actual,
            completion_reason=completion_reason,
        )

    async def handle_reconcile_amount(
        self,
        chat_id: int,
        amount: Decimal,
        trip: Trip | None = None,
        completion_reason: str | None = None,
    ) -> CompletionResult:
        """Record final actual spend amount, update ledger without double counting, and mark COMPLETED."""
        reconciling_id = self.conversation_repo.get_reconciling_trip_id(chat_id)
        active_trip = trip or (self.trip_repo.get_trip(reconciling_id) if reconciling_id else None) or self.trip_repo.get_active_trip(chat_id)

        if not active_trip:
            logger.warning("[COMPLETION] No active or reconciling trip found for chat_id=%s", chat_id)
            return CompletionResult(
                trip_id=None,
                status="NO_ACTIVE_TRIP",
                message_text="No active trip found for reconciliation.",
                error="NO_ACTIVE_TRIP",
            )

        if amount <= Decimal("0.00"):
            return CompletionResult(
                trip_id=active_trip.id,
                status="ERROR",
                message_text="Please provide a valid positive amount for final reconciliation.",
                error="INVALID_AMOUNT",
            )

        # 1. Retrieve current ledger entries and sum already recorded actual spending
        entries = self.ledger_repo.get_ledger_entries(active_trip.id)
        actual_entries = [e for e in entries if e.actual_amount is not None]
        already_recorded = sum(e.actual_amount for e in actual_entries) if actual_entries else Decimal("0.00")

        # 2. Prevent double-counting: add adjustment for the unreconciled portion
        adjustment = amount - already_recorded
        if adjustment != Decimal("0.00"):
            adj_entry = LedgerEntry(
                id=uuid4(),
                trip_id=active_trip.id,
                category="activities",
                description="Final reconciliation adjustment",
                allocated_amount=Decimal("0.00"),
                planned_amount=Decimal("0.00"),
                spent_amount=adjustment,
                remaining_amount=Decimal("0.00"),
                actual_amount=adjustment,
                source="user_reported",
                created_at=utc_now(),
            )
            self.ledger_repo.add_ledger_entry(adj_entry)

        # 3. Calculate authoritative final actual spend directly from ledger
        all_entries = self.ledger_repo.get_ledger_entries(active_trip.id)
        final_actual_spent = sum(e.actual_amount for e in all_entries if e.actual_amount is not None)

        # Determine completion reason: caller-provided > pending conversation reason > USER_CONFIRMED
        pending = self.conversation_repo.get_pending_intent(chat_id)
        final_reason = completion_reason or (pending.completion_reason if pending else None)
        if final_reason is None:
            final_reason = "USER_CONFIRMED"

        # 4. Mark trip COMPLETED and clear active-trip pointer
        self.trip_repo.update_trip_status(
            trip_id=active_trip.id,
            status="COMPLETED",
            completion_reason=final_reason,
            is_active=False,
        )

        # 5. Clear pending conversation state
        self.conversation_repo.clear_pending_intent(chat_id)

        # 6. Format final summary
        variance = active_trip.budget_total - final_actual_spent
        if variance > Decimal("0.00"):
            diff_line = f"Difference: ₹{format_currency_amount(variance)} under planned budget"
        elif variance < Decimal("0.00"):
            diff_line = f"Difference: ₹{format_currency_amount(abs(variance))} over planned budget"
        else:
            diff_line = "Difference: ₹0 on planned budget"

        cat_spent: dict[str, Decimal] = {}
        for e in all_entries:
            if e.actual_amount is not None and e.actual_amount > Decimal("0.00"):
                cat_spent[e.category] = cat_spent.get(e.category, Decimal("0.00")) + e.actual_amount

        ledger_summary = None
        unspent_reserve = Decimal("0.00")
        try:
            ledger_summary = self.ledger_manager.get_summary(active_trip.id)
            unspent_reserve = getattr(ledger_summary, "rescue_reserve_remaining", None)
            if unspent_reserve is None and ledger_summary and getattr(ledger_summary, "allocation", None):
                unspent_reserve = getattr(ledger_summary.allocation, "rescue_fund_allocated", Decimal("0.00"))
        except Exception:
            pass
        if unspent_reserve is None:
            unspent_reserve = Decimal("0.00")

        summary_parts = [
            f"🎉 Trip completed and reconciled!\n",
            f"Planned budget: ₹{format_currency_amount(active_trip.budget_total)}",
            f"Recorded actual spend: ₹{format_currency_amount(final_actual_spent)}",
            f"{diff_line}",
        ]
        if cat_spent:
            summary_parts.append("\n📊 *Actual Spending by Category:*")
            for cat, amt in cat_spent.items():
                cat_label = cat.replace("_", " ").title()
                summary_parts.append(f"• {cat_label}: ₹{format_currency_amount(amt)}")

        if unspent_reserve > Decimal("0.00"):
            summary_parts.append(f"\n🛡️ *Unspent Reserve:* ₹{format_currency_amount(unspent_reserve)}")

        summary_parts.append("\nℹ️ *Disclosure:* Recorded actual spending is user-reported. Days without recorded expenses are not assumed to be zero.")
        summary = "\n".join(summary_parts)

        return CompletionResult(
            trip_id=active_trip.id,
            status="COMPLETED",
            message_text=summary,
            planned_budget=active_trip.budget_total,
            recorded_actual_spend=final_actual_spent,
            final_variance=variance,
            completion_reason=final_reason,
        )

    async def handle_skip_reconciliation(
        self,
        chat_id: int,
        trip: Trip | None = None,
        completion_reason: Any = None,
    ) -> CompletionResult:
        """Finalize trip with skip semantics: preserve recorded expenses, do not invent amounts."""
        reconciling_id = self.conversation_repo.get_reconciling_trip_id(chat_id)
        active_trip = trip or (self.trip_repo.get_trip(reconciling_id) if reconciling_id else None) or self.trip_repo.get_active_trip(chat_id)

        if not active_trip:
            logger.warning("[COMPLETION] No active or reconciling trip found for chat_id=%s", chat_id)
            return CompletionResult(
                trip_id=None,
                status="NO_ACTIVE_TRIP",
                message_text="No active trip found to complete.",
                error="NO_ACTIVE_TRIP",
            )

        # Calculate existing recorded actual spending (never use planned_amount)
        entries = self.ledger_repo.get_ledger_entries(active_trip.id)
        actual_entries = [e for e in entries if e.actual_amount is not None]
        recorded_actual = sum(e.actual_amount for e in actual_entries) if actual_entries else Decimal("0.00")

        # Determine completion reason: caller-provided > pending conversation reason > USER_SKIPPED_RECONCILIATION
        if completion_reason is not None:
            final_reason = completion_reason
        else:
            pending = self.conversation_repo.get_pending_intent(chat_id)
            final_reason = pending.completion_reason if pending and pending.completion_reason else "USER_SKIPPED_RECONCILIATION"

        # Mark trip COMPLETED and clear active-trip pointer
        self.trip_repo.update_trip_status(
            trip_id=active_trip.id,
            status="COMPLETED",
            completion_reason=final_reason,
            is_active=False,
        )

        # Clear pending conversation state
        self.conversation_repo.clear_pending_intent(chat_id)

        summary = (
            f"🎉 Trip completed!\n\n"
            f"Planned budget: ₹{format_currency_amount(active_trip.budget_total)}\n"
            f"Recorded actual spend: ₹{format_currency_amount(recorded_actual)}\n"
            f"Final reconciliation: skipped\n\n"
            f"ℹ️ *Disclosure:* Recorded actual spending is user-reported. Budlance preserves recorded amounts without assuming unrecorded items were zero."
        )

        return CompletionResult(
            trip_id=active_trip.id,
            status="COMPLETED",
            message_text=summary,
            planned_budget=active_trip.budget_total,
            recorded_actual_spend=recorded_actual,
            reconciliation_skipped=True,
            completion_reason=final_reason,
        )

    async def complete_trip(
        self,
        chat_id: int,
        trip_id: UUID | None = None,
        completion_reason: str | None = None,
    ) -> CompletionResult:
        """Complete a trip directly with optional completion_reason, setting status=COMPLETED and is_active=False."""
        target_trip = (self.trip_repo.get_trip(trip_id) if trip_id else None) or self.trip_repo.get_active_trip(chat_id)
        if not target_trip:
            return CompletionResult(
                trip_id=None,
                status="NO_ACTIVE_TRIP",
                message_text="No active trip found to complete.",
                error="NO_ACTIVE_TRIP",
            )
        self.trip_repo.update_trip_status(
            trip_id=target_trip.id,
            status="COMPLETED",
            completion_reason=completion_reason,
            is_active=False,
        )
        self.conversation_repo.clear_pending_intent(chat_id)
        return CompletionResult(
            trip_id=target_trip.id,
            status="COMPLETED",
            message_text="Trip completed.",
            completion_reason=completion_reason,
        )


async def handle_trip_complete(
    chat_id: int,
    trip_repo: TripRepository | None = None,
    ledger_repo: LedgerRepository | None = None,
    conversation_repo: ConversationStateRepository | None = None,
    trip: Trip | None = None,
    completion_reason: str | None = None,
) -> CompletionResult:
    """Module-level convenience function for trip completion."""
    handler = TripCompletionHandler(
        trip_repo=trip_repo or TripRepository(),
        ledger_repo=ledger_repo or LedgerRepository(),
        conversation_repo=conversation_repo or ConversationStateRepository(),
    )
    return await handler.handle_trip_complete(chat_id=chat_id, trip=trip, completion_reason=completion_reason)
