"""
Live HTML daily revenue report.

GET /api/daily_report?key=YOUR_REPORT_KEY

Returns a styled HTML page covering P1 FY2026 through end of current fiscal
period. Columns: Date | Day | Food | Bev | Games | Karaoke | Events | Other |
Total | LY Total | +/- $ | +/- %

Auto-refreshes every 5 minutes.

Required env vars:
  SUPABASE_URL, SUPABASE_SERVICE_KEY
  REPORT_KEY   — secret query param (?key=...) to protect the page
"""

import json
import os
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
REPORT_KEY   = os.environ.get("REPORT_KEY", "")


# ---------------------------------------------------------------------------
# Fiscal calendar
# ---------------------------------------------------------------------------
FY2026_START    = date(2025, 12, 29)
PRIOR_YR_OFFSET = timedelta(days=364)

_WEEKS_PER_PERIOD = {
    1: 5, 2: 4, 3: 4,
    4: 5, 5: 4, 6: 4,
    7: 5, 8: 4, 9: 4,
    10: 5, 11: 4, 12: 4,
}


def _period_bounds(period: int) -> tuple:
    week_offset = sum(_WEEKS_PER_PERIOD[p] for p in range(1, period))
    start = FY2026_START + timedelta(weeks=week_offset)
    end   = start + timedelta(weeks=_WEEKS_PER_PERIOD[period]) - timedelta(days=1)
    return start, end


def _current_period(today: date) -> int:
    delta = (today - FY2026_START).days
    if delta < 0:
        return 1
    fy_week    = delta // 7
    cumulative = 0
    for p in range(1, 13):
        cumulative += _WEEKS_PER_PERIOD[p]
        if fy_week < cumulative:
            return p
    return 12


# ---------------------------------------------------------------------------
# GoTab category → report bucket
# ---------------------------------------------------------------------------
_FOOD_CATS = frozenset({
    "chicken.", "dessert", "event food", "extra sauces and cheese dips",
    "fry platters", "half pound burgers", "legacy menu items",
    "pizza and flatbreads", "pretzels", "tacos", "tater kegs", "wraps",
})
_BEV_CATS = frozenset({"beverage", "soda pop", "wine"})


def _bucket(category: str) -> str:
    cat = (category or "").lower().strip()
    if cat in _FOOD_CATS:      return "food"
    if cat in _BEV_CATS:       return "bev"
    if cat == "karaoke":       return "karaoke"
    if cat == "entertainment": return "games"
    return "other"


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
# Data fetch + merge
# ---------------------------------------------------------------------------
_ZERO = {"food": 0.0, "bev": 0.0, "games": 0.0,
         "karaoke": 0.0, "events": 0.0, "other": 0.0, "total": 0.0}


def _fetch(start: date, end: date) -> dict:
    by_date: dict = {}

    for row in _supa_paged(
        f"/sales?report_date=gte.{start}&report_date=lte.{end}"
        f"&select=report_date,category,net_sales"
    ):
        d   = row["report_date"]
        bk  = _bucket(row.get("category") or "")
        amt = float(row.get("net_sales") or 0)
        if d not in by_date:
            by_date[d] = dict(_ZERO)
        by_date[d][bk] = round(by_date[d][bk] + amt, 2)

    # Try with per-game columns; fall back if revenue_schema.sql hasn't been run yet
    try:
        ts_rows = _supa_paged(
            f"/ts_events?event_date=gte.{start}&event_date=lte.{end}"
            f"&deleted_at=is.null"
            f"&select=event_date,food_amount,beverage_amount,events_amount,"
            f"bowling_amount,mini_golf_amount,darts_amount,shuffle_board_amount,pool_amount"
        )
    except Exception:
        ts_rows = _supa_paged(
            f"/ts_events?event_date=gte.{start}&event_date=lte.{end}"
            f"&deleted_at=is.null"
            f"&select=event_date,food_amount,beverage_amount,events_amount"
        )
    for row in ts_rows:
        d = row["event_date"]
        if d not in by_date:
            by_date[d] = dict(_ZERO)
        by_date[d]["food"]   = round(by_date[d]["food"]   + float(row.get("food_amount")     or 0), 2)
        by_date[d]["bev"]    = round(by_date[d]["bev"]    + float(row.get("beverage_amount") or 0), 2)
        by_date[d]["events"] = round(by_date[d]["events"] + float(row.get("events_amount")   or 0), 2)
        game = sum(float(row.get(c) or 0) for c in (
            "bowling_amount", "mini_golf_amount", "darts_amount",
            "shuffle_board_amount", "pool_amount",
        ))
        by_date[d]["games"] = round(by_date[d]["games"] + game, 2)

    for v in by_date.values():
        v["total"] = round(v["food"] + v["bev"] + v["games"] +
                           v["karaoke"] + v["events"] + v["other"], 2)
    return by_date


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
def _fmt(val) -> str:
    if val == "" or val is None:
        return ""
    return f"${val:,.2f}"


def _fmt_pct(val) -> str:
    if val == "" or val is None:
        return ""
    return f"{val * 100:+.1f}%"


def _var_class(val) -> str:
    if not isinstance(val, float):
        return ""
    return "pos" if val >= 0 else "neg"


def _sum_col(rows: list, key: str):
    vals = [r[key] for r in rows if isinstance(r.get(key), float)]
    return round(sum(vals), 2) if vals else ""


def _subtotal(rows: list) -> dict:
    food    = _sum_col(rows, "food")
    bev     = _sum_col(rows, "bev")
    games   = _sum_col(rows, "games")
    karaoke = _sum_col(rows, "karaoke")
    events  = _sum_col(rows, "events")
    other   = _sum_col(rows, "other")
    total   = _sum_col(rows, "total")
    ly      = _sum_col(rows, "ly_total")
    var_d   = round(total - ly, 2) if isinstance(total, float) and isinstance(ly, float) and ly else ""
    var_p   = round(var_d / ly, 4) if isinstance(var_d, float) and isinstance(ly, float) and ly else ""
    return dict(food=food, bev=bev, games=games, karaoke=karaoke,
                events=events, other=other, total=total,
                ly_total=ly, var_d=var_d, var_p=var_p)


_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _build_rows(today: date, actuals: dict, ly: dict) -> list:
    """Return list of dicts with all display fields, plus _type for styling."""
    rows = []
    cur  = _current_period(today)

    for period in range(1, cur + 1):
        p_start, p_end = _period_bounds(period)
        rows.append({"_type": "period_hdr", "label": f"Period {period}"})

        period_data = []
        for week in range(1, _WEEKS_PER_PERIOD[period] + 1):
            w_start = p_start + timedelta(weeks=week - 1)
            w_end   = w_start + timedelta(days=6)
            rows.append({
                "_type": "week_hdr",
                "label": f"Week {week} — {w_start.strftime('%-m/%-d')} to {w_end.strftime('%-m/%-d')}",
            })

            week_data = []
            for delta in range(7):
                d      = (w_start + timedelta(days=delta))
                ds     = d.isoformat()
                is_fut = d > today
                act    = actuals.get(ds, {})
                ly_v   = ly.get(ds, {})

                food    = act.get("food",    "") if not is_fut else ""
                bev     = act.get("bev",     "") if not is_fut else ""
                games   = act.get("games",   "") if not is_fut else ""
                karaoke = act.get("karaoke", "") if not is_fut else ""
                events  = act.get("events",  "") if not is_fut else ""
                other   = act.get("other",   "") if not is_fut else ""
                total   = act.get("total",   "") if not is_fut else ""
                ly_tot  = ly_v.get("total",  "") if ly_v else ""

                if not is_fut and isinstance(total, float) and isinstance(ly_tot, float) and ly_tot:
                    var_d = round(total - ly_tot, 2)
                    var_p = round(var_d / ly_tot, 4)
                else:
                    var_d = var_p = ""

                row = {
                    "_type":    "future" if is_fut else "data",
                    "date":     ds,
                    "day":      _DAYS[d.weekday()],
                    "food":     food,
                    "bev":      bev,
                    "games":    games,
                    "karaoke":  karaoke,
                    "events":   events,
                    "other":    other,
                    "total":    total,
                    "ly_total": ly_tot,
                    "var_d":    var_d,
                    "var_p":    var_p,
                }
                rows.append(row)
                week_data.append(row)
                period_data.append(row)

            sub = _subtotal(week_data)
            sub["_type"]  = "week_sub"
            sub["label"]  = f"Week {week} Total"
            rows.append(sub)

        ptot = _subtotal(period_data)
        ptot["_type"] = "period_tot"
        ptot["label"] = f"Period {period} Total"
        rows.append(ptot)

    return rows


def _render_html(rows: list, today: date, cur_period: int) -> str:
    def tr(row: dict) -> str:
        t = row["_type"]

        if t in ("period_hdr",):
            return (f'<tr class="period-hdr">'
                    f'<td colspan="12">{row["label"]}</td></tr>')

        if t == "week_hdr":
            return (f'<tr class="week-hdr">'
                    f'<td colspan="12">{row["label"]}</td></tr>')

        if t in ("week_sub", "period_tot"):
            css   = "week-sub" if t == "week_sub" else "period-tot"
            vc    = _var_class(row.get("var_d"))
            return (
                f'<tr class="{css}">'
                f'<td colspan="2">{row["label"]}</td>'
                f'<td>{_fmt(row.get("food"))}</td>'
                f'<td>{_fmt(row.get("bev"))}</td>'
                f'<td>{_fmt(row.get("games"))}</td>'
                f'<td>{_fmt(row.get("karaoke"))}</td>'
                f'<td>{_fmt(row.get("events"))}</td>'
                f'<td>{_fmt(row.get("other"))}</td>'
                f'<td>{_fmt(row.get("total"))}</td>'
                f'<td>{_fmt(row.get("ly_total"))}</td>'
                f'<td class="{vc}">{_fmt(row.get("var_d"))}</td>'
                f'<td class="{vc}">{_fmt_pct(row.get("var_p"))}</td>'
                f'</tr>'
            )

        css = "future" if t == "future" else "data"
        vc  = _var_class(row.get("var_d"))
        return (
            f'<tr class="{css}">'
            f'<td>{row["date"]}</td>'
            f'<td>{row["day"]}</td>'
            f'<td>{_fmt(row.get("food"))}</td>'
            f'<td>{_fmt(row.get("bev"))}</td>'
            f'<td>{_fmt(row.get("games"))}</td>'
            f'<td>{_fmt(row.get("karaoke"))}</td>'
            f'<td>{_fmt(row.get("events"))}</td>'
            f'<td>{_fmt(row.get("other"))}</td>'
            f'<td>{_fmt(row.get("total"))}</td>'
            f'<td>{_fmt(row.get("ly_total"))}</td>'
            f'<td class="{vc}">{_fmt(row.get("var_d"))}</td>'
            f'<td class="{vc}">{_fmt_pct(row.get("var_p"))}</td>'
            f'</tr>'
        )

    table_rows = "\n".join(tr(r) for r in rows)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="300">
<title>On Par — Sales Variance Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 13px;
    background: #f0f2f5;
    color: #1a1a2e;
  }}

  header {{
    background: #0d2547;
    color: #fff;
    padding: 14px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
    box-shadow: 0 2px 8px rgba(0,0,0,.4);
  }}

  header h1 {{ font-size: 16px; font-weight: 600; letter-spacing: .5px; }}
  header span {{ font-size: 12px; opacity: .7; }}

  .wrap {{ padding: 20px; }}
  .table-scroll {{ overflow-x: auto; }}

  table {{
    width: 100%;
    border-collapse: collapse;
    background: #fff;
    box-shadow: 0 1px 4px rgba(0,0,0,.12);
  }}

  thead th {{
    background: #203864;
    color: #fff;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .6px;
    padding: 10px 12px;
    text-align: right;
    white-space: nowrap;
    position: sticky;
    top: 52px;
    z-index: 10;
  }}

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

  tr.data:hover {{ background: #f7f9ff; }}

  tr.future td {{ color: #aaa; background: #fafafa; }}

  tr.period-hdr td {{
    background: #0d2547;
    color: #fff;
    font-weight: 700;
    font-size: 12px;
    letter-spacing: .4px;
    padding: 9px 12px;
  }}

  tr.week-hdr td {{
    background: #4472c4;
    color: #fff;
    font-weight: 600;
    font-size: 11px;
    padding: 7px 12px;
  }}

  tr.week-sub td {{
    background: #dde8f5;
    font-weight: 700;
    border-top: 1px solid #b8d0ea;
  }}

  tr.period-tot td {{
    background: #bdd7ee;
    font-weight: 700;
    font-size: 13px;
    border-top: 2px solid #8ab4d8;
  }}

  .pos {{ color: #006100; font-weight: 600; }}
  .neg {{ color: #9c0006; font-weight: 600; }}

  .updated {{
    text-align: center;
    padding: 12px;
    font-size: 11px;
    color: #888;
  }}
</style>
</head>
<body>
<header>
  <h1>On Par Entertainment — Sales Variance</h1>
  <span>Period {cur_period} &nbsp;·&nbsp; As of {today.strftime('%B %-d, %Y')} &nbsp;·&nbsp; Refreshes every 5 min</span>
</header>
<div class="wrap">
<div class="table-scroll">
<table>
  <thead>
    <tr>
      <th>Date</th><th>Day</th>
      <th>Food</th><th>Bev</th><th>Games</th>
      <th>Karaoke</th><th>Events</th><th>Other</th>
      <th>Total</th><th>LY Total</th>
      <th>+/- $</th><th>+/- %</th>
    </tr>
  </thead>
  <tbody>
{table_rows}
  </tbody>
</table>
</div>
<p class="updated">Data as of {today.strftime('%B %-d, %Y')} &nbsp;·&nbsp; LY = same fiscal day FY2025 (364-day offset)</p>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(today: date = None) -> str:
    today = today or date.today()

    cur_period    = _current_period(today)
    _, period_end = _period_bounds(cur_period)

    actuals = _fetch(FY2026_START, period_end)

    ly_start = FY2026_START - PRIOR_YR_OFFSET
    ly_end   = period_end   - PRIOR_YR_OFFSET
    ly_raw   = _fetch(ly_start, ly_end)

    # Re-key LY rows to current-year dates by fiscal position
    ly: dict = {}
    for i in range((period_end - FY2026_START).days + 1):
        cy = (FY2026_START + timedelta(days=i)).isoformat()
        lk = (ly_start     + timedelta(days=i)).isoformat()
        if lk in ly_raw:
            ly[cy] = ly_raw[lk]

    rows = _build_rows(today, actuals, ly)
    return _render_html(rows, today, cur_period)


# ---------------------------------------------------------------------------
# Vercel handler
# ---------------------------------------------------------------------------
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

        try:
            html = run()
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
