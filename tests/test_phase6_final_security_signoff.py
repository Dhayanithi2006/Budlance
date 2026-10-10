"""Budlance — Phase 6 Final Security Sign-Off Test Suite.

Audits and proves:
1. Stripe Webhook Signature Verification:
   - Valid signature through HTTP endpoint /payment/webhook/stripe.
   - Missing signature header rejected (HTTP 400, no state mutation).
   - Invalid / forged signature rejected (HTTP 400, no state mutation).
   - Expired signature outside tolerance window rejected (HTTP 400).
   - Tampered / modified payload rejected via raw body byte validation.
   - Missing webhook secret fails closed (CONFIG_ERROR, HTTP 400).
   - Valid signature cannot bypass business gates (unpaid status, amount, currency, trip association).
2. Concurrent Webhook Idempotency & TOCTOU Protection:
   - Concurrent delivery race prevented via atomic per-event critical section; exactly one fulfills.
   - Durable idempotency across repository reconstruction when persistent storage is configured.
   - Out-of-order late failure / expiration cannot revoke established paid pass.
"""

import asyncio
from contextlib import contextmanager
from decimal import Decimal
import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4
import pytest
from httpx import ASGITransport, AsyncClient

from budlance.api.app import create_app
from budlance.api.routes import set_payment_service
from budlance.config import Settings
from budlance.db.repositories.trip_pass_repo import TripPassRepository
from budlance.payment.service import PaymentService


@contextmanager
def _mock_security_settings(**overrides):
    defaults = {
        "stripe_webhook_secret": "whsec_test_secret_signoff_2026",
        "stripe_api_key": "sk_test_signoff_key",
        "app_env": "development",
        "enable_trip_pass": True,
        "trip_pass_amount": Decimal("49.00"),
        "trip_pass_currency": "INR",
        "telegram_bot_username": "budlance_bot",
    }
    defaults.update(overrides)
    mocked_settings = Settings(**defaults)
    with patch("budlance.config.get_settings", return_value=mocked_settings), \
         patch("budlance.payment.service.get_settings", return_value=mocked_settings), \
         patch("budlance.api.routes.get_settings", return_value=mocked_settings), \
         patch("budlance.db.repositories.trip_pass_repo.get_settings", return_value=mocked_settings):
        yield mocked_settings


def _generate_stripe_signature(payload_bytes: bytes, secret: str, timestamp: int | None = None) -> str:
    """Generate Stripe v1 signature header conforming to Stripe's webhook signing specification."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode("utf-8") + payload_bytes
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


class MockSupabaseTableQuery:
    def __init__(self, table_data: dict[str, dict]):
        self._table = table_data
        self._filters: list[tuple[str, str, Any]] = []
        self._limit_val: int | None = None

    def select(self, *columns):
        return self

    def eq(self, column: str, value: Any):
        self._filters.append(("eq", column, str(value) if isinstance(value, UUID) else value))
        return self

    def limit(self, count: int):
        self._limit_val = count
        return self

    def insert(self, data: dict):
        key = data.get("id", str(uuid4()))
        self._table[key] = dict(data)
        res = MagicMock()
        res.data = [dict(data)]
        mock_exec = MagicMock()
        mock_exec.execute = MagicMock(return_value=res)
        return mock_exec

    def update(self, patch_data: dict):
        self._patch = patch_data
        return self

    def execute(self):
        # If this was an update call:
        if hasattr(self, "_patch"):
            updated_rows = []
            for k, row in self._table.items():
                match = True
                for op, col, val in self._filters:
                    if op == "eq" and str(row.get(col)) != str(val):
                        match = False
                if match:
                    row.update(self._patch)
                    updated_rows.append(dict(row))
            res = MagicMock()
            res.data = updated_rows
            return res

        # Otherwise select query:
        matched_rows = []
        for row in self._table.values():
            match = True
            for op, col, val in self._filters:
                if op == "eq" and str(row.get(col)) != str(val):
                    match = False
            if match:
                matched_rows.append(dict(row))

        if self._limit_val is not None:
            matched_rows = matched_rows[:self._limit_val]

        res = MagicMock()
        res.data = matched_rows
        return res


class MockSupabasePersistentClient:
    def __init__(self):
        self.tables: dict[str, dict[str, dict]] = {"trip_passes": {}}

    def table(self, table_name: str):
        if table_name not in self.tables:
            self.tables[table_name] = {}
        return MockSupabaseTableQuery(self.tables[table_name])


# =========================================================================
# SECTION 1: STRIPE WEBHOOK SIGNATURE VERIFICATION
# =========================================================================

@pytest.mark.asyncio
async def test_security_01_valid_signature_through_http_endpoint():
    """Trace HTTP webhook endpoint -> PaymentService.verify_webhook_event() with valid signature."""
    secret = "whsec_test_secret_signoff_2026"
    with _mock_security_settings(stripe_webhook_secret=secret):
        trip_pass_repo = TripPassRepository(client=None)
        service = PaymentService(
            trip_pass_repo=trip_pass_repo,
            pass_amount=Decimal("49.00"),
            pass_currency="INR",
            default_provider="stripe",
        )
        set_payment_service(service)

        trip_id = uuid4()
        trip_pass_repo.create_pass(trip_id=trip_id, telegram_user_id=101, telegram_chat_id=201)

        payload = {
            "id": "evt_sec_valid_01",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_sec_valid_01",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "paid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig_header = _generate_stripe_signature(raw_body, secret)

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/payment/webhook/stripe",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": sig_header,
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "PAID_VERIFIED"
        assert data["trip_id"] == str(trip_id)

        pass_rec = trip_pass_repo.get_by_trip_id(trip_id)
        assert pass_rec is not None
        assert pass_rec.status == "PAID_VERIFIED"
        assert pass_rec.is_unlocked is True
        assert "evt_sec_valid_01" in pass_rec.metadata.get("processed_events", [])


@pytest.mark.asyncio
async def test_security_02_missing_signature_rejected_without_mutation():
    """Missing Stripe-Signature header must be rejected without mutating state or recording event."""
    secret = "whsec_test_secret_signoff_2026"
    with _mock_security_settings(stripe_webhook_secret=secret):
        trip_pass_repo = TripPassRepository(client=None)
        service = PaymentService(
            trip_pass_repo=trip_pass_repo,
            pass_amount=Decimal("49.00"),
            pass_currency="INR",
            default_provider="stripe",
        )
        set_payment_service(service)

        trip_id = uuid4()
        trip_pass_repo.create_pass(trip_id=trip_id, telegram_user_id=101, telegram_chat_id=201)

        payload = {
            "id": "evt_sec_missing_sig",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_sec_missing_sig",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "paid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        raw_body = json.dumps(payload).encode("utf-8")

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/payment/webhook/stripe",
                content=raw_body,
                headers={"Content-Type": "application/json"},
                # Missing Stripe-Signature header completely
            )

        assert resp.status_code == 400
        assert "Missing Stripe signature" in resp.json()["detail"]

        # Verify zero state mutation
        pass_rec = trip_pass_repo.get_by_trip_id(trip_id)
        assert pass_rec.status == "FREE"
        assert pass_rec.is_unlocked is False
        assert "evt_sec_missing_sig" not in pass_rec.metadata.get("processed_events", [])


@pytest.mark.asyncio
async def test_security_03_invalid_signature_rejected_without_mutation():
    """Forged/invalid Stripe signature within timestamp tolerance must be rejected with HTTP 400 and zero mutation."""
    secret = "whsec_test_secret_signoff_2026"
    with _mock_security_settings(stripe_webhook_secret=secret):
        trip_pass_repo = TripPassRepository(client=None)
        service = PaymentService(
            trip_pass_repo=trip_pass_repo,
            pass_amount=Decimal("49.00"),
            pass_currency="INR",
            default_provider="stripe",
        )
        set_payment_service(service)

        trip_id = uuid4()
        trip_pass_repo.create_pass(trip_id=trip_id, telegram_user_id=101, telegram_chat_id=201)

        payload = {
            "id": "evt_sec_forged_sig",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_sec_forged_sig",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "paid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        raw_body = json.dumps(payload).encode("utf-8")
        current_ts = int(time.time())
        forged_sig = f"t={current_ts},v1=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/payment/webhook/stripe",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": forged_sig,
                },
            )

        assert resp.status_code == 400
        assert "Invalid Stripe signature" in resp.json()["detail"]

        pass_rec = trip_pass_repo.get_by_trip_id(trip_id)
        assert pass_rec.status == "FREE"
        assert pass_rec.is_unlocked is False
        assert "evt_sec_forged_sig" not in pass_rec.metadata.get("processed_events", [])


@pytest.mark.asyncio
async def test_security_03b_expired_timestamp_signature_rejected():
    """Signatures outside tolerance window (older than 300 seconds) must be rejected with HTTP 400."""
    secret = "whsec_test_secret_signoff_2026"
    with _mock_security_settings(stripe_webhook_secret=secret):
        trip_pass_repo = TripPassRepository(client=None)
        service = PaymentService(
            trip_pass_repo=trip_pass_repo,
            pass_amount=Decimal("49.00"),
            pass_currency="INR",
            default_provider="stripe",
        )
        set_payment_service(service)

        trip_id = uuid4()
        trip_pass_repo.create_pass(trip_id=trip_id, telegram_user_id=101, telegram_chat_id=201)

        payload = {
            "id": "evt_sec_expired_ts",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_sec_expired_ts",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "paid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        raw_body = json.dumps(payload).encode("utf-8")
        expired_ts = int(time.time()) - 400  # Exceeds 300s tolerance
        expired_sig = _generate_stripe_signature(raw_body, secret, timestamp=expired_ts)

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/payment/webhook/stripe",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": expired_sig,
                },
            )

        assert resp.status_code == 400
        assert "Stripe webhook signature expired" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_security_04_tampered_payload_rejected_by_raw_body_check():
    """Modifying even a single character in payload bytes invalidates HMAC-SHA256 signature."""
    secret = "whsec_test_secret_signoff_2026"
    with _mock_security_settings(stripe_webhook_secret=secret):
        trip_pass_repo = TripPassRepository(client=None)
        service = PaymentService(
            trip_pass_repo=trip_pass_repo,
            pass_amount=Decimal("49.00"),
            pass_currency="INR",
            default_provider="stripe",
        )
        set_payment_service(service)

        trip_id = uuid4()
        trip_pass_repo.create_pass(trip_id=trip_id, telegram_user_id=101, telegram_chat_id=201)

        payload = {
            "id": "evt_sec_tamper",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_sec_tamper",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "paid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        original_raw = json.dumps(payload).encode("utf-8")
        valid_sig = _generate_stripe_signature(original_raw, secret)

        # Attacker modifies raw bytes (e.g. changing an inner parameter) while sending original signature
        tampered_payload = dict(payload)
        tampered_payload["data"]["object"]["amount_total"] = 100
        tampered_raw = json.dumps(tampered_payload).encode("utf-8")

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/payment/webhook/stripe",
                content=tampered_raw,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": valid_sig,
                },
            )

        assert resp.status_code == 400
        assert "Invalid Stripe signature" in resp.json()["detail"]

        pass_rec = trip_pass_repo.get_by_trip_id(trip_id)
        assert pass_rec.status == "FREE"
        assert pass_rec.is_unlocked is False


@pytest.mark.asyncio
async def test_security_05_missing_webhook_secret_fails_closed():
    """When webhook secret is unconfigured/empty, endpoint must fail closed with CONFIG_ERROR."""
    with _mock_security_settings(stripe_webhook_secret=""):
        trip_pass_repo = TripPassRepository(client=None)
        service = PaymentService(
            trip_pass_repo=trip_pass_repo,
            pass_amount=Decimal("49.00"),
            pass_currency="INR",
            default_provider="stripe",
        )
        set_payment_service(service)

        trip_id = uuid4()
        trip_pass_repo.create_pass(trip_id=trip_id, telegram_user_id=101, telegram_chat_id=201)

        payload = {
            "id": "evt_sec_no_secret",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_sec_no_secret",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "paid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig_header = f"t={int(time.time())},v1=abcdef123456"

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/payment/webhook/stripe",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": sig_header,
                },
            )

        assert resp.status_code == 400
        assert "Stripe webhook secret is not configured" in resp.json()["detail"]

        pass_rec = trip_pass_repo.get_by_trip_id(trip_id)
        assert pass_rec.status == "FREE"
        assert pass_rec.is_unlocked is False


@pytest.mark.asyncio
async def test_security_06_valid_signature_cannot_bypass_business_gates():
    """A valid signature must NOT bypass payment_status, amount, currency, or trip association gates."""
    secret = "whsec_test_secret_signoff_2026"
    with _mock_security_settings(stripe_webhook_secret=secret):
        trip_pass_repo = TripPassRepository(client=None)
        service = PaymentService(
            trip_pass_repo=trip_pass_repo,
            pass_amount=Decimal("49.00"),
            pass_currency="INR",
            default_provider="stripe",
        )

        trip_id = uuid4()
        trip_pass_repo.create_pass(trip_id=trip_id, telegram_user_id=101, telegram_chat_id=201)

        # Gate 1: payment_status="unpaid" (session completed but funds not captured)
        payload_unpaid = {
            "id": "evt_gate_unpaid",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_gate_unpaid",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "unpaid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        raw_unpaid = json.dumps(payload_unpaid).encode("utf-8")
        sig_unpaid = _generate_stripe_signature(raw_unpaid, secret)
        res_unpaid = await service.verify_webhook_event(
            provider="stripe", payload=payload_unpaid, signature=sig_unpaid, raw_body=raw_unpaid
        )
        assert res_unpaid.success is False
        assert res_unpaid.status == "CHECKOUT_PENDING"
        assert res_unpaid.error == "PAYMENT_UNPAID"
        assert trip_pass_repo.get_by_trip_id(trip_id).is_unlocked is False

        # Gate 2: Amount mismatch (e.g. 100 paise instead of 4900)
        payload_amt = {
            "id": "evt_gate_amt",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_gate_amt",
                    "amount_total": 100,
                    "currency": "inr",
                    "payment_status": "paid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        raw_amt = json.dumps(payload_amt).encode("utf-8")
        sig_amt = _generate_stripe_signature(raw_amt, secret)
        res_amt = await service.verify_webhook_event(
            provider="stripe", payload=payload_amt, signature=sig_amt, raw_body=raw_amt
        )
        assert res_amt.success is False
        assert res_amt.error == "AMOUNT_MISMATCH"
        assert trip_pass_repo.get_by_trip_id(trip_id).is_unlocked is False

        # Gate 3: Currency mismatch (e.g. USD instead of INR)
        payload_curr = {
            "id": "evt_gate_curr",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_gate_curr",
                    "amount_total": 4900,
                    "currency": "usd",
                    "payment_status": "paid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        raw_curr = json.dumps(payload_curr).encode("utf-8")
        sig_curr = _generate_stripe_signature(raw_curr, secret)
        res_curr = await service.verify_webhook_event(
            provider="stripe", payload=payload_curr, signature=sig_curr, raw_body=raw_curr
        )
        assert res_curr.success is False
        assert res_curr.error == "CURRENCY_MISMATCH"
        assert trip_pass_repo.get_by_trip_id(trip_id).is_unlocked is False

        # Gate 4: Unassociated / missing trip ID
        random_trip_id = uuid4()
        payload_notrip = {
            "id": "evt_gate_notrip",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_gate_notrip",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "paid",
                    "client_reference_id": str(random_trip_id),
                    "metadata": {"trip_id": str(random_trip_id)},
                }
            },
        }
        raw_notrip = json.dumps(payload_notrip).encode("utf-8")
        sig_notrip = _generate_stripe_signature(raw_notrip, secret)
        res_notrip = await service.verify_webhook_event(
            provider="stripe", payload=payload_notrip, signature=sig_notrip, raw_body=raw_notrip
        )
        assert res_notrip.success is False
        assert res_notrip.error == "TRIP_NOT_FOUND"


# =========================================================================
# SECTION 2: CONCURRENT WEBHOOK IDEMPOTENCY & DURABILITY
# =========================================================================

@pytest.mark.asyncio
async def test_security_07_concurrent_webhook_deliveries_gather_single_fulfillment():
    """Concurrent deliveries of the exact same event must atomically fulfill once without race."""
    secret = "whsec_test_secret_signoff_2026"
    with _mock_security_settings(stripe_webhook_secret=secret):
        trip_pass_repo = TripPassRepository(client=None)
        service = PaymentService(
            trip_pass_repo=trip_pass_repo,
            pass_amount=Decimal("49.00"),
            pass_currency="INR",
            default_provider="stripe",
        )

        trip_id = uuid4()
        trip_pass_repo.create_pass(trip_id=trip_id, telegram_user_id=101, telegram_chat_id=201)

        payload = {
            "id": "evt_concurrent_race_test",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_concurrent_race_test",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "paid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = _generate_stripe_signature(raw_body, secret)

        # Dispatch 5 identical webhook deliveries concurrently via asyncio.gather
        results = await asyncio.gather(
            service.verify_webhook_event(provider="stripe", payload=payload, signature=sig, raw_body=raw_body),
            service.verify_webhook_event(provider="stripe", payload=payload, signature=sig, raw_body=raw_body),
            service.verify_webhook_event(provider="stripe", payload=payload, signature=sig, raw_body=raw_body),
            service.verify_webhook_event(provider="stripe", payload=payload, signature=sig, raw_body=raw_body),
            service.verify_webhook_event(provider="stripe", payload=payload, signature=sig, raw_body=raw_body),
        )

        # Exactly 1 delivery must claim fulfillment (is_duplicate=False)
        # All other 4 deliveries must detect deduplication (is_duplicate=True)
        primary_fulfillments = [r for r in results if not r.is_duplicate and r.success]
        duplicate_responses = [r for r in results if r.is_duplicate and r.success]

        assert len(primary_fulfillments) == 1, f"Expected 1 primary fulfillment, got {len(primary_fulfillments)}"
        assert len(duplicate_responses) == 4, f"Expected 4 duplicate deduplications, got {len(duplicate_responses)}"

        pass_rec = trip_pass_repo.get_by_trip_id(trip_id)
        assert pass_rec.status == "PAID_VERIFIED"
        assert pass_rec.is_unlocked is True
        # Processed events should contain the event ID exactly once
        events = pass_rec.metadata.get("processed_events", [])
        assert events.count("evt_concurrent_race_test") == 1


@pytest.mark.asyncio
async def test_security_08_durable_idempotency_across_repo_reconstruction():
    """Persistent storage retains processed_events across repository object reconstruction."""
    secret = "whsec_test_secret_signoff_2026"
    client = MockSupabasePersistentClient()

    with _mock_security_settings(stripe_webhook_secret=secret):
        # 1. Instantiate repo 1 and service 1
        repo1 = TripPassRepository(client=client)
        service1 = PaymentService(
            trip_pass_repo=repo1,
            pass_amount=Decimal("49.00"),
            pass_currency="INR",
            default_provider="stripe",
        )

        trip_id = uuid4()
        repo1.create_pass(trip_id=trip_id, telegram_user_id=101, telegram_chat_id=201)

        payload = {
            "id": "evt_durable_reconstruct_01",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_durable_reconstruct_01",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "paid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = _generate_stripe_signature(raw_body, secret)

        res1 = await service1.verify_webhook_event(
            provider="stripe", payload=payload, signature=sig, raw_body=raw_body
        )
        assert res1.success is True
        assert res1.is_duplicate is False
        assert res1.status == "PAID_VERIFIED"

        # 2. Destroy repo 1 and service 1 completely to prove no reliance on in-memory instance state
        del repo1
        del service1

        # 3. Reconstruct fresh repo 2 and service 2 from the same underlying database adapter
        repo2 = TripPassRepository(client=client)
        service2 = PaymentService(
            trip_pass_repo=repo2,
            pass_amount=Decimal("49.00"),
            pass_currency="INR",
            default_provider="stripe",
        )

        # 4. Redeliver the exact same webhook event to service 2
        res2 = await service2.verify_webhook_event(
            provider="stripe", payload=payload, signature=sig, raw_body=raw_body
        )
        assert res2.success is True
        assert res2.is_duplicate is True
        assert res2.status == "PAID_VERIFIED"

        # Pass in repo 2 retains PAID_VERIFIED
        pass_rec = repo2.get_by_trip_id(trip_id)
        assert pass_rec.status == "PAID_VERIFIED"
        assert pass_rec.is_unlocked is True
        assert "evt_durable_reconstruct_01" in pass_rec.metadata.get("processed_events", [])


@pytest.mark.asyncio
async def test_security_09_late_out_of_order_failure_cannot_revoke_paid():
    """A late failed or expired event cannot revoke an established PAID_VERIFIED entitlement."""
    secret = "whsec_test_secret_signoff_2026"
    with _mock_security_settings(stripe_webhook_secret=secret):
        trip_pass_repo = TripPassRepository(client=None)
        service = PaymentService(
            trip_pass_repo=trip_pass_repo,
            pass_amount=Decimal("49.00"),
            pass_currency="INR",
            default_provider="stripe",
        )

        trip_id = uuid4()
        trip_pass_repo.create_pass(trip_id=trip_id, telegram_user_id=101, telegram_chat_id=201)

        # 1. Establish verified paid pass
        payload_paid = {
            "id": "evt_paid_established",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_paid_established",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "paid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        raw_paid = json.dumps(payload_paid).encode("utf-8")
        sig_paid = _generate_stripe_signature(raw_paid, secret)
        res_paid = await service.verify_webhook_event(
            provider="stripe", payload=payload_paid, signature=sig_paid, raw_body=raw_paid
        )
        assert res_paid.success is True
        assert res_paid.status == "PAID_VERIFIED"
        assert trip_pass_repo.get_by_trip_id(trip_id).is_unlocked is True

        # 2. Out-of-order delayed failure event arrives
        payload_failed = {
            "id": "evt_delayed_failure",
            "type": "checkout.session.async_payment_failed",
            "data": {
                "object": {
                    "id": "cs_delayed_failure",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "unpaid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        raw_failed = json.dumps(payload_failed).encode("utf-8")
        sig_failed = _generate_stripe_signature(raw_failed, secret)
        res_failed = await service.verify_webhook_event(
            provider="stripe", payload=payload_failed, signature=sig_failed, raw_body=raw_failed
        )
        # Event verification records failure event as unsuccessful payment result
        assert res_failed.success is False
        # But the underlying pass in the database CANNOT be downgraded:
        pass_rec = trip_pass_repo.get_by_trip_id(trip_id)
        assert pass_rec.status == "PAID_VERIFIED"
        assert pass_rec.is_unlocked is True

        # 3. Out-of-order expired event arrives
        payload_expired = {
            "id": "evt_delayed_expired",
            "type": "checkout.session.expired",
            "data": {
                "object": {
                    "id": "cs_delayed_expired",
                    "amount_total": 4900,
                    "currency": "inr",
                    "payment_status": "unpaid",
                    "client_reference_id": str(trip_id),
                    "metadata": {"trip_id": str(trip_id)},
                }
            },
        }
        raw_expired = json.dumps(payload_expired).encode("utf-8")
        sig_expired = _generate_stripe_signature(raw_expired, secret)
        res_expired = await service.verify_webhook_event(
            provider="stripe", payload=payload_expired, signature=sig_expired, raw_body=raw_expired
        )
        assert res_expired.success is False
        # The underlying pass in the database remains PAID_VERIFIED and unlocked
        assert trip_pass_repo.get_by_trip_id(trip_id).status == "PAID_VERIFIED"
        assert trip_pass_repo.get_by_trip_id(trip_id).is_unlocked is True
