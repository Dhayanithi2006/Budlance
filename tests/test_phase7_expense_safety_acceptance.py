"""Comprehensive safety acceptance tests for Budlance Phase 7:
Expense Ingestion Deduplication, Storage-Level Atomicity, and Financial Integrity.

Verification criteria:
1. Simultaneous duplicate submissions across separate application instances.
2. Replay after repository/handler reconstruction.
3. Distinct compound expenses from a single Telegram update.
4. Legitimate separate events with identical amounts and descriptions.
5. Reversal and correction integrity following duplicate submission attempts.
6. Trip completion reconciliation accuracy with no double-counting.
7. Atomic rollback integrity on multi-expense batch collision.
"""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4
import pytest

from budlance.ai.service import AIIntentService
from budlance.db.models import BudgetAllocation, LedgerEntry, Trip, utc_now
from budlance.db.repositories.conversation_repo import ConversationStateRepository
from budlance.db.repositories.intent_repo import IntentRepository
from budlance.db.repositories.itinerary_repo import ItineraryRepository
from budlance.db.repositories.ledger_repo import (
    DuplicateLedgerEntryError,
    LedgerRepository,
    derive_expense_entry_id,
    extract_event_tag,
)
from budlance.db.repositories.rescue_repo import RescueRepository
from budlance.db.repositories.trip_pass_repo import TripPassRepository
from budlance.db.repositories.trip_repo import TripRepository
from budlance.db.repositories.user_repo import UserRepository
from budlance.ledger.manager import VirtualLedgerManager
from budlance.lifecycle.booking_handler import BookingLifecycleHandler
from budlance.lifecycle.completion_handler import TripCompletionHandler
from budlance.orchestrator.orchestrator import BudlanceOrchestrator
from budlance.serpapi.models import DataSource, TravelDataEnvelope


@pytest.fixture(autouse=True)
def reset_stores():
    """Reset class-level stores before and after each test."""
    LedgerRepository.reset_in_memory_store()
    BookingLifecycleHandler.reset_in_memory_store()
    yield
    LedgerRepository.reset_in_memory_store()
    BookingLifecycleHandler.reset_in_memory_store()


@pytest.fixture
def base_repos():
    return {
        "user_repo": UserRepository(client=None),
        "trip_repo": TripRepository(client=None),
        "intent_repo": IntentRepository(client=None),
        "itinerary_repo": ItineraryRepository(client=None),
        "ledger_repo": LedgerRepository(client=None),
        "rescue_repo": RescueRepository(client=None),
        "conversation_repo": ConversationStateRepository(client=None),
        "trip_pass_repo": TripPassRepository(client=None),
    }


@pytest.fixture
def active_trip(base_repos):
    """Seed an active trip with baseline budget allocations."""
    user = base_repos["user_repo"].get_or_create_user(telegram_user_id=778899)
    trip = base_repos["trip_repo"].create_trip(
        user_id=user.id,
        telegram_chat_id=778899,
        budget_total=Decimal("30000.00"),
        destination="Munnar",
        origin="Chennai",
        currency="INR",
        people_count=2,
        duration_days=3,
        is_active=True,
    )
    base_repos["trip_repo"].update_trip_status(trip.id, "ACTIVE", is_active=True)
    trip = base_repos["trip_repo"].get_trip(trip.id)

    alloc = BudgetAllocation(
        id=uuid4(),
        trip_id=trip.id,
        transport_allocated=Decimal("8000.00"),
        stay_allocated=Decimal("10000.00"),
        food_allocated=Decimal("6000.00"),
        activities_discretionary=Decimal("3000.00"),
        rescue_fund_allocated=Decimal("3000.00"),
        total_budget=Decimal("30000.00"),
    )
    base_repos["ledger_repo"].save_budget_allocation(alloc)
    return trip


def _mock_cache_manager():
    mock_cache = AsyncMock()
    async def _get(engine: str, **kwargs):
        return TravelDataEnvelope(
            source=DataSource.LIVE,
            engine=engine,
            query_hash="qh",
            data={},
            is_fallback=False,
        )
    mock_cache.get_travel_data = AsyncMock(side_effect=_get)
    return mock_cache


def create_orchestrator(repos) -> BudlanceOrchestrator:
    ai = AIIntentService(use_mock=True)
    cache = _mock_cache_manager()
    return BudlanceOrchestrator(
        user_repo=repos["user_repo"],
        trip_repo=repos["trip_repo"],
        intent_repo=repos["intent_repo"],
        itinerary_repo=repos["itinerary_repo"],
        ledger_repo=repos["ledger_repo"],
        rescue_repo=repos["rescue_repo"],
        conversation_repo=repos["conversation_repo"],
        trip_pass_repo=repos["trip_pass_repo"],
        ai_service=ai,
        cache_manager=cache,
        enable_trip_pass=True,
    )


# =========================================================================
# Scenario 1: Deterministic UUIDv5 and Event Tag Extraction
# =========================================================================

def test_deterministic_uuidv5_and_event_tag_extraction():
    """Verify that compound event tags are extracted accurately and yield deterministic UUIDv5 primary keys."""
    trip_id = uuid4()
    tag1 = "tg_upd_12345_msg_678:0"
    tag2 = "tg_upd_12345_msg_678:1"

    id1_a = derive_expense_entry_id(trip_id, tag1)
    id1_b = derive_expense_entry_id(trip_id, tag1)
    id2 = derive_expense_entry_id(trip_id, tag2)

    # Determinism: exact same UUID for identical (trip_id, compound_event_tag)
    assert id1_a == id1_b
    assert isinstance(id1_a, UUID)
    # Distinctness: different compound indices yield different UUIDs
    assert id1_a != id2

    # Tag extraction
    desc = "Lunch at cafe [evt:tg_upd_12345_msg_678:0]"
    assert extract_event_tag(desc) == "tg_upd_12345_msg_678:0"
    assert extract_event_tag("Unrelated expense without tag") is None
    assert extract_event_tag("") is None
    assert extract_event_tag(None) is None


# =========================================================================
# Scenario 2: Simultaneous Duplicate Submissions Across Separate Instances
# =========================================================================

@pytest.mark.asyncio
async def test_simultaneous_duplicate_submissions_separate_instances(active_trip, base_repos):
    """When multiple separate application instances/handlers process the exact same event concurrently:
    - Persistent storage atomically rejects duplicate inserts with DuplicateLedgerEntryError.
    - Handlers catch this and return idempotent responses.
    - Exactly 1 row is created; total spent amount is never multiplied.
    """
    trip_id = active_trip.id
    chat_id = active_trip.telegram_chat_id
    event_id = "tg_upd_5001_msg_1001"

    # Create 5 distinct orchestrator and handler instances sharing the persistent repository layer
    instances = [create_orchestrator(base_repos) for _ in range(5)]

    # Concurrently execute handle_user_message with the exact same event_id
    tasks = [
        inst.handle_user_message(
            telegram_user_id=chat_id,
            chat_id=chat_id,
            message="Spent ₹650 on lunch",
            event_id=event_id,
        )
        for inst in instances
    ]
    results = await asyncio.gather(*tasks)

    # All instances should return successfully (one logged, rest deduplicated)
    statuses = [r.status for r in results]
    assert "EXPENSE_LOGGED" in statuses
    # The deduplicated ones should return EXPENSE_LOGGED with already recorded message
    for r in results:
        assert ("650" in r.message_text or "already recorded" in r.message_text.lower())

    # Persistent storage check: exactly ONE ledger entry must exist
    entries = base_repos["ledger_repo"].get_ledger_entries(trip_id)
    actual_entries = [e for e in entries if e.actual_amount is not None]
    assert len(actual_entries) == 1
    assert actual_entries[0].actual_amount == Decimal("650.00")

    # VirtualLedgerManager summary check
    ledger_mgr = VirtualLedgerManager(base_repos["ledger_repo"])
    summary = ledger_mgr.get_summary(trip_id)
    assert summary.total_spent == Decimal("650.00")


# =========================================================================
# Scenario 3: Replay After Repository Reconstruction
# =========================================================================

@pytest.mark.asyncio
async def test_replay_after_repository_reconstruction(active_trip, base_repos):
    """Simulate complete process restart by reconstructing new repository, manager, and orchestrator instances.
    Verify that replaying the identical event_id is idempotently recognized from persistent storage.
    """
    trip_id = active_trip.id
    chat_id = active_trip.telegram_chat_id
    event_id = "tg_upd_6002_msg_2002"

    # Instance 1 processes the expense
    orch1 = create_orchestrator(base_repos)
    res1 = await orch1.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹1200 on museum tickets",
        event_id=event_id,
    )
    assert res1.status == "EXPENSE_LOGGED"
    assert "1,200" in res1.message_text or "1200" in res1.message_text

    # Reconstruct completely new ledger repository, manager, and orchestrator instances (simulate restart)
    new_repos = {
        "user_repo": base_repos["user_repo"],
        "trip_repo": base_repos["trip_repo"],
        "intent_repo": IntentRepository(client=None),
        "itinerary_repo": ItineraryRepository(client=None),
        "ledger_repo": LedgerRepository(client=None),
        "rescue_repo": RescueRepository(client=None),
        "conversation_repo": ConversationStateRepository(client=None),
        "trip_pass_repo": TripPassRepository(client=None),
    }
    orch2 = create_orchestrator(new_repos)

    # Instance 2 receives replayed event
    res2 = await orch2.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹1200 on museum tickets",
        event_id=event_id,
    )
    assert res2.status == "EXPENSE_LOGGED"
    assert "already recorded" in res2.message_text.lower()

    # Verify storage remains with exactly 1 entry of ₹1200
    entries = new_repos["ledger_repo"].get_ledger_entries(trip_id)
    actual_entries = [e for e in entries if e.actual_amount is not None]
    assert len(actual_entries) == 1
    assert actual_entries[0].actual_amount == Decimal("1200.00")


# =========================================================================
# Scenario 4: Distinct Expenses From One Update (Compound Identifiers)
# =========================================================================

@pytest.mark.asyncio
async def test_distinct_expenses_from_one_update(active_trip, base_repos):
    """A single natural-language message containing multiple expenses generates distinct compound tags.
    Both must be inserted into storage with unique deterministic UUIDs and counted accurately.
    """
    trip_id = active_trip.id
    chat_id = active_trip.telegram_chat_id
    event_id = "tg_upd_7003_msg_3003"

    orch = create_orchestrator(base_repos)
    res = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹850 on lunch and ₹220 on metro",
        event_id=event_id,
    )
    assert res.status == "EXPENSE_LOGGED"

    entries = base_repos["ledger_repo"].get_ledger_entries(trip_id)
    actual_entries = [e for e in entries if e.actual_amount is not None]
    assert len(actual_entries) == 2

    # Validate distinct deterministic IDs and compound tags
    entry_ids = [e.id for e in actual_entries]
    assert len(set(entry_ids)) == 2

    tag_0_id = derive_expense_entry_id(trip_id, f"{event_id}:0")
    tag_1_id = derive_expense_entry_id(trip_id, f"{event_id}:1")
    assert tag_0_id in entry_ids
    assert tag_1_id in entry_ids

    amounts = sorted([e.actual_amount for e in actual_entries])
    assert amounts == [Decimal("220.00"), Decimal("850.00")]

    ledger_mgr = VirtualLedgerManager(base_repos["ledger_repo"])
    summary = ledger_mgr.get_summary(trip_id)
    assert summary.total_spent == Decimal("1070.00")


# =========================================================================
# Scenario 5: Legitimate Separate Events With Identical Amounts
# =========================================================================

@pytest.mark.asyncio
async def test_legitimate_separate_events_with_identical_amounts(active_trip, base_repos):
    """Two distinct events with identical amount (₹500) and identical description (coffee):
    Because event_ids differ ('tg_upd_1' vs 'tg_upd_2'), both must be accepted and recorded.
    """
    trip_id = active_trip.id
    chat_id = active_trip.telegram_chat_id

    orch = create_orchestrator(base_repos)

    res1 = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹500 on coffee",
        event_id="tg_upd_8001_msg_4001",
    )
    assert res1.status == "EXPENSE_LOGGED"
    assert "already recorded" not in res1.message_text.lower()

    res2 = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹500 on coffee",
        event_id="tg_upd_8002_msg_4002",
    )
    assert res2.status == "EXPENSE_LOGGED"
    assert "already recorded" not in res2.message_text.lower()

    entries = base_repos["ledger_repo"].get_ledger_entries(trip_id)
    actual_entries = [e for e in entries if e.actual_amount is not None]
    assert len(actual_entries) == 2
    assert all(e.actual_amount == Decimal("500.00") for e in actual_entries)

    ledger_mgr = VirtualLedgerManager(base_repos["ledger_repo"])
    summary = ledger_mgr.get_summary(trip_id)
    assert summary.total_spent == Decimal("1000.00")


# =========================================================================
# Scenario 6: Reversal and Correction Integrity Following Duplicate Attempts
# =========================================================================

@pytest.mark.asyncio
async def test_reversal_integrity_following_duplicate_attempt(active_trip, base_repos):
    """Ensure duplicate attempts do not create ghost rows that corrupt reversals:
    - Insert ₹750 dinner expense (event_id="evt_dinner").
    - Attempt duplicate submission (event_id="evt_dinner") -> rejected.
    - Reverse ₹750 expense.
    - Net actual spend must be exactly ₹0.00.
    """
    trip_id = active_trip.id
    chat_id = active_trip.telegram_chat_id
    event_id = "evt_dinner_9001"

    orch = create_orchestrator(base_repos)

    # 1. First submission
    res1 = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹750 on dinner",
        event_id=event_id,
    )
    assert res1.status == "EXPENSE_LOGGED"

    # 2. Duplicate submission attempt
    res2 = await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹750 on dinner",
        event_id=event_id,
    )
    assert "already recorded" in res2.message_text.lower()

    # 3. Issue reversal via ledger repository for the target amount
    rev_entry = base_repos["ledger_repo"].record_expense_reversal(
        trip_id=trip_id,
        target_amount=Decimal("750.00"),
        category="daily_survival",
        reason="Mistaken duplicate entry",
    )
    assert rev_entry is not None
    assert rev_entry.actual_amount == Decimal("-750.00")

    # Verify net spend is exactly 0
    ledger_mgr = VirtualLedgerManager(base_repos["ledger_repo"])
    summary = ledger_mgr.get_summary(trip_id)
    assert summary.total_spent == Decimal("0.00")


# =========================================================================
# Scenario 7: Trip Completion Reconciliation Integrity
# =========================================================================

@pytest.mark.asyncio
async def test_trip_completion_reconciliation_integrity(active_trip, base_repos):
    """Confirm that failed or duplicate inserts do not corrupt completion reconciliation totals."""
    trip_id = active_trip.id
    chat_id = active_trip.telegram_chat_id

    orch = create_orchestrator(base_repos)

    # Legitimate spend: ₹1500 lunch + ₹300 transport
    await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹1500 on team lunch",
        event_id="evt_rec_1",
    )
    await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹300 on auto rickshaw",
        event_id="evt_rec_2",
    )

    # Duplicate attempts for both events
    await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹1500 on team lunch",
        event_id="evt_rec_1",
    )
    await orch.handle_user_message(
        telegram_user_id=chat_id,
        chat_id=chat_id,
        message="Spent ₹300 on auto rickshaw",
        event_id="evt_rec_2",
    )

    # Run trip completion reconciliation
    comp_handler = TripCompletionHandler(
        trip_repo=base_repos["trip_repo"],
        ledger_repo=base_repos["ledger_repo"],
        conversation_repo=base_repos["conversation_repo"],
    )
    p_res = await comp_handler.handle_trip_complete(chat_id, active_trip, completion_reason="Trip completed successfully")
    assert p_res.status == "PENDING_RECONCILIATION"
    assert p_res.recorded_actual_spend == Decimal("1800.00")

    recon_res = await comp_handler.handle_reconcile_amount(chat_id, Decimal("1800.00"), active_trip)
    assert recon_res.status == "COMPLETED"
    assert recon_res.recorded_actual_spend == Decimal("1800.00")
    assert recon_res.planned_budget == Decimal("30000.00")
    assert recon_res.final_variance == Decimal("28200.00")


# =========================================================================
# Scenario 8: Direct Storage-Level Atomic Concurrency and Rollback
# =========================================================================

def test_direct_atomic_storage_rejection_and_rollback(active_trip, base_repos):
    """Test direct LedgerRepository layer for atomic DuplicateLedgerEntryError and rollback."""
    repo = base_repos["ledger_repo"]
    trip_id = active_trip.id
    tag = "evt_direct_test_1:0"
    entry_id = derive_expense_entry_id(trip_id, tag)

    entry1 = LedgerEntry(
        id=entry_id,
        trip_id=trip_id,
        category="daily_survival",
        description=f"Dinner at restaurant [evt:{tag}]",
        allocated_amount=Decimal("0.00"),
        planned_amount=Decimal("0.00"),
        spent_amount=Decimal("950.00"),
        remaining_amount=Decimal("0.00"),
        actual_amount=Decimal("950.00"),
        source="user_reported",
        created_at=utc_now(),
    )
    # First insert must succeed
    saved1 = repo.add_ledger_entry(entry1)
    assert saved1.id == entry_id

    # Second insert with identical entry_id must raise DuplicateLedgerEntryError
    with pytest.raises(DuplicateLedgerEntryError):
        repo.add_ledger_entry(entry1)

    # Second insert with different id but same event tag in description must also raise DuplicateLedgerEntryError
    entry_dup_tag = LedgerEntry(
        id=uuid4(),
        trip_id=trip_id,
        category="daily_survival",
        description=f"Another dinner attempt [evt:{tag}]",
        allocated_amount=Decimal("0.00"),
        planned_amount=Decimal("0.00"),
        spent_amount=Decimal("950.00"),
        remaining_amount=Decimal("0.00"),
        actual_amount=Decimal("950.00"),
        source="user_reported",
        created_at=utc_now(),
    )
    with pytest.raises(DuplicateLedgerEntryError):
        repo.add_ledger_entry(entry_dup_tag)

    # Rollback capability test: delete_ledger_entry rolls back the entry cleanly
    deleted = repo.delete_ledger_entry(entry_id, trip_id)
    assert deleted is True

    # Now the entry can be inserted again without collision
    saved2 = repo.add_ledger_entry(entry1)
    assert saved2.id == entry_id


# =========================================================================
# Scenario 9: True Transaction-Level Batch Atomicity (All-or-Nothing)
# =========================================================================

def test_batch_atomicity_second_item_failure_leaves_zero_committed(active_trip, base_repos):
    """When a multi-item batch fails on the second item:
    All-or-nothing atomicity guarantees that the first item is NEVER committed to storage.
    """
    repo = base_repos["ledger_repo"]
    trip_id = active_trip.id
    event_id = "batch_atomicity_evt_99"

    # Step 1: Pre-seed an existing entry that collides with item 1
    colliding_tag = f"{event_id}:1"
    existing_entry = LedgerEntry(
        id=derive_expense_entry_id(trip_id, colliding_tag),
        trip_id=trip_id,
        category="daily_survival",
        description=f"Existing transport [evt:{colliding_tag}]",
        allocated_amount=Decimal("0.00"),
        planned_amount=Decimal("0.00"),
        spent_amount=Decimal("200.00"),
        actual_amount=Decimal("200.00"),
        source="user_reported",
        created_at=utc_now(),
    )
    repo.add_ledger_entry(existing_entry)

    # Initial state: exactly 1 actual entry (the pre-seeded one)
    initial_entries = [e for e in repo.get_ledger_entries(trip_id) if e.actual_amount is not None]
    assert len(initial_entries) == 1

    # Step 2: Attempt to insert a batch where Item 0 is brand new, but Item 1 collides
    item0 = LedgerEntry(
        id=derive_expense_entry_id(trip_id, f"{event_id}:0"),
        trip_id=trip_id,
        category="daily_survival",
        description=f"New Lunch [evt:{event_id}:0]",
        allocated_amount=Decimal("0.00"),
        planned_amount=Decimal("0.00"),
        spent_amount=Decimal("750.00"),
        actual_amount=Decimal("750.00"),
        source="user_reported",
        created_at=utc_now(),
    )
    item1_colliding = LedgerEntry(
        id=derive_expense_entry_id(trip_id, colliding_tag),
        trip_id=trip_id,
        category="daily_survival",
        description=f"Colliding transport [evt:{colliding_tag}]",
        allocated_amount=Decimal("0.00"),
        planned_amount=Decimal("0.00"),
        spent_amount=Decimal("200.00"),
        actual_amount=Decimal("200.00"),
        source="user_reported",
        created_at=utc_now(),
    )

    # Step 3: Attempt atomic batch insert -> MUST fail with DuplicateLedgerEntryError
    with pytest.raises(DuplicateLedgerEntryError):
        repo.add_ledger_entries([item0, item1_colliding])

    # Step 4: Verify storage invariant: Item 0 MUST NOT be committed
    current_entries = [e for e in repo.get_ledger_entries(trip_id) if e.actual_amount is not None]
    assert len(current_entries) == 1
    assert current_entries[0].id == existing_entry.id
    assert not any(e.id == item0.id for e in current_entries)


# =========================================================================
# Scenario 10: Strict Error Code Discrimination (PostgreSQL 23505 vs Others)
# =========================================================================

class MockPostgrestAPIError(Exception):
    """Simulate postgrest.exceptions.APIError with code and details attributes."""
    def __init__(self, code: str | int, message: str, details: str = ""):
        super().__init__(f"{message} (code: {code})")
        self.code = str(code)
        self.message = message
        self.details = details


def test_strict_error_code_discrimination():
    """Verify that is_unique_violation strictly identifies PostgreSQL 23505 unique violations
    and refuses to classify unrelated database errors as duplicates.
    """
    from budlance.db.repositories.ledger_repo import is_unique_violation

    # 1. Real PostgreSQL unique key violation: code 23505
    unique_err = MockPostgrestAPIError(
        code="23505",
        message="duplicate key value violates unique constraint 'ledger_entries_pkey'",
        details="Key (id)=(467db894) already exists.",
    )
    assert is_unique_violation(unique_err) is True

    # 2. Foreign key violation: code 23503 -> MUST NOT be classified as unique violation
    fk_err = MockPostgrestAPIError(
        code="23503",
        message="violates foreign key constraint 'ledger_entries_trip_id_fkey'",
        details="Key (trip_id)=(...) is not present in table 'trips'.",
    )
    assert is_unique_violation(fk_err) is False

    # 3. Check constraint violation: code 23514 -> MUST NOT be classified as unique violation
    check_err = MockPostgrestAPIError(
        code="23514",
        message="new row violates check constraint 'check_ledger_entries_day_number'",
    )
    assert is_unique_violation(check_err) is False

    # 4. Undefined table: code 42P01 -> MUST NOT be classified as unique violation
    table_err = MockPostgrestAPIError(
        code="42P01",
        message="relation 'ledger_entries' does not exist",
    )
    assert is_unique_violation(table_err) is False

    # 5. Edge case: FK error whose details text coincidentally mentions the word 'unique'
    tricky_fk_err = MockPostgrestAPIError(
        code="23503",
        message="foreign key error on unique index reference",
        details="unique reference not found",
    )
    assert is_unique_violation(tricky_fk_err) is False


def test_repo_re_raises_unrelated_database_errors(active_trip):
    """Verify that LedgerRepository.add_ledger_entries re-raises non-duplicate database errors."""
    mock_client = MagicMock()
    # Configure mock client table insert to raise foreign key error 23503
    mock_table = MagicMock()
    mock_client.table.return_value = mock_table
    mock_table.insert.return_value = mock_table
    mock_table.execute.side_effect = MockPostgrestAPIError(
        code="23503",
        message="foreign key constraint violation",
    )

    repo = LedgerRepository(client=mock_client)
    entry = LedgerEntry(
        id=uuid4(),
        trip_id=active_trip.id,
        category="daily_survival",
        description="Test Entry",
        allocated_amount=Decimal("0.00"),
        planned_amount=Decimal("0.00"),
        spent_amount=Decimal("100.00"),
        actual_amount=Decimal("100.00"),
        source="user_reported",
        created_at=utc_now(),
    )

    # Must re-raise MockPostgrestAPIError, NOT catch it as DuplicateLedgerEntryError
    with pytest.raises(MockPostgrestAPIError):
        repo.add_ledger_entry(entry)

