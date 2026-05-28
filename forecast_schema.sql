-- Forecast table for SalesForecastAgent
-- Run once in Supabase SQL editor (Dashboard → SQL Editor → New query)

CREATE TABLE IF NOT EXISTS forecasts (
    id                  UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    forecast_date       DATE        NOT NULL,
    category            TEXT        NOT NULL,
    predicted_net_sales NUMERIC(12, 2) NOT NULL,
    lower_80            NUMERIC(12, 2),
    upper_80            NUMERIC(12, 2),
    model_version       TEXT        NOT NULL DEFAULT '1.0',
    created_at          TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (forecast_date, category, model_version)
);

-- Index for date-range queries
CREATE INDEX IF NOT EXISTS forecasts_date_idx
    ON forecasts (forecast_date);

-- Index for category-range queries
CREATE INDEX IF NOT EXISTS forecasts_category_idx
    ON forecasts (category);
