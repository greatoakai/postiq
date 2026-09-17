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
import hashlib
import html
import json
import os
import re
import signal
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
# Written by reconcile (REPORT_STAMP_FILENAME there): started_at before the morning report reads
# the ledger, sent_at once the email has gone. See last_report_at().
REPORT_STAMP_PATH = bot.DATA_DIR / "report_stamp.json"
# A report still "running" this long after it started has died; stop holding retries for it.
REPORT_RUN_GRACE = timedelta(hours=1)

# An ALLOWLIST, not a denylist. Since PR #16 a plain FAILED means nothing was
# submitted — V1 wraps submit_payment in AT_PAYMENT_FORM like V2 and the balance
# post — but the retry path still refuses to lean on that alone: only reasons that
# cannot have reached TA's payment form are retried. The app-not-rendering error
# qualifies because it is raised solely by _ensure_sidebar, reachable only from the
# two sidebar clicks, both of which run before any form is filled.
RETRYABLE_REASONS = (bot.APP_NOT_RENDERING_REASON,)

# The leg labels post_payment chains reasons with ("V2: ...; V2-retry: ...; V1: ...").
_REASON_LEG_LABEL_RX = re.compile(r"\b(?:V2-retry|V2|V1)\s*:")


def _reason_is_only_retryable(reason):
    """True only if `reason` is made of allowlisted reasons and nothing else.

    Fails CLOSED. Strip every allowlisted reason, every leg label and every
    separator; if ANY text is left, some leg failed for a reason not known to be
    pre-submission, and the payment may already be in TA. It doesn't depend on how
    bot_v2 punctuates the chain: if that spelling ever changes, the unrecognised
    text is left over and the retry is refused — where splitting on the expected
    separators would instead let one allowlisted leg vouch for the whole chain.
    """
    if not reason or not any(allowed in reason for allowed in RETRYABLE_REASONS):
        return False
    residue = reason
    for allowed in RETRYABLE_REASONS:
        residue = residue.replace(allowed, "")
    residue = _REASON_LEG_LABEL_RX.sub("", residue)
    return not re.sub(r"[\s;]", "", residue)

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


class StateUnreadableError(RuntimeError):
    """poll_state.json exists but cannot be read. Refuse to run rather than guess."""


class StatePersistError(RuntimeError):
    """posted_payment_ids could not be saved, so the next run could not dedupe."""


def load_state():
    """Read the poller's state. A MISSING file is a first run; an UNREADABLE one is not.

    Starting fresh on a corrupt file resets the cursor to midnight and forgets every
    posted id, so the run would re-post the whole day to TA. Refuse instead, and leave
    the file where it is for a person to look at.
    """
    if not STATE_PATH.exists():
        return {"last_polled_at": None, "last_action_at": None, "posted_payment_ids": []}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception as e:
        raise StateUnreadableError(
            f"{STATE_PATH.name} exists but could not be read ({e}). Not posting: starting "
            f"fresh would re-post today's payments. Restore it from a backup.")


def _full_fsync(fd):
    """Flush to the physical disk. On macOS os.fsync only hands data to the drive,
    which may still hold it in its own cache; F_FULLFSYNC asks the drive to flush."""
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


def _atomic_write_json(path, obj):
    """Write JSON so a kill OR a power loss leaves either the old file or the new one.

    Sibling temp file, full flush, rename over the real file, then sync the directory
    so the rename itself survives a power loss. Used for every file whose loss would
    re-post or hide a payment: the state and the ledger.
    """
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as fh:
        fh.write(json.dumps(obj, indent=2))
        fh.flush()
        _full_fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            _full_fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass  # some filesystems refuse a directory sync; the rename is still atomic


def save_state(state):
    """Write state atomically and durably. See _atomic_write_json."""
    _atomic_write_json(STATE_PATH, state)


LEDGER_DIR = bot.DATA_DIR / "poll_ledger"


class LedgerUnreadableError(RuntimeError):
    """A day's ledger file exists but cannot be read. Never overwrite it."""


def _read_ledger_file(path):
    """A day's ledger rows. A MISSING file is an empty day; an UNREADABLE one is not.

    Treating an unreadable file as empty and then writing to it is how a day's
    recorded payments get erased — after which already-posted payments look missing
    on the morning report and staff post them again.
    """
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text())
    except Exception as e:
        raise LedgerUnreadableError(
            f"{path.name} exists but could not be read ({e}); refusing to overwrite it")
    if not isinstance(entries, list):
        raise LedgerUnreadableError(f"{path.name} is not a list; refusing to overwrite it")
    return entries


# A durable record of a payment the poller is ABOUT to post: written before money moves,
# removed once the outcome is saved. If a run is killed, or both of its writes fail, after
# TA saved a payment, this is the only trace on disk that it was started — and it is what
# stops the next run posting it again. One small file per Square payment id.
INFLIGHT_DIR = bot.DATA_DIR / "poll_inflight"


def _inflight_path(payment_id):
    return INFLIGHT_DIR / f"{payment_id}.json"


def mark_inflight(item):
    """Durably record the intent to post `item`. Raises if that cannot be done."""
    INFLIGHT_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(_inflight_path(item["id"]), {
        "id": item["id"], "name": item.get("name"), "date": item.get("date"),
        "amount": item.get("amount"),
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})


def clear_inflight(payment_id):
    try:
        _inflight_path(payment_id).unlink()
    except FileNotFoundError:
        pass


def inflight_ids():
    """Payment ids a run started posting without (yet) recording the outcome."""
    if not INFLIGHT_DIR.exists():
        return []
    return [p.stem for p in INFLIGHT_DIR.glob("*.json")]


def _inflight_record(payment_id):
    """What an intent marker says about its payment (name, date, amount); {} if unreadable."""
    try:
        rec = json.loads(_inflight_path(payment_id).read_text())
        return rec if isinstance(rec, dict) else {}
    except Exception:
        return {}


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
    entries = _read_ledger_file(path)
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
    _atomic_write_json(path, entries)


def acquire_run_lock():
    """Take the poller's run lock. Returns (handle, may_run).

    Two pollers at once post the same payments twice: both read the same cursor and the
    same posted_payment_ids, and intent markers can't help, because each run reads them
    only at startup. So the lock fails CLOSED: if it can't even be opened (say it was left
    owned by another user after a sudo run), refuse to run and alert — exactly as for an
    unwritable state file.
    """
    try:
        handle = open(LOCK_PATH, "w")
    except Exception as e:
        alert_admin("run-lock-unavailable",
                    f"Refusing to run: the poller's lock file {LOCK_PATH} cannot be opened ({e}). "
                    f"Without it two runs could post the same payments twice, so no payments are "
                    f"being posted until this is fixed." + _pending_markers_note())
        return None, False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None, False  # another run holds it: the normal, quiet case
    except OSError as e:
        # Not contention: this filesystem can't lock at all. Refusing quietly would stop
        # posting for good with nobody told, so alert exactly like an unopenable lock file.
        handle.close()
        alert_admin("run-lock-unavailable",
                    f"Refusing to run: the poller cannot lock {LOCK_PATH} ({e}). Without a lock "
                    f"two runs could post the same payments twice, so no payments are being "
                    f"posted until this is fixed." + _pending_markers_note())
        return None, False
    return handle, True


def _cleared_keys():
    """Payments staff have confirmed they handled by hand (reconcile --clear)."""
    try:
        return set(json.loads(CLEARED_PATH.read_text()).get("keys", []))
    except Exception:
        return set()


def last_report_at(now_utc=None):
    """The newest moment the emailed outstanding list may have read the ledger, as UTC.

    A failure older than this may have been in front of staff, who may be posting it by
    hand right now — so the bot must leave it alone however recent it looks.

    The report runs at 08:00 only if the Mac is awake; otherwise launchd runs it on wake,
    reading the ledger then. So take the latest of: the scheduled 08:00, the report's own
    sent_at, and — while a report has started and not finished — now (until REPORT_RUN_GRACE
    says it died, then the latest it could have read). Every error leans later: a later
    cutoff only means fewer retries.

    On a weekday after REPORT_HOUR, the report is due; until it leaves a stamp from today,
    hold every retry (cutoff = now). Either it hasn't run yet — the Mac slept through 08:00
    and it runs on wake — or it ran and couldn't record when, and staff may already have a
    list this poller knows nothing about.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    local_now = now_utc.astimezone()
    boundary = local_now.replace(hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
    report_due_today = boundary <= local_now and local_now.weekday() < 5
    if boundary > local_now:
        boundary -= timedelta(days=1)
    marks = [boundary.astimezone(timezone.utc)]
    try:
        raw = REPORT_STAMP_PATH.read_text()
    except FileNotFoundError:
        raw = "{}"
    except Exception as e:
        log(f"Report stamp {REPORT_STAMP_PATH} unreadable ({e}) — no auto-retries this run.")
        return now_utc

    def stamp(key):
        v = d.get(key)
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if v else None

    try:
        d = json.loads(raw)
        started, sent = stamp("started_at"), stamp("sent_at")
    except Exception as e:
        log(f"Report stamp {REPORT_STAMP_PATH} malformed ({e}) — no auto-retries this run.")
        return now_utc
    if report_due_today and not (started and started.astimezone().date() == local_now.date()):
        return now_utc
    if sent:
        # Stamps are whole seconds; a failure in that same second may still have been read.
        marks.append(sent + timedelta(seconds=1))
    if started and (sent is None or started > sent):
        marks.append(min(now_utc, started + REPORT_RUN_GRACE))
    return max(marks)


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
        if not _reason_is_only_retryable(reason):
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
        entries = _read_ledger_file(path)
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
        _atomic_write_json(path, entries)
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


# Set by SIGTERM. launchd sends it on unload, and by default it kills the process on
# the spot — possibly between TA saving a payment and this run recording it, which
# the next run would then post again. Catching it lets the payment in progress finish
# and persist; the batch then stops before starting another. launchd still sends
# SIGKILL if a run outlasts its grace period (20s by default — this job sets no
# ExitTimeOut), and SIGKILL cannot be caught, so this narrows the window; it does not
# close it. The handler only sets a flag: raising from a signal handler could land
# inside a save.
_termination_requested = False


def _request_termination(signum, frame):
    global _termination_requested
    _termination_requested = True


def _flush(on_result, result):
    """Persist one outcome now.

    Losing posted_payment_ids mid-batch is not survivable — the next run could not
    tell this payment was already posted — so StatePersistError stops the batch.
    Anything else (a ledger write, say) is logged and the batch carries on.
    """
    if not on_result:
        return
    try:
        on_result(result)
    except StatePersistError:
        raise
    except Exception as e:
        log(f"  WARNING: could not persist the outcome for {result.get('id')}: {e}")


class LedgerWriteError(RuntimeError):
    """A ledger row could not be written mid-batch. It gets a second chance at the end."""


# Who hears about it when the poller stops, or can't record what it did. Same person
# reconcile.ADMIN_TO emails; kept here so the poller doesn't import the report.
ALERT_TO = "travis@greatoakcounseling.com"


def alert_admin(kind, detail):
    """Email the admin that the poller stopped or couldn't record something.

    Repeats of the SAME message are sent at most once a day: the poller fires every 30
    minutes, and a stuck condition would otherwise send forty identical emails. The limit
    is keyed on the message, not just the kind, so an early, harmless alert can never
    suppress a later, different one of the same kind — say, a payment that posted in TA
    but has no ledger row.
    """
    log(f"ALERT [{kind}]: {detail}")
    digest = hashlib.sha1(f"{kind}\n{detail}".encode()).hexdigest()[:12]
    marker = bot.DATA_DIR / f".alerted_{kind}_{datetime.now():%Y%m%d}_{digest}"
    if marker.exists():
        return
    try:
        sent = bot.send_email(ALERT_TO, None, f"PostIQ poller: {kind}",
                              f"<p>{html.escape(detail)}</p>", html=True)
    except Exception as e:
        log(f"  (could not send the alert email: {e})")
        return
    if sent:
        try:
            marker.write_text(datetime.now().isoformat())
        except Exception:
            pass


def _write_result_row(r):
    record_ledger(r["id"], r["name"], r["date"], r["amount"],
                  r["status"], r.get("account", ""), r.get("error", ""),
                  r.get("method", ""), r.get("note", ""))


def make_persister(state, posted_ids):
    """The per-payment persistence callback for post_new_payments.

    Both records are attempted every time, state first. posted_payment_ids is the
    primary guard against posting a payment twice, so a failure there stops the batch
    (StatePersistError). But the ledger row is still written first: _ledger_says_may_be_in_ta
    lets the next run recognise the payment from the ledger alone, so a failed state
    save no longer means a re-post. A ledger failure on its own is recorded in
    persist.ledger_failures for a second attempt at the end of the run.

    last_action_at is deliberately left to the end of the run, so cadence behaves
    exactly as it always has.
    """
    ledger_failures = []

    def persist(r):
        state["posted_payment_ids"] = sorted(posted_ids)
        state_error = ledger_error = None
        try:
            save_state(state)
        except Exception as e:
            state_error = e
        try:
            _write_result_row(r)
        except Exception as e:
            ledger_error = e
            ledger_failures.append(r)
        if state_error is not None:
            raise StatePersistError(
                f"could not save posted_payment_ids after {r.get('id')} ({state_error})"
                + ("" if ledger_error else
                   "; its ledger row WAS written, so the next run will still recognise it"))
        if ledger_error is not None:
            raise LedgerWriteError(f"could not write the ledger row for {r.get('id')}: {ledger_error}")

    persist.ledger_failures = ledger_failures
    return persist


def retry_failed_ledger_writes(persist):
    """Second chance for ledger rows that failed mid-batch. Returns (row, error) still unwritten.

    A row that lands now also clears its payment's intent marker: post_new_payments kept the
    marker only because the row hadn't landed. Leaving it would make the next run's sweep see
    a recorded FAILED/FLAGGED/ERROR row plus a marker, take it for an interrupted post, and
    overwrite the accurate row with a generic may-have-posted flag.
    """
    failures = getattr(persist, "ledger_failures", [])
    still = []
    for r in list(failures):
        try:
            _write_result_row(r)
        except Exception as e:
            still.append((r, e))
            continue
        try:
            failures.remove(r)
        except ValueError:
            pass
        _clear_inflight_quietly(r["id"])
    return still


def _ledger_row(payment_id, date):
    """This payment's ledger row, or None (no row, no date, or an unreadable ledger)."""
    if not date:
        return None
    try:
        entries = _read_ledger_file(LEDGER_DIR / f"{date.replace('/', '')}.json")
    except LedgerUnreadableError:
        return None
    return next((e for e in entries if isinstance(e, dict) and e.get("id") == payment_id), None)


def _ledger_says_may_be_in_ta(payment_id, date):
    """The ledger's second opinion before posting: OK, or flagged may-have-posted.

    posted_payment_ids is the primary record, but if a state save ever failed after a
    payment went through, its ledger row may be the only record that it did.
    """
    e = _ledger_row(payment_id, date)
    return bool(e) and (e.get("status") == "OK"
                        or bot.MAY_HAVE_POSTED_MARKER in (e.get("reason") or ""))


def _ledger_says_already_attempted(payment_id, date):
    """A recorded outcome other than posted: FAILED, ERROR, FLAGGED, SKIPPED_NO_NAME.

    A payment like that turns up among a run's "new" payments whenever the cursor didn't
    move past it — the overlap window, or a run that stopped early. Posting it from there
    would bypass every safeguard the retry gate applies (the reason allowlist, the report
    cutoff, the attempt limit), so only the retry gate may re-attempt it. WOULD_POST is a
    shadow run's note, not an attempt.
    """
    e = _ledger_row(payment_id, date)
    return bool(e) and bool(e.get("status")) and e.get("status") not in ("OK", "WOULD_POST")


def _retry_now_on_report(item):
    """Has the morning report started since this retry was queued, so it may list it?

    retry_candidates() applied the cutoff when the run began, but a run can be minutes into
    logging in and posting before it reaches this payment. Checked AFTER the intent marker is
    written: a report that reads the ledger before the marker exists wrote started_at first,
    so this sees it; one that reads after sees the marker, and says may-have-posted.
    """
    stamp = item.get("retry_failed_at")
    if not stamp:
        return False
    try:
        failed_at = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return failed_at < last_report_at()


def _pending_markers_note():
    """For an alert sent while the poller is refusing to run: payments left mid-post.

    Only a later run turns an intent marker into a ledger flag. While the poller can't run,
    these are the payments that may already be in TA with nothing else on record saying so.
    """
    try:
        ids = sorted(inflight_ids())
    except Exception:
        return ""
    if not ids:
        return ""
    parts = []
    for pid in ids:
        rec = _inflight_record(pid)
        parts.append(f"{rec.get('name') or '?'} ${rec.get('amount') or '?'} on "
                     f"{rec.get('date') or '?'} (sq:{pid})")
    return (f" {len(ids)} payment(s) were being posted when a run was interrupted and are NOT "
            f"yet recorded — they may already be in TA; check before posting any by hand: "
            + "; ".join(parts) + ".")


def _clear_inflight_quietly(payment_id):
    try:
        clear_inflight(payment_id)
    except Exception as e:
        log(f"  WARNING: could not clear the in-flight marker for {payment_id}: {e}")


def handle_interrupted_post(payment_id, name, date, amount, account, posted_ids, live):
    """An earlier run durably recorded it was about to post this, then never recorded how
    it went — it was killed, or both its writes failed. The money may already be in TA.

    Never post it again: retire it, flag it may-have-posted so the morning report tells
    staff to check the ledger first, and tell the admin now rather than at 08:00.

    The intent marker is removed ONLY once that flag is durably in the ledger. A payment
    retired with no record reads as simply unposted on the morning report — staff would
    post it, and it may already be in TA. So if the flag can't be written, the marker stays
    and the next run tries again (and alerts again, at most once a day).
    """
    posted_ids.add(payment_id)
    log(f"  SKIP {name} ${amount} on {date} (sq:{payment_id}) — an earlier run started posting "
        f"it and was interrupted; it may already be in TA")
    if not live:
        return
    flagged = False
    if date:  # record_ledger silently writes nothing without a date
        try:
            record_ledger(payment_id, name, date, amount, "FLAGGED", account,
                          bot._may_have_posted_flag(
                              name, "an earlier run started posting it and was interrupted "
                                    "before it could record the outcome"))
            flagged = True
        except Exception as e:
            log(f"  WARNING: could not flag {payment_id} in the ledger: {e}")
    alert_admin("interrupted-post",
                f"{name} ${amount} on {date} (sq:{payment_id}): an earlier run started posting "
                f"this and was interrupted before recording the outcome, so it may already be in "
                f"TA. The bot will not post it again. Check the client's ledger in TA and post by "
                f"hand only if it isn't there."
                + ("" if flagged else " It could NOT be flagged in the ledger either, so the "
                                      "morning report may show it as simply unposted."))
    if flagged:
        _clear_inflight_quietly(payment_id)


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
                if _termination_requested:
                    log("  SIGTERM — not starting another payment.")
                    break

                # Record the intent BEFORE money moves. If it can't be written, don't post:
                # a payment that goes through and leaves no trace is the double-post case.
                try:
                    mark_inflight(item)
                except Exception as e:
                    raise StatePersistError(
                        f"could not durably record the intent to post {item['id']} ({e}); "
                        f"stopped before posting it") from e
                try:
                    report_raced = _retry_now_on_report(item)
                except Exception:
                    report_raced = True
                if report_raced:
                    log(f"  (not retrying {item['name']} — the morning report may be listing it "
                        f"for staff to post by hand)")
                    # Nothing was attempted. If this clear fails, a later run flags the payment
                    # may-have-posted: more caution than needed, never less.
                    _clear_inflight_quietly(item["id"])
                    continue

                # Exactly one recorded outcome per payment, decided here and only here.
                try:
                    success, method, error, note, *_ = bot.post_payment(
                        page, item["name"], item["date"], item["amount"],
                        account=item.get("account"))
                    if success:
                        posted_ids.add(item["id"])
                        result = {**item, "status": "OK", "method": method, "note": note or ""}
                    else:
                        # A payment that reached TA's payment form before failing may
                        # already be in TA. Retire the id BEFORE persisting, so the save
                        # below covers it — otherwise a crash straight after would leave
                        # the one kind of payment that must never be re-posted unretired.
                        # The ledger still carries the flag for the morning report.
                        if bot.MAY_HAVE_POSTED_MARKER in (error or ""):
                            posted_ids.add(item["id"])
                        result = {**item, "status": method or "FAILED", "error": error}
                except Exception as e:
                    result = {**item, "status": "ERROR", "error": str(e)}

                results.append(result)
                _flush(on_result, result)  # StatePersistError stops the batch; intent is kept
                # Outcome saved. Clear the intent record only if the ledger row landed too: a
                # payment in posted_payment_ids with no ledger row reads as unposted on the
                # morning report, so the marker stays until the ledger records it.
                if result not in getattr(on_result, "ledger_failures", []):
                    _clear_inflight_quietly(item["id"])

                if result["status"] == "OK":
                    log(f"  POSTED {item['name']} ${item['amount']} ({result.get('method')})")
                    try:
                        _self_heal_account(item.get("customer_id"), item.get("account"), item["name"])
                    except Exception as e:
                        log(f"  WARNING: account self-heal failed for {item['name']}: {e}")
                    continue

                if result["status"] == "ERROR":
                    log(f"  ERROR {item['name']}: {result['error']}")
                else:
                    log(f"  NOT POSTED {item['name']}: {result.get('error')}")
                    if bot.MAY_HAVE_POSTED_MARKER in (result.get("error") or ""):
                        log(f"  (not retrying {item['name']} — it may already be in TA)")

                # Reset the browser for the next payment. Kept apart from the outcome
                # above: a recovery failure must not rewrite this payment's recorded
                # status (FAILED -> ERROR), which also hid it from the retry gate.
                try:
                    bot.recover_to_dashboard(page)
                except Exception as e:
                    log(f"  WARNING: could not reset the browser after {item['name']}: {e}")
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
            log("Could not take the run lock (another run is going, or the lock is "
                "unavailable) — skipping this fire rather than risk posting payments twice.")
            return
        signal.signal(signal.SIGTERM, _request_termination)

    try:
        state = load_state()
    except StateUnreadableError as e:
        log(f"ERROR: {e}")
        if not args.dry_run:
            alert_admin("state-unreadable",
                        f"{e} The poller is refusing to run until this is fixed, so new "
                        f"payments are NOT being posted." + _pending_markers_note())
        sys.exit(1)

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

    # Only a live run changes anything on disk or in TA; dry runs and shadow runs look.
    live = not args.dry_run and not args.shadow

    # Prove state can be saved BEFORE any side effect — before a retry attempt is spent,
    # and long before TA is touched. A payment that posts and then can't be recorded is the
    # one outcome this system can't take back, and a persistent failure (a full disk, a .tmp
    # left owned by another user) would otherwise re-post it on every run.
    if live:
        try:
            save_state(state)
        except Exception as e:
            alert_admin("state-save-failed",
                        f"Refusing to post: poll_state.json cannot be written ({e}). No payments "
                        f"were attempted this run." + _pending_markers_note())
            sys.exit(1)

    # Payments an earlier run recorded it was about to post. Each is dealt with below — as it
    # comes up among this run's payments or retries, or in the sweep after that. A marker is
    # only ever removed once a durable record makes it redundant.
    inflight = set(inflight_ids())
    handled = set()

    # Build post list; flag any payment whose name can't be resolved (don't post blanks)
    to_post, unresolved = [], []
    for p in new:
        name, date, amount, account = extract_payment_fields(p)
        if _ledger_says_may_be_in_ta(p["id"], date):
            posted_ids.add(p["id"])  # heal state from the ledger
            log(f"  SKIP {name} ${amount} on {date} (sq:{p['id']}) — the ledger shows it may "
                f"already be in TA")
            if p["id"] in inflight:
                handled.add(p["id"])
                if live:
                    _clear_inflight_quietly(p["id"])  # the ledger already records it
            continue
        if p["id"] in inflight:
            handled.add(p["id"])
            handle_interrupted_post(p["id"], name, date, amount, account, posted_ids, live)
            continue
        if _ledger_says_already_attempted(p["id"], date):
            log(f"  SKIP {name} ${amount} on {date} (sq:{p['id']}) — already attempted; only "
                f"the retry gate may try it again")
            continue
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
        if row["id"] in inflight:
            # An interrupted retry: its FAILED row predates the attempt that was cut off.
            handled.add(row["id"])
            handle_interrupted_post(row["id"], row.get("name", ""), row.get("date", ""),
                                    row.get("amount", ""), row.get("account", ""), posted_ids, live)
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
                        "account": account, "customer_id": p.get("customer_id", ""),
                        "retry_failed_at": row.get("failed_at")})
        log(f"  RETRY {name} ${amount} on {date} — earlier failure could not have reached "
            f"TA's payment form (attempt {attempt} of {RETRY_MAX_ATTEMPTS})")

    # Every other intent marker: payments already retired in posted_payment_ids, and any that
    # didn't come up this run at all. Clear one only if the ledger durably records the outcome
    # (OK, or flagged may-have-posted). Otherwise it was interrupted: flag it and tell the admin.
    # Never clear it silently, and never ignore it — either way it could end up retired with no
    # record, which the morning report would show as simply unposted.
    for mid in sorted(inflight - handled - queued):
        rec = _inflight_record(mid)
        if _ledger_says_may_be_in_ta(mid, rec.get("date") or ""):
            posted_ids.add(mid)
            if live:
                _clear_inflight_quietly(mid)
        else:
            handle_interrupted_post(mid, rec.get("name") or "", rec.get("date") or "",
                                    rec.get("amount") or "", "", posted_ids, live)

    if not to_post and not unresolved:
        # --dry-run persists nothing: it is run for diagnosis, sometimes while a
        # real run is going, and writing a stale dict back here once advanced the
        # live cursor past payments and rewound last_action_at (2026-09-16).
        if not args.dry_run:
            # posted_payment_ids too: the ledger check above may have healed it.
            state.update(posted_payment_ids=sorted(posted_ids),
                         last_polled_at=next_cursor, last_action_at=now.isoformat())
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
    # (state was proven writable above, before any side effect)
    for u in unresolved:
        try:
            record_ledger(u["id"], u["name"], u["date"], u["amount"],
                          "SKIPPED_NO_NAME", u["account"], "Square payment has no customer attached — payer unknown")
        except Exception as e:
            alert_admin("ledger-write-failed",
                        f"Could not record the unattributed Square payment {u['id']} "
                        f"(${u['amount']} on {u['date']}) in the ledger: {e}")
        log(f"  SKIP (no name resolved) ${u['amount']} on {u['date']} (sq:{u['id']}) — needs manual posting")

    persist = make_persister(state, posted_ids)
    try:
        results = post_new_payments(to_post, posted_ids, on_result=persist)
    except StatePersistError as e:
        unwritten = retry_failed_ledger_writes(persist)
        alert_admin("state-save-failed",
                    f"{e}. Stopped mid-batch; the cursor was not advanced. "
                    + (f"{len(unwritten)} ledger row(s) could not be written either — check "
                       f"those payments in TA before posting anything by hand."
                       if unwritten else ""))
        sys.exit(1)

    unwritten = retry_failed_ledger_writes(persist)
    if unwritten:
        alert_admin("ledger-write-failed",
                    "These outcomes could not be written to the ledger, so the morning report "
                    "may show them wrongly. Check each in TA before posting anything by hand: "
                    + "; ".join(f"{r['name']} ${r['amount']} on {r['date']} was {r['status']} ({e})"
                                for r, e in unwritten))

    if _termination_requested:
        # Stopped before attempting everything. Advancing the cursor now would move it
        # past the payments never attempted — which have no ledger row, so nothing
        # would ever pick them up again. Keep the old cursor; next run re-reads them and
        # skips the ones already in posted_payment_ids.
        state["posted_payment_ids"] = sorted(posted_ids)
        save_state(state)
        log(f"Stopped early on SIGTERM after {len(results)} of {len(to_post)} payment(s); "
            f"cursor left in place so the rest are picked up next run.")
        return

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
