"""
Daily Google Sheets report builder.

Runs at 0 14 * * * (10 AM ET) after all data sync crons complete.
Rebuilds a single tab covering P1 FY2026 through end of current fiscal period:

  Date | Day | Food | Bev | Games | Karaoke | Events | Other | Total | LY Total | +/- $ | +/- %

LY = same fiscal position 364 days prior (Option A: 52-week offset).
Games = all GoTab Entertainment + Tripleseat per-game amounts.
Other = Merchandise + Reservations + Open Item + Bottle Svc + Gift Card + Redeemed.

Required env vars:
  SUPABASE_URL, SUPABASE_SERVICE_KEY
  GOOGLE_SHEETS_CREDENTIALS  — service account JSON as a single-line string
  GOOGLE_SHEET_ID            — spreadsheet ID from the sheet URL
  GOOGLE_SHEET_TAB           — tab name (default: "Daily Report")
  CRON_SECRET                — optional bearer token for cron auth
"""

import json
import os
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler

from google.oauth2 import service_account
import google.auth.transport.requests

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
CRON_SECRET  = os.environ.get("CRON_SECRET", "")
SHEET_ID     = os.environ["GOOGLE_SHEET_ID"]
SHEET_TAB    = os.environ.get("GOOGLE_SHEET_TAB", "Daily Report")


# ---------------------------------------------------------------------------
# Fiscal calendar
# ---------------------------------------------------------------------------
FY2026_START      = date(2025, 12, 29)
PRIOR_YR_OFFSET   = timedelta(days=364)

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
    fy_week = delta // 7
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


def _gotab_bucket(category: str, product: str) -> str:
    cat = (category or "").lower().strip()
    if cat in _FOOD_CATS:       return "food"
    if cat in _BEV_CATS:        return "bev"
    if cat == "karaoke":        return "karaoke"
    if cat == "entertainment":  return "games"
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


def _supa_get_paged(base_path: str) -> list:
    all_rows, offset = [], 0
    while True:
        sep  = "&" if "?" in base_path else "?"
        rows = _supa_get(f"{base_path}{sep}limit=1000&offset={offset}")
        all_rows.extend(rows)
        if len(rows) < 1000:
            break
        offset += 1000
    return all_rows


# ---------------------------------------------------------------------------
# Data fetching and merging
# ---------------------------------------------------------------------------
_ZERO = {"food": 0.0, "bev": 0.0, "games": 0.0,
         "karaoke": 0.0, "events": 0.0, "other": 0.0, "total": 0.0}


def _fetch_and_merge(start: date, end: date) -> dict:
    """
    Fetch GoTab + Tripleseat for [start, end] and return
    {date_str: {food, bev, games, karaoke, events, other, total}}.
    """
    # GoTab
    gt_rows = _supa_get_paged(
        f"/sales?report_date=gte.{start.isoformat()}"
        f"&report_date=lte.{end.isoformat()}"
        f"&select=report_date,category,product,net_sales"
    )
    by_date: dict = {}
    for row in gt_rows:
        d    = row["report_date"]
        buck = _gotab_bucket(row.get("category") or "", row.get("product") or "")
        amt  = float(row.get("net_sales") or 0)
        if d not in by_date:
            by_date[d] = dict(_ZERO)
        by_date[d][buck] = round(by_date[d][buck] + amt, 2)

    # Tripleseat
    ts_rows = _supa_get_paged(
        f"/ts_events?event_date=gte.{start.isoformat()}"
        f"&event_date=lte.{end.isoformat()}"
        f"&deleted_at=is.null"
        f"&select=event_date,food_amount,beverage_amount,events_amount,"
        f"bowling_amount,mini_golf_amount,darts_amount,shuffle_board_amount,pool_amount"
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
        by_date[d]["games"]  = round(by_date[d]["games"]  + game, 2)

    # Compute totals
    for d, v in by_date.items():
        v["total"] = round(v["food"] + v["bev"] + v["games"] +
                           v["karaoke"] + v["events"] + v["other"], 2)
    return by_date


# ---------------------------------------------------------------------------
# Google Sheets auth
# ---------------------------------------------------------------------------
def _get_token() -> str:
    creds_info = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def _sheets_req(method: str, path: str, body=None, token: str = "") -> dict:
    url  = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _ensure_tab(token: str) -> int:
    meta = _sheets_req("GET", "", token=token)
    for sheet in meta.get("sheets", []):
        props = sheet["properties"]
        if props["title"] == SHEET_TAB:
            return props["sheetId"]
    resp = _sheets_req("POST", ":batchUpdate", body={
        "requests": [{"addSheet": {"properties": {"title": SHEET_TAB}}}]
    }, token=token)
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


# ---------------------------------------------------------------------------
# Sheet content building
# ---------------------------------------------------------------------------
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _data_row(d: str, act: dict, ly: dict, today: date) -> list:
    dt  = date.fromisoformat(d)
    day = _DAYS[dt.weekday()]
    is_future = dt > today

    if is_future:
        food = bev = games = karaoke = events = other = total = ""
    else:
        food    = act.get("food",    0.0)
        bev     = act.get("bev",     0.0)
        games   = act.get("games",   0.0)
        karaoke = act.get("karaoke", 0.0)
        events  = act.get("events",  0.0)
        other   = act.get("other",   0.0)
        total   = act.get("total",   0.0)

    ly_total = ly.get("total", "") if ly else ""
    if not is_future and isinstance(total, float) and isinstance(ly_total, float) and ly_total:
        var_d = round(total - ly_total, 2)
        var_p = round((total - ly_total) / ly_total, 4)
    else:
        var_d = var_p = ""

    return [d, day, food, bev, games, karaoke, events, other, total, ly_total, var_d, var_p]


def _sum_col(rows: list, idx: int):
    vals = [r[idx] for r in rows if isinstance(r[idx], (int, float))]
    return round(sum(vals), 2) if vals else ""


def _subtotal_row(label: str, data_rows: list) -> list:
    food    = _sum_col(data_rows, 2)
    bev     = _sum_col(data_rows, 3)
    games   = _sum_col(data_rows, 4)
    karaoke = _sum_col(data_rows, 5)
    events  = _sum_col(data_rows, 6)
    other   = _sum_col(data_rows, 7)
    total   = _sum_col(data_rows, 8)
    ly      = _sum_col(data_rows, 9)
    var_d   = round(total - ly, 2) if isinstance(total, float) and isinstance(ly, float) and ly else ""
    var_p   = round(var_d / ly, 4) if isinstance(var_d, float) and isinstance(ly, float) and ly else ""
    return [label, "", food, bev, games, karaoke, events, other, total, ly, var_d, var_p]


def _build_sheet(today: date, actuals: dict, ly: dict) -> tuple:
    """Return (values, row_meta) where row_meta = [(row_idx, row_type), ...]."""
    values, meta = [], []

    values.append(["Date", "Day", "Food", "Bev", "Games",
                   "Karaoke", "Events", "Other", "Total",
                   "LY Total", "+/- $", "+/- %"])
    meta.append((0, "header"))
    idx = 1

    cur_period = _current_period(today)
    for period in range(1, cur_period + 1):
        p_start, p_end = _period_bounds(period)

        values.append([f"Period {period}", *[""] * 11])
        meta.append((idx, "period_hdr"))
        idx += 1

        period_rows = []
        for week in range(1, _WEEKS_PER_PERIOD[period] + 1):
            w_start = p_start + timedelta(weeks=week - 1)
            w_end   = w_start + timedelta(days=6)
            label   = f"  Week {week}  ({w_start.strftime('%-m/%-d')}–{w_end.strftime('%-m/%-d')})"

            values.append([label, *[""] * 11])
            meta.append((idx, "week_hdr"))
            idx += 1

            week_rows = []
            for delta in range(7):
                d   = (w_start + timedelta(days=delta)).isoformat()
                row = _data_row(d, actuals.get(d, {}), ly.get(d), today)
                dt  = date.fromisoformat(d)
                values.append(row)
                meta.append((idx, "data" if dt <= today else "future"))
                week_rows.append(row)
                period_rows.append(row)
                idx += 1

            sub = _subtotal_row(f"  Week {week} Total", week_rows)
            values.append(sub)
            meta.append((idx, "week_sub"))
            idx += 1

        ptot = _subtotal_row(f"Period {period} Total", period_rows)
        values.append(ptot)
        meta.append((idx, "period_tot"))
        idx += 1

    return values, meta


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
_COL_WIDTHS = [100, 45, 90, 90, 90, 90, 90, 90, 90, 90, 90, 90]


def _rgb(r, g, b):
    return {"red": r / 255, "green": g / 255, "blue": b / 255}


def _cell_fmt(sid, r1, r2, c1, c2, **fields):
    return {
        "repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                      "startColumnIndex": c1, "endColumnIndex": c2},
            "cell": {"userEnteredFormat": fields},
            "fields": "userEnteredFormat(" + ",".join(fields.keys()) + ")",
        }
    }


_CURRENCY = {"numberFormat": {"type": "CURRENCY", "pattern": '"$"#,##0.00'}}
_PERCENT  = {"numberFormat": {"type": "PERCENT",  "pattern": "0.0%"}}


def _build_format_requests(sid: int, meta: list, values: list) -> list:
    reqs = []

    for i, w in enumerate(_COL_WIDTHS):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w}, "fields": "pixelSize",
        }})

    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sid,
                       "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 2}},
        "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
    }})

    _STYLES = {
        "header":     (_rgb(32, 56, 100),   True,  _rgb(255, 255, 255), 10),
        "period_hdr": (_rgb(13, 37, 71),    True,  _rgb(255, 255, 255), 11),
        "week_hdr":   (_rgb(68, 114, 196),  True,  _rgb(255, 255, 255), 10),
        "data":       (_rgb(255, 255, 255), False, _rgb(0, 0, 0),        10),
        "future":     (_rgb(242, 242, 242), False, _rgb(166, 166, 166),  10),
        "week_sub":   (_rgb(221, 235, 247), True,  _rgb(0, 0, 0),        10),
        "period_tot": (_rgb(189, 215, 238), True,  _rgb(0, 0, 0),        10),
    }

    for ri, rt in meta:
        bg, bold, fg, size = _STYLES[rt]
        reqs.append(_cell_fmt(sid, ri, ri + 1, 0, 12,
            backgroundColor=bg,
            textFormat={"bold": bold, "foregroundColor": fg, "fontSize": size}))

        if rt in ("data", "future", "week_sub", "period_tot"):
            reqs.append(_cell_fmt(sid, ri, ri + 1, 2, 11, **_CURRENCY))
            reqs.append(_cell_fmt(sid, ri, ri + 1, 11, 12, **_PERCENT))

        # Variance column red/green
        if rt in ("data", "week_sub", "period_tot"):
            row   = values[ri]
            var_d = row[10]
            if isinstance(var_d, float):
                color = _rgb(0, 97, 0) if var_d >= 0 else _rgb(156, 0, 6)
                reqs.append(_cell_fmt(sid, ri, ri + 1, 10, 12,
                    textFormat={"foregroundColor": color}))

    return reqs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(today: date = None) -> str:
    today = today or date.today()

    token    = _get_token()
    sheet_id = _ensure_tab(token)

    cur_period       = _current_period(today)
    fy_start         = FY2026_START
    _, period_end    = _period_bounds(cur_period)

    # Fetch current year actuals (full period range for display)
    actuals = _fetch_and_merge(fy_start, period_end)

    # Fetch LY in the equivalent fiscal date range
    ly_raw   = _fetch_and_merge(fy_start - PRIOR_YR_OFFSET,
                                period_end - PRIOR_YR_OFFSET)

    # Re-key LY rows from LY dates to CY dates (same positional offset)
    ly: dict = {}
    for i in range((period_end - fy_start).days + 1):
        cy_date = (fy_start + timedelta(days=i)).isoformat()
        ly_date = (fy_start - PRIOR_YR_OFFSET + timedelta(days=i)).isoformat()
        if ly_date in ly_raw:
            ly[cy_date] = ly_raw[ly_date]

    values, meta = _build_sheet(today, actuals, ly)

    # Clear tab and rewrite
    range_a1 = f"'{SHEET_TAB}'!A1:L{len(values) + 2}"
    _sheets_req("POST",
        f"/values/{_urlencode(range_a1)}:clear",
        token=token)

    _sheets_req("PUT",
        f"/values/{_urlencode(range_a1)}?valueInputOption=RAW",
        body={"range": range_a1, "majorDimension": "ROWS", "values": values},
        token=token)

    fmt_reqs = _build_format_requests(sheet_id, meta, values)
    for i in range(0, len(fmt_reqs), 500):
        _sheets_req("POST", ":batchUpdate",
            body={"requests": fmt_reqs[i:i + 500]}, token=token)

    data_rows = sum(1 for _, rt in meta if rt in ("data", "future"))
    return (f"ok: {data_rows} rows | through period {cur_period} "
            f"({fy_start.isoformat()} → {period_end.isoformat()})")


def _urlencode(s: str) -> str:
    return urllib.request.pathname2url(s)


# ---------------------------------------------------------------------------
# Vercel handler
# ---------------------------------------------------------------------------
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get("authorization", "")
        if CRON_SECRET and auth != f"Bearer {CRON_SECRET}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        try:
            result = run()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": result}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass
