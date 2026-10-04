-- ============================================================================
-- Budlance Supabase Migration: Add completion_reason column to trips table
-- Migration: 20261002000001_add_completion_reason.sql
-- ============================================================================

ALTER TABLE trips ADD COLUMN IF NOT EXISTS completion_reason VARCHAR(50) DEFAULT NULL;

-- Reload Supabase PostgREST schema cache
NOTIFY pgrst, 'reload schema';
