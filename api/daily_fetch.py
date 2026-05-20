import json
import time
import urllib.request
import urllib.error
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler
import os

# --- Config from environment variables ---
GOTAB_AUTH_URL    = "https://gotab.io/api/oauth/token"
GOTAB_GRAPH_URL   = "https://gotab.io/api/graph"
LOCATION_ID       = os.environ.get("GOTAB_LOCATION_ID", "112479")
API_ACCESS_ID     = os.environ["GOTAB_API_ACCESS_ID"]
API_ACCESS_SECRET = os.environ["GOTAB_API_ACCESS_SECRET"]
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_SERVICE_KEY"]
CRON_SECRET       = os.environ.get("CRON_SECRET", "")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

REFUND_TYPES = {"REFUND", "OPEN_REFUND", "ORDER_RULE_REFUND"}

LEDGER_QUERY = """
query LedgerEntries($locationId: BigInt!, $fiscalDay: Date!, $offset: Int!) {
  ledgerEntriesList(
    filter: {
      tabLocationId: { equalTo: $locationId }
      fiscalDay: { equalTo: $fiscalDay }
    }
    first: 500
    offset: $offset
    orderBy: TRANSACTION_NAME_ASC
  ) {
    transactionName
    adjustmentType
    amount
    quantity
    accountingStream { reportingGroup }
    product { name category { name } }
    zone { name }
  }
}
"""


# --- GoTab helpers ---

def get_token():
    data = json.dumps({
        "api_access_id": API_ACCESS_ID,
        "api_access_secret": API_ACCESS_SECRET,
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(
        GOTAB_AUTH_URL, data=data,
        headers={"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["token"]


def gql(token, query, variables={}):
    data = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        GOTAB_GRAPH_URL, data=data,
        headers={
            "User-Agent": UA, "Content-Type": "application/json",
            "Accept": "application/json", "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_ledger_entries(token, fiscal_day):
    entries = []
    offset = 0
    while True:
        time.sleep(0.4)
        result = gql(token, LEDGER_QUERY, {
            "locationId": LOCATION_ID,
            "fiscalDay": fiscal_day,
            "offset": offset,
        })
        if result.get("errors"):
            raise RuntimeError(f"GraphQL errors: {result['errors']}")
        batch = result.get("data", {}).get("ledgerEntriesList") or []
        entries.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
    return entries


def aggregate_entries(entries):
    groups = {}
    for e in entries:
        txn_name = (e.get("transactionName") or "").strip()
        if not txn_name:
            continue

        stream = e.get("accountingStream") or {}
        if stream.get("reportingGroup") != "NET_SALES":
            continue

        product_obj = e.get("product") or {}
        category_obj = product_obj.get("category") or {}
        category = (category_obj.get("name") or "").strip()
        zone_obj = e.get("zone") or {}
        zone = (zone_obj.get("name") or "").strip() or None

        key = (txn_name, zone, category)
        if key not in groups:
            groups[key] = {
                "product": txn_name, "category": category, "zone": zone,
                "gross_qty": 0.0, "gross_sales": 0.0,
                "refund_qty": 0.0, "refund_amount": 0.0,
                "comp_qty": 0.0, "comp_amount": 0.0,
                "void_qty": 0.0, "void_amount": 0.0,
            }

        g = groups[key]
        amount = (e.get("amount") or 0) / 100.0
        qty = float(e.get("quantity") or 0)
        adj = e.get("adjustmentType")

        if adj is None:
            g["gross_qty"] += qty
            g["gross_sales"] += amount
        elif adj == "VOID":
            g["void_qty"] += abs(qty)
            g["void_amount"] += amount
        elif adj == "COMP":
            g["comp_qty"] += abs(qty)
            g["comp_amount"] += amount
        elif adj in REFUND_TYPES:
            g["refund_qty"] += abs(qty)
            g["refund_amount"] += amount

    rows = []
    for g in groups.values():
        net_sales = round(g["gross_sales"] + g["refund_amount"] + g["comp_amount"] + g["void_amount"], 2)
        net_qty = round(g["gross_qty"] - g["refund_qty"] - g["comp_qty"] - g["void_qty"], 4)
        rows.append({
            "product":       g["product"],
            "category":      g["category"],
            "zone":          g["zone"],
            "gross_qty":     round(g["gross_qty"], 4),
            "net_qty":       net_qty,
            "gross_sales":   round(g["gross_sales"], 2),
            "net_sales":     net_sales,
            "refund_qty":    g["refund_qty"] or None,
            "refund_amount": round(g["refund_amount"], 2) if g["refund_amount"] else None,
            "comp_qty":      g["comp_qty"] or None,
            "comp_amount":   round(g["comp_amount"], 2) if g["comp_amount"] else None,
            "void_qty":      g["void_qty"] or None,
            "void_amount":   round(g["void_amount"], 2) if g["void_amount"] else None,
        })
    return rows


# --- Supabase helpers ---

def supa_get(path):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1{path}",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def supa_post(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1{path}", data=data,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# --- Main logic ---

def run(target_date: str) -> str:
    # Skip if already loaded
    existing = supa_get(f"/report_dates?report_date=eq.{target_date}&select=id")
    if existing:
        return f"skipped: {target_date} already in database"

    token = get_token()
    entries = fetch_ledger_entries(token, target_date)
    if not entries:
        return f"no data: no ledger entries found for {target_date}"

    rows = aggregate_entries(entries)
    rd = supa_post("/report_dates", {"report_date": target_date, "filename": f"api:{target_date}"})
    report_date_id = rd[0]["id"]

    payload = []
    for r in rows:
        net = r.get("net_sales")
        payload.append({
            "report_date_id": report_date_id,
            "report_date":    target_date,
            "category":       r.get("category") or "Beverage",
            "product":        r["product"],
            "zone":           r.get("zone"),
            "gross_qty":      r.get("gross_qty"),   "net_qty":      r.get("net_qty"),
            "gross_sales":    r.get("gross_sales"), "net_sales":    net,
            "refund_qty":     r.get("refund_qty"),  "refund_amount": r.get("refund_amount"),
            "comp_qty":       r.get("comp_qty"),    "comp_amount":  r.get("comp_amount"),
            "void_qty":       r.get("void_qty"),    "void_amount":  r.get("void_amount"),
            "is_discount":    "Discount" in r["product"] or (net is not None and net < 0),
        })

    supa_post("/sales", payload)
    net_total = sum(r["net_sales"] or 0 for r in rows if r.get("net_sales") is not None)
    return f"ok: loaded {len(rows)} rows for {target_date} (net ${net_total:,.2f})"


# --- Vercel handler ---

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Verify cron secret to block unauthorized calls
        auth = self.headers.get("authorization", "")
        if CRON_SECRET and auth != f"Bearer {CRON_SECRET}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        target_date = (date.today() - timedelta(days=1)).isoformat()
        try:
            result = run(target_date)
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
