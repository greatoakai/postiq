#!/usr/bin/env python3
"""
poll_square.py — near-real-time payment posting (DEV / dev-test branch only).

Polls the Square Payments API for newly COMPLETED payments and posts each one to
TherapyAppointment using the existing bot_v2 logic — instead of the once-a-day
CSV batch. Intended to run on a schedule and post within the cadence window.

STATUS: v1, dev-test only. NOT wired into production. Before this can run / merge:
  1. SQUARE_ACCESS_TOKEN must be in .env. The daily-CSV flow doesn't use a Square
     API token, so one must be created in the Square Developer Dashboard
     (scope: PAYMENTS_READ). Until then this script exits cleanly.
  2. SQUARE_ACCESS_TOKEN needs PAYMENTS_READ + CUSTOMERS_READ. The payer name is
     the linked customer's given_name + family_name (validated 2026-06-15 against
     the 6/13 CSV — matches "Full Name", NOT the cardholder name, which can be a
     parent/payer). The posted amount is the BASE (total / 1.03), since the client
     pays a 3% card surcharge that is not a payment toward their therapy balance.
  3. Live test (--once --dry-run, then --once) once the token is in place.

Cadence (machine-local time, so CST/CDT DST is automatic):
  - Mon-Fri 08:00-18:30  -> act every run (effectively every 30 min)
  - otherwise / weekends -> act only hourly
The launchd job fires every 30 min; the script decides whether to act based on
elapsed time since its last action, so it's robust to scheduler jitter.

Idempotency: every posted Square payment id is recorded in the state file, so a
payment is never posted twice. Each poll also re-scans a small overlap window to
catch eventually-consistent stragglers (Square warns new payments can take a few
seconds to appear). The daily CSV batch stays as a reconciliation backstop.

Usage:
  python3 scripts/poll_square.py            # scheduled run (respects cadence)
  python3 scripts/poll_square.py --once     # ignore cadence; poll once now
  python3 scripts/poll_square.py --dry-run  # show what would post; post nothing
  python3 scripts/poll_square.py --since 2026-06-15T00:00:00Z   # override cursor
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reuse the production bot's login / navigation / posting logic verbatim.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright  # noqa: E402
import bot_v2 as bot  # noqa: E402

SQUARE_ACCESS_TOKEN = os.getenv("SQUARE_ACCESS_TOKEN", "")
SQUARE_API_BASE = os.getenv("SQUARE_API_BASE", "https://connect.squareup.com")
SQUARE_VERSION = "2025-01-23"  # Square-Version header

STATE_PATH = bot.DATA_DIR / "poll_state.json"
POLL_LOG = bot.LOG_DIR / f"poll_{datetime.now().strftime('%Y%m%d')}.log"

# Cadence config
BUSINESS_DAYS = {0, 1, 2, 3, 4}            # Mon-Fri
BUSINESS_START = (8, 0)                    # 08:00 local
BUSINESS_END = (18, 30)                    # 18:30 local
INTERVAL_BUSINESS_MIN = 30
INTERVAL_OFFHOURS_MIN = 60
CADENCE_SLACK_MIN = 5                      # tolerate a slightly-early scheduler fire
OVERLAP_MIN = 5                            # re-scan window for eventual consistency


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(POLL_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception as e:
            log(f"WARNING: could not read {STATE_PATH.name} ({e}); starting fresh")
    return {"last_polled_at": None, "last_action_at": None, "posted_payment_ids": []}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


LEDGER_DIR = bot.DATA_DIR / "poll_ledger"


def record_ledger(payment_id, name, date, amount, status, account=""):
    """Append/update a payment in the per-transaction-date ledger (idempotent by id).

    The ledger is the poller's record of what it did (or, in shadow mode, would
    do) each day — it's what the daily reconciliation diffs against the full CSV.
    `date` is MM/DD/YYYY (the Square transaction date). status is one of:
    OK / FAILED / ERROR / SKIPPED_NO_NAME / WOULD_POST (shadow).
    `account` is the Square customer reference_id == TA "Account Number"
    (C#########) used as the deterministic TA match key; "" if Square has none.
    """
    if not date:
        return
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    path = LEDGER_DIR / f"{date.replace('/', '')}.json"
    entries = []
    if path.exists():
        try:
            entries = json.loads(path.read_text())
        except Exception:
            entries = []
    entries = [e for e in entries if e.get("id") != payment_id]
    entries.append({"id": payment_id, "name": name, "date": date,
                    "amount": amount, "status": status, "account": account})
    path.write_text(json.dumps(entries, indent=2))


def in_business_hours(now):
    if now.weekday() not in BUSINESS_DAYS:
        return False
    start = now.replace(hour=BUSINESS_START[0], minute=BUSINESS_START[1], second=0, microsecond=0)
    end = now.replace(hour=BUSINESS_END[0], minute=BUSINESS_END[1], second=0, microsecond=0)
    return start <= now <= end


def cadence_should_act(state, now):
    """Return (should_act, interval_min) honoring 30-min business / 60-min off-hours."""
    interval = INTERVAL_BUSINESS_MIN if in_business_hours(now) else INTERVAL_OFFHOURS_MIN
    last = state.get("last_action_at")
    if not last:
        return True, interval
    try:
        elapsed_min = (now - datetime.fromisoformat(last)).total_seconds() / 60
    except Exception:
        return True, interval
    return elapsed_min >= (interval - CADENCE_SLACK_MIN), interval


def square_get(path, params=None):
    url = f"{SQUARE_API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {SQUARE_ACCESS_TOKEN}",
        "Square-Version": SQUARE_VERSION,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_completed_payments(since_iso):
    """Fetch COMPLETED payments created since `since_iso` (RFC3339 UTC). Paginates."""
    out, cursor = [], None
    while True:
        params = {"begin_time": since_iso, "sort_order": "ASC", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = square_get("/v2/payments", params)
        out.extend(p for p in data.get("payments", []) if p.get("status") == "COMPLETED")
        cursor = data.get("cursor")
        if not cursor:
            return out


_customer_cache = {}


def resolve_customer(customer_id):
    """Resolve a Square customer_id to {'name', 'account'} (cached).

    name    : given_name + family_name — the client, matching the daily CSV's
              "Full Name" (NOT the cardholder, who can be a parent/payer).
    account : the customer's reference_id, which equals TA's "Account Number"
              (C#########). This is the deterministic TA match key — it sidesteps
              every name-matching failure (nicknames, misspellings, maiden names,
              Jr/multi-word surnames). "" when Square has no reference_id set.
    """
    if not customer_id:
        return {"name": "", "account": ""}
    if customer_id in _customer_cache:
        return _customer_cache[customer_id]
    info = {"name": "", "account": ""}
    try:
        c = square_get(f"/v2/customers/{customer_id}").get("customer", {})
        info["name"] = f"{(c.get('given_name') or '').strip()} {(c.get('family_name') or '').strip()}".strip()
        info["account"] = (c.get("reference_id") or "").strip()
    except Exception as e:
        log(f"  WARNING: could not resolve customer {customer_id}: {e}")
    _customer_cache[customer_id] = info
    return info


def extract_payment_fields(p):
    """Map a Square payment -> (name, date, amount, account).

    name    : the linked customer's given_name + family_name (the client), which
              matches the daily CSV exporter's "Full Name" — NOT the cardholder
              name (validated 2026-06-15 against the 6/13 CSV).
    amount  : the BASE applied to the client's balance = total / 1.03 (the client
              pays a 3% card surcharge on top; that fee is not a payment toward
              their therapy balance — matches the CSV "Base Amount" column).
    date    : the payment's local (Central) transaction date, MM/DD/YYYY.
    account : the customer's reference_id == TA "Account Number" (C#########), the
              deterministic match key. "" when Square has no reference_id set.
    """
    total_cents = (p.get("amount_money") or {}).get("amount", 0)
    amount = f"{round(total_cents / 100 / 1.03, 2):.2f}"
    created = p.get("created_at", "")
    try:
        date = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone().strftime("%m/%d/%Y")
    except Exception:
        date = ""
    cust = resolve_customer(p.get("customer_id", ""))
    return cust["name"], date, amount, cust["account"]


def post_new_payments(to_post, posted_ids):
    """Post each new payment via bot_v2 in a single browser session. Returns results."""
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=bot.HEADLESS)
        page = browser.new_page()
        page.set_default_timeout(bot.ACTION_TIMEOUT)
        try:
            bot.login(page)
            for item in to_post:
                try:
                    success, method, error, *_ = bot.post_payment(
                        page, item["name"], item["date"], item["amount"],
                        account=item.get("account"))
                    if success:
                        posted_ids.add(item["id"])
                        results.append({**item, "status": "OK", "method": method})
                        log(f"  POSTED {item['name']} ${item['amount']} ({method})")
                    else:
                        results.append({**item, "status": method or "FAILED", "error": error})
                        log(f"  NOT POSTED {item['name']}: {error}")
                        bot.recover_to_dashboard(page)
                except Exception as e:
                    results.append({**item, "status": "ERROR", "error": str(e)})
                    log(f"  ERROR {item['name']}: {e}")
                    try:
                        bot.recover_to_dashboard(page)
                    except Exception:
                        pass
        finally:
            browser.close()
    return results


def main():
    ap = argparse.ArgumentParser(description="Near-real-time Square -> TA poster (dev-test).")
    ap.add_argument("--once", action="store_true", help="Ignore cadence; poll once now.")
    ap.add_argument("--dry-run", action="store_true", help="Show what would post; post nothing, persist nothing.")
    ap.add_argument("--shadow", action="store_true",
                    help="Observe-only: record would-post to the ledger for reconciliation, but post nothing.")
    ap.add_argument("--since", default=None, help="Override cursor (RFC3339 UTC).")
    args = ap.parse_args()

    now = datetime.now()
    state = load_state()

    # Cadence gate (skipped by --once)
    if not args.once:
        should_act, interval = cadence_should_act(state, now)
        if not should_act:
            log(f"Cadence not elapsed (interval={interval}m) — skipping this fire.")
            return

    if not SQUARE_ACCESS_TOKEN:
        log("SQUARE_ACCESS_TOKEN not set in .env — cannot poll. (See module header.)")
        sys.exit(1)

    # Cursor: where to start fetching from
    since = args.since or state.get("last_polled_at")
    if not since:
        start_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    poll_started = datetime.now(timezone.utc)

    log(f"Polling Square for COMPLETED payments since {since} ...")
    try:
        payments = fetch_completed_payments(since)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300] if hasattr(e, "read") else ""
        log(f"ERROR: Square API {e.code} {e.reason} — {body}")
        sys.exit(1)
    except Exception as e:
        log(f"ERROR: Square request failed — {e}")
        sys.exit(1)

    posted_ids = set(state.get("posted_payment_ids", []))
    new = [p for p in payments if p.get("id") not in posted_ids]
    log(f"Square returned {len(payments)} completed payment(s); {len(new)} new.")

    # Advance cursor with a small overlap so eventually-consistent payments aren't
    # missed next time (dedup via posted_ids handles the re-scan).
    next_cursor = (poll_started - timedelta(minutes=OVERLAP_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not new:
        state.update(last_polled_at=next_cursor, last_action_at=now.isoformat())
        save_state(state)
        log("Nothing new to post.")
        return

    # Build post list; flag any payment whose name can't be resolved (don't post blanks)
    to_post, unresolved = [], []
    for p in new:
        name, date, amount, account = extract_payment_fields(p)
        rec = {"id": p["id"], "name": name, "date": date, "amount": amount, "account": account}
        (to_post if name else unresolved).append(rec)
    # --- DRY RUN: preview only, no side effects ---
    if args.dry_run:
        log("DRY RUN — would post:")
        for item in to_post:
            log(f"  {item['name']} — ${item['amount']} on {item['date']} "
                f"[acct {item['account'] or 'NONE'}] (sq:{item['id']})")
        for u in unresolved:
            log(f"  SKIP (no name) ${u['amount']} on {u['date']} (sq:{u['id']})")
        return

    # --- SHADOW: record would-post to the ledger for reconciliation; post nothing ---
    if args.shadow:
        log("SHADOW — recording would-post to ledger (no posting):")
        for item in to_post:
            record_ledger(item["id"], item["name"], item["date"], item["amount"],
                          "WOULD_POST", item["account"])
            log(f"  WOULD POST {item['name']} ${item['amount']} on {item['date']} "
                f"[acct {item['account'] or 'NONE'}]")
        for u in unresolved:
            record_ledger(u["id"], u["name"], u["date"], u["amount"],
                          "SKIPPED_NO_NAME", u["account"])
            log(f"  SKIP (no name) ${u['amount']} on {u['date']} (sq:{u['id']})")
        state.update(last_polled_at=next_cursor, last_action_at=now.isoformat())
        save_state(state)
        log(f"Shadow done: {len(to_post)} would-post, {len(unresolved)} skipped (no name).")
        return

    # --- LIVE: post for real, record outcomes to the ledger ---
    for u in unresolved:
        record_ledger(u["id"], u["name"], u["date"], u["amount"],
                      "SKIPPED_NO_NAME", u["account"])
        log(f"  SKIP (no name resolved) ${u['amount']} on {u['date']} (sq:{u['id']}) — needs manual posting")
    results = post_new_payments(to_post, posted_ids)
    for r in results:
        record_ledger(r["id"], r["name"], r["date"], r["amount"],
                      r["status"], r.get("account", ""))
    state.update(
        posted_payment_ids=sorted(posted_ids),
        last_polled_at=next_cursor,
        last_action_at=now.isoformat(),
    )
    save_state(state)
    ok = sum(1 for r in results if r["status"] == "OK")
    log(f"Done: {ok}/{len(results)} posted, {len(unresolved)} skipped (no name).")


if __name__ == "__main__":
    main()
