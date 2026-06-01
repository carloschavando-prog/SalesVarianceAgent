"""
Live HTML labor cost report.

GET /api/labor_report?key=4464&year=2026

Columns: Date | Day | Total Sales | KIT Hrs | KIT $ | KIT % |
         FOH Hrs | FOH $ | FOH % | Total Hrs | Total $ | Total %

KIT % = KIT cost / food sales
FOH % = FOH cost / (total sales - food sales)
Total % = total cost / total sales

Week subtotals show actual vs scheduled hours.
Newest period/week at top. Year tabs FY2023-FY2026.
Auto-refreshes every 5 minutes.

Required env vars:
  SUPABASE_URL, SUPABASE_SERVICE_KEY, REPORT_KEY
"""

import json
import os
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
REPORT_KEY   = os.environ.get("REPORT_KEY", "")

# ---------------------------------------------------------------------------
# Fiscal calendar (identical to daily_report.py)
# ---------------------------------------------------------------------------
_WEEKS_PER_PERIOD = {
    1: 5, 2: 4, 3: 4,
    4: 5, 5: 4, 6: 4,
    7: 5, 8: 4, 9: 4,
    10: 5, 11: 4, 12: 4,
}

_FY_STARTS = {
    2023: date(2023, 1, 2),
    2024: date(2024, 1, 1),
    2025: date(2024, 12, 30),
    2026: date(2025, 12, 29),
}

AVAILABLE_YEARS = sorted(_FY_STARTS.keys())

_REVENUE_COLS = [
    "food", "beverage", "mini_golf", "bowling", "karaoke", "darts",
    "shuffle_board", "pool", "merchandise", "events", "open_item",
    "bottle_svc", "reservations", "gift_card", "redeemed",
]


def _fy_start(fy: int) -> date:
    return _FY_STARTS[fy]


def _period_bounds(period: int, fy: int) -> tuple:
    week_offset = sum(_WEEKS_PER_PERIOD[p] for p in range(1, period))
    start = _fy_start(fy) + timedelta(weeks=week_offset)
    end   = start + timedelta(weeks=_WEEKS_PER_PERIOD[period]) - timedelta(days=1)
    return start, end


def _current_period(today: date, fy: int) -> int:
    delta = (today - _fy_start(fy)).days
    if delta < 0:
        return 1
    fy_week    = delta // 7
    cumulative = 0
    for p in range(1, 13):
        cumulative += _WEEKS_PER_PERIOD[p]
        if fy_week < cumulative:
            return p
    return 12


def _periods_to_show(fy: int, today: date) -> int:
    current_fy = max(y for y in _FY_STARTS if _fy_start(y) <= today)
    if fy == current_fy:
        return _current_period(today, fy)
    return 12


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _supa_get(path: str) -> list:
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1{path}",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept":        "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _supa_paged(base: str) -> list:
    rows, offset = [], 0
    while True:
        sep   = "&" if "?" in base else "?"
        batch = _supa_get(f"{base}{sep}limit=1000&offset={offset}")
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return rows


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def _fetch_labor(start: date, end: date) -> dict:
    """Returns {date_str: {kit_hours, kit_sched, kit_cost, foh_hours, foh_sched, foh_cost,
                           total_hours, total_sched, total_cost}}"""
    rows = _supa_paged(
        f"/labor_daily?date=gte.{start}&date=lte.{end}"
        f"&select=date,kit_hours,kit_sched,kit_cost,foh_hours,foh_sched,foh_cost,"
        f"total_hours,total_sched,total_cost"
    )
    return {r["date"]: r for r in rows}


def _fetch_revenue(start: date, end: date) -> dict:
    """Returns {date_str: {food, total}} aggregated across gotab + tripleseat."""
    col_select = ",".join(_REVENUE_COLS)
    rows = _supa_paged(
        f"/daily_revenue?report_date=gte.{start}&report_date=lte.{end}"
        f"&select=report_date,{col_select}"
    )
    by_date: dict = {}
    for row in rows:
        d = row["report_date"]
        if d not in by_date:
            by_date[d] = {"food": 0.0, "total": 0.0}
        food = float(row.get("food") or 0)
        total = sum(float(row.get(c) or 0) for c in _REVENUE_COLS)
        by_date[d]["food"]  = round(by_date[d]["food"]  + food,  2)
        by_date[d]["total"] = round(by_date[d]["total"] + total, 2)
    return by_date


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_sales(val) -> str:
    if val is None or val == "":
        return ""
    return f"${val:,.0f}"


def _fmt_cost(val) -> str:
    if val is None or val == "":
        return ""
    return f"${val:,.2f}"


def _fmt_hrs(val) -> str:
    if val is None or val == "":
        return ""
    return f"{val:.1f}"


def _fmt_pct(val) -> str:
    if val is None or val == "":
        return ""
    return f"{val:.1f}%"


def _pct(cost, sales) -> str:
    if not sales:
        return ""
    return _fmt_pct(cost / sales * 100)


def _pct_val(cost, sales):
    if not sales:
        return None
    return cost / sales * 100


def _pct_class(val) -> str:
    """Color code: labor % above 30% is red, 20-30% yellow, below 20% green."""
    if val is None:
        return ""
    if val >= 30:
        return "pct-high"
    if val >= 20:
        return "pct-mid"
    return "pct-low"


def _hrs_display(actual, sched) -> str:
    """Format hours as '42.5 / 48 sched' for subtotal rows."""
    if actual is None:
        return ""
    if sched:
        return f"{actual:.1f} / {sched:.1f}"
    return f"{actual:.1f}"


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _build_rows(fy: int, today: date, labor: dict, revenue: dict) -> list:
    rows        = []
    num_periods = _periods_to_show(fy, today)

    for period in range(num_periods, 0, -1):
        p_start, p_end = _period_bounds(period, fy)
        rows.append({"_type": "period_hdr", "label": f"Period {period}"})

        period_data = []

        for week in range(_WEEKS_PER_PERIOD[period], 0, -1):
            w_start = p_start + timedelta(weeks=week - 1)
            w_end   = w_start + timedelta(days=6)
            rows.append({
                "_type": "week_hdr",
                "label": (
                    f"Period {period}  ·  Week {week}"
                    f"  —  {w_start.strftime('%-m/%-d/%Y')}"
                    f" to {w_end.strftime('%-m/%-d/%Y')}"
                ),
            })

            week_data = []
            for delta in range(7):
                d      = w_start + timedelta(days=delta)
                ds     = d.isoformat()
                is_fut = d > today

                lab = labor.get(ds, {})
                rev = revenue.get(ds, {})

                if is_fut or not lab:
                    row = {
                        "_type": "future" if is_fut else "nodata",
                        "date": ds, "day": _DAYS[d.weekday()],
                    }
                else:
                    food_sales  = rev.get("food",  0.0)
                    total_sales = rev.get("total", 0.0)
                    foh_sales   = max(total_sales - food_sales, 0.0)

                    kit_cost  = float(lab.get("kit_cost")  or 0)
                    foh_cost  = float(lab.get("foh_cost")  or 0)
                    tot_cost  = float(lab.get("total_cost") or 0)
                    kit_hours = float(lab.get("kit_hours") or 0)
                    foh_hours = float(lab.get("foh_hours") or 0)
                    tot_hours = float(lab.get("total_hours") or 0)

                    row = {
                        "_type":       "data",
                        "date":        ds,
                        "day":         _DAYS[d.weekday()],
                        "total_sales": total_sales,
                        "food_sales":  food_sales,
                        "foh_sales":   foh_sales,
                        "kit_hours":   kit_hours,
                        "kit_cost":    kit_cost,
                        "kit_pct":     _pct_val(kit_cost, food_sales),
                        "foh_hours":   foh_hours,
                        "foh_cost":    foh_cost,
                        "foh_pct":     _pct_val(foh_cost, foh_sales),
                        "tot_hours":   tot_hours,
                        "tot_cost":    tot_cost,
                        "tot_pct":     _pct_val(tot_cost, total_sales),
                        # sched for subtotal aggregation
                        "kit_sched":   float(lab.get("kit_sched")   or 0),
                        "foh_sched":   float(lab.get("foh_sched")   or 0),
                        "tot_sched":   float(lab.get("total_sched") or 0),
                    }

                rows.append(row)
                week_data.append(row)
                period_data.append(row)

            rows.append(_subtotal_row(week_data, f"Period {period}  ·  Week {week}  Total", "week_sub"))

        rows.append(_subtotal_row(period_data, f"Period {period}  Total", "period_tot"))

    return rows


def _subtotal_row(data_rows: list, label: str, row_type: str) -> dict:
    def _sum(key):
        return round(sum(float(r.get(key) or 0) for r in data_rows if r.get("_type") == "data"), 2)

    kit_cost  = _sum("kit_cost")
    foh_cost  = _sum("foh_cost")
    tot_cost  = _sum("tot_cost")
    food      = _sum("food_sales")
    foh_s     = _sum("foh_sales")
    total_s   = _sum("total_sales")
    kit_h     = _sum("kit_hours")
    kit_sch   = _sum("kit_sched")
    foh_h     = _sum("foh_hours")
    foh_sch   = _sum("foh_sched")
    tot_h     = _sum("tot_hours")
    tot_sch   = _sum("tot_sched")

    return {
        "_type":      row_type,
        "label":      label,
        "total_sales": total_s,
        "kit_hours":  kit_h,  "kit_sched":  kit_sch,
        "kit_cost":   kit_cost,
        "kit_pct":    _pct_val(kit_cost, food),
        "foh_hours":  foh_h,  "foh_sched":  foh_sch,
        "foh_cost":   foh_cost,
        "foh_pct":    _pct_val(foh_cost, foh_s),
        "tot_hours":  tot_h,  "tot_sched":  tot_sch,
        "tot_cost":   tot_cost,
        "tot_pct":    _pct_val(tot_cost, total_s),
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _tr(row: dict) -> str:
    t = row["_type"]

    if t == "period_hdr":
        return f'<tr class="period-hdr"><td colspan="12">{row["label"]}</td></tr>'

    if t == "week_hdr":
        return f'<tr class="week-hdr"><td colspan="12">{row["label"]}</td></tr>'

    if t in ("future", "nodata"):
        label = "—" if t == "nodata" else ""
        return (
            f'<tr class="{t}">'
            f'<td>{row["date"]}</td><td>{row["day"]}</td>'
            f'<td>{label}</td><td>{label}</td><td>{label}</td><td>{label}</td>'
            f'<td>{label}</td><td>{label}</td><td>{label}</td>'
            f'<td>{label}</td><td>{label}</td><td>{label}</td>'
            f'</tr>'
        )

    if t in ("week_sub", "period_tot"):
        css = "week-sub" if t == "week_sub" else "period-tot"
        kp  = row.get("kit_pct")
        fp  = row.get("foh_pct")
        tp  = row.get("tot_pct")
        return (
            f'<tr class="{css}">'
            f'<td colspan="2">{row["label"]}</td>'
            f'<td>{_fmt_sales(row.get("total_sales"))}</td>'
            f'<td class="hrs">{_hrs_display(row.get("kit_hours"), row.get("kit_sched"))}</td>'
            f'<td>{_fmt_cost(row.get("kit_cost"))}</td>'
            f'<td class="{_pct_class(kp)}">{_fmt_pct(kp)}</td>'
            f'<td class="hrs">{_hrs_display(row.get("foh_hours"), row.get("foh_sched"))}</td>'
            f'<td>{_fmt_cost(row.get("foh_cost"))}</td>'
            f'<td class="{_pct_class(fp)}">{_fmt_pct(fp)}</td>'
            f'<td class="hrs">{_hrs_display(row.get("tot_hours"), row.get("tot_sched"))}</td>'
            f'<td>{_fmt_cost(row.get("tot_cost"))}</td>'
            f'<td class="{_pct_class(tp)}">{_fmt_pct(tp)}</td>'
            f'</tr>'
        )

    # regular data row
    kp = row.get("kit_pct")
    fp = row.get("foh_pct")
    tp = row.get("tot_pct")
    return (
        f'<tr class="data">'
        f'<td>{row["date"]}</td><td>{row["day"]}</td>'
        f'<td>{_fmt_sales(row.get("total_sales"))}</td>'
        f'<td class="hrs">{_fmt_hrs(row.get("kit_hours"))}</td>'
        f'<td>{_fmt_cost(row.get("kit_cost"))}</td>'
        f'<td class="{_pct_class(kp)}">{_fmt_pct(kp)}</td>'
        f'<td class="hrs">{_fmt_hrs(row.get("foh_hours"))}</td>'
        f'<td>{_fmt_cost(row.get("foh_cost"))}</td>'
        f'<td class="{_pct_class(fp)}">{_fmt_pct(fp)}</td>'
        f'<td class="hrs">{_fmt_hrs(row.get("tot_hours"))}</td>'
        f'<td>{_fmt_cost(row.get("tot_cost"))}</td>'
        f'<td class="{_pct_class(tp)}">{_fmt_pct(tp)}</td>'
        f'</tr>'
    )


def _render_html(fy: int, rows: list, today: date, key: str) -> str:
    table_rows = "\n".join(_tr(r) for r in rows)
    cur_period = _periods_to_show(fy, today)

    tabs_html = "".join(
        f'<a href="?key={key}&year={y}" class="tab{"active" if y == fy else ""}">'
        f'FY{y}</a>'
        for y in reversed(AVAILABLE_YEARS)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="300">
<title>On Par — Labor Report FY{fy}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  html, body {{
    height: 100%;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 13px;
    background: #f0f2f5;
    color: #1a1a2e;
    overflow: hidden;
  }}

  header {{
    background: #1b4332;
    color: #fff;
    padding: 0 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 48px;
    flex-shrink: 0;
    box-shadow: 0 2px 8px rgba(0,0,0,.4);
  }}

  header h1 {{ font-size: 15px; font-weight: 600; letter-spacing: .4px; }}
  header span {{ font-size: 12px; opacity: .75; }}

  .tab-bar {{
    background: #0d2b1e;
    display: flex;
    align-items: center;
    padding: 0 24px;
    height: 38px;
    flex-shrink: 0;
    gap: 4px;
  }}

  .tab {{
    color: #a8d5b5;
    text-decoration: none;
    font-size: 12px;
    font-weight: 600;
    padding: 5px 16px;
    border-radius: 4px;
    letter-spacing: .4px;
    transition: background .15s;
  }}

  .tab:hover {{ background: #1b4332; color: #fff; }}
  .tabactive {{ background: #2d6a4f; color: #fff; }}

  .table-scroll {{
    height: calc(100vh - 86px);
    overflow: auto;
  }}

  table {{
    width: 100%;
    border-collapse: collapse;
    background: #fff;
  }}

  thead th {{
    background: #2d6a4f;
    color: #fff;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .6px;
    padding: 10px 12px;
    text-align: right;
    white-space: nowrap;
    position: sticky;
    top: 0;
    z-index: 10;
  }}

  thead th.group-kit  {{ background: #1e5f3e; border-left: 2px solid #74c69d; }}
  thead th.group-foh  {{ background: #2a4858; border-left: 2px solid #74b9d4; }}
  thead th.group-tot  {{ background: #3d3d3d; border-left: 2px solid #aaa; }}
  thead th:first-child,
  thead th:nth-child(2) {{ text-align: left; }}

  td {{
    padding: 7px 12px;
    text-align: right;
    border-bottom: 1px solid #eef0f3;
    white-space: nowrap;
  }}

  td:first-child,
  td:nth-child(2) {{ text-align: left; }}

  td.hrs {{ color: #555; font-size: 12px; }}

  td.kit-sep  {{ border-left: 2px solid #d8f3dc; }}
  td.foh-sep  {{ border-left: 2px solid #cce5f0; }}
  td.tot-sep  {{ border-left: 2px solid #ddd; }}

  tr.data:hover {{ background: #f2faf5; }}

  tr.future td, tr.nodata td {{ color: #bbb; background: #fafafa; }}

  tr.period-hdr td {{
    background: #0d2b1e;
    color: #fff;
    font-weight: 700;
    font-size: 12px;
    letter-spacing: .4px;
    padding: 9px 12px;
  }}

  tr.week-hdr td {{
    background: #40916c;
    color: #fff;
    font-weight: 600;
    font-size: 11px;
    padding: 7px 12px;
  }}

  tr.week-sub td {{
    background: #d8f3dc;
    font-weight: 700;
    border-top: 1px solid #95d5a8;
  }}

  tr.period-tot td {{
    background: #b7e4c7;
    font-weight: 700;
    font-size: 13px;
    border-top: 2px solid #74c69d;
  }}

  .pct-low  {{ color: #006100; font-weight: 600; }}
  .pct-mid  {{ color: #7d4e00; font-weight: 600; }}
  .pct-high {{ color: #9c0006; font-weight: 600; }}

  .legend {{
    display: flex;
    gap: 16px;
    padding: 6px 24px;
    font-size: 11px;
    background: #f8f9fa;
    border-bottom: 1px solid #e0e0e0;
  }}
  .legend span {{ display: flex; align-items: center; gap: 4px; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
  .dot-low  {{ background: #006100; }}
  .dot-mid  {{ background: #c67c00; }}
  .dot-high {{ background: #9c0006; }}

  .updated {{
    text-align: center;
    padding: 10px;
    font-size: 11px;
    color: #888;
    background: #fff;
  }}
</style>
</head>
<body>
<header>
  <h1>On Par Entertainment — Labor Report</h1>
  <span>FY{fy} &nbsp;·&nbsp; Period {cur_period} &nbsp;·&nbsp; {today.strftime('%B %-d, %Y')} &nbsp;·&nbsp; Refreshes every 5 min</span>
</header>
<div class="tab-bar">{tabs_html}</div>
<div class="legend">
  <span><span class="dot dot-low"></span> &lt;20% labor</span>
  <span><span class="dot dot-mid"></span> 20–30%</span>
  <span><span class="dot dot-high"></span> &gt;30%</span>
  <span style="margin-left:16px;color:#555">KIT % = KIT cost ÷ Food sales &nbsp;·&nbsp; FOH % = FOH cost ÷ (Total − Food) &nbsp;·&nbsp; Total % = Total cost ÷ Total sales</span>
  <span style="margin-left:16px;color:#555">Week subtotals: actual hrs / sched hrs</span>
</div>
<div class="table-scroll">
<table>
  <thead>
    <tr>
      <th rowspan="1">Date</th>
      <th>Day</th>
      <th>Total Sales</th>
      <th class="group-kit">KIT Hrs</th>
      <th class="group-kit">KIT $</th>
      <th class="group-kit">KIT %</th>
      <th class="group-foh">FOH Hrs</th>
      <th class="group-foh">FOH $</th>
      <th class="group-foh">FOH %</th>
      <th class="group-tot">Total Hrs</th>
      <th class="group-tot">Total $</th>
      <th class="group-tot">Total %</th>
    </tr>
  </thead>
  <tbody>
{table_rows}
  </tbody>
</table>
<p class="updated">Source: 7Shifts time punches &nbsp;·&nbsp; Sales from GoTab + Tripleseat &nbsp;·&nbsp; Data refreshes nightly at 6 AM ET</p>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main + Vercel handler
# ---------------------------------------------------------------------------

def run(fy: int, today: date) -> str:
    fy_start    = _fy_start(fy)
    num_periods = _periods_to_show(fy, today)
    _, fy_end   = _period_bounds(num_periods, fy)

    labor   = _fetch_labor(fy_start, fy_end)
    revenue = _fetch_revenue(fy_start, fy_end)
    rows    = _build_rows(fy, today, labor, revenue)
    return _render_html(fy, rows, today, "")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs  = parse_qs(urlparse(self.path).query)
        key = qs.get("key", [""])[0]

        if REPORT_KEY and key != REPORT_KEY:
            self.send_response(401)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        today = date.today()
        try:
            fy_param = int(qs.get("year", ["2026"])[0])
        except ValueError:
            fy_param = 2026
        fy = fy_param if fy_param in _FY_STARTS else 2026

        try:
            fy_start    = _fy_start(fy)
            num_periods = _periods_to_show(fy, today)
            _, fy_end   = _period_bounds(num_periods, fy)

            labor   = _fetch_labor(fy_start, fy_end)
            revenue = _fetch_revenue(fy_start, fy_end)
            rows    = _build_rows(fy, today, labor, revenue)
            html    = _render_html(fy, rows, today, key)

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, format, *args):
        pass
