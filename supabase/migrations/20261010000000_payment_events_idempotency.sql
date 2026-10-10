-- Migration: Add payment_events table and atomic claim function for distributed webhook idempotency
-- Enforces database-level uniqueness on event_id and atomic Trip Pass fulfillment.

CREATE TABLE IF NOT EXISTS payment_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id VARCHAR(255) NOT NULL,
    trip_id UUID NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    provider VARCHAR(50) NOT NULL DEFAULT 'stripe',
    event_type VARCHAR(100) NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'COMPLETED', -- PROCESSING, COMPLETED, FAILED
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_payment_events_event_id UNIQUE(event_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_events_event_id ON payment_events(event_id);
CREATE INDEX IF NOT EXISTS idx_payment_events_trip_id ON payment_events(trip_id);

-- Atomic PostgreSQL RPC for distributed claiming and status transition within a single transaction
CREATE OR REPLACE FUNCTION claim_and_fulfill_trip_pass(
    p_event_id TEXT,
    p_trip_id UUID,
    p_provider TEXT,
    p_event_type TEXT,
    p_target_status TEXT,
    p_payment_reference TEXT DEFAULT NULL,
    p_metadata JSONB DEFAULT '{}'::jsonb
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_pass RECORD;
    v_new_status TEXT;
    v_inserted_id UUID;
BEGIN
    -- 1. Check if event was already recorded in payment_events
    IF EXISTS (SELECT 1 FROM payment_events WHERE event_id = p_event_id) THEN
        SELECT * INTO v_pass FROM trip_passes WHERE trip_id = p_trip_id;
        RETURN jsonb_build_object(
            'success', true,
            'is_duplicate', true,
            'pass_status', v_pass.status,
            'message', 'Event already processed'
        );
    END IF;

    -- 2. Fetch current pass with row lock
    SELECT * INTO v_pass FROM trip_passes WHERE trip_id = p_trip_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'success', false,
            'is_duplicate', false,
            'error', 'PASS_NOT_FOUND',
            'message', 'Trip pass not found'
        );
    END IF;

    -- 3. Atomically insert event with UNIQUE constraint.
    -- If another concurrent process inserts simultaneously, ON CONFLICT DO NOTHING ensures safety.
    INSERT INTO payment_events (event_id, trip_id, provider, event_type, status, metadata)
    VALUES (p_event_id, p_trip_id, p_provider, p_event_type, 'COMPLETED', p_metadata)
    ON CONFLICT (event_id) DO NOTHING
    RETURNING id INTO v_inserted_id;

    -- If no row was inserted, a concurrent delivery won the race!
    IF v_inserted_id IS NULL THEN
        SELECT * INTO v_pass FROM trip_passes WHERE trip_id = p_trip_id;
        RETURN jsonb_build_object(
            'success', true,
            'is_duplicate', true,
            'pass_status', v_pass.status,
            'message', 'Event already processed'
        );
    END IF;

    -- 4. Out-of-order downgrade protection:
    v_new_status := p_target_status;
    IF v_pass.status IN ('PAID', 'PAID_VERIFIED', 'DEMO_ACCESS') AND p_target_status IN (
        'CHECKOUT_PENDING', 'PAYMENT_FAILED', 'PAYMENT_CANCELLED', 'PAYMENT_ABANDONED', 'PAYMENT_EXPIRED'
    ) THEN
        v_new_status := v_pass.status;
    END IF;

    -- 5. Update trip_pass atomically in the same transaction
    UPDATE trip_passes
    SET status = v_new_status,
        payment_reference = COALESCE(p_payment_reference, payment_reference),
        provider = COALESCE(p_provider, provider),
        metadata = trip_passes.metadata || p_metadata || jsonb_build_object('last_event_id', p_event_id),
        updated_at = NOW()
    WHERE trip_id = p_trip_id;

    RETURN jsonb_build_object(
        'success', true,
        'is_duplicate', false,
        'pass_status', v_new_status,
        'message', 'Event claimed and pass updated'
    );
END;
$$;
