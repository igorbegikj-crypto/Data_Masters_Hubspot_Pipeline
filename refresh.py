#!/usr/bin/env python3
"""
refresh.py — DataMasters Sales Pipeline Dashboard
Pulls all deals from HubSpot CRM, computes metrics, and injects
fresh data into SalesPipelineDashboard.html.

Run manually:   python refresh.py
Runs via cron:  every Saturday at 23:00 (see .github/workflows/refresh.yml)
"""

import os
import json
import time
import re
from datetime import datetime, timezone
from collections import defaultdict

import requests

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
HUBSPOT_TOKEN = os.environ["HUBSPOT_TOKEN"]   # set as GitHub Secret
HUBSPOT_URL   = "https://api.hubapi.com/crm/v3/objects/deals/search"
HTML_FILE     = "SalesPipelineDashboard.html"

PROPERTIES = [
    "dealname", "dealstage", "amount",
    "createdate", "closedate",
    "hs_is_closed_won", "hs_is_closed"
]

STAGE_MAP = {
    "584181495": {"label": "Introductory", "prob": 0.02},
    "584181496": {"label": "Prospect",     "prob": 0.10},
    "584181497": {"label": "Qualified",    "prob": 0.20},
    "584181690": {"label": "Opportunity",  "prob": 0.50},
    "584181691": {"label": "Closing",      "prob": 0.75},
    "584181693": {"label": "Won",          "prob": 1.00},
    "584181694": {"label": "Lost",         "prob": 0.00},
}
ACTIVE_STAGE_IDS = ["584181495","584181496","584181497","584181690","584181691"]
WON_STAGE_ID     = "584181693"
LOST_STAGE_ID    = "584181694"

MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

# ─────────────────────────────────────────────────────────────
# FETCH ALL DEALS (paginated)
# ─────────────────────────────────────────────────────────────
def fetch_all_deals():
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type":  "application/json"
    }
    deals   = []
    after   = None
    page    = 0

    while True:
        payload = {
            "limit":      100,
            "properties": PROPERTIES,
            "sorts":      [{"propertyName": "createdate", "direction": "DESCENDING"}]
        }
        if after:
            payload["after"] = after

        resp = requests.post(HUBSPOT_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

        for r in data.get("results", []):
            p = r.get("properties", {})
            deals.append({
                "dealname":  p.get("dealname", ""),
                "dealstage": p.get("dealstage", ""),
                "amount":    float(p.get("amount") or 0),
                "createdate": (p.get("createdate") or "")[:10],
                "closedate":  (p.get("closedate")  or "")[:10],
                "is_won":  p.get("hs_is_closed_won") == "true",
                "is_lost": p.get("hs_is_closed") == "true" and p.get("hs_is_closed_won") != "true",
            })

        paging = data.get("paging", {})
        after  = paging.get("next", {}).get("after")
        page  += 1
        print(f"  Page {page}: {len(data.get('results', []))} deals fetched (total so far: {len(deals)})")

        if not after:
            break

        time.sleep(0.1)   # stay under HubSpot rate limit

    print(f"✓ Total deals fetched: {len(deals)}")
    return deals

# ─────────────────────────────────────────────────────────────
# COMPUTE METRICS
# ─────────────────────────────────────────────────────────────
def date_filter(deals, start_date=None, use_create=False):
    """Filter deals by closedate (or createdate) >= start_date."""
    if not start_date:
        return deals
    out = []
    for d in deals:
        ref = d["createdate"] if use_create else (d["closedate"] or d["createdate"])
        if ref >= start_date:
            out.append(d)
    return out

def compute_kpis(deals):
    active = [d for d in deals if not d["is_won"] and not d["is_lost"]]
    won    = [d for d in deals if d["is_won"]]
    lost   = [d for d in deals if d["is_lost"]]

    won_rev  = sum(d["amount"] for d in won)
    lost_rev = sum(d["amount"] for d in lost)
    pipe_val = sum(d["amount"] for d in active)
    weighted = sum(d["amount"] * STAGE_MAP.get(d["dealstage"], {}).get("prob", 0) for d in active)
    closed   = len(won) + len(lost)

    return {
        "pipeVal":    round(pipe_val),
        "weighted":   round(weighted),
        "pipeCount":  len(active),
        "wonRev":     round(won_rev),
        "lostRev":    round(lost_rev),
        "wonCount":   len(won),
        "lostCount":  len(lost),
        "winRatio":   round(len(won) / closed * 100, 1) if closed else 0,
        "avgWon":     round(won_rev / len(won)) if won else 0,
        "totalClosed": closed,
    }

def stage_breakdown(deals):
    rows = []
    active = [d for d in deals if not d["is_won"] and not d["is_lost"]]
    for sid in ACTIVE_STAGE_IDS:
        sd  = [d for d in active if d["dealstage"] == sid]
        amt = sum(d["amount"] for d in sd)
        prob = STAGE_MAP[sid]["prob"]
        rows.append({
            "label":    STAGE_MAP[sid]["label"],
            "amount":   round(amt),
            "weighted": round(amt * prob),
            "count":    len(sd),
        })
    return rows

def monthly_won(deals, year):
    by_month = defaultdict(float)
    for d in deals:
        if d["is_won"] and d["closedate"] and d["closedate"].startswith(str(year)):
            mo = int(d["closedate"][5:7]) - 1
            by_month[mo] += d["amount"]
    return [round(by_month.get(i, 0)) for i in range(12)]

def yoy_kpis(deals):
    """Compute same-period YoY: Jan 1 → today for current and prior year."""
    now      = datetime.now()
    cur_year = now.year
    prev_year= cur_year - 1
    # Format: YYYY-MM-DD boundary — same month+day, different year
    ytd_end_cur  = now.strftime("%Y-%m-%d")
    ytd_end_prev = now.strftime(f"{prev_year}-%m-%d")
    ytd_start_cur  = f"{cur_year}-01-01"
    ytd_start_prev = f"{prev_year}-01-01"
    mtd_start_cur  = now.strftime(f"{cur_year}-%m-01")
    mtd_start_prev = now.strftime(f"{prev_year}-%m-01")

    def won_in_range(start, end):
        return sum(d["amount"] for d in deals
                   if d["is_won"] and d["closedate"] and start <= d["closedate"] <= end)

    ytd_cur  = round(won_in_range(ytd_start_cur,  ytd_end_cur))
    ytd_prev = round(won_in_range(ytd_start_prev, ytd_end_prev))
    mtd_cur  = round(won_in_range(mtd_start_cur,  ytd_end_cur))
    mtd_prev = round(won_in_range(mtd_start_prev, ytd_end_prev))

    def growth(cur, prev):
        if prev == 0: return "+∞" if cur > 0 else "0.0"
        return f"{(cur-prev)/prev*100:+.1f}"

    return {
        "ytd_cur": ytd_cur,   "ytd_prev": ytd_prev,
        "mtd_cur": mtd_cur,   "mtd_prev": mtd_prev,
        "ytd_growth": growth(ytd_cur, ytd_prev),
        "mtd_growth": growth(mtd_cur, mtd_prev),
        "period_label": now.strftime("Jan–%b %d"),
        "mtd_label": now.strftime("%b %d"),
        "cur_year": cur_year, "prev_year": prev_year,
    }

def activities(deals, year=2026):
    result = []
    for mo_idx, mo_name in enumerate(MONTHS[:3]):
        mo_str = f"{year}-{str(mo_idx+1).zfill(2)}"
        md = [d for d in deals if d["createdate"].startswith(mo_str)]
        result.append({
            "month": mo_name,
            "new":   len(md),
            "won":   len([d for d in md if d["is_won"]]),
            "lost":  len([d for d in md if d["is_lost"]]),
        })
    return result

def fmt_k(v):
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v//1_000}K"
    return f"${v}"

def fmt_pct(v):
    return f"{v:.1f}%"

# ─────────────────────────────────────────────────────────────
# BUILD JS DATA BLOCK
# ─────────────────────────────────────────────────────────────
def build_js_block(deals, refresh_ts, total_deals):
    now_year  = datetime.now().year
    prev_year = now_year - 1

    # Date boundaries
    ytd_start = f"{now_year}-01-01"
    mtd_start = datetime.now().strftime(f"{now_year}-%m-01")

    all_deals = deals
    ytd_deals = date_filter(deals, ytd_start)
    mtd_deals = date_filter(deals, mtd_start)

    all_kpis = compute_kpis(all_deals)
    ytd_kpis = compute_kpis(ytd_deals)
    mtd_kpis = compute_kpis(mtd_deals)

    all_stg = stage_breakdown(all_deals)
    ytd_stg = stage_breakdown(ytd_deals)
    mtd_stg = stage_breakdown(mtd_deals)

    mon_curr = monthly_won(deals, now_year)
    mon_prev = monthly_won(deals, prev_year)
    act      = activities(deals, now_year)
    yoy      = yoy_kpis(deals)

    def stg_js(rows):
        items = ",\n  ".join(
            f'{{ label:{json.dumps(r["label"])}, amount:{r["amount"]}, weighted:{r["weighted"]}, count:{r["count"]} }}'
            for r in rows
        )
        return f"[\n  {items}\n]"

    def kpi_js(k, stg):
        won_count  = k["wonCount"]
        lost_count = k["lostCount"]
        avg_str    = fmt_k(k["avgWon"]) if k["avgWon"] else "—"
        # YoY note for header
        return f"""{{
  pipeVal:     {k["pipeVal"]},
  pipeValFmt:  {json.dumps(fmt_k(k["pipeVal"]))},
  weighted:    {k["weighted"]},
  weightedFmt: {json.dumps(fmt_k(k["weighted"]))},
  pipeCount:   {k["pipeCount"]},
  wonRev:      {k["wonRev"]},
  wonRevFmt:   {json.dumps(fmt_k(k["wonRev"]))},
  lostRev:     {k["lostRev"]},
  lostRevFmt:  {json.dumps(fmt_k(k["lostRev"]))},
  wonCount:    {won_count},
  lostCount:   {lost_count},
  winRatio:    {json.dumps(fmt_pct(k["winRatio"]))},
  avgWon:      {json.dumps(avg_str)},
  totalClosed: {k["totalClosed"]}
}}"""

    return f"""// ── AUTO-GENERATED BY refresh.py — DO NOT EDIT MANUALLY ──
// Last refresh: {refresh_ts}
// Total deals:  {total_deals}

const REFRESH_TS    = {json.dumps(refresh_ts)};
const TOTAL_DEALS   = {total_deals};
const STAGE_COLORS  = ['#3b82f6','#6366f1','#8b5cf6','#06b6d4','#f59e0b'];
const PROBS         = [0.02, 0.10, 0.20, 0.50, 0.75];
const MONTHS        = {json.dumps(MONTHS)};

const ytdKpis = {kpi_js(ytd_kpis, ytd_stg)};
const mtdKpis = {kpi_js(mtd_kpis, mtd_stg)};
const allKpis = {kpi_js(all_kpis, all_stg)};

const ytdStg  = {stg_js(ytd_stg)};
const mtdStg  = {stg_js(mtd_stg)};
const allStg  = {stg_js(all_stg)};

const mon2026 = {json.dumps(mon_curr)};
const mon2025 = {json.dumps(mon_prev)};
const actData = {json.dumps(act)};
const yoyData = {json.dumps(yoy)};
"""

# ─────────────────────────────────────────────────────────────
# INJECT INTO HTML
# ─────────────────────────────────────────────────────────────
def inject_into_html(js_block, refresh_ts, total_deals):
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    # Replace the data block between markers
    pattern = r"(// ── AUTO-GENERATED BY refresh\.py.*?)(?=// ─────────────────────────────────────────────────────────\n// HELPERS)"
    replacement = js_block + "\n"

    new_html, count = re.subn(pattern, replacement, html, flags=re.DOTALL)
    if count == 0:
        print("⚠  Marker not found — inserting block before HELPERS comment")
        new_html = html.replace(
            "// ─────────────────────────────────────────────────────────\n// HELPERS",
            js_block + "\n// ─────────────────────────────────────────────────────────\n// HELPERS"
        )

    # Update header refresh timestamp and deal count
    new_html = re.sub(
        r'(<span id="refreshTime">)[^<]*(</span>)',
        rf'\g<1>{refresh_ts}\2',
        new_html
    )
    new_html = re.sub(
        r"(refreshed every Saturday · )\d+ live deals",
        rf"\g<1>{total_deals} live deals",
        new_html
    )
    # Also update footer deal count
    new_html = re.sub(
        r"(\d+) live deals",
        f"{total_deals} live deals",
        new_html
    )

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"✓ {HTML_FILE} updated with {total_deals} deals")

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("─" * 50)
    print("DataMasters Dashboard Refresh")
    print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("─" * 50)

    print("\n[1/3] Fetching deals from HubSpot...")
    deals = fetch_all_deals()

    refresh_ts  = datetime.now(timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    total_deals = len(deals)

    print("\n[2/3] Computing metrics...")
    js_block = build_js_block(deals, refresh_ts, total_deals)
    print("✓ Metrics computed")

    print("\n[3/3] Injecting into HTML...")
    inject_into_html(js_block, refresh_ts, total_deals)

    print("\n─" * 50)
    print(f"✓ Done! Dashboard refreshed with {total_deals} deals.")
    print("─" * 50)
