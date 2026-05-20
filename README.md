# GoTab Product Mix — Daily Data Pipeline

Automated daily pipeline that fetches the previous day's product mix data from the GoTab API and loads it into a Supabase database. Runs every morning at 4:00 AM EST via Vercel Cron.

---

## What It Does

1. Triggers at 4:00 AM EST every day
2. Fetches all ledger entries for yesterday from the GoTab GraphQL API
3. Aggregates them into a product mix summary (gross qty, net qty, gross sales, net sales, refunds, comps, voids)
4. Writes the results to Supabase (`report_dates` + `sales` tables)
5. Skips silently if that date is already loaded (safe to re-run)

---

## Project Structure

```
GoTab_Product_Mix/
  api/
    daily_fetch.py    # Vercel serverless function — GoTab fetch + Supabase write
  vercel.json         # Cron schedule (4 AM EST = 9 AM UTC)
  requirements.txt    # No third-party dependencies
  README.md
```

---

## Supabase Schema

```sql
CREATE TABLE IF NOT EXISTS report_dates (
    id          BIGSERIAL PRIMARY KEY,
    report_date DATE NOT NULL UNIQUE,
    filename    TEXT
);

CREATE TABLE IF NOT EXISTS sales (
    id               BIGSERIAL PRIMARY KEY,
    report_date_id   BIGINT REFERENCES report_dates(id),
    report_date      DATE NOT NULL,
    category         TEXT,
    product          TEXT NOT NULL,
    zone             TEXT,
    gross_qty        NUMERIC,
    net_qty          NUMERIC,
    gross_sales      NUMERIC,
    net_sales        NUMERIC,
    refund_qty       NUMERIC,
    refund_amount    NUMERIC,
    comp_qty         NUMERIC,
    comp_amount      NUMERIC,
    void_qty         NUMERIC,
    void_amount      NUMERIC,
    is_discount      BOOLEAN DEFAULT FALSE
);
```

Run this once in the Supabase SQL Editor to create the tables before deploying.

---

## Environment Variables

Set these in your Vercel project settings under **Settings → Environment Variables**:

| Variable | Description |
|---|---|
| `GOTAB_API_ACCESS_ID` | GoTab API access ID |
| `GOTAB_API_ACCESS_SECRET` | GoTab API access secret |
| `GOTAB_LOCATION_ID` | GoTab location ID (defaults to `112479`) |
| `SUPABASE_URL` | Supabase project URL (e.g. `https://xxx.supabase.co`) |
| `SUPABASE_SERVICE_KEY` | Supabase service role key (`sb_secret_...`) |
| `CRON_SECRET` | A random secret string to secure the cron endpoint |

---

## Deployment

### 1. Push to GitHub
```bash
cd /path/to/GoTab_Product_Mix
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/GoTab_Product_Mix.git
git push -u origin main
```

### 2. Connect to Vercel
- Go to [vercel.com](https://vercel.com) → **Add New Project**
- Import the `GoTab_Product_Mix` GitHub repository
- Add all environment variables listed above
- Deploy

### 3. Verify the Cron Job
- In Vercel dashboard → your project → **Cron Jobs** tab
- You should see one job: `GET /api/daily_fetch` at `0 9 * * *`
- Click **Trigger** to run it manually and confirm it works

---

## Manual Trigger

You can trigger the function manually at any time by visiting:

```
https://your-vercel-domain.vercel.app/api/daily_fetch
```

You'll get a JSON response:
```json
{ "status": "ok: loaded 94 rows for 2026-05-20 (net $4,123.45)" }
```

Or if already loaded:
```json
{ "status": "skipped: 2026-05-20 already in database" }
```

---

## Category Mapping

The pipeline maps GoTab product categories to these reporting buckets:

| Bucket | GoTab Category |
|---|---|
| Food | Chicken., Pizza and Flatbreads, Pretzels, Fry Platters, Wraps, Half Pound Burgers, Tater Kegs, Extra Sauces and Cheese Dips |
| Beverage | Beverage (or blank category) |
| Entertainment | Entertainment — Mini Golf, Bowling, Darts, Pool Table |
| Karaoke | Reservations — Karaoke, Royal Room, Prime Room, Ocean Room, Disco Room, Gem Room, Royal Sun |
| Reservations | All other Reservations |

---

## Notes

- Only `NET_SALES` accounting stream entries are included — taxes, tips, autograt, deferred revenue, and processor entries are excluded automatically
- Discounts (negative net sales or "Discount" in product name) are flagged with `is_discount = TRUE`
- The GoTab API rate limit is 4 requests/sec — the function includes built-in pacing
- Vercel Cron Jobs require a **Pro plan** or higher
