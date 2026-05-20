-- Run once in the Supabase SQL Editor.
-- Creates the daily_revenue aggregation table and adds per-game columns to ts_events.

-- ---------------------------------------------------------------------------
-- daily_revenue: one row per date + source ('gotab' | 'tripleseat')
-- Populated nightly by api/revenue_report.py after both upstream syncs finish.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_revenue (
    id            BIGSERIAL PRIMARY KEY,
    report_date   DATE    NOT NULL,
    source        TEXT    NOT NULL CHECK (source IN ('gotab', 'tripleseat')),
    food          NUMERIC(12,2) NOT NULL DEFAULT 0,
    beverage      NUMERIC(12,2) NOT NULL DEFAULT 0,
    mini_golf     NUMERIC(12,2) NOT NULL DEFAULT 0,
    bowling       NUMERIC(12,2) NOT NULL DEFAULT 0,
    karaoke       NUMERIC(12,2) NOT NULL DEFAULT 0,
    darts         NUMERIC(12,2) NOT NULL DEFAULT 0,
    shuffle_board NUMERIC(12,2) NOT NULL DEFAULT 0,
    pool          NUMERIC(12,2) NOT NULL DEFAULT 0,
    merchandise   NUMERIC(12,2) NOT NULL DEFAULT 0,
    events        NUMERIC(12,2) NOT NULL DEFAULT 0,
    open_item     NUMERIC(12,2) NOT NULL DEFAULT 0,
    bottle_svc    NUMERIC(12,2) NOT NULL DEFAULT 0,
    reservations  NUMERIC(12,2) NOT NULL DEFAULT 0,
    gift_card     NUMERIC(12,2) NOT NULL DEFAULT 0,
    redeemed      NUMERIC(12,2) NOT NULL DEFAULT 0,
    synced_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (report_date, source)
);

-- ---------------------------------------------------------------------------
-- Per-game revenue breakdown on ts_events.
-- Populated by tripleseat_fetch.py from the Tripleseat API category_totals.
-- Rows synced before this migration will have NULL values.
-- ---------------------------------------------------------------------------
ALTER TABLE ts_events
    ADD COLUMN IF NOT EXISTS bowling_amount       NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS mini_golf_amount     NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS darts_amount         NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS shuffle_board_amount NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS pool_amount          NUMERIC(12,2);
