-- Run once in the Supabase SQL Editor.
-- Creates the labor_daily table for 7Shifts nightly labor aggregation.

CREATE TABLE IF NOT EXISTS labor_daily (
    date          DATE PRIMARY KEY,
    kit_hours     NUMERIC(8,2)  NOT NULL DEFAULT 0,
    kit_sched     NUMERIC(8,2)  NOT NULL DEFAULT 0,
    kit_cost      NUMERIC(10,2) NOT NULL DEFAULT 0,
    foh_hours     NUMERIC(8,2)  NOT NULL DEFAULT 0,
    foh_sched     NUMERIC(8,2)  NOT NULL DEFAULT 0,
    foh_cost      NUMERIC(10,2) NOT NULL DEFAULT 0,
    total_hours   NUMERIC(8,2)  NOT NULL DEFAULT 0,
    total_sched   NUMERIC(8,2)  NOT NULL DEFAULT 0,
    total_cost    NUMERIC(10,2) NOT NULL DEFAULT 0,
    fetched_at    TIMESTAMPTZ DEFAULT NOW()
);
