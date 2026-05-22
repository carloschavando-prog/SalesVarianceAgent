# SalesVarianceAgent — On Par Entertainment

Automated revenue reporting pipeline for On Par Entertainment (Dayton/Troy, OH).
Pulls data from GoTab (POS) and Tripleseat (events) into Supabase nightly, and
serves a live HTML sales variance report at a password-protected URL.

---

## Live Report

```
https://sales-variance-agent.vercel.app/api/daily_report?key=4464
```

- **Year tabs:** FY2026 · FY2025 · FY2024 · FY2023 (partial — GoTab data starts Oct 19, 2023)
- **Layout:** Newest period/week at top, scroll down for older data
- **Columns:** Date | Day | Food | Bev | Games | Karaoke | Events | Other | Total | LY Total | +/- $ | +/- %
- **LY Total** = same fiscal day 364 days prior (identical fiscal position, prior year)
- Red = below last year · Green = above last year
- Header row frozen — stays visible while scrolling
- Auto-refreshes every 5 minutes
- Password: `4464` (passed as `?key=4464` in URL)

---

## Fiscal Calendar

**5-4-4 quarter pattern.** Periods 1, 4, 7, 10 = 5 weeks. All others = 4 weeks.

| FY | Start Date | End Date |
|----|-----------|---------|
| FY2023 | Jan 2, 2023 | Dec 31, 2023 |
| FY2024 | Jan 1, 2024 | Dec 29, 2024 |
| FY2025 | Dec 30, 2024 | Dec 28, 2025 |
| FY2026 | Dec 29, 2025 | Dec 27, 2026 |

| Period | Weeks | FY2026 Dates |
|--------|-------|-------------|
| P1 | 5 | Dec 29, 2025 – Feb 1, 2026 |
| P2 | 4 | Feb 2 – Mar 1, 2026 |
| P3 | 4 | Mar 2 – Mar 29, 2026 |
| P4 | 5 | Mar 30 – May 3, 2026 |
| P5 | 4 | May 4 – May 31, 2026 |
| P6 | 4 | Jun 1 – Jun 28, 2026 |
| P7 | 5 | Jun 29 – Aug 2, 2026 |
| P8 | 4 | Aug 3 – Aug 30, 2026 |
| P9 | 4 | Aug 31 – Sep 27, 2026 |
| P10 | 5 | Sep 28 – Nov 1, 2026 |
| P11 | 4 | Nov 2 – Nov 29, 2026 |
| P12 | 4 | Nov 30 – Dec 27, 2026 |

---

## Revenue Category Mapping

### GoTab → Report Buckets

| Column | GoTab Categories |
|--------|----------------|
| **Food** | Chicken. / Dessert / Event Food / Extra Sauces and Cheese Dips / Fry Platters / Half Pound Burgers / Legacy Menu Items / Pizza and Flatbreads / Pretzels / Tacos / Tater Kegs / Wraps |
| **Bev** | Beverage / Soda Pop / Wine |
| **Games** | Entertainment (all products: Mini Golf / Bowling / Darts / Shuffle Board / Pool Table) |
| **Karaoke** | Karaoke |
| **Other** | Merchandise / Reservations / Open Item / Bottle Service / Gift Card / Redeemed |

### Tripleseat → Report Buckets

| Column | Tripleseat Field |
|--------|----------------|
| **Food** | `food_amount` (split from BEO document) |
| **Bev** | `beverage_amount` (split from BEO document) |
| **Events** | `events_amount` (booking fees + extra hours) |
| **Games** | `bowling_amount` + `mini_golf_amount` + `darts_amount` + `shuffle_board_amount` + `pool_amount` |

> GoTab EVENTS column is always $0 — events revenue comes from Tripleseat only.

---

## Architecture

```
GoTab API
    └── daily_fetch.py  (5 AM ET daily)
            └── sales table (Supabase)
                    └──╮
Tripleseat API          ├── daily_report.py ──→ Live HTML report (on-demand)
    └── tripleseat_fetch.py  (7 AM ET daily)
            └── ts_events table (Supabase)
                    └──╯
                    └── revenue_report.py  (9 AM ET daily)
                            └── daily_revenue table (Supabase)
                                    └── revenue_export.py ──→ Excel .xlsx download
```

---

## Project Structure

```
SalesVarianceAgent/
  api/
    daily_fetch.py          GoTab ledger → sales table          (cron 5 AM ET)
    tripleseat_fetch.py     Tripleseat events → ts_events table  (cron 7 AM ET)
    beo_parser.py           Splits food/bev/events from BEO documents
    revenue_report.py       Aggregates both → daily_revenue table (cron 9 AM ET)
    revenue_export.py       On-demand Excel export
    daily_report.py         Live HTML variance report
  revenue_schema.sql        Run once in Supabase — creates daily_revenue + per-game columns
  tripleseat_schema.sql     Run once in Supabase — creates ts_bookings, ts_events, ts_leads
  vercel.json               Cron schedules
  pyproject.toml
  requirements.txt
  README.md
```

---

## Cron Schedule

| Endpoint | UTC | ET | Purpose |
|----------|-----|----|---------|
| `/api/daily_fetch` | `0 9 * * *` | 5:00 AM | Pull GoTab sales into `sales` table |
| `/api/tripleseat_fetch` | `0 11 * * *` | 7:00 AM | Pull Tripleseat events into `ts_events` |
| `/api/revenue_report` | `0 13 * * *` | 9:00 AM | Aggregate both → `daily_revenue` table |

> `/api/daily_report` is on-demand only (no cron) — it queries Supabase live each page load.

---

## Environment Variables

Set in **Vercel → SalesVarianceAgent → Settings → Environment Variables**:

| Variable | Description |
|----------|-------------|
| `SUPABASE_URL` | `https://jrzfczhsqshejnrxgmuq.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Supabase service role key (`sb_secret_...`) |
| `CRON_SECRET` | Protects cron endpoints (`onpar-cron-2026-xK9mP`) |
| `REPORT_KEY` | Password for the HTML report URL (`4464`) |
| `GOTAB_API_ACCESS_ID` | GoTab API access ID |
| `GOTAB_API_ACCESS_SECRET` | GoTab API access secret |
| `GOTAB_LOCATION_ID` | GoTab location ID (default: `112479`) |
| `TS_CLIENT_ID` | Tripleseat OAuth2 application UID |
| `TS_CLIENT_SECRET` | Tripleseat OAuth2 application secret |

---

## Supabase Setup (one-time)

Run these two SQL files in **Supabase → SQL Editor** before the first nightly sync:

1. `tripleseat_schema.sql` — creates `ts_bookings`, `ts_events`, `ts_leads` tables
2. `revenue_schema.sql` — creates `daily_revenue` table and adds per-game columns to `ts_events`

---

## Data Range in Supabase

| Table | Earliest Date | Latest Date |
|-------|--------------|------------|
| `sales` (GoTab) | Oct 19, 2023 | present |
| `ts_events` (Tripleseat) | Nov 10, 2023 | present |

FY2023 tab shows partial data (mid-P4 onward). FY2024, FY2025, FY2026 are complete.

---

## Manual Triggers

```bash
# Pull yesterday's GoTab sales
curl -s -H "Authorization: Bearer onpar-cron-2026-xK9mP" \
  https://sales-variance-agent.vercel.app/api/daily_fetch

# Pull yesterday's Tripleseat events
curl -s -H "Authorization: Bearer onpar-cron-2026-xK9mP" \
  https://sales-variance-agent.vercel.app/api/tripleseat_fetch

# Aggregate into daily_revenue (supports ?date=YYYY-MM-DD for backfill)
curl -s -H "Authorization: Bearer onpar-cron-2026-xK9mP" \
  "https://sales-variance-agent.vercel.app/api/revenue_report?date=2026-05-01"

# Download Excel report for a period
curl -o report_p6.xlsx \
  "https://sales-variance-agent.vercel.app/api/revenue_export?period=6&year=2026"
```

---

## Deployment

- **GitHub:** `https://github.com/carloschavando-prog/SalesVarianceAgent`
- **Vercel:** Auto-deploys on every push to `main`
- **Live URL:** `https://sales-variance-agent.vercel.app`

---

## Backups

| Location | Path |
|----------|------|
| GitHub | `https://github.com/carloschavando-prog/SalesVarianceAgent` |
| Local visible | `~/Documents/SalesVarianceAgent/` |
| Local hidden | `~/.SalesVarianceAgent/` |

To view hidden folder in Finder: **Cmd + Shift + .** (toggles hidden files)
