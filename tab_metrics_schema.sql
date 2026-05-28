-- Tab-level daily metrics from GoTab API
-- Run once in Supabase SQL editor before running tab_metrics_fetch.py

CREATE TABLE IF NOT EXISTS tab_metrics (
    id                UUID    DEFAULT gen_random_uuid() PRIMARY KEY,
    report_date       DATE    NOT NULL UNIQUE,
    tab_count         INT     NOT NULL,   -- all tabs that day
    revenue_tab_count INT     NOT NULL,   -- tabs with total > 0
    guest_count       INT     NOT NULL,   -- sum of numGuests across all tabs
    hourly_opens      JSONB,             -- { "19": 34, "20": 82, ... }  UTC hour key
    zone_tab_counts   JSONB,             -- { "Main Zone": 84, "Bowling Lane": 10, ... }
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS tab_metrics_date_idx ON tab_metrics (report_date);
