import json
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
import os

from beo_parser import compute_food_bev

# Best-effort status reporting to the Agent Control Board (never breaks this agent).
try:
    from control_board import report
except Exception:
    def report(*_a, **_k):
        return

# --- Config ---
TS_TOKEN_URL   = "https://api.tripleseat.com/oauth2/token"
TS_API_BASE    = "https://api.tripleseat.com/v1"
TS_CLIENT_ID   = os.environ.get("TS_CLIENT_ID", "")
TS_CLIENT_SECRET = os.environ.get("TS_CLIENT_SECRET", "")
SUPABASE_URL   = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY   = os.environ.get("SUPABASE_SERVICE_KEY", "")
CRON_SECRET    = os.environ.get("CRON_SECRET", "")

UPSERT_CHUNK   = 100   # rows per Supabase upsert call
TS_PAGE_DELAY  = 0.25  # seconds between Tripleseat page requests


# --- Tripleseat helpers ---

def get_ts_token() -> str:
    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     TS_CLIENT_ID,
        "client_secret": TS_CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(
        TS_TOKEN_URL, data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept":       "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["access_token"]


def ts_get(token: str, path: str) -> dict:
    req = urllib.request.Request(
        f"{TS_API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept":        "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_all_pages(token: str, endpoint: str) -> list:
    records = []
    page = 1
    while True:
        time.sleep(TS_PAGE_DELAY)
        result = ts_get(token, f"/{endpoint}.json?page={page}")
        batch = result.get("results") or []
        records.extend(batch)
        if page >= (result.get("total_pages") or 1):
            break
        page += 1
    return records


# --- Transform helpers ---

def _str(v):
    return str(v).strip() or None if v is not None else None


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _date(v):
    if not v:
        return None
    s = str(v).strip()
    return s[:10] if len(s) >= 10 else None


def _ts(v):
    return str(v).strip() or None if v else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def transform_booking(b: dict) -> dict:
    contact = b.get("contact") or {}
    emails  = contact.get("email_addresses") or []
    account = b.get("account") or {}
    return {
        "id":                   b["id"],
        "name":                 _str(b.get("name")),
        "status":               _str(b.get("status")),
        "start_date":           _date(b.get("start_date")),
        "end_date":             _date(b.get("end_date")),
        "definite_date":        _date(b.get("definite_date")),
        "tentative_date":       _date(b.get("tentative_date")),
        "lost_date":            _date(b.get("lost_date")),
        "total_actual_amount":  _num(b.get("total_actual_amount")),
        "total_grand_total":    _num(b.get("total_grand_total")),
        "market_segment":       _str(b.get("market_segment")),
        "contact_id":           _int(contact.get("id")),
        "contact_name":         (
            f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip() or None
        ),
        "contact_email":        _str(emails[0]["address"]) if emails else None,
        "account_id":           _int(account.get("id")),
        "account_name":         _str(account.get("name")),
        "location_id":          _int(b.get("location_id")),
        "created_at":           _ts(b.get("created_at")),
        "updated_at":           _ts(b.get("updated_at")),
        "deleted_at":           _ts(b.get("deleted_at")),
        "synced_at":            _now_iso(),
    }


def _extract_game_amounts(e: dict) -> dict:
    """Extract per-game revenue from category_totals (Tripleseat API field)."""
    games = {
        "bowling_amount": 0.0,
        "mini_golf_amount": 0.0,
        "darts_amount": 0.0,
        "shuffle_board_amount": 0.0,
        "pool_amount": 0.0,
        "karaoke_amount": 0.0,
    }
    for ct in (e.get("category_totals") or []):
        name  = (ct.get("name") or "").lower()
        total = float(ct.get("total") or 0)
        if not total:
            continue
        if "bowling" in name:
            games["bowling_amount"] += total
        elif "mini golf" in name or "minigolf" in name:
            games["mini_golf_amount"] += total
        elif "dart" in name:
            games["darts_amount"] += total
        elif "shuffle" in name:
            games["shuffle_board_amount"] += total
        elif "pool" in name or "billiard" in name:
            games["pool_amount"] += total
        elif "karaoke" in name:
            games["karaoke_amount"] += total
    return {k: round(v, 2) if v else None for k, v in games.items()}


def _compute_events_amount(e: dict) -> float:
    """
    Sum booking fees (from billing_totals) and extra-hour charges
    (from category_totals) — these go in the events_amount column.
    """
    total = 0.0
    for item in (e.get("billing_totals") or []):
        name = (item.get("name") or "").lower()
        if "booking fee" in name or "booking_fee" in name:
            total += float(item.get("total") or 0)
    for item in (e.get("category_totals") or []):
        name = (item.get("name") or "").lower()
        if "extra hour" in name or "additional hour" in name:
            total += float(item.get("total") or 0)
    return round(total, 2) or None


def transform_event(e: dict) -> dict:
    rooms      = e.get("rooms") or []
    room_names = ", ".join(r["name"] for r in rooms if r.get("name")) or None

    food, bev, method = compute_food_bev(e)
    events_amt = _compute_events_amount(e)
    game_amts  = _extract_game_amounts(e)

    return {
        "id":                     e["id"],
        "booking_id":             _int(e.get("booking_id")),
        "name":                   _str(e.get("name")),
        "status":                 _str(e.get("status")),
        "event_date":             _date(e.get("event_date_iso8601")),
        "event_start":            _ts(e.get("event_start_iso8601")),
        "event_end":              _ts(e.get("event_end_iso8601")),
        "event_timezone":         _str(e.get("event_timezone")),
        "event_type":             _str(e.get("event_type")),
        "event_style":            _str(e.get("event_style")),
        "guest_count":            _int(e.get("guest_count")),
        "guaranteed_guest_count": _int(e.get("guaranteed_guest_count")),
        "food_and_beverage_min":  _num(e.get("food_and_beverage_min")),
        "price_per_person":       _num(e.get("price_per_person")),
        "deposit_amount":         _num(e.get("deposit_amount")),
        "rental_fee":             _num(e.get("rental_fee")),
        "actual_amount":          _num(e.get("actual_amount")),
        "grand_total":            _num(e.get("grand_total")),
        "amount_due":             _num(e.get("amount_due")),
        "room_names":             room_names,
        "description":            _str(e.get("description")),
        "food_amount":            food if food else None,
        "beverage_amount":        bev  if bev  else None,
        "events_amount":          events_amt,
        "split_method":           method,
        "bowling_amount":         game_amts["bowling_amount"],
        "mini_golf_amount":       game_amts["mini_golf_amount"],
        "darts_amount":           game_amts["darts_amount"],
        "shuffle_board_amount":   game_amts["shuffle_board_amount"],
        "pool_amount":            game_amts["pool_amount"],
        "karaoke_amount":         game_amts["karaoke_amount"],
        "contact_id":             _int(e.get("contact_id")),
        "account_id":             _int(e.get("account_id")),
        "location_id":            _int(e.get("location_id")),
        "created_at":             _ts(e.get("created_at")),
        "updated_at":             _ts(e.get("updated_at")),
        "deleted_at":             _ts(e.get("deleted_at")),
        "synced_at":              _now_iso(),
    }


def transform_lead(l: dict) -> dict:
    lead_src = l.get("lead_source") or {}
    return {
        "id":                _int(l["id"]),
        "first_name":        _str(l.get("first_name")),
        "last_name":         _str(l.get("last_name")),
        "company":           _str(l.get("company")),
        "email_address":     _str(l.get("email_address")),
        "phone_number":      _str(l.get("phone_number")),
        "event_date":        _date(l.get("event_date")),
        "guest_count":       _int(l.get("guest_count")),
        "event_description": _str(l.get("event_description")),
        "event_style":       _str(l.get("event_style")),
        "start_time":        _str(l.get("start_time")),
        "end_time":          _str(l.get("end_time")),
        "lead_source":       _str(lead_src.get("name")),
        "booking_lead":      bool(l.get("booking_lead")),
        "email_opt_in":      bool(l.get("email_opt_in")),
        "converted_at":      _ts(l.get("converted_at")),
        "turned_down_at":    _ts(l.get("turned_down_at")),
        "contact_id":        _int(l.get("contact_id")),
        "account_id":        _int(l.get("account_id")),
        "event_id":          _int(l.get("event_id")),
        "booking_id":        _int(l.get("booking_id")),
        "created_at":        _ts(l.get("created_at")),
        "updated_at":        _ts(l.get("updated_at")),
        "deleted_at":        _ts(l.get("deleted_at")),
        "synced_at":         _now_iso(),
    }


# --- Supabase helpers ---

def supa_upsert(table: str, rows: list) -> None:
    """Upsert rows in chunks; ON CONFLICT updates all columns via merge-duplicates.

    If a column doesn't exist yet (Postgres error 42703), strips that column from
    all rows and retries once — allows forward-compatible deploys before a schema
    migration has been applied.
    """
    _UNKNOWN_COL_CODE = "42703"

    def _do_upsert(chunk: list) -> None:
        data = json.dumps(chunk).encode()
        req  = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/{table}",
            data=data,
            headers={
                "apikey":        SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "resolution=merge-duplicates",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if _UNKNOWN_COL_CODE in body:
                # Schema migration not yet applied — strip unknown columns and retry
                import json as _json
                err_obj = _json.loads(body) if body.startswith("{") else {}
                msg = err_obj.get("message", "")
                # Extract the offending column name from "column X does not exist"
                bad_col = None
                if "column" in msg and "does not exist" in msg:
                    parts = msg.split('"')
                    if len(parts) >= 2:
                        bad_col = parts[1]
                    else:
                        # fallback: strip known new columns
                        bad_col = "karaoke_amount"
                if bad_col:
                    clean = [{k: v for k, v in row.items() if k != bad_col}
                             for row in chunk]
                    _do_upsert(clean)
                    return
            raise

    for i in range(0, len(rows), UPSERT_CHUNK):
        _do_upsert(rows[i : i + UPSERT_CHUNK])


# --- Main sync logic ---

def run() -> str:
    token = get_ts_token()
    now   = _now_iso()

    # Bookings
    raw_bookings = fetch_all_pages(token, "bookings")
    bookings = [transform_booking(b) for b in raw_bookings]
    supa_upsert("ts_bookings", bookings)

    # Events — must come after bookings (FK constraint).
    # transform_event may fetch a BEO document per event; small delay prevents
    # hammering the Tripleseat portal on large syncs.
    raw_events = fetch_all_pages(token, "events")
    events = []
    for e in raw_events:
        events.append(transform_event(e))
        time.sleep(0.2)
    supa_upsert("ts_events", events)

    # Leads
    raw_leads = fetch_all_pages(token, "leads")
    leads = [transform_lead(l) for l in raw_leads]
    supa_upsert("ts_leads", leads)

    return (
        f"ok: upserted {len(bookings)} bookings, "
        f"{len(events)} events, {len(leads)} leads at {now}"
    )


# --- Vercel handler ---

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get("authorization", "")
        if CRON_SECRET and auth != f"Bearer {CRON_SECRET}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        try:
            report("sales", "started", current_task="Tripleseat fetch")
            result = run()
            report("sales", "finished", output=f"Tripleseat fetch — {result}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": result}).encode())
        except Exception as e:
            report("sales", "failed", message=f"Tripleseat fetch: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass
