-- Migration: Atomic Ledger Entry Deduplication & Batch Atomicity
-- Enforces database-level uniqueness on compound event tags within ledger_entries.
-- Note: Deterministic primary key entry_id (UUIDv5) derived from (trip_id, event_tag)
-- already enforces distributed uniqueness via PRIMARY KEY (id). This unique index provides
-- defense-in-depth across all database instances.

-- 1. Create expression index for event tag lookups
CREATE INDEX IF NOT EXISTS idx_ledger_entries_trip_evt_tag
ON ledger_entries (trip_id, (substring(description from '\[evt:([^\]]+)\]')))
WHERE description LIKE '%[evt:%';

-- 2. Unique Index on (trip_id, extracted event tag)
-- Ensures no two ledger entries for the same trip can possess the same compound event identifier
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_entries_trip_event_tag
ON ledger_entries (trip_id, (substring(description from '\[evt:([^\]]+)\]')))
WHERE description LIKE '%[evt:%';

-- 3. Atomic batch insertion RPC
-- Executes all-or-nothing multi-row insertion in a single PostgreSQL transaction.
CREATE OR REPLACE FUNCTION insert_ledger_entries_batch(p_entries JSONB)
RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_entry JSONB;
BEGIN
    FOR v_entry IN SELECT * FROM jsonb_array_elements(p_entries)
    LOOP
        INSERT INTO ledger_entries (
            id, trip_id, category, description, allocated_amount, planned_amount,
            spent_amount, remaining_amount, actual_amount, day_number, source, created_at
        ) VALUES (
            (v_entry->>'id')::UUID,
            (v_entry->>'trip_id')::UUID,
            v_entry->>'category',
            v_entry->>'description',
            COALESCE((v_entry->>'allocated_amount')::NUMERIC, 0.00),
            COALESCE((v_entry->>'planned_amount')::NUMERIC, 0.00),
            COALESCE((v_entry->>'spent_amount')::NUMERIC, 0.00),
            COALESCE((v_entry->>'remaining_amount')::NUMERIC, 0.00),
            (v_entry->>'actual_amount')::NUMERIC,
            (v_entry->>'day_number')::INT,
            COALESCE(v_entry->>'source', 'user_reported'),
            COALESCE((v_entry->>'created_at')::TIMESTAMPTZ, NOW())
        );
    END LOOP;
    RETURN jsonb_build_object('success', true, 'count', jsonb_array_length(p_entries));
EXCEPTION
    WHEN unique_violation THEN
        RAISE EXCEPTION 'DUPLICATE_LEDGER_ENTRY: %', SQLERRM USING ERRCODE = '23505';
END;
$$;
