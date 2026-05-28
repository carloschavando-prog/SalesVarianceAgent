-- Daily weather for Dayton, OH — sourced from Open-Meteo (free, no key)
-- Run once in Supabase SQL editor before running weather_fetch.py --backfill

CREATE TABLE IF NOT EXISTS daily_weather (
    report_date         DATE    PRIMARY KEY,
    max_temp_f          NUMERIC(5,1),
    min_temp_f          NUMERIC(5,1),
    precipitation_in    NUMERIC(5,2),   -- total daily precip in inches
    wind_speed_mph      NUMERIC(5,1),   -- max 10m wind
    weather_code        SMALLINT,       -- WMO weather interpretation code
    weather_desc        TEXT,           -- human label derived from code
    source              TEXT DEFAULT 'open-meteo',
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS daily_weather_date_idx ON daily_weather (report_date);
