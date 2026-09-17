#!/usr/bin/env python3
"""
poll_square.py — near-real-time payment posting. THIS RUNS IN PRODUCTION.

Polls the Square Payments API for newly COMPLETED payments and posts each one to
TherapyAppointment using the existing bot_v2 logic — instead of the once-a-day
CSV batch. Intended to run on a schedule and post within the cadence window.

STATUS: LIVE. Wired into production as the launchd job `com.greatoak.postiq-poll-live`
on Minute 0 and Minute 30 — it posts real client payments to TherapyAppointment every
half hour. Verified 2026-08-28 from logs/poll_live_stdout.log: successful Square polls
at 09:00 and 09:30.

This header read "DEV / dev-test branch only. NOT wired into production" until
2026-08-28, while the job had been posting payments on the half hour. The three
preconditions below were all met and the status line was never updated — so the file
described itself as inert while it was the money path. Anyone reading it to decide
whether a change here was safe would have concluded it was.

It also runs from the `feat/refid-account-match` branch, not main. That is simply the
state of the working tree launchd executes, not a release process — so a `git checkout`
in this directory changes what posts payments.

Preconditions below are all SATISFIED; kept for the requirements they record.
  1. SQUARE_ACCESS_TOKEN is in .env and authenticating. The daily-CSV flow doesn't use a Square
     API token, so one must be created in the Square Developer Dashboard
     (scope: PAYMENTS_READ). Until then this script exits cleanly.
  2. SQUARE_ACCESS_TOKEN needs PAYMENTS_READ + CUSTOMERS_READ (plus CUSTOMERS_WRITE
     for --backfill-missing). The payer name is the linked customer's given_name +
     family_name (validated 2026-06-15 against the 6/13 CSV — matches "Full Name",
     NOT the cardholder name, which can be a parent/payer). The posted amount is the
     BASE (total / 1.03), since the client pays a 3% card surcharge that is not a
     payment toward their therapy balance.
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
  python3 scripts/poll_square.py --backfill-missing --dry-run   # find missing Account #s (no writes)
  python3 scripts/poll_square.py --backfill-missing             # write TA Account #s back to Square
"""

import argparse
import fcntl
import json
import os
import re
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

# Re-attempting payments that failed for reasons which prove nothing was submitted.
# The cursor only looks back OVERLAP_MIN, so without this a payment that failed to a
# passing TA glitch waits for the morning report and a person (Rebecca Alvarado,
# 2026-09-15: TA served a blank page for ~5 minutes, and the payment sat unposted
# for 17 hours). The window deliberately expires long before the 08:00 report goes
# out, so the bot never races staff who are working that list by hand.
RETRY_WINDOW_MIN = 180                     # only failures this recent
RETRY_MAX_ATTEMPTS = 2                     # per payment, then leave it for a person
RETRY_MAX_PER_RUN = 5                      # never let a backlog stall a run
CLEARED_PATH = bot.DATA_DIR / "manual_cleared.json"
LOCK_PATH = bot.DATA_DIR / "poll_square.lock"

# An ALLOWLIST, not a denylist. "status == FAILED" is not proof that nothing was
# submitted: V1 calls submit_payment without V2's AT_PAYMENT_FORM wrapper, so a
# raise after TA had already saved comes back as a plain FAILED carrying no
# may-have-posted marker — and re-posting that is a second charge to a client.
# Only reasons that cannot have reached TA's payment form belong here. The
# app-not-rendering error qualifies because it is raised solely by
# _ensure_sidebar, reachable only from the two sidebar clicks, both of which run
# before any form is filled.
RETRYABLE_REASONS = (bot.APP_NOT_RENDERING_REASON,)

# Reasons chain as "V2: ...; V2-retry: ...; V1: ...". EVERY leg has to be
# allowlisted, never just one: TA's blank shell can fail both V2 legs and then
# recover, letting V1 run the whole way, click Save, and have TA save it — and
# post_payment_v1 does not wrap submit_payment in AT_PAYMENT_FORM the way V2
# does, so that comes back as a plain FAILED still carrying the V2 legs' text.
_REASON_LEG_RX = re.compile(r";\s*(?=V2-retry:|V2:|V1:)")

# reconcile emails the outstanding list at this hour; once a failure has appeared
# on it, staff may be posting it by hand, so the bot must stop touching it.
REPORT_HOUR = 8


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


def record_ledger(payment_id, name, date, amount, status, account="", reason="",
                  method="", note=""):
    """Append/update a payment in the per-transaction-date ledger (idempotent by id).

    The ledger is the poller's record of what it did (or, in shadow mode, would
    do) each day — it's what the daily reconciliation diffs against the full CSV.
    `date` is MM/DD/YYYY (the Square transaction date). status is one of:
    OK / FAILED / FLAGGED / ERROR / SKIPPED_NO_NAME / WOULD_POST (shadow).
    `account` is the Square customer reference_id == TA "Account Number"
    (C#########) used as the deterministic TA match key; "" if Square has none.
    `reason` is the failure text for non-OK outcomes — reconcile.py turns it into
    the plain-language "why it didn't post" staff see in the morning report.
    `method` is how it posted (V2, V2-balance, V1, ...) and `note` is bot_v2's
    per-payment note; together they let the report say a payment went to the
    client's open balance rather than to a date of service, and flag the ones
    that landed as an unapplied credit.
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
    prior = next((e for e in entries
                  if isinstance(e, dict) and e.get("id") == payment_id), {})
    entries = [e for e in entries
               if not (isinstance(e, dict) and e.get("id") == payment_id)]
    row = {"id": payment_id, "name": name, "date": date,
           "amount": amount, "status": status, "account": account,
           "reason": reason, "method": method, "note": note}
    if status != "OK":
        # When it failed, and how many attempts have been spent on it. Rows written
        # before this existed have neither, so they are never auto-retried. The
        # count is incremented when an attempt is *queued* (mark_retry_attempt), not
        # here: a run that dies mid-attempt must still have spent one.
        # The ORIGINAL failure time, kept across re-attempts. Restamping it would
        # walk the row forward past last_report_at() and let the bot start
        # retrying something staff have already seen on the morning list.
        row["failed_at"] = prior.get("failed_at") or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        try:
            row["retries"] = int(prior.get("retries") or 0)
        except (TypeError, ValueError):
            row["retries"] = 0
    entries.append(row)
    path.write_text(json.dumps(entries, indent=2))


def acquire_run_lock():
    """Take the poller's run lock. Returns (handle, may_run).

    Two pollers at once post the same payments twice: both read the same cursor
    and the same posted_payment_ids, and the later save_state() drops the other's
    ids. Retries widen that window, since a candidate stays eligible for hours.
    A lock we cannot even open is not worth blocking money on, so that case runs.
    """
    try:
        handle = open(LOCK_PATH, "w")
    except Exception as e:
        log(f"WARNING: could not open the run lock ({e}) — continuing without it.")
        return None, True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        return None, False
    return handle, True


def _cleared_keys():
    """Payments staff have confirmed they handled by hand (reconcile --clear)."""
    try:
        return set(json.loads(CLEARED_PATH.read_text()).get("keys", []))
    except Exception:
        return set()


def last_report_at():
    """When reconcile last emailed the outstanding list, as UTC.

    A failure older than this has been in front of staff, who may be posting it by
    hand right now — so the bot must leave it alone however recent it looks.
    """
    local_now = datetime.now().astimezone()
    boundary = local_now.replace(hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
    if boundary > local_now:
        boundary -= timedelta(days=1)
    return boundary.astimezone(timezone.utc)


def _retryable_row(e, posted_ids, cleared, cutoff):
    """Is this ledger row one the bot may re-attempt on its own?

    Defensive throughout: a single malformed row must never abort a run and leave
    that run's genuinely new payments unposted.
    """
    try:
        if not isinstance(e, dict) or e.get("status") != "FAILED":
            return None
        pid = e.get("id")
        if not pid or pid in posted_ids or f"sq:{pid}" in cleared:
            return None
        reason = e.get("reason") or ""
        if bot.MAY_HAVE_POSTED_MARKER in reason:
            return None
        legs = [leg.strip() for leg in _REASON_LEG_RX.split(reason) if leg.strip()]
        if not legs or not all(
                any(allowed in leg for allowed in RETRYABLE_REASONS) for leg in legs):
            return None
        if int(e.get("retries") or 0) >= RETRY_MAX_ATTEMPTS:
            return None
        stamp = e.get("failed_at")
        if not stamp:
            return None
        failed_at = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, AttributeError):
        return None
    return failed_at if failed_at >= cutoff else None


def retry_candidates(posted_ids):
    """Recent failures that are safe to re-attempt, oldest first.

    Safe means the recorded reason is one that cannot have reached TA's payment
    form (RETRYABLE_REASONS). Everything else is left for a person:

      not an allowlisted reason  it might have submitted — never assume otherwise
      FLAGGED                    a human judgement call (multiple appointments, ...)
      may-have-posted            the money may already be in TA — never retried
      SKIPPED_NO_NAME            no client to post to
      cleared                    staff already handled it by hand
      before the last report     staff have seen it and may be posting it now
      older than the window      stale; the report carries it
      out of attempts            stop trying and let the report carry it
    """
    cutoff = max(datetime.now(timezone.utc) - timedelta(minutes=RETRY_WINDOW_MIN),
                 last_report_at())
    cleared, out = _cleared_keys(), []
    for path in sorted(LEDGER_DIR.glob("*.json")):
        try:
            entries = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(entries, list):
            continue
        for e in entries:
            failed_at = _retryable_row(e, posted_ids, cleared, cutoff)
            if failed_at:
                out.append((failed_at, e))
    out.sort(key=lambda pair: pair[0])
    return [e for _stamp, e in out[:RETRY_MAX_PER_RUN]]


def mark_retry_attempt(row):
    """Spend an attempt before making it. True only if that was actually recorded.

    A crash mid-attempt must still cost one, and an attempt that could not be
    written down must not be made at all — otherwise the count never advances and
    the same payment is re-queued every run for the whole window.
    """
    date = row.get("date") or ""
    path = LEDGER_DIR / f"{date.replace('/', '')}.json"
    try:
        entries = json.loads(path.read_text())
        if not isinstance(entries, list):
            return False
        hit = False
        for e in entries:
            if isinstance(e, dict) and e.get("id") == row.get("id"):
                try:
                    e["retries"] = int(e.get("retries") or 0) + 1
                except (TypeError, ValueError):
                    e["retries"] = 1
                hit = True
        if not hit:
            return False
        path.write_text(json.dumps(entries, indent=2))
        return True
    except Exception as err:
        log(f"  WARNING: could not record the retry attempt for {row.get('id')}: {err}")
        return False


def payment_still_completed(payment_id):
    """Re-read a payment from Square so a retry uses authoritative figures."""
    try:
        p = square_get(f"/v2/payments/{payment_id}").get("payment", {})
    except Exception as e:
        log(f"  (skipping retry of {payment_id}: could not re-read it from Square — {e})")
        return None
    if p.get("status") != "COMPLETED":
        return None
    # A refunded payment stays COMPLETED — the refund is a separate object — so
    # posting it again would put money into TA that the client no longer owes.
    refunded = (p.get("refunded_money") or {}).get("amount") or 0
    if refunded or p.get("refund_ids"):
        log(f"  (skipping retry of {payment_id}: it has been refunded in Square)")
        return None
    return p


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


def _square_request(method, path, params=None, body=None):
    """Issue an authenticated Square API request and return the parsed JSON.

    Shared by square_get/square_put so the auth + Square-Version headers live in
    one place. `body` (a dict) is JSON-encoded for write methods.
    """
    url = f"{SQUARE_API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Authorization": f"Bearer {SQUARE_ACCESS_TOKEN}",
        "Square-Version": SQUARE_VERSION,
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def square_get(path, params=None):
    return _square_request("GET", path, params=params)


def square_put(path, body):
    """PUT JSON to Square. Requires the relevant write scope on SQUARE_ACCESS_TOKEN
    (CUSTOMERS_WRITE for customers)."""
    return _square_request("PUT", path, body=body)


def update_customer_reference(customer_id, account):
    """Set a Square customer's reference_id (== TA Account #) and return the value
    Square echoes back (read-after-write confirmation). Raises on API error.

    UpdateCustomer returns the full updated customer, so the response itself is
    the confirmation — no separate GET needed. Also refresh the local cache so a
    subsequent resolve_customer() reflects the new account.
    """
    resp = square_put(f"/v2/customers/{customer_id}", {"reference_id": account})
    confirmed = ((resp.get("customer") or {}).get("reference_id") or "").strip()
    if customer_id in _customer_cache:
        _customer_cache[customer_id]["account"] = confirmed
    return confirmed


HEAL_LOG = bot.DATA_DIR / "poll_heals.json"
_healed = set()  # customer_ids self-healed this run (avoid duplicate writes)


def record_heal(name, before, after, customer_id):
    """Append a self-heal to the rolling log so the daily reconcile can surface it
    for staff to confirm. Exactly-once reporting via a per-entry 'reported' flag."""
    entries = []
    if HEAL_LOG.exists():
        try:
            entries = json.loads(HEAL_LOG.read_text())
        except Exception:
            entries = []
    entries.append({"name": name, "before": before, "after": after,
                    "customer_id": customer_id, "reported": False,
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M")})
    HEAL_LOG.write_text(json.dumps(entries, indent=2))


def _self_heal_account(customer_id, raw, name):
    """Autonomous self-heal (LIVE only): after a payment posts via a stripped/
    malformed account #, write the canonical C######### back to Square so future
    payments match, and record it for staff confirmation. One write per customer."""
    canon = bot.normalize_account(raw)
    if not customer_id or not canon or canon == (raw or "").strip():
        return  # no id, empty, already canonical, or unreconstructable -> nothing to do
    if customer_id in _healed:
        return
    _healed.add(customer_id)
    try:
        confirmed = update_customer_reference(customer_id, canon)
        log(f"  [heal] account '{raw}' -> '{confirmed}' for {name} — corrected in Square")
        record_heal(name, raw, confirmed, customer_id)
    except Exception as e:
        log(f"  [heal] WARNING: could not correct '{raw}' for {name} in Square: {e}")


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
        return dict(_customer_cache[customer_id])
    info = {"name": "", "account": ""}
    try:
        c = square_get(f"/v2/customers/{customer_id}").get("customer", {})
        info["name"] = f"{(c.get('given_name') or '').strip()} {(c.get('family_name') or '').strip()}".strip()
        info["account"] = (c.get("reference_id") or "").strip()
    except Exception as e:
        log(f"  WARNING: could not resolve customer {customer_id}: {e}")
    _customer_cache[customer_id] = info
    return dict(info)


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


def _flush(on_result, result):
    """Persist one outcome now. Never let bookkeeping abort a run mid-batch."""
    if not on_result:
        return
    try:
        on_result(result)
    except Exception as e:
        log(f"  WARNING: could not persist the outcome for {result.get('id')}: {e}")


def post_new_payments(to_post, posted_ids, on_result=None):
    """Post each new payment via bot_v2 in a single browser session. Returns results.

    `on_result(result)` runs after every payment, before the next one starts. The
    caller uses it to write the ledger row and save posted_payment_ids
    immediately: a crash mid-batch must not leave a payment that TA already saved
    still recorded as FAILED, because the retry path would then post it again.
    """
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=bot.HEADLESS)
        page = browser.new_page()
        bot.block_beacon(page)
        page.set_default_timeout(bot.ACTION_TIMEOUT)
        try:
            bot.login(page)
            for item in to_post:
                try:
                    success, method, error, note, *_ = bot.post_payment(
                        page, item["name"], item["date"], item["amount"],
                        account=item.get("account"))
                    if success:
                        posted_ids.add(item["id"])
                        results.append({**item, "status": "OK", "method": method,
                                        "note": note or ""})
                        _flush(on_result, results[-1])
                        log(f"  POSTED {item['name']} ${item['amount']} ({method})")
                        _self_heal_account(item.get("customer_id"), item.get("account"), item["name"])
                    else:
                        results.append({**item, "status": method or "FAILED", "error": error})
                        _flush(on_result, results[-1])
                        log(f"  NOT POSTED {item['name']}: {error}")
                        # A payment that reached TA's payment form before failing
                        # may already be in TA. The poll cursor keeps a 5-minute
                        # overlap and re-feeds anything not in posted_ids, so
                        # leaving it out would have the next cycle post it again.
                        # Retire the id; the ledger still carries the flag, so it
                        # shows up on the morning report for a person to settle.
                        if bot.MAY_HAVE_POSTED_MARKER in (error or ""):
                            posted_ids.add(item["id"])
                            log(f"  (not retrying {item['name']} — it may already be in TA)")
                        bot.recover_to_dashboard(page)
                except Exception as e:
                    results.append({**item, "status": "ERROR", "error": str(e)})
                    _flush(on_result, results[-1])
                    log(f"  ERROR {item['name']}: {e}")
                    try:
                        bot.recover_to_dashboard(page)
                    except Exception:
                        pass
        finally:
            browser.close()
    return results


def backfill_missing(since_iso, dry_run=False, limit=None):
    """Self-heal missing Square Account #s. Posts NOTHING.

    For each recent COMPLETED payment whose Square customer has no reference_id,
    look the client up in TA by name, read their Account # off the results row,
    and write it back to Square (customer.reference_id). After this, that client
    matches deterministically by Account # forever.

    dry_run: do the TA lookup and report the Account # we WOULD set, but write
    nothing to Square. Returns a list of per-customer result dicts.
    """
    log(f"BACKFILL: scanning COMPLETED payments since {since_iso} for missing Account #s ...")
    try:
        payments = fetch_completed_payments(since_iso)
    except Exception as e:
        log(f"ERROR: Square request failed — {e}")
        return []

    # Unique customers missing a reference_id, skipping ones we can't name-match.
    seen, targets = set(), []
    for p in payments:
        cid = p.get("customer_id", "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        cust = resolve_customer(cid)
        if cust["account"]:
            continue  # already has an Account #
        if not cust["name"]:
            log(f"  SKIP {cid}: Square customer has no name — can't look up in TA")
            continue
        targets.append({"customer_id": cid, "name": cust["name"]})

    if limit:
        targets = targets[:limit]
    log(f"BACKFILL: {len(targets)} customer(s) missing an Account #"
        + (" — DRY RUN, nothing will be written" if dry_run
           else " — LIVE: will WRITE reference_id to PRODUCTION Square"))
    if not targets:
        return []

    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=bot.HEADLESS)
        page = browser.new_page()
        bot.block_beacon(page)
        page.set_default_timeout(bot.ACTION_TIMEOUT)
        try:
            bot.login(page)
            # Warm up the Clients page once so the FIRST client's search inputs are
            # hydrated — a cold first-search-after-login can otherwise come back with
            # empty fields and zero results (observed for the first target).
            try:
                bot.navigate_to_clients(page)
                page.wait_for_timeout(1500)
            except Exception:
                pass
            for t in targets:
                rec = {**t, "found": "", "status": ""}
                try:
                    # TA's Clients search is flaky (a search can intermittently
                    # return zero rows). An empty result only ever causes a miss,
                    # never a wrong write, so retry once before giving up.
                    acct = ""
                    for scrape_attempt in range(2):
                        acct = bot.scrape_account_for_name(page, t["name"])
                        if acct:
                            break
                        if scrape_attempt == 0:
                            page.wait_for_timeout(1500)
                    if not acct:
                        rec["status"] = "NOT_FOUND_IN_TA"
                        log(f"  {t['name']}: no unique Account # found in TA — needs manual review")
                    elif dry_run:
                        rec.update(found=acct, status="WOULD_SET")
                        log(f"  {t['name']}: would set Square reference_id = {acct}")
                    else:
                        confirmed = update_customer_reference(t["customer_id"], acct)
                        ok = (confirmed == acct)
                        rec.update(found=acct, confirmed=confirmed,
                                   status="SET" if ok else "SET_MISMATCH")
                        log(f"  {t['name']}: set Square reference_id = {acct}"
                            + ("" if ok else f" but Square returned {confirmed!r}"))
                except Exception as e:
                    rec["status"] = "ERROR"
                    rec["error"] = str(e)
                    log(f"  ERROR {t['name']}: {e}")
                    try:
                        bot.recover_to_dashboard(page)
                    except Exception:
                        pass
                results.append(rec)
        finally:
            browser.close()

    n_set = sum(1 for r in results if r["status"] == "SET")
    n_would = sum(1 for r in results if r["status"] == "WOULD_SET")
    log(f"BACKFILL done: {n_set} set, {n_would} would-set (dry-run), "
        f"{len(results) - n_set - n_would} unresolved/error.")
    return results


def main():
    ap = argparse.ArgumentParser(description="Near-real-time Square -> TA poster (dev-test).")
    ap.add_argument("--once", action="store_true", help="Ignore cadence; poll once now.")
    ap.add_argument("--dry-run", action="store_true", help="Show what would post; post nothing, persist nothing.")
    ap.add_argument("--shadow", action="store_true",
                    help="Observe-only: record would-post to the ledger for reconciliation, but post nothing.")
    ap.add_argument("--since", default=None, help="Override cursor (RFC3339 UTC).")
    ap.add_argument("--backfill-missing", action="store_true",
                    help="Self-heal: for Square customers missing a reference_id, look up their "
                         "TA Account # and write it back to Square. Posts nothing. Honors "
                         "--dry-run (lookup only), --since (default: last 7 days), and --limit.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap how many customers --backfill-missing processes (for smoke tests).")
    args = ap.parse_args()

    now = datetime.now()

    # --- BACKFILL: self-heal missing Account #s; posts nothing, ignores cadence/state ---
    if args.backfill_missing:
        if not SQUARE_ACCESS_TOKEN:
            log("SQUARE_ACCESS_TOKEN not set in .env — cannot backfill.")
            sys.exit(1)
        since = args.since or (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        backfill_missing(since, dry_run=args.dry_run, limit=args.limit)
        return

    # One poller at a time. Skipped for --dry-run, which posts nothing and must
    # stay usable for diagnosis while a real run is going.
    lock_handle = None
    if not args.dry_run:
        lock_handle, may_run = acquire_run_lock()
        if not may_run:
            log("Another poller run is still going — skipping this fire rather than "
                "posting the same payments twice.")
            return

    state = load_state()

    # Cadence gate (skipped by --once) — cheap skip before any token/network work.
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

    # Build post list; flag any payment whose name can't be resolved (don't post blanks)
    to_post, unresolved = [], []
    for p in new:
        name, date, amount, account = extract_payment_fields(p)
        rec = {"id": p["id"], "name": name, "date": date, "amount": amount,
               "account": account, "customer_id": p.get("customer_id", "")}
        (to_post if name else unresolved).append(rec)

    # Re-attempt recent failures the cursor has already moved past. Never in
    # shadow: shadow rewrites the row as WOULD_POST, which reconcile counts as
    # posted, so the payment would vanish from the outstanding list unposted.
    queued = {r["id"] for r in to_post}
    for row in ([] if args.shadow else retry_candidates(posted_ids)):
        if row["id"] in queued:
            continue
        p = payment_still_completed(row["id"])
        if not p:
            continue
        name, date, amount, account = extract_payment_fields(p)
        if not name:
            continue
        attempt = int(row.get("retries") or 0) + 1
        if not args.dry_run and not mark_retry_attempt(row):
            log(f"  (skipping retry of {row['id']}: could not record the attempt)")
            continue
        queued.add(row["id"])
        to_post.append({"id": row["id"], "name": name, "date": date, "amount": amount,
                        "account": account, "customer_id": p.get("customer_id", "")})
        log(f"  RETRY {name} ${amount} on {date} — earlier failure could not have reached "
            f"TA's payment form (attempt {attempt} of {RETRY_MAX_ATTEMPTS})")

    if not to_post and not unresolved:
        # --dry-run persists nothing: it is run for diagnosis, sometimes while a
        # real run is going, and writing a stale dict back here once advanced the
        # live cursor past payments and rewound last_action_at (2026-09-16).
        if not args.dry_run:
            state.update(last_polled_at=next_cursor, last_action_at=now.isoformat())
            save_state(state)
        log("Nothing new to post.")
        return
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
                          "SKIPPED_NO_NAME", u["account"], "Square payment has no customer attached — payer unknown")
            log(f"  SKIP (no name) ${u['amount']} on {u['date']} (sq:{u['id']})")
        state.update(last_polled_at=next_cursor, last_action_at=now.isoformat())
        save_state(state)
        log(f"Shadow done: {len(to_post)} would-post, {len(unresolved)} skipped (no name).")
        return

    # --- LIVE: post for real, record outcomes to the ledger ---
    for u in unresolved:
        record_ledger(u["id"], u["name"], u["date"], u["amount"],
                      "SKIPPED_NO_NAME", u["account"], "Square payment has no customer attached — payer unknown")
        log(f"  SKIP (no name resolved) ${u['amount']} on {u['date']} (sq:{u['id']}) — needs manual posting")
    def persist(r):
        record_ledger(r["id"], r["name"], r["date"], r["amount"],
                      r["status"], r.get("account", ""), r.get("error", ""),
                      r.get("method", ""), r.get("note", ""))
        state["posted_payment_ids"] = sorted(posted_ids)
        state["last_action_at"] = datetime.now().isoformat()
        save_state(state)

    results = post_new_payments(to_post, posted_ids, on_result=persist)
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
