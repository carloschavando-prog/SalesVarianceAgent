#!/usr/bin/env python3
"""
forecast_report.py — On Par Entertainment
Generates a modern HTML sales forecast report for labor scheduling.

Sections:
  1. Summary cards  — weekly totals, top event, weather outlook
  2. Daily forecast table  — 14-30 days with holiday / weather / event flags
  3. Hourly revenue chart  — per-day bar chart for shift planning
  4. Shift recommendations — prep / open / build / peak / cut / close
  5. Upcoming events  — Tripleseat bookings with status

Usage:
  python forecast_report.py              # writes forecast_report.html
  python forecast_report.py --days 14    # shorter horizon
  python forecast_report.py --open       # also opens in browser

Vercel handler: GET /api/forecast_report
"""

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from collections import defaultdict
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Add parent dir so we can import forecast_agent helpers
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forecast_agent import (
    fetch_daily_sales, fetch_events, load_weather,
    load_tab_hourly_profiles, build_forecasts,
    build_hourly_forecast, shift_recommendations,
    FORECAST_GROUPS, MODEL_VERSION, DEFAULT_FORECAST_DAYS,
    DEFAULT_HISTORY_WEEKS, FOOD_CATS, FOH_CATS,
)
from holiday_calendar import get_day_info
from weather_fetch import weather_multiplier

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "forecast_report.html")

_DOW_FULL  = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
_DOW_SHORT = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

# ── Category colours ──────────────────────────────────────────────────────────

CAT_COLORS = {
    "Food":           "#f97316",
    "Beverage":       "#3b82f6",
    "Entertainment":  "#8b5cf6",
    "Karaoke":        "#ec4899",
    "Reservations":   "#14b8a6",
    "Merchandise":    "#f59e0b",
    "Open Item":      "#6b7280",
    "Bottle Service": "#ef4444",
    "Events":         "#10b981",
}

STATUS_BADGE = {
    "DEFINITE":     ("bg-green-100 text-green-800",  "Definite"),
    "TENTATIVE":    ("bg-yellow-100 text-yellow-800", "Tentative"),
    "PROSPECT":     ("bg-blue-100 text-blue-800",     "Prospect"),
    "CLOSED":       ("bg-green-100 text-green-800",   "Closed"),
    "PENDING_AUTH": ("bg-orange-100 text-orange-800", "Pending"),
    "LOST":         ("bg-red-100 text-red-800",       "Lost"),
}


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _fmt_money(v: float) -> str:
    return f"${v:,.0f}"


def _fmt_hour(h: int) -> str:
    h = int(h) % 24
    if h == 0:   return "12 AM"
    if h == 12:  return "12 PM"
    return f"{h} AM" if h < 12 else f"{h - 12} PM"


# ── Data assembly ─────────────────────────────────────────────────────────────

def assemble(horizon_days: int = DEFAULT_FORECAST_DAYS) -> dict:
    daily    = fetch_daily_sales(history_weeks=DEFAULT_HISTORY_WEEKS)
    events   = fetch_events(horizon_days=horizon_days + 60)
    weather  = load_weather(horizon_days=horizon_days)
    profiles = load_tab_hourly_profiles(history_weeks=DEFAULT_HISTORY_WEEKS)

    results, last_date, _ = build_forecasts(daily, horizon_days, events, weather)
    hourly = build_hourly_forecast(results, profiles, last_date)

    # Build per-date summary dict
    by_date = defaultdict(lambda: {
        "cats": defaultdict(float),
        "total": 0.0,
        "closed": False,
        "holiday_name": "",
        "holiday_mult": 1.0,
        "weather_mult": 1.0,
        "weather_desc": "",
        "max_temp_f": None,
        "event_names": [],
        "event_guests": 0.0,
    })
    for r in results:
        d = r["forecast_date"]
        by_date[d]["cats"][r["category"]] += r["predicted"]
        by_date[d]["total"]         += r["predicted"]
        by_date[d]["closed"]         = r["day_closed"]
        by_date[d]["holiday_name"]   = r.get("holiday_name", "")
        by_date[d]["holiday_mult"]   = r.get("holiday_mult", 1.0)
        by_date[d]["weather_mult"]   = r.get("weather_mult", 1.0)
        if r.get("event_names"):
            by_date[d]["event_names"]  = r["event_names"]
            by_date[d]["event_guests"] = r["event_guests"]

    # Attach weather descriptions
    for d, w in (weather or {}).items():
        if d in by_date:
            by_date[d]["weather_desc"] = w.get("weather_desc", "")
            by_date[d]["max_temp_f"]   = w.get("max_temp_f")

    dates = sorted(by_date.keys())

    # Shift recommendations per day
    shifts = {d: shift_recommendations(hourly.get(d, {})) for d in dates}

    # Future events for the event calendar section
    today_str = date.today().isoformat()
    future_events = {
        d: ev for d, ev in sorted(events.items())
        if d >= today_str and ev["effective_guests"] >= 1
    }

    return {
        "dates":         dates,
        "by_date":       dict(by_date),
        "hourly":        hourly,
        "shifts":        shifts,
        "future_events": future_events,
        "last_date":     str(last_date),
        "generated_at":  date.today().isoformat(),
    }


# ── HTML builder ──────────────────────────────────────────────────────────────

def build_html(data: dict) -> str:
    dates        = data["dates"]
    by_date      = data["by_date"]
    hourly       = data["hourly"]
    shifts       = data["shifts"]
    future_events = data["future_events"]
    generated_at = data["generated_at"]
    last_date    = data["last_date"]

    cats = [c for c in FORECAST_GROUPS if any(
        by_date[d]["cats"].get(c, 0) > 0 for d in dates
    )]

    # ── Summary stats ─────────────────────────────────────────────────────────
    week1_dates  = dates[:7]
    week2_dates  = dates[7:14]
    week1_total  = sum(by_date[d]["total"] for d in week1_dates)
    week2_total  = sum(by_date[d]["total"] for d in week2_dates)
    best_day     = max(dates[:14], key=lambda d: by_date[d]["total"]) if dates else ""
    best_day_rev = by_date[best_day]["total"] if best_day else 0

    event_count  = sum(1 for d in dates[:30] if by_date[d]["event_names"])
    weather_days = sum(1 for d in dates[:14]
                       if abs(by_date[d]["weather_mult"] - 1.0) > 0.02)

    # ── Hourly chart data (JS) ─────────────────────────────────────────────────
    hourly_js_parts = []
    for d in dates[:14]:
        h = hourly.get(d, {})
        if not h:
            continue
        hrs  = sorted(h.keys(), key=lambda x: int(x))
        vals = [round(h[x], 2) for x in hrs]
        lbsl = [_fmt_hour(int(x)) for x in hrs]
        dt   = date.fromisoformat(d)
        title = f"{_DOW_SHORT[dt.weekday()]} {dt.strftime('%b %-d')}"
        shift = shifts.get(d, {})
        shift_json = json.dumps(shift)
        total_day = round(by_date[d]["total"], 2)
        holiday = _esc(by_date[d]["holiday_name"])
        events_str = _esc(", ".join(by_date[d]["event_names"][:2]))
        hourly_js_parts.append(
            f"{{date:'{d}',title:'{_esc(title)}',labels:{json.dumps(lbsl)},"
            f"values:{json.dumps(vals)},total:{total_day},"
            f"shift:{shift_json},holiday:'{holiday}',events:'{events_str}'}}"
        )
    hourly_js = "[" + ",\n".join(hourly_js_parts) + "]"

    # ── Forecast table rows ────────────────────────────────────────────────────
    table_rows = []
    for i, d in enumerate(dates[:30]):
        info    = by_date[d]
        dt      = date.fromisoformat(d)
        dow     = _DOW_SHORT[dt.weekday()]
        is_wknd = dt.weekday() >= 4
        closed  = info["closed"]

        # Row style
        if closed:
            row_cls = "bg-gray-50 opacity-60"
        elif is_wknd:
            row_cls = "bg-indigo-50/40"
        else:
            row_cls = "hover:bg-gray-50"

        # Badges
        badges = []
        if info["holiday_name"]:
            m = info["holiday_mult"]
            color = "bg-amber-100 text-amber-800" if m >= 1 else "bg-orange-100 text-orange-800"
            badges.append(f'<span class="badge {color}">♦ {_esc(info["holiday_name"])} ×{m:.2f}</span>')
        if abs(info["weather_mult"] - 1.0) > 0.02:
            wm = info["weather_mult"]
            wdesc = info["weather_desc"]
            tf = f'{info["max_temp_f"]}°F' if info["max_temp_f"] else ""
            color = "bg-blue-100 text-blue-800" if wm > 1.0 else "bg-slate-100 text-slate-700"
            badges.append(f'<span class="badge {color}">☁ {_esc(wdesc)} {tf} ×{wm:.2f}</span>')
        if info["event_names"]:
            for en in info["event_names"][:2]:
                badges.append(f'<span class="badge bg-emerald-100 text-emerald-800">★ {_esc(en)}</span>')

        badge_html = "".join(badges)

        # Category cells
        n_cols = len(cats) + 3   # cats + Food Total + FOH Total + Grand Total
        if closed:
            cat_cells = f'<td colspan="{n_cols}" class="px-3 py-2 text-center text-sm text-gray-400 italic">Closed</td>'
        else:
            food_total = sum(info["cats"].get(c, 0.0) for c in cats if c in FOOD_CATS)
            foh_total  = sum(info["cats"].get(c, 0.0) for c in cats if c in FOH_CATS)
            grand_total = food_total + foh_total
            cat_cells = ""
            for c in cats:
                v = info["cats"].get(c, 0.0)
                cat_cells += f'<td class="px-3 py-2 text-right text-sm text-gray-700 tabular-nums">{_fmt_money(v) if v > 0 else "—"}</td>'
            # Food Total
            cat_cells += f'<td class="px-3 py-2 text-right text-sm font-semibold text-orange-700 tabular-nums border-l border-orange-100">{_fmt_money(food_total)}</td>'
            # FOH Total
            cat_cells += f'<td class="px-3 py-2 text-right text-sm font-semibold text-indigo-700 tabular-nums">{_fmt_money(foh_total)}</td>'
            # Grand Total
            total_cls = "font-bold text-gray-900 bg-gray-50" if is_wknd else "font-semibold text-gray-800"
            cat_cells += f'<td class="px-3 py-2 text-right text-sm {total_cls} tabular-nums border-l border-gray-200">{_fmt_money(grand_total)}</td>'

        # Day number indicator
        day_num = i + 1

        table_rows.append(f"""
        <tr class="{row_cls} border-b border-gray-100 align-top">
          <td class="px-3 py-2 whitespace-nowrap">
            <div class="flex items-center gap-2">
              <span class="text-xs font-mono text-gray-400 w-5 text-right">{day_num}</span>
              <div>
                <div class="text-sm font-semibold text-gray-900">{dow} <span class="font-normal text-gray-500">{dt.strftime('%b %-d')}</span></div>
                <div class="flex flex-wrap gap-1 mt-1">{badge_html}</div>
              </div>
            </div>
          </td>
          {cat_cells}
        </tr>""")

    table_rows_html = "\n".join(table_rows)

    # ── Table header cells ─────────────────────────────────────────────────────
    cat_headers = "".join(
        f'<th class="px-3 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide whitespace-nowrap">'
        f'<span class="inline-block w-2 h-2 rounded-full mr-1" style="background:{CAT_COLORS.get(c,"#94a3b8")}"></span>'
        f'{c}</th>'
        for c in cats
    )
    # Subtotal headers
    cat_headers += (
        '<th class="px-3 py-3 text-right text-xs font-semibold text-orange-700 uppercase tracking-wide whitespace-nowrap border-l border-orange-100">Food Total</th>'
        '<th class="px-3 py-3 text-right text-xs font-semibold text-indigo-700 uppercase tracking-wide whitespace-nowrap">FOH Total</th>'
        '<th class="px-3 py-3 text-right text-xs font-semibold text-gray-900 uppercase tracking-wide whitespace-nowrap border-l border-gray-200">Grand Total</th>'
    )

    # ── Event calendar rows ────────────────────────────────────────────────────
    event_rows = []
    for d, ev in list(future_events.items())[:20]:
        dt = date.fromisoformat(d)
        for e in ev["events"]:
            status = e.get("status", "")
            badge_cls, badge_label = STATUS_BADGE.get(status, ("bg-gray-100 text-gray-700", status))
            wt = int(e.get("weight", 0) * 100)
            event_rows.append(f"""
            <tr class="border-b border-gray-100 hover:bg-gray-50">
              <td class="px-4 py-3 whitespace-nowrap">
                <div class="text-sm font-semibold text-gray-900">{dt.strftime('%a, %b %-d')}</div>
                <div class="text-xs text-gray-500">{dt.strftime('%Y')}</div>
              </td>
              <td class="px-4 py-3 text-sm text-gray-800">{_esc(e.get('name',''))}</td>
              <td class="px-4 py-3 text-center">
                <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium {badge_cls}">{badge_label}</span>
              </td>
              <td class="px-4 py-3 text-right text-sm font-medium text-gray-700">{e.get('guests',0):,}</td>
              <td class="px-4 py-3 text-right text-xs text-gray-500">{wt}%</td>
            </tr>""")

    event_rows_html = "\n".join(event_rows) if event_rows else \
        '<tr><td colspan="5" class="px-4 py-8 text-center text-sm text-gray-400">No upcoming events found</td></tr>'

    # ── Shift legend cards ─────────────────────────────────────────────────────
    shift_legend = [
        ("prep",       "🔪", "Prep / Kitchen In",    "bg-orange-50  border-orange-200 text-orange-700"),
        ("open",       "🚪", "Doors Open",            "bg-green-50   border-green-200  text-green-700"),
        ("build",      "📈", "Build to Medium Staff", "bg-blue-50    border-blue-200   text-blue-700"),
        ("peak_start", "⚡", "Full Staff (Peak)",     "bg-purple-50  border-purple-200 text-purple-700"),
        ("peak_end",   "✂️", "Start Cuts",            "bg-rose-50    border-rose-200   text-rose-700"),
        ("close",      "🔒", "Last Staff Out",        "bg-slate-50   border-slate-200  text-slate-700"),
    ]

    # ── Full HTML ──────────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>On Par — Sales Forecast</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    body {{ font-family: 'Inter', system-ui, sans-serif; }}
    .badge {{ display:inline-flex; align-items:center; padding:1px 7px; border-radius:9999px; font-size:11px; font-weight:500; white-space:nowrap; }}
    .tabular-nums {{ font-variant-numeric: tabular-nums; }}
    .card {{ background:white; border-radius:12px; box-shadow:0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04); border:1px solid #f1f5f9; }}
    .section-title {{ font-size:15px; font-weight:600; color:#0f172a; letter-spacing:-.01em; }}
    ::-webkit-scrollbar {{ height:6px; width:6px; }}
    ::-webkit-scrollbar-track {{ background:#f8fafc; }}
    ::-webkit-scrollbar-thumb {{ background:#cbd5e1; border-radius:3px; }}
    .chart-nav-btn {{ transition: all .15s; }}
    .chart-nav-btn:hover {{ background:#6366f1; color:white; }}
    .shift-chip {{ display:inline-flex; flex-direction:column; align-items:center; min-width:70px; padding:8px 12px; border-radius:10px; border-width:1px; }}
  </style>
</head>
<body class="bg-slate-50 min-h-screen">

<!-- ── Top nav ──────────────────────────────────────────────────────────── -->
<header class="bg-white border-b border-slate-200 sticky top-0 z-50">
  <div class="max-w-screen-xl mx-auto px-6 py-3 flex items-center justify-between">
    <div class="flex items-center gap-3">
      <div class="w-8 h-8 rounded-lg bg-indigo-600 flex items-center justify-center text-white font-bold text-sm">OP</div>
      <div>
        <div class="font-semibold text-gray-900 text-sm leading-tight">On Par Entertainment</div>
        <div class="text-xs text-gray-400">Sales Forecast — Model v{MODEL_VERSION}</div>
      </div>
    </div>
    <div class="flex items-center gap-4 text-xs text-gray-500">
      <span>History through <strong class="text-gray-700">{last_date}</strong></span>
      <span class="text-gray-300">|</span>
      <span>Generated <strong class="text-gray-700">{generated_at}</strong></span>
      <a href="#forecast"  class="text-indigo-600 hover:underline font-medium">Forecast</a>
      <a href="#hourly"    class="text-indigo-600 hover:underline font-medium">Hourly</a>
      <a href="#events"    class="text-indigo-600 hover:underline font-medium">Events</a>
    </div>
  </div>
</header>

<main class="max-w-screen-xl mx-auto px-6 py-8 space-y-8">

  <!-- ── Summary cards ─────────────────────────────────────────────────── -->
  <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
    <div class="card p-5">
      <div class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-1">Next 7 Days</div>
      <div class="text-2xl font-bold text-gray-900">{_fmt_money(week1_total)}</div>
      <div class="text-xs text-gray-500 mt-1">Projected revenue</div>
    </div>
    <div class="card p-5">
      <div class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-1">Days 8 – 14</div>
      <div class="text-2xl font-bold text-gray-900">{_fmt_money(week2_total)}</div>
      <div class="text-xs text-gray-500 mt-1">Projected revenue</div>
    </div>
    <div class="card p-5">
      <div class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-1">Best Day (14d)</div>
      <div class="text-2xl font-bold text-indigo-600">{_fmt_money(best_day_rev)}</div>
      <div class="text-xs text-gray-500 mt-1">{date.fromisoformat(best_day).strftime('%A, %b %-d') if best_day else '—'}</div>
    </div>
    <div class="card p-5">
      <div class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-1">Signals Active</div>
      <div class="text-2xl font-bold text-gray-900">{event_count + weather_days}</div>
      <div class="text-xs text-gray-500 mt-1">{event_count} events · {weather_days} weather days</div>
    </div>
  </div>

  <!-- ── Forecast table ─────────────────────────────────────────────────── -->
  <div id="forecast" class="card overflow-hidden">
    <div class="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
      <div>
        <div class="section-title">30-Day Forecast</div>
        <div class="text-xs text-gray-400 mt-0.5">
          <span class="text-amber-600 font-medium">♦ holiday</span> &nbsp;
          <span class="text-blue-600 font-medium">☁ weather</span> &nbsp;
          <span class="text-emerald-600 font-medium">★ event</span>
        </div>
      </div>
    </div>
    <div class="overflow-x-auto">
      <table class="w-full">
        <thead class="bg-gray-50/80 border-b border-gray-100">
          <tr>
            <th class="px-3 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide w-56">Date</th>
            {cat_headers}
          </tr>
        </thead>
        <tbody>
          {table_rows_html}
        </tbody>
      </table>
    </div>
  </div>

  <!-- ── Hourly chart ───────────────────────────────────────────────────── -->
  <div id="hourly" class="card p-6">
    <div class="flex items-center justify-between mb-6">
      <div>
        <div class="section-title">Hourly Revenue Distribution</div>
        <div class="text-xs text-gray-400 mt-0.5">Use to determine staff in / out times</div>
      </div>
      <div class="flex items-center gap-2">
        <button id="prevDay" class="chart-nav-btn w-8 h-8 rounded-lg border border-gray-200 text-gray-600 flex items-center justify-center text-sm font-bold">‹</button>
        <span id="chartDayLabel" class="text-sm font-semibold text-gray-700 min-w-32 text-center"></span>
        <button id="nextDay" class="chart-nav-btn w-8 h-8 rounded-lg border border-gray-200 text-gray-600 flex items-center justify-center text-sm font-bold">›</button>
      </div>
    </div>

    <!-- Shift chips row -->
    <div id="shiftChips" class="flex flex-wrap gap-2 mb-6"></div>

    <!-- Chart -->
    <div class="relative h-64 md:h-80">
      <canvas id="hourlyChart"></canvas>
    </div>

    <!-- Day total + annotations -->
    <div id="chartMeta" class="mt-4 flex flex-wrap items-center gap-4 text-sm text-gray-600"></div>
  </div>

  <!-- ── Events calendar ────────────────────────────────────────────────── -->
  <div id="events" class="card overflow-hidden">
    <div class="px-6 py-4 border-b border-gray-100">
      <div class="section-title">Upcoming Events</div>
      <div class="text-xs text-gray-400 mt-0.5">From Tripleseat — weighted by booking status</div>
    </div>
    <div class="overflow-x-auto">
      <table class="w-full">
        <thead class="bg-gray-50/80 border-b border-gray-100">
          <tr>
            <th class="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Date</th>
            <th class="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Event</th>
            <th class="px-4 py-3 text-center text-xs font-semibold text-gray-500 uppercase tracking-wide">Status</th>
            <th class="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">Guests</th>
            <th class="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">Weight</th>
          </tr>
        </thead>
        <tbody>
          {event_rows_html}
        </tbody>
      </table>
    </div>
  </div>

  <footer class="text-center text-xs text-gray-400 pb-6">
    On Par Entertainment · Forecast model v{MODEL_VERSION} · Generated {generated_at}
  </footer>
</main>

<!-- ── JS ────────────────────────────────────────────────────────────────── -->
<script>
const HOURLY_DATA = {hourly_js};

const SHIFT_META = [
  {{key:'prep',       icon:'🔪', label:'Prep In',      cls:'bg-orange-50 border-orange-200 text-orange-700'}},
  {{key:'open',       icon:'🚪', label:'Doors Open',   cls:'bg-green-50 border-green-200 text-green-700'}},
  {{key:'build',      icon:'📈', label:'Build Staff',  cls:'bg-blue-50 border-blue-200 text-blue-700'}},
  {{key:'peak_start', icon:'⚡', label:'Full Staff',   cls:'bg-purple-50 border-purple-200 text-purple-700'}},
  {{key:'peak_end',   icon:'✂️', label:'Start Cuts',   cls:'bg-rose-50 border-rose-200 text-rose-700'}},
  {{key:'close',      icon:'🔒', label:'Last Out',     cls:'bg-slate-50 border-slate-200 text-slate-700'}},
];

let currentIdx = 0;
let chart = null;

function renderChart(idx) {{
  if (!HOURLY_DATA.length) return;
  idx = Math.max(0, Math.min(idx, HOURLY_DATA.length - 1));
  currentIdx = idx;
  const day = HOURLY_DATA[idx];

  document.getElementById('chartDayLabel').textContent = day.title;

  // Shift chips
  const chipsEl = document.getElementById('shiftChips');
  chipsEl.innerHTML = '';
  if (day.shift && Object.keys(day.shift).length) {{
    SHIFT_META.forEach(m => {{
      if (!day.shift[m.key]) return;
      const chip = document.createElement('div');
      chip.className = `shift-chip ${{m.cls}} border`;
      chip.innerHTML = `<span class="text-base">${{m.icon}}</span><span class="text-xs font-semibold mt-1">${{day.shift[m.key]}}</span><span class="text-xs opacity-70">${{m.label}}</span>`;
      chipsEl.appendChild(chip);
    }});
  }} else {{
    chipsEl.innerHTML = '<span class="text-xs text-gray-400 italic">No hourly data for this day</span>';
  }}

  // Meta row
  const meta = document.getElementById('chartMeta');
  let metaHtml = `<span class="font-semibold text-gray-900">Daily total: ${{day.total.toLocaleString('en-US', {{style:'currency', currency:'USD', maximumFractionDigits:0}})}}</span>`;
  if (day.holiday) metaHtml += `<span class="badge bg-amber-100 text-amber-800">♦ ${{day.holiday}}</span>`;
  if (day.events)  metaHtml += `<span class="badge bg-emerald-100 text-emerald-800">★ ${{day.events}}</span>`;
  meta.innerHTML = metaHtml;

  // Chart
  const ctx = document.getElementById('hourlyChart').getContext('2d');
  if (chart) chart.destroy();

  // Highlight peak hours
  const maxVal = Math.max(...day.values);
  const bgColors = day.values.map(v =>
    v >= maxVal * 0.8 ? 'rgba(99,102,241,0.85)' :
    v >= maxVal * 0.5 ? 'rgba(99,102,241,0.55)' :
                        'rgba(99,102,241,0.25)'
  );

  chart = new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: day.labels,
      datasets: [{{
        label: 'Revenue',
        data: day.values,
        backgroundColor: bgColors,
        borderRadius: 6,
        borderSkipped: false,
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          callbacks: {{
            label: ctx => ' $' + ctx.parsed.y.toLocaleString('en-US', {{maximumFractionDigits:0}}),
          }}
        }}
      }},
      scales: {{
        x: {{
          grid: {{ display: false }},
          ticks: {{ font: {{ size: 11 }}, color: '#94a3b8' }}
        }},
        y: {{
          grid: {{ color: '#f1f5f9' }},
          ticks: {{
            font: {{ size: 11 }}, color: '#94a3b8',
            callback: v => '$' + (v >= 1000 ? (v/1000).toFixed(0)+'k' : v)
          }}
        }}
      }}
    }}
  }});
}}

document.getElementById('prevDay').addEventListener('click', () => renderChart(currentIdx - 1));
document.getElementById('nextDay').addEventListener('click', () => renderChart(currentIdx + 1));

if (HOURLY_DATA.length) renderChart(0);
</script>

</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def generate(horizon_days: int = DEFAULT_FORECAST_DAYS,
             output_path: str = OUTPUT_PATH) -> str:
    print("\n" + "="*60)
    print("  On Par — Forecast Report Generator")
    print("="*60 + "\n")
    data = assemble(horizon_days)
    html = build_html(data)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n  Report saved → {output_path}")
    return output_path


REPORT_KEY = os.environ.get("REPORT_KEY", "")


# ── Vercel handler ────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs  = parse_qs(urlparse(self.path).query)
        key = qs.get("key", [""])[0]
        if REPORT_KEY and key != REPORT_KEY:
            self.send_response(401)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Unauthorized — add ?key=YOUR_KEY to the URL")
            return
        try:
            data = assemble()
            html = build_html(data)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, *_):
        pass


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="On Par — forecast HTML report")
    parser.add_argument("--days",   type=int, default=DEFAULT_FORECAST_DAYS)
    parser.add_argument("--output", type=str, default=OUTPUT_PATH)
    parser.add_argument("--open",   action="store_true", help="Open in browser when done")
    args = parser.parse_args()

    path = generate(horizon_days=args.days, output_path=args.output)

    if args.open:
        import webbrowser
        webbrowser.open(f"file://{os.path.abspath(path)}")
