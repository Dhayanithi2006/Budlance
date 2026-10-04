-- Migration: Add trip_passes table for Budlance Trip Pass monetization
-- Supports FREE, CHECKOUT_PENDING, PAID, PAYMENT_FAILED, PAYMENT_ABANDONED states

CREATE TABLE IF NOT EXISTS trip_passes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id UUID NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    telegram_user_id BIGINT NOT NULL,
    telegram_chat_id BIGINT NOT NULL,
    amount NUMERIC(10,2) NOT NULL DEFAULT 49.00,
    currency VARCHAR(3) NOT NULL DEFAULT 'INR',
    provider VARCHAR(50) NOT NULL DEFAULT 'razorpay',
    payment_reference VARCHAR(255),
    status VARCHAR(30) NOT NULL DEFAULT 'FREE',
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_trip_passes_trip UNIQUE(trip_id)
);

CREATE INDEX IF NOT EXISTS idx_trip_passes_trip ON trip_passes(trip_id);
CREATE INDEX IF NOT EXISTS idx_trip_passes_chat ON trip_passes(telegram_chat_id);
CREATE INDEX IF NOT EXISTS idx_trip_passes_ref ON trip_passes(payment_reference);
