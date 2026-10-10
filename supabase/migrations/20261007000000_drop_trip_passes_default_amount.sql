-- Migration: Remove database default for trip_passes.amount to enforce centralized application configuration
-- Prevents split-brain defaults between database and application settings.

ALTER TABLE trip_passes ALTER COLUMN amount DROP DEFAULT;
