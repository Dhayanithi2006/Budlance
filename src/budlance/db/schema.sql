-- Budlance PostgreSQL Schema (Supabase)
-- Represents the 14 frozen entities from docs/PROJECT_SPEC.md

-- 1. Users
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    first_name TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Trips
CREATE TABLE IF NOT EXISTS trips (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    telegram_chat_id BIGINT NOT NULL,
    destination TEXT,
    origin TEXT,
    budget_total NUMERIC(12,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'INR',
    people_count INT DEFAULT 1,
    duration_days INT DEFAULT 1,
    current_day INT NOT NULL DEFAULT 1,
    status VARCHAR(30) DEFAULT 'PLANNING', -- PLANNING, ACTIVE, COMPLETED
    completion_reason VARCHAR(50) DEFAULT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trips_user_active ON trips(user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_trips_chat_active ON trips(telegram_chat_id, is_active);

-- 3. Trip Intents
CREATE TABLE IF NOT EXISTS trip_intents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id UUID NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    budget NUMERIC(12,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'INR',
    people INT NOT NULL,
    days INT NOT NULL,
    origin TEXT,
    destination TEXT,
    interests JSONB DEFAULT '[]'::jsonb,
    traveler_type VARCHAR(50),
    raw_prompt TEXT,
    extracted_at TIMESTAMPTZ DEFAULT NOW()
);

-- 4. Trip Options
CREATE TABLE IF NOT EXISTS trip_options (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id UUID NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    option_tier VARCHAR(50) DEFAULT 'standard',
    total_estimated_cost NUMERIC(12,2) NOT NULL,
    is_selected BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 5. Flight Options
CREATE TABLE IF NOT EXISTS flight_options (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_option_id UUID NOT NULL REFERENCES trip_options(id) ON DELETE CASCADE,
    airline TEXT,
    flight_number TEXT,
    departure_airport VARCHAR(10),
    arrival_airport VARCHAR(10),
    departure_time TIMESTAMPTZ,
    arrival_time TIMESTAMPTZ,
    price NUMERIC(12,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'INR',
    deep_link TEXT,
    is_fallback BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 6. Hotel Options
CREATE TABLE IF NOT EXISTS hotel_options (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_option_id UUID NOT NULL REFERENCES trip_options(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    hotel_class INT,
    address TEXT,
    price_per_night NUMERIC(12,2) NOT NULL,
    total_price NUMERIC(12,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'INR',
    rating NUMERIC(3,1),
    review_count INT,
    deep_link TEXT,
    is_fallback BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 7. Place Options
CREATE TABLE IF NOT EXISTS place_options (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_option_id UUID NOT NULL REFERENCES trip_options(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    category VARCHAR(50),
    address TEXT,
    rating NUMERIC(3,1),
    estimated_cost NUMERIC(12,2) DEFAULT 0.00,
    is_fallback BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 8. Itineraries
CREATE TABLE IF NOT EXISTS itineraries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id UUID NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    days JSONB NOT NULL,
    is_feasible BOOLEAN NOT NULL DEFAULT TRUE,
    feasibility_note TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 9. Budget Allocations
CREATE TABLE IF NOT EXISTS budget_allocations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id UUID NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    transport_allocated NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    stay_allocated NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    food_allocated NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    activities_discretionary NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    rescue_fund_allocated NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    total_budget NUMERIC(12,2) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 10. Ledger Entries
CREATE TABLE IF NOT EXISTS ledger_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id UUID NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    category VARCHAR(50) NOT NULL, -- fixed_booking, daily_survival, activities, rescue
    description TEXT NOT NULL,
    allocated_amount NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    planned_amount NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    spent_amount NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    remaining_amount NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    actual_amount NUMERIC(12,2) DEFAULT NULL,
    day_number INT DEFAULT NULL,
    source VARCHAR(30) NOT NULL DEFAULT 'estimated', -- live, estimated, fallback, user_reported
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 11. Plan Attempts
CREATE TABLE IF NOT EXISTS plan_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id UUID NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    attempt_number INT NOT NULL CHECK (attempt_number BETWEEN 1 AND 4),
    downgrade_type VARCHAR(50) NOT NULL, -- hotel_tier_down, transport_class_down, reduce_trip_length, trim_discretionary_b
    was_feasible BOOLEAN NOT NULL,
    cost_calculated NUMERIC(12,2) NOT NULL,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 12. Rescue Events
CREATE TABLE IF NOT EXISTS rescue_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id UUID NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    rescue_type VARCHAR(50) NOT NULL, -- weather_closure, price_dispute
    user_message TEXT NOT NULL,
    resolution_summary TEXT NOT NULL,
    ledger_impact NUMERIC(12,2) DEFAULT 0.00,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 13. Search Cache
CREATE TABLE IF NOT EXISTS search_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_hash VARCHAR(64) UNIQUE NOT NULL,
    engine VARCHAR(50) NOT NULL,
    params_json JSONB NOT NULL,
    response_data JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_search_cache_hash ON search_cache(query_hash);

-- 14. API Usage
CREATE TABLE IF NOT EXISTS api_usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id UUID REFERENCES trips(id) ON DELETE SET NULL,
    engine VARCHAR(50) NOT NULL,
    call_count INT NOT NULL DEFAULT 1,
    cached_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_api_usage_trip ON api_usage(trip_id);

-- 15. Trip Passes (Monetization & Access Control)
CREATE TABLE IF NOT EXISTS trip_passes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id UUID NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    telegram_user_id BIGINT NOT NULL,
    telegram_chat_id BIGINT NOT NULL,
    amount NUMERIC(10,2) NOT NULL,
    currency VARCHAR(3) NOT NULL DEFAULT 'INR',
    provider VARCHAR(50) NOT NULL DEFAULT 'razorpay',
    payment_reference VARCHAR(255),
    status VARCHAR(30) NOT NULL DEFAULT 'FREE', -- FREE, CHECKOUT_PENDING, PAID, PAYMENT_FAILED, PAYMENT_ABANDONED
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_trip_passes_trip UNIQUE(trip_id)
);

CREATE INDEX IF NOT EXISTS idx_trip_passes_trip ON trip_passes(trip_id);
CREATE INDEX IF NOT EXISTS idx_trip_passes_chat ON trip_passes(telegram_chat_id);
CREATE INDEX IF NOT EXISTS idx_trip_passes_ref ON trip_passes(payment_reference);

-- 16. Payment Events (Distributed Idempotency & Concurrency Safety)
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
