"""Conversation state repository — stores pending (incomplete) trip intents per chat_id.

Uses the existing search_cache table with engine='conversation_state' and a 24-hour TTL.
No schema changes required. This survives application restart as long as Supabase is live.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from supabase import Client

from budlance.ai.schemas import ParsedTripIntent
from budlance.db.client import get_supabase_client

logger = logging.getLogger(__name__)

_CONVERSATION_ENGINE = "conversation_state"
_HASH_PREFIX = "conv_state_chat_"
_TTL_HOURS = 24


def _chat_key(chat_id: int) -> str:
    return f"{_HASH_PREFIX}{chat_id}"


def _intent_to_dict(intent: ParsedTripIntent) -> dict:
    return {
        "budget": float(intent.budget) if intent.budget is not None else None,
        "currency": intent.currency,
        "people": intent.people,
        "days": intent.days,
        "origin": intent.origin,
        "destination": intent.destination,
        "interests": intent.interests,
        "travel_party": intent.travel_party,
        "traveler_type": intent.traveler_type,
        "transport_mode": intent.transport_mode,
        "transport_class": intent.transport_class,
        "booking_confirmed": intent.booking_confirmed,
        "pending_action": intent.pending_action,
        "reconciling_trip_id": intent.reconciling_trip_id,
    }


def _dict_to_intent(data: dict) -> ParsedTripIntent:
    return ParsedTripIntent(
        budget=Decimal(str(data["budget"])) if data.get("budget") is not None else None,
        currency=data.get("currency", "INR"),
        people=data.get("people"),
        days=data.get("days"),
        origin=data.get("origin"),
        destination=data.get("destination"),
        interests=data.get("interests", []),
        travel_party=data.get("travel_party") or (data.get("traveler_type") if data.get("traveler_type") in ("solo", "couple", "friends", "family", "relatives") else None),
        traveler_type=data.get("traveler_type"),
        transport_mode=data.get("transport_mode"),
        transport_class=data.get("transport_class"),
        booking_confirmed=bool(data.get("booking_confirmed", False)),
        pending_action=data.get("pending_action"),
        reconciling_trip_id=data.get("reconciling_trip_id"),
    )


class ConversationStateRepository:
    """Persist and retrieve pending (incomplete) trip intents per Telegram chat_id.

    Backed by the existing search_cache table with engine='conversation_state'.
    Falls back to in-memory storage when no Supabase client is available.
    """

    def __init__(self, client: Client | None = None) -> None:
        self._client = client or get_supabase_client()
        self._memory_store: dict[int, ParsedTripIntent] = {}

    def save_pending_intent(self, chat_id: int, intent: ParsedTripIntent) -> None:
        """Persist a pending (incomplete) trip intent for the given chat_id."""
        key = _chat_key(chat_id)
        intent_data = _intent_to_dict(intent)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=_TTL_HOURS)

        if self._client:
            try:
                self._client.table("search_cache").upsert(
                    {
                        "id": str(uuid4()),
                        "query_hash": key,
                        "engine": _CONVERSATION_ENGINE,
                        "params_json": {"chat_id": chat_id},
                        "response_data": intent_data,
                        "expires_at": expires_at.isoformat(),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    on_conflict="query_hash",
                ).execute()
                logger.debug(
                    "[CONVERSATION] Saved pending intent for chat_id=%s: missing=%s",
                    chat_id,
                    [f for f in ("budget", "people", "days", "origin") if intent_data.get(f) is None],
                )
            except Exception as exc:
                logger.warning(
                    "[CONVERSATION] Failed to save pending intent to Supabase for chat_id=%s: %s. "
                    "Using in-memory fallback.",
                    chat_id,
                    exc,
                )
                self._memory_store[chat_id] = intent
            return

        self._memory_store[chat_id] = intent

    def get_pending_intent(self, chat_id: int) -> ParsedTripIntent | None:
        """Retrieve a pending (incomplete) trip intent for the given chat_id, if it exists and has not expired."""
        key = _chat_key(chat_id)
        now = datetime.now(timezone.utc)

        if self._client:
            try:
                res = (
                    self._client.table("search_cache")
                    .select("*")
                    .eq("query_hash", key)
                    .eq("engine", _CONVERSATION_ENGINE)
                    .gt("expires_at", now.isoformat())
                    .limit(1)
                    .execute()
                )
                if res.data:
                    intent_data = res.data[0].get("response_data", {})
                    return _dict_to_intent(intent_data)
                return None
            except Exception as exc:
                logger.warning(
                    "[CONVERSATION] Failed to load pending intent from Supabase for chat_id=%s: %s. "
                    "Checking in-memory.",
                    chat_id,
                    exc,
                )
                return self._memory_store.get(chat_id)

        return self._memory_store.get(chat_id)

    def clear_pending_intent(self, chat_id: int) -> None:
        """Remove the pending intent once planning has completed (successfully or abandoned)."""
        key = _chat_key(chat_id)

        if self._client:
            try:
                self._client.table("search_cache").delete().eq("query_hash", key).execute()
            except Exception as exc:
                logger.warning(
                    "[CONVERSATION] Failed to clear pending intent from Supabase for chat_id=%s: %s",
                    chat_id,
                    exc,
                )
        self._memory_store.pop(chat_id, None)

    def save_reconciliation_state(
        self,
        chat_id: int,
        trip_id: UUID,
        planned_budget: Decimal | None = None,
        completion_reason: str | None = None,
    ) -> None:
        """Persist explicit pending LOG_ACTUAL_SPEND state for trip reconciliation."""
        reconcile_intent = ParsedTripIntent(
            pending_action="LOG_ACTUAL_SPEND",
            reconciling_trip_id=str(trip_id),
            budget=planned_budget,
            completion_reason=completion_reason,
        )
        self.save_pending_intent(chat_id, reconcile_intent)

    def is_reconciling(self, chat_id: int) -> bool:
        """Check if conversation is awaiting final reconciliation response."""
        intent = self.get_pending_intent(chat_id)
        return bool(intent and intent.pending_action == "LOG_ACTUAL_SPEND")

    def get_reconciling_trip_id(self, chat_id: int) -> UUID | None:
        """Retrieve trip ID undergoing reconciliation, if any."""
        intent = self.get_pending_intent(chat_id)
        if intent and intent.pending_action == "LOG_ACTUAL_SPEND" and intent.reconciling_trip_id:
            try:
                return UUID(intent.reconciling_trip_id)
            except (ValueError, TypeError):
                return None
        return None
