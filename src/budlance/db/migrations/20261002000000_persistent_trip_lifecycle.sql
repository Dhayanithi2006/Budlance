-- ============================================================================
-- Budlance Supabase Migration: Persistent Trip Lifecycle Schema Extension
-- Migration: 20261002000000_persistent_trip_lifecycle.sql
--
-- Extends the database schema to support full-trip persistent lifecycle:
-- 1. trips: adds current_day (INT DEFAULT 1), normalizes status to PLANNING, ACTIVE, COMPLETED
-- 2. ledger_entries: adds actual_amount (NUMERIC(12,2)), day_number (INT)
-- 3. itineraries: enriches days JSONB elements with lifecycle status (UPCOMING, IN_PROGRESS, COMPLETED, MODIFIED)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Extend trips table
-- ----------------------------------------------------------------------------
ALTER TABLE trips ADD COLUMN IF NOT EXISTS current_day INT NOT NULL DEFAULT 1;

-- Backfill / normalize existing status values
UPDATE trips
SET status = 'ACTIVE'
WHERE UPPER(status) = 'ACTIVE' OR (is_active = TRUE AND UPPER(status) NOT IN ('PLANNING', 'COMPLETED', 'CANCELLED'));

UPDATE trips
SET status = 'PLANNING'
WHERE UPPER(status) = 'PLANNING' AND status != 'PLANNING';

UPDATE trips
SET status = 'COMPLETED'
WHERE UPPER(status) IN ('COMPLETED', 'CANCELLED') AND status != 'COMPLETED';

-- Handle legacy inactive records with missing/null status
UPDATE trips
SET status = 'COMPLETED'
WHERE is_active = FALSE AND (status IS NULL OR UPPER(status) NOT IN ('PLANNING', 'ACTIVE', 'COMPLETED'));

-- Update default for status column
ALTER TABLE trips ALTER COLUMN status SET DEFAULT 'PLANNING';

-- Add constraints safely using DO blocks
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'check_trips_current_day'
    ) THEN
        ALTER TABLE trips ADD CONSTRAINT check_trips_current_day CHECK (current_day >= 1);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'check_trips_status'
    ) THEN
        ALTER TABLE trips ADD CONSTRAINT check_trips_status CHECK (status IN ('PLANNING', 'ACTIVE', 'COMPLETED'));
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- 2. Extend ledger_entries table
-- ----------------------------------------------------------------------------
ALTER TABLE ledger_entries ADD COLUMN IF NOT EXISTS actual_amount NUMERIC(12,2) DEFAULT NULL;
ALTER TABLE ledger_entries ADD COLUMN IF NOT EXISTS day_number INT DEFAULT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'check_ledger_entries_day_number'
    ) THEN
        ALTER TABLE ledger_entries ADD CONSTRAINT check_ledger_entries_day_number CHECK (day_number IS NULL OR day_number >= 1);
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- 3. Non-destructive backfill for itineraries.days JSONB
-- Enriches each day in the days JSONB array with a status if not already present:
-- - COMPLETED trips -> days receive "COMPLETED"
-- - PLANNING trips -> days receive "UPCOMING"
-- - ACTIVE trips:
--     day_number < current_day -> "COMPLETED"
--     day_number = current_day -> "IN_PROGRESS"
--     day_number > current_day -> "UPCOMING"
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    r RECORD;
    d_elem JSONB;
    new_days JSONB;
    d_num INT;
    d_status TEXT;
    trip_stat TEXT;
    c_day INT;
BEGIN
    -- Check if table itineraries exists
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'itineraries') THEN
        FOR r IN
            SELECT i.id, i.days, t.status AS trip_status, COALESCE(t.current_day, 1) AS trip_current_day
            FROM itineraries i
            JOIN trips t ON t.id = i.trip_id
            WHERE i.days IS NOT NULL AND jsonb_typeof(i.days) = 'array'
        LOOP
            new_days := '[]'::jsonb;
            trip_stat := UPPER(COALESCE(r.trip_status, 'PLANNING'));
            c_day := r.trip_current_day;

            FOR d_elem IN SELECT * FROM jsonb_array_elements(r.days)
            LOOP
                -- If day already has a status, preserve it
                IF d_elem ? 'status' AND d_elem->>'status' IS NOT NULL AND d_elem->>'status' != '' THEN
                    new_days := new_days || jsonb_build_array(d_elem);
                ELSE
                    -- Extract day_number (handle both day_number and day keys)
                    IF d_elem ? 'day_number' THEN
                        d_num := (d_elem->>'day_number')::INT;
                    ELSIF d_elem ? 'day' THEN
                        d_num := (d_elem->>'day')::INT;
                    ELSE
                        d_num := 1;
                    END IF;

                    -- Determine appropriate status
                    IF trip_stat = 'COMPLETED' THEN
                        d_status := 'COMPLETED';
                    ELSIF trip_stat = 'PLANNING' THEN
                        d_status := 'UPCOMING';
                    ELSIF trip_stat = 'ACTIVE' THEN
                        IF d_num < c_day THEN
                            d_status := 'COMPLETED';
                        ELSIF d_num = c_day THEN
                            d_status := 'IN_PROGRESS';
                        ELSE
                            d_status := 'UPCOMING';
                        END IF;
                    ELSE
                        d_status := 'UPCOMING';
                    END IF;

                    new_days := new_days || jsonb_build_array(d_elem || jsonb_build_object('status', d_status));
                END IF;
            END LOOP;

            UPDATE itineraries
            SET days = new_days
            WHERE id = r.id;
        END LOOP;
    END IF;
END $$;

-- Reload Supabase PostgREST schema cache
NOTIFY pgrst, 'reload schema';

