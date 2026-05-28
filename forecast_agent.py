#!/usr/bin/env python3
"""
SalesForecastAgent — On Par Entertainment  v2.0
Predicts next 7–30 days of daily net sales by category group,
with hourly breakdown for labor scheduling.

Signals layered in priority order:
  1. EW DOW seasonal mean + linear trend          (base model)
  2. Holiday calendar — DOW override + multiplier (holiday_calendar.py)
  3. Weather adjustment — rain/temp effect         (weather_fetch.py)
  4. Tripleseat event uplift                       (ts_events table)
  5. Hourly profile — distributes daily total      (tab_metrics table)

Usage:
  python forecast_agent.py                   # 30-day forecast, print + JSON
  python forecast_agent.py --days 14         # shorter horizon
  python forecast_agent.py --push            # also write to Supabase
  python forecast_agent.py --no-events       # skip event uplift
  python forecast_agent.py --no-weather      # skip weather adjustment
"""

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean, stdev

from holiday_calendar import get_effective_dow, get_multiplier, is_closed, et_offset
from weather_fetch import (load_weather_from_supabase, fetch_forecast_weather,
                            weather_multiplier)

# ── Configuration ─────────────────────────────────────────────────────────────

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", ""))

CATEGORY_MAP = {
    "Chicken":                      "Food",
    "Chicken.":                     "Food",
    "Dessert":                      "Food",
    "Event Food":                   "Food",
    "Extra Sauces and Cheese Dips": "Food",
    "Fry Platters":                 "Food",
    "Half Pound Burgers":           "Food",
    "Legacy menu Items":            "Food",
    "Lunch Sandwiches and Wings":   "Food",
    "Mozzarella Sticks":            "Food",
    "Patio Party Menu":             "Food",
    "Pizza and Flatbreads":         "Food",
    "Pretzels":                     "Food",
    "Sides Menu":                   "Food",
    "Taco Tuesday Tacos":           "Food",
    "Tacos":                        "Food",
    "Tater Kegs":                   "Food",
    "Wraps":                        "Food",
    "Beverage":                     "Beverage",
    "Soda Pop":                     "Beverage",
    "Wine":                         "Beverage",
    "Entertainment":                "Entertainment",
    "Karaoke":                      "Karaoke",
    "Reservations":                 "Karaoke",   # karaoke reservations → Karaoke
    "Merchandise":                  "Merchandise",
    "Bottle Service":               "Karaoke",   # legacy — fold into Karaoke if any remain
    "Open Item":                    "Open Item",
    "Tripleseat":                   "Events",
}

FORECAST_GROUPS = [
    "Food", "Beverage", "Entertainment", "Karaoke",
    "Merchandise", "Open Item", "Events",
]

# ── Totals groupings ──────────────────────────────────────────────────────────
# Food Total  = Food
# FOH Total   = everything the floor sells (all non-Food)
# Grand Total = Food + FOH
FOOD_CATS = {"Food"}
FOH_CATS  = {"Beverage", "Entertainment", "Karaoke", "Merchandise", "Open Item", "Events"}

EVENT_STATUS_WEIGHT = {
    "DEFINITE":     1.00,
    "TENTATIVE":    0.60,
    "PROSPECT":     0.25,
    "CLOSED":       1.00,
    "LOST":         0.00,
    "PENDING_AUTH": 0.80,
}

# Fallback hourly shape (fraction of daily tabs per ET hour) when tab_metrics
# not yet backfilled.  Derived from a sample Saturday + domain knowledge.
# Key = local ET hour (0-23).
_FALLBACK_HOURLY: dict = {
    # Weekdays (Mon-Thu): shorter window, similar peak time
    0: {16: 0.05, 17: 0.09, 18: 0.12, 19: 0.14, 20: 0.18, 21: 0.17,
        22: 0.12, 23: 0.08, 0:  0.05},
    1: {16: 0.05, 17: 0.09, 18: 0.12, 19: 0.14, 20: 0.18, 21: 0.17,
        22: 0.12, 23: 0.08, 0:  0.05},
    2: {16: 0.05, 17: 0.09, 18: 0.12, 19: 0.14, 20: 0.18, 21: 0.17,
        22: 0.12, 23: 0.08, 0:  0.05},
    3: {16: 0.04, 17: 0.08, 18: 0.11, 19: 0.16, 20: 0.20, 21: 0.19,
        22: 0.13, 23: 0.07, 0:  0.02},
    # Friday: earlier and longer
    4: {15: 0.02, 16: 0.06, 17: 0.10, 18: 0.12, 19: 0.14, 20: 0.17,
        21: 0.16, 22: 0.11, 23: 0.08, 0: 0.04},
    # Saturday: biggest, latest tail
    5: {15: 0.01, 16: 0.02, 17: 0.06, 18: 0.08, 19: 0.13, 20: 0.18,
        21: 0.17, 22: 0.14, 23: 0.10, 0: 0.06, 1: 0.04, 2: 0.01},
    # Sunday: earlier close
    6: {14: 0.02, 15: 0.04, 16: 0.08, 17: 0.12, 18: 0.16, 19: 0.18,
        20: 0.17, 21: 0.12, 22: 0.07, 23: 0.04},
}

MODEL_VERSION           = "2.0"
DEFAULT_FORECAST_DAYS   = 30
DEFAULT_HISTORY_WEEKS   = 52
SEASONAL_LOOKBACK_WEEKS = 26
TREND_LOOKBACK_WEEKS    = 12
WEEKLY_DECAY            = 0.88
MIN_EVENT_GUESTS        = 20
PAGE_SIZE               = 1000

# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }


def supabase_get(table, params):
    url = f"{SUPABASE_URL}/rest/v1/{table}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_sb_headers())
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase GET /{table} → {e.code}: {e.read().decode()}") from e


def supabase_upsert(table, rows, on_conflict):
    url     = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={urllib.parse.quote(on_conflict)}"
    headers = {**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
    data    = json.dumps(rows).encode()
    req     = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase upsert /{table} → {e.code}: {e.read().decode()}") from e


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_daily_sales(history_weeks: int) -> dict:
    cutoff  = (date.today() - timedelta(weeks=history_weeks)).isoformat()
    print(f"Fetching sales history from {cutoff} onwards...")
    daily   = defaultdict(lambda: defaultdict(float))
    offset, fetched = 0, 0
    while True:
        rows = supabase_get("sales", {
            "select":      "report_date,category,net_sales",
            "report_date": f"gte.{cutoff}",
            "order":       "report_date.asc",
            "limit":       PAGE_SIZE,
            "offset":      offset,
        })
        if not rows:
            break
        for row in rows:
            grp = CATEGORY_MAP.get(row["category"], row["category"])
            net = float(row["net_sales"] or 0)
            daily[row["report_date"][:10]][grp] += net
        fetched += len(rows)
        print(f"  {fetched:,} rows...", end="\r", flush=True)
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    print(f"\n  Done — {fetched:,} rows across {len(daily)} dates.")
    return dict(daily)


def fetch_events(horizon_days: int = 90) -> dict:
    history_cutoff = (date.today() - timedelta(weeks=DEFAULT_HISTORY_WEEKS)).isoformat()
    future_cutoff  = (date.today() + timedelta(days=horizon_days)).isoformat()
    rows, offset   = [], 0
    while True:
        params = urllib.parse.urlencode([
            ("select",     "event_date,name,status,guest_count,deleted_at"),
            ("event_date", f"gte.{history_cutoff}"),
            ("event_date", f"lte.{future_cutoff}"),
            ("order",      "event_date.asc"),
            ("limit",      PAGE_SIZE),
            ("offset",     offset),
        ])
        url = f"{SUPABASE_URL}/rest/v1/ts_events?{params}"
        req = urllib.request.Request(url, headers=_sb_headers())
        try:
            with urllib.request.urlopen(req) as r:
                batch = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Supabase /ts_events → {e.code}: {e.read().decode()}") from e
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    by_date: dict = defaultdict(lambda: {"effective_guests": 0.0, "events": []})
    for r in rows:
        if r.get("deleted_at"):
            continue
        d      = (r.get("event_date") or "")[:10]
        if not d or d > future_cutoff:
            continue
        weight = EVENT_STATUS_WEIGHT.get(r.get("status") or "PROSPECT", 0.0)
        if weight == 0.0:
            continue
        guests = (r.get("guest_count") or 0) * weight
        by_date[d]["effective_guests"] += guests
        by_date[d]["events"].append({
            "name":   r.get("name", ""),
            "status": r.get("status", ""),
            "guests": r.get("guest_count") or 0,
            "weight": weight,
        })
    return dict(by_date)


def load_weather(horizon_days: int) -> dict:
    """
    Returns { date_str: weather_row } covering:
    - historical dates (from Supabase daily_weather table)
    - next 16 days (live from Open-Meteo forecast)
    Falls back gracefully if Supabase table does not exist yet.
    """
    cutoff = (date.today() - timedelta(weeks=DEFAULT_HISTORY_WEEKS)).isoformat()
    end    = (date.today() + timedelta(days=min(horizon_days, 16))).isoformat()

    weather: dict = {}

    # Historical
    try:
        weather.update(load_weather_from_supabase(cutoff, date.today().isoformat()))
    except Exception:
        pass   # table not yet created — skip historical weather

    # Live forecast (Open-Meteo, free, no key)
    try:
        rows = fetch_forecast_weather(min(horizon_days, 16))
        for r in rows:
            weather[r["report_date"]] = r
    except Exception as e:
        print(f"  Warning: weather forecast unavailable ({e})")

    return weather


def load_tab_hourly_profiles(history_weeks: int) -> dict:
    """
    Returns { dow_int: { et_hour: fraction } } from tab_metrics.hourly_opens.
    Falls back to _FALLBACK_HOURLY if table is empty or not yet created.
    """
    cutoff = (date.today() - timedelta(weeks=history_weeks)).isoformat()
    try:
        rows = supabase_get("tab_metrics", {
            "select":      "report_date,hourly_opens",
            "report_date": f"gte.{cutoff}",
            "limit":       400,
        })
    except Exception:
        print("  tab_metrics not yet available — using fallback hourly profile.")
        return _FALLBACK_HOURLY

    if not rows:
        return _FALLBACK_HOURLY

    dow_buckets: dict = defaultdict(lambda: defaultdict(list))
    for row in rows:
        ho = row.get("hourly_opens")
        if not ho:
            continue
        d   = date.fromisoformat(row["report_date"])
        dow = d.weekday()
        tz_off = et_offset(d)
        total  = sum(ho.values())
        if total == 0:
            continue
        for hr_str, cnt in ho.items():
            et_hr = (int(hr_str) + tz_off) % 24
            dow_buckets[dow][et_hr].append(cnt / total)

    profiles: dict = {}
    for dow, hour_fracs in dow_buckets.items():
        raw = {hr: mean(fracs) for hr, fracs in hour_fracs.items() if fracs}
        total = sum(raw.values())
        profiles[dow] = {hr: f / total for hr, f in raw.items()} if total else {}

    return profiles if profiles else _FALLBACK_HOURLY


# ── Math helpers ──────────────────────────────────────────────────────────────

def weighted_mean(values: list, weights: list) -> float:
    total_w = sum(weights)
    return sum(v * w for v, w in zip(values, weights)) / total_w if total_w else 0.0


def linreg(xs: list, ys: list):
    n = len(xs)
    if n < 2:
        return 0.0, (mean(ys) if ys else 0.0)
    mx, my = mean(xs), mean(ys)
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    slope = num / den if den else 0.0
    return slope, my - slope * mx


def ci_halfwidth(residuals: list, z: float = 1.282) -> float:
    return z * stdev(residuals) if len(residuals) >= 3 else 0.0


# ── Event uplift ──────────────────────────────────────────────────────────────

def compute_event_uplift_rates(daily: dict, events: dict, cats: list) -> dict:
    today_str   = date.today().isoformat()
    event_dates = {
        d for d, ev in events.items()
        if ev["effective_guests"] >= MIN_EVENT_GUESTS and d < today_str
    }
    ev_sales:  dict = defaultdict(lambda: defaultdict(list))
    non_sales: dict = defaultdict(lambda: defaultdict(list))
    ev_guests: dict = defaultdict(list)

    for d in sorted(daily.keys()):
        if d >= today_str or sum(daily[d].values()) < 1:
            continue
        dow = date.fromisoformat(d).weekday()
        if d in event_dates:
            for cat in cats:
                ev_sales[cat][dow].append(daily[d].get(cat, 0.0))
            ev_guests[dow].append(events[d]["effective_guests"])
        else:
            for cat in cats:
                non_sales[cat][dow].append(daily[d].get(cat, 0.0))

    rates: dict = {}
    for cat in cats:
        rates[cat] = {}
        for dow in range(7):
            ev_s  = ev_sales[cat].get(dow, [])
            non_s = non_sales[cat].get(dow, [])
            g     = ev_guests.get(dow, [])
            if len(ev_s) < 2 or len(non_s) < 2 or not g:
                rates[cat][dow] = 0.0
                continue
            avg_g = mean(g)
            delta = mean(ev_s) - mean(non_s)
            rates[cat][dow] = max(-30.0, min(100.0, delta / avg_g)) if avg_g else 0.0
    return rates


# ── Core forecasting ──────────────────────────────────────────────────────────

def build_forecasts(daily: dict, horizon: int = 30,
                    events=None, weather: dict = None) -> tuple:
    """
    Returns (results_list, last_history_date, all_dow_means).

    Each result item:
      { forecast_date, category, predicted, lower_80, upper_80,
        event_guests, event_uplift, event_names,
        holiday_name, holiday_mult, weather_mult, day_closed }
    """
    all_dates  = sorted(daily.keys())
    if not all_dates:
        raise ValueError("No sales data returned from Supabase.")

    last_date  = date.fromisoformat(all_dates[-1])
    open_dates = sorted(d for d in all_dates if sum(daily[d].values()) > 1.0)
    print(f"  History: {all_dates[0]} → {all_dates[-1]}  "
          f"({len(all_dates)} dates, {len(open_dates)} open)")

    present = {grp for day in daily.values() for grp in day}
    cats    = [g for g in FORECAST_GROUPS if g in present]
    cats   += [g for g in present if g not in cats]

    all_dow_means: dict = {}
    results = []

    for cat in cats:
        ts     = [(d, daily[d].get(cat, 0.0)) for d in open_dates]
        values = [v for _, v in ts]
        if not ts or all(v == 0 for v in values):
            continue

        # ── Seasonal DOW baselines (EW) ────────────────────────────────
        seasonal_ts = ts[-(SEASONAL_LOOKBACK_WEEKS * 7):]

        dow_vals: dict = defaultdict(list)
        for d, v in seasonal_ts:
            dow_vals[date.fromisoformat(d).weekday()].append(v)

        overall_w        = [WEEKLY_DECAY ** (len(values) - 1 - i) for i in range(len(values))]
        overall_mean_val = weighted_mean(values, overall_w)

        dow_means: dict = {}
        for dow, vals in dow_vals.items():
            n = len(vals)
            w = [WEEKLY_DECAY ** (n - 1 - i) for i in range(n)]
            dow_means[dow] = weighted_mean(vals, w)
        for dow in range(7):
            if dow not in dow_means:
                dow_means[dow] = overall_mean_val

        all_dow_means[cat] = dow_means

        # ── Trend ─────────────────────────────────────────────────────
        trend_cutoff = (last_date - timedelta(weeks=TREND_LOOKBACK_WEEKS)).isoformat()
        trend_ts     = [(d, v) for d, v in ts if d >= trend_cutoff]
        if len(trend_ts) >= 14:
            anchor  = date.fromisoformat(trend_ts[0][0])
            weekly: dict = defaultdict(float)
            for d, v in trend_ts:
                weekly[(date.fromisoformat(d) - anchor).days // 7] += v
            wk_keys = sorted(weekly)
            wk_vals = [weekly[k] for k in wk_keys]
            slope_wk, _ = linreg(wk_keys, wk_vals)
            max_adj  = overall_mean_val * 0.002 if overall_mean_val else 0.0
            daily_trend = max(-max_adj, min(max_adj, slope_wk / 7.0))
        else:
            daily_trend = 0.0

        # ── CI ────────────────────────────────────────────────────────
        residuals = [v - dow_means[date.fromisoformat(d).weekday()]
                     for d, v in seasonal_ts]
        ci_hw = ci_halfwidth(residuals)

        # ── Emit base forecasts ────────────────────────────────────────
        for days_ahead in range(1, horizon + 1):
            fdate = last_date + timedelta(days=days_ahead)

            from holiday_calendar import get_day_info
            hinfo = get_day_info(fdate)

            # Closed days → zero
            if hinfo.is_closed:
                results.append({
                    "forecast_date": fdate.isoformat(),
                    "category":      cat,
                    "predicted":     0.0,
                    "lower_80":      0.0,
                    "upper_80":      0.0,
                    "event_guests":  0.0,
                    "event_uplift":  0.0,
                    "event_names":   [],
                    "holiday_name":  hinfo.name,
                    "holiday_mult":  0.0,
                    "weather_mult":  1.0,
                    "day_closed":    True,
                })
                continue

            # ── Effective DOW (holiday override) ──────────────────────
            eff_dow  = hinfo.effective_dow if hinfo.dow_override is not None else fdate.weekday()
            base     = dow_means.get(eff_dow, overall_mean_val)
            pred     = max(0.0, base + daily_trend * days_ahead)

            # ── Holiday multiplier ────────────────────────────────────
            pred    *= hinfo.multiplier

            # ── Weather multiplier ────────────────────────────────────
            w_row    = (weather or {}).get(fdate.isoformat(), {})
            w_mult   = 1.0
            if w_row:
                is_wknd = fdate.weekday() >= 4
                w_mult  = weather_multiplier(
                    w_row.get("max_temp_f"),
                    w_row.get("precipitation_in", 0.0),
                    w_row.get("weather_code", 0),
                    is_wknd,
                )
                pred   *= w_mult

            # ── Scale CI proportionally ───────────────────────────────
            scale  = hinfo.multiplier * w_mult
            ci_adj = ci_hw * scale
            lower  = max(0.0, pred - ci_adj)
            upper  = pred + ci_adj

            results.append({
                "forecast_date": fdate.isoformat(),
                "category":      cat,
                "predicted":     round(pred, 2),
                "lower_80":      round(lower, 2),
                "upper_80":      round(upper, 2),
                "event_guests":  0.0,
                "event_uplift":  0.0,
                "event_names":   [],
                "holiday_name":  hinfo.name if hinfo.type != "normal" else "",
                "holiday_mult":  round(hinfo.multiplier, 3),
                "weather_mult":  round(w_mult, 3),
                "day_closed":    False,
            })

    # ── Event uplifts ──────────────────────────────────────────────────────
    if events:
        uplift_rates = compute_event_uplift_rates(daily, events, cats)
        for r in results:
            if r["day_closed"]:
                continue
            d  = r["forecast_date"]
            ev = events.get(d)
            if not ev or ev["effective_guests"] < 1:
                continue
            cat  = r["category"]
            dow  = date.fromisoformat(d).weekday()
            rate = uplift_rates.get(cat, {}).get(dow, 0.0)
            uplift = max(0.0, rate * ev["effective_guests"])
            r["predicted"]    = round(r["predicted"] + uplift, 2)
            r["upper_80"]     = round(r["upper_80"]  + uplift, 2)
            r["lower_80"]     = round(max(0.0, r["lower_80"] + uplift * 0.5), 2)
            r["event_guests"] = round(ev["effective_guests"], 1)
            r["event_uplift"] = round(uplift, 2)
            r["event_names"]  = [
                f"{e['name']} ({e['guests']}g/{e['status']})"
                for e in ev["events"] if e["weight"] > 0
            ]

    return results, last_date, all_dow_means


# ── Hourly breakdown ──────────────────────────────────────────────────────────

def build_hourly_forecast(results: list, hourly_profiles: dict,
                           last_date) -> dict:
    """
    Returns { date_str: { et_hour: predicted_revenue } }

    Uses the DOW hourly profile to distribute each day's total revenue
    across hours.  Holiday days use their override DOW's profile.
    """
    from holiday_calendar import get_day_info

    daily_totals: dict = defaultdict(float)
    for r in results:
        if not r["day_closed"]:
            daily_totals[r["forecast_date"]] += r["predicted"]

    hourly: dict = {}
    for d_str, total in daily_totals.items():
        fdate   = date.fromisoformat(d_str)
        hinfo   = get_day_info(fdate)
        profile_dow = hinfo.dow_override if hinfo.dow_override is not None else fdate.weekday()
        profile = hourly_profiles.get(profile_dow,
                  hourly_profiles.get(fdate.weekday(), _FALLBACK_HOURLY.get(fdate.weekday(), {})))
        if not profile:
            continue
        hourly[d_str] = {hr: round(total * frac, 2) for hr, frac in profile.items()}

    return hourly


def shift_recommendations(hourly_day: dict) -> dict:
    """
    Given { et_hour: revenue }, return suggested staff wave times.
    Returns {
        "prep":       "3:00 PM",  # 1h before doors, kitchen in
        "open":       "4:00 PM",  # first guests expected
        "build":      "6:00 PM",  # ramp to medium staffing
        "peak_start": "8:00 PM",  # full staff
        "peak_end":   "10:00 PM", # start cuts
        "close":      "1:00 AM",  # last staff out
    }
    """
    if not hourly_day:
        return {}

    total      = sum(hourly_day.values())
    peak_rev   = max(hourly_day.values())
    hours      = sorted(hourly_day.keys())

    def fmt(h: int) -> str:
        h = h % 24
        suffix = "AM" if h < 12 else "PM"
        display = h if h <= 12 else h - 12
        if display == 0:
            display = 12
        return f"{display}:00 {suffix}"

    # "Open" = first hour where hourly revenue > 3% of daily total
    open_h  = next((h for h in hours if hourly_day[h] >= total * 0.03), hours[0] if hours else 16)
    # "Build" = first hour >= 10% of peak
    build_h = next((h for h in hours if hourly_day[h] >= peak_rev * 0.10), open_h)
    # "Peak" = hours where revenue >= 50% of peak
    peak_hrs = [h for h in hours if hourly_day[h] >= peak_rev * 0.50]
    peak_start = peak_hrs[0]  if peak_hrs else build_h
    peak_end   = peak_hrs[-1] if peak_hrs else build_h
    # "Close" = last hour with any significant revenue
    close_h = next((h for h in reversed(hours) if hourly_day[h] >= total * 0.02), hours[-1] if hours else 2)
    close_h = (close_h + 1) % 24   # 1 hour after last significant revenue

    return {
        "prep":       fmt((open_h - 1) % 24),
        "open":       fmt(open_h),
        "build":      fmt(build_h),
        "peak_start": fmt(peak_start),
        "peak_end":   fmt(peak_end),
        "close":      fmt(close_h),
    }


# ── Console output ─────────────────────────────────────────────────────────────

_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def print_forecast_table(results: list, last_date, events: dict,
                          weather: dict, show_days: int = 14):
    by_date: dict = defaultdict(dict)
    ev_info: dict = {}
    hol_info: dict = {}
    for r in results:
        by_date[r["forecast_date"]][r["category"]] = r["predicted"]
        if r.get("event_names"):
            ev_info[r["forecast_date"]] = r["event_names"]
        if r.get("holiday_name"):
            hol_info[r["forecast_date"]] = (r["holiday_name"], r["holiday_mult"])

    all_forecast_dates = sorted(by_date)
    display_dates      = all_forecast_dates[:show_days]
    cats = [c for c in FORECAST_GROUPS if any(c in by_date[d] for d in display_dates)]
    col  = 10

    sep = "─" * (18 + col * len(cats) + col)
    hdr = (f"{'Date':<12} {'DOW':<4}{'':<4}" +
           "".join(f"{c[:col-1]:>{col}}" for c in cats) +
           f"{'TOTAL':>{col}}")

    print(f"\n{sep}")
    print(f"  On Par Entertainment — Sales Forecast  (history through {last_date})")
    print(f"  Model v{MODEL_VERSION}: seasonal + holiday + weather + events")
    print(f"  * event   ♦ holiday   ☁ weather adj   [closed]")
    print(sep)
    print(hdr)
    print(sep)

    for d in display_dates:
        dt     = date.fromisoformat(d)
        vals   = [by_date[d].get(c, 0.0) for c in cats]
        total  = sum(vals)
        closed = any(r.get("day_closed") for r in results if r["forecast_date"] == d)

        flags = ""
        if d in ev_info:   flags += "*"
        if d in hol_info:  flags += "♦"
        w_row = (weather or {}).get(d, {})
        if w_row and w_row.get("weather_mult", 1.0) != 1.0:
            flags += "☁"

        row = (f"{d:<12} {_DOW[dt.weekday()]:<4}{flags:<4}" +
               ("".join(f"{v:>{col},.0f}" for v in vals) + f"{total:>{col},.0f}"
                if not closed else f"{'[CLOSED]':>{col * (len(cats) + 1)}}"))
        print(row)

        if d in ev_info:
            for evt in ev_info[d][:2]:
                print(f"             {'':>4}   ↳ {evt}")
        if d in hol_info:
            name, mult = hol_info[d]
            print(f"             {'':>4}   ♦ {name}  ×{mult:.2f}")
        if w_row:
            wm = w_row.get("weather_mult")
            if wm and abs(wm - 1.0) > 0.01:
                desc = w_row.get("weather_desc", "")
                tf   = w_row.get("max_temp_f", "?")
                print(f"             {'':>4}   ☁ {desc}, max {tf}°F  ×{wm:.2f}")

    if len(all_forecast_dates) > show_days:
        print(f"\n  (first {show_days} days shown; full detail in JSON)")

    print(f"\n  Weekly totals")
    print(sep)
    for ws in range(0, len(all_forecast_dates), 7):
        week_dates = all_forecast_dates[ws:ws + 7]
        total = sum(sum(by_date[d].get(c, 0.0) for c in cats) for d in week_dates)
        ev_days = sum(1 for d in week_dates if d in ev_info)
        hol_days = sum(1 for d in week_dates if d in hol_info)
        note = ""
        if ev_days:  note += f"  {ev_days} event day(s)"
        if hol_days: note += f"  {hol_days} holiday(s)"
        print(f"  Week {ws//7+1}: {week_dates[0]} – {week_dates[-1]}  |  "
              f"Total: ${total:>11,.2f}{note}")
    print(sep + "\n")


def save_json(results: list, last_date, events: dict, weather: dict,
              hourly: dict, path: str = "forecast_output.json"):
    # Attach weather to weather_by_date for JSON
    weather_out = {
        d: {k: v for k, v in w.items() if k != "source"}
        for d, w in (weather or {}).items()
        if d >= date.today().isoformat()
    }
    output = {
        "generated_at":    date.today().isoformat(),
        "history_through": str(last_date),
        "model": {
            "name":                    "SeasonalHolidayWeatherEvent",
            "version":                 MODEL_VERSION,
            "seasonal_lookback_weeks": SEASONAL_LOOKBACK_WEEKS,
            "trend_lookback_weeks":    TREND_LOOKBACK_WEEKS,
            "weekly_decay":            WEEKLY_DECAY,
        },
        "weather_forecast": weather_out,
        "event_calendar": {
            d: ev for d, ev in sorted((events or {}).items())
            if ev["effective_guests"] >= 1 and d >= date.today().isoformat()
        },
        "hourly_forecast": hourly,
        "forecasts": results,
    }
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Saved → {path}")


def push_to_supabase(results: list):
    rows = [
        {
            "forecast_date":       r["forecast_date"],
            "category":            r["category"],
            "predicted_net_sales": r["predicted"],
            "lower_80":            r["lower_80"],
            "upper_80":            r["upper_80"],
            "model_version":       MODEL_VERSION,
        }
        for r in results
    ]
    batch = 200
    for i in range(0, len(rows), batch):
        supabase_upsert("forecasts", rows[i:i + batch],
                        on_conflict="forecast_date,category,model_version")
        print(f"  Upserted {min(i+batch, len(rows))}/{len(rows)} rows...",
              end="\r", flush=True)
    print(f"\n  Written to Supabase forecasts table.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="On Par Entertainment — daily sales forecaster v2")
    parser.add_argument("--days",       type=int, default=DEFAULT_FORECAST_DAYS)
    parser.add_argument("--history",    type=int, default=DEFAULT_HISTORY_WEEKS)
    parser.add_argument("--show",       type=int, default=14)
    parser.add_argument("--output",     type=str, default="forecast_output.json")
    parser.add_argument("--push",       action="store_true")
    parser.add_argument("--no-events",  action="store_true")
    parser.add_argument("--no-weather", action="store_true")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  On Par Entertainment — SalesForecastAgent  v{MODEL_VERSION}")
    print(f"{'='*60}\n")

    daily = fetch_daily_sales(history_weeks=args.history)

    events: dict = {}
    if not args.no_events:
        print("Loading Tripleseat events...")
        events = fetch_events(horizon_days=args.days + 60)
        future_ev = {d: ev for d, ev in events.items()
                     if d >= date.today().isoformat() and ev["effective_guests"] >= 1}
        print(f"  {len(future_ev)} future event dates")

    weather: dict = {}
    if not args.no_weather:
        print("Loading weather data...")
        weather = load_weather(horizon_days=args.days)
        print(f"  {len(weather)} weather records loaded")

    print("Loading hourly profiles from tab_metrics...")
    hourly_profiles = load_tab_hourly_profiles(history_weeks=args.history)
    source = "tab_metrics" if hourly_profiles is not _FALLBACK_HOURLY else "fallback defaults"
    print(f"  Hourly profiles loaded ({source})")

    print("\nBuilding forecasts...")
    results, last_date, _ = build_forecasts(
        daily, horizon=args.days, events=events, weather=weather)
    n_cats = len({r["category"] for r in results})
    print(f"  {len(results):,} forecast points ({args.days} days × {n_cats} categories)\n")

    print("Building hourly breakdown...")
    hourly = build_hourly_forecast(results, hourly_profiles, last_date)

    # Attach weather multipliers for display
    for r in results:
        w = weather.get(r["forecast_date"], {})
        if w and "weather_mult" not in w:
            is_wknd = date.fromisoformat(r["forecast_date"]).weekday() >= 4
            w["weather_mult"] = weather_multiplier(
                w.get("max_temp_f"), w.get("precipitation_in", 0),
                w.get("weather_code", 0), is_wknd)

    print_forecast_table(results, last_date, events=events,
                         weather=weather, show_days=args.show)
    save_json(results, last_date, events=events, weather=weather,
              hourly=hourly, path=args.output)

    if args.push:
        print("\nPushing to Supabase...")
        push_to_supabase(results)

    print("\nDone.\n")


if __name__ == "__main__":
    main()
