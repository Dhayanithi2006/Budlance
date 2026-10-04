"""Phase 3 Verification & Regression Tests: Canonical Trip Status Persistence.

Verifies:
- Test A: Lowercase create (status="planning" -> persisted as "PLANNING")
- Test B: Mixed-case create (status="Planning" -> persisted as "PLANNING")
- Test C: Lowercase update (status="active" -> persisted as "ACTIVE")
- Test D: Completion update (status="completed" -> persisted as "COMPLETED")
- Test E: Invalid status rejection ("cancelled", "invalid" -> raises ValueError, never silently mapped)
- Test F: Live constraint regression (ensures check_trips_status 23514 cannot be triggered)
- Test G: Lifecycle semantics preserved (PLANNING remains PLANNING, ACTIVE remains ACTIVE)
"""

from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4
import pytest
from pydantic import ValidationError

from budlance.db.models import Trip, TripStatus
from budlance.db.repositories.trip_repo import TripRepository


# ============================================================================
# Test A — Lowercase create ("planning" -> "PLANNING")
# ============================================================================

def test_a_lowercase_create():
    """Test A — Input: status="planning".

    Expected persisted payload sent to database is canonical "PLANNING".
    """
    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_client.table.return_value = mock_table
    mock_table.insert.return_value.execute.return_value.data = [{
        "id": str(uuid4()),
        "user_id": str(uuid4()),
        "telegram_chat_id": 1001,
        "destination": "Goa",
        "origin": "Mumbai",
        "budget_total": 25000.0,
        "currency": "INR",
        "people_count": 2,
        "duration_days": 3,
        "current_day": 1,
        "status": "PLANNING",
        "is_active": True,
        "created_at": "2026-10-03T12:00:00Z",
        "updated_at": "2026-10-03T12:00:00Z",
    }]

    repo = TripRepository(client=mock_client)
    trip = repo.create_trip(
        user_id=uuid4(),
        telegram_chat_id=1001,
        budget_total=Decimal("25000.00"),
        destination="Goa",
        status="planning",  # Lowercase input
    )

    # 1. Model reflects canonical status
    assert trip.status == "PLANNING"

    # 2. Database insert payload receives canonical uppercase
    insert_call = mock_table.insert.call_args[0][0]
    assert insert_call["status"] == "PLANNING"
    assert insert_call["status"] != "planning"


# ============================================================================
# Test B — Mixed-case create ("Planning" -> "PLANNING")
# ============================================================================

def test_b_mixed_case_create():
    """Test B — Input: status="Planning".

    Expected persisted value is canonical "PLANNING".
    """
    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_client.table.return_value = mock_table
    mock_table.insert.return_value.execute.return_value.data = [{
        "id": str(uuid4()),
        "user_id": str(uuid4()),
        "telegram_chat_id": 1002,
        "destination": "Delhi",
        "origin": "Chennai",
        "budget_total": 50000.0,
        "currency": "INR",
        "people_count": 2,
        "duration_days": 3,
        "current_day": 1,
        "status": "PLANNING",
        "is_active": True,
        "created_at": "2026-10-03T12:00:00Z",
        "updated_at": "2026-10-03T12:00:00Z",
    }]

    repo = TripRepository(client=mock_client)
    trip = repo.create_trip(
        user_id=uuid4(),
        telegram_chat_id=1002,
        budget_total=Decimal("50000.00"),
        destination="Delhi",
        status="Planning",  # Mixed-case input
    )

    assert trip.status == "PLANNING"
    insert_call = mock_table.insert.call_args[0][0]
    assert insert_call["status"] == "PLANNING"


# ============================================================================
# Test C — Lowercase update ("active" -> "ACTIVE")
# ============================================================================

def test_c_lowercase_update():
    """Test C — Input: "active".

    Expected persisted database payload is canonical "ACTIVE".
    """
    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_client.table.return_value = mock_table
    mock_table.update.return_value.eq.return_value.execute.return_value.data = [{"status": "ACTIVE"}]

    repo = TripRepository(client=mock_client)
    trip_id = uuid4()
    success = repo.update_trip_status(
        trip_id=trip_id,
        status="active",  # Lowercase input
    )

    assert success is True
    update_call = mock_table.update.call_args[0][0]
    assert update_call["status"] == "ACTIVE"
    assert update_call["status"] != "active"


# ============================================================================
# Test D — Completion update ("completed" -> "COMPLETED")
# ============================================================================

def test_d_completion_update():
    """Test D — Input: "completed".

    Expected persisted database payload is canonical "COMPLETED".
    """
    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_client.table.return_value = mock_table
    mock_table.update.return_value.eq.return_value.execute.return_value.data = [{"status": "COMPLETED"}]

    repo = TripRepository(client=mock_client)
    trip_id = uuid4()
    success = repo.update_trip_status(
        trip_id=trip_id,
        status="completed",  # Lowercase input
        completion_reason="USER_CONFIRMED",
        is_active=False,
    )

    assert success is True
    update_call = mock_table.update.call_args[0][0]
    assert update_call["status"] == "COMPLETED"
    assert update_call["completion_reason"] == "USER_CONFIRMED"
    assert update_call["is_active"] is False


# ============================================================================
# Test E — Invalid status rejection (No silent mapping)
# ============================================================================

def test_e_invalid_status_rejection():
    """Test E — Input: "cancelled", "unknown", "in_progress".

    Expected: ValueError / validation rejection; must NOT silently map to COMPLETED or PLANNING.
    """
    repo = TripRepository(client=None)
    uid = uuid4()

    # 1. "cancelled" must NOT become "COMPLETED"
    with pytest.raises(ValueError, match="Invalid trip status: 'cancelled'"):
        repo.create_trip(
            user_id=uid,
            telegram_chat_id=1003,
            budget_total=Decimal("10000.00"),
            status="cancelled",
        )

    # 2. "unknown" must NOT silently become "PLANNING"
    with pytest.raises(ValueError, match="Invalid trip status: 'unknown'"):
        repo.create_trip(
            user_id=uid,
            telegram_chat_id=1003,
            budget_total=Decimal("10000.00"),
            status="unknown",
        )

    # 3. update_trip_status rejects invalid statuses
    trip = repo.create_trip(
        user_id=uid,
        telegram_chat_id=1003,
        budget_total=Decimal("10000.00"),
        status="PLANNING",
    )
    with pytest.raises(ValueError, match="Invalid trip status: 'cancelled'"):
        repo.update_trip_status(trip.id, status="cancelled")

    with pytest.raises(ValueError, match="Invalid trip status: 'in_progress'"):
        repo.update_trip_status(trip.id, status="in_progress")

    # 4. Trip model validation rejects invalid statuses
    with pytest.raises(ValidationError):
        Trip(
            user_id=uid,
            telegram_chat_id=1003,
            budget_total=Decimal("10000.00"),
            status="cancelled",  # type: ignore[arg-type]
        )


# ============================================================================
# Test F — Regression test of previous live Supabase 23514 check constraint error
# ============================================================================

def test_f_check_trips_status_constraint_regression():
    """Test F — Reproduce database CHECK constraint check_trips_status behavior.

    Verify that when application callers pass lowercase 'planning', the repository
    boundary converts it to canonical 'PLANNING', preventing error 23514.
    """
    allowed_statuses = {"PLANNING", "ACTIVE", "COMPLETED"}

    def _simulated_supabase_insert(payload):
        status = payload.get("status")
        if status not in allowed_statuses:
            # Emulate PostgreSQL 23514 check constraint violation
            raise RuntimeError(
                f'new row for relation "trips" violates check constraint "check_trips_status" '
                f'(Code 23514, failing row status="{status}")'
            )
        return MagicMock(execute=lambda: MagicMock(data=[{
            **payload,
            "created_at": "2026-10-03T12:00:00Z",
            "updated_at": "2026-10-03T12:00:00Z",
        }]))

    mock_client = MagicMock()
    mock_client.table.return_value.insert = _simulated_supabase_insert

    repo = TripRepository(client=mock_client)

    # Calling create_trip with lowercase "planning" MUST NOT trigger the simulated constraint violation
    trip = repo.create_trip(
        user_id=uuid4(),
        telegram_chat_id=1004,
        budget_total=Decimal("30000.00"),
        destination="Kerala",
        status="planning",  # lowercase caller input
    )
    assert trip.status == "PLANNING"


# ============================================================================
# Test G — Lifecycle semantics preserved (PLANNING stays PLANNING)
# ============================================================================

def test_g_lifecycle_semantics_preserved():
    """Test G — Verify that newly created trip stays PLANNING (does not prematurely become ACTIVE).

    Phase 3 must not change lifecycle transitions.
    """
    repo = TripRepository(client=None)
    uid = uuid4()

    trip = repo.create_trip(
        user_id=uid,
        telegram_chat_id=1005,
        budget_total=Decimal("15000.00"),
        destination="Ooty",
        status="planning",
        is_active=True,
    )

    assert trip.status == "PLANNING"
    assert trip.status != "ACTIVE"
    assert trip.current_day == 1
    assert trip.is_active is True
