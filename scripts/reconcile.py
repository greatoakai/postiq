#!/usr/bin/env python3
"""
reconcile.py — the daily PostIQ reconciliation report.

Every morning (com.greatoak.postiq-reconcile, 8:00 AM) this compares the live
poller's per-day ledger (data/poll_ledger/MMDDYYYY.json — every Square payment it
posted to TherapyAppointment, or tried to) against the FULL daily Square CSV (the
authoritative list of that day's payments), and emails the result to staff.

Since the 2026-06-18 shadow->live cutover this is the ONLY daily report: the
poller posts in near-real-time and the 7:00 AM batch just syncs/archives the CSV.
So the email is written for the person who finishes the job by hand — it leads
with the payments that still need to be posted manually in TA.

Each CSV payment is classified:
  MATCHED      poller posted it with a matching amount — nothing to do
  GAP          in the CSV, poller never handled it — needs manual posting
  ERROR        poller tried but failed/skipped — needs manual posting
  DISCREPANCY  name matches but the amount differs — verify, don't blind-post
Plus:
  EXTRA        poller posted something not in the CSV — verify

The email also carries a rolling BACKLOG: every payment from the last
BACKLOG_DAYS days that never posted automatically and hasn't been marked
cleared. Staff work that list in TA; Travis clears it with --clear-through once
they confirm it's done.

Weekdays cover yesterday. Monday covers Friday, Saturday and Sunday in one
email; Saturday and Sunday send nothing, so the weekend's payments arrive as a
single Monday to-do list instead of three reports nobody read.

Usage:
  python3 scripts/reconcile.py --csv "Square Payment Archive/06.13.2026_Daily.Square.Log.csv"
  python3 scripts/reconcile.py --date 06.13.2026
  python3 scripts/reconcile.py --email                     # the day(s) due today, to staff
  python3 scripts/reconcile.py --clear-through 07.31.2026  # backlog confirmed done through 7/31
  python3 scripts/reconcile.py --clear sq:rpo9hD8fXgFEtKJnXa0Rs5Co688YY
"""

import argparse
import json
import re
import sys
from collections import OrderedDict
from datetime import datetime, timedelta
from html import escape as esc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot_v2 as bot  # noqa: E402

LEDGER_DIR = bot.DATA_DIR / "poll_ledger"
POSTED_OK = ("OK", "WOULD_POST")
# CSVs land in the live repo; the ledger lives wherever this runs (dev worktree
# during the shadow window). Search both so reconcile works from either tree.
LIVE_ROOT = Path("/Users/travmegsam/Developer/postiq")

# Who gets the morning report. Hannah works the manual-posting list; Travis owns
# the system and gets the housekeeping sections. Add supportstaff@ to STAFF_TO if
# the whole billing desk should see it.
STAFF_TO = "hannah@greatoakcounseling.com"
ADMIN_TO = "travis@greatoakcounseling.com"

# Rolling look-back for the "still outstanding" list.
BACKLOG_DAYS = 45
# Payments staff have confirmed they handled by hand, so they stop being listed.
CLEARED_FILE = bot.DATA_DIR / "manual_cleared.json"


def _norm(name):
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _amt(a):
    try:
        return f"{float(str(a).replace('$', '').replace(',', '')):.2f}"
    except Exception:
        return str(a)


def _account(raw):
    """TA Account # for display, or "" if Square's reference_id isn't one.

    Square reference_ids are inconsistent — canonical C#########, the same value
    with the leading "C00" stripped by numeric coercion, and occasionally a UUID
    or the client's own name. normalize_account repairs the first two and rejects
    the rest; sending staff off to search TA for a UUID helps nobody.
    """
    return bot.normalize_account(raw or "")


_NAME_NOISE = {"iii", "the", "and"}


def _shares_name_token(a, b):
    """Do two spellings of a name share a distinctive word?

    Used to decide whether a CSV row and an unmatched poller failure are the same
    person under two spellings ("Cash Stone IV" / "William Stone IV" share
    "stone") rather than two different clients who happened to pay the same
    amount that day.
    """
    def toks(n):
        return {t for t in re.split(r"[^a-z]+", _norm(n))
                if len(t) >= 3 and t not in _NAME_NOISE}
    return bool(toks(a) & toks(b))


def _clean(name):
    """Display form of a client name — the CSV sometimes has doubled spaces."""
    return re.sub(r"\s+", " ", (name or "").strip())


def _money(a):
    try:
        return f"${float(str(a).replace('$', '').replace(',', '')):,.2f}"
    except Exception:
        return f"${a}"


def _dt(mmddyyyy):
    try:
        return datetime.strptime(mmddyyyy.replace(".", "/"), "%m/%d/%Y")
    except Exception:
        return None


HEAL_LOG = bot.DATA_DIR / "poll_heals.json"


def load_unreported_heals():
    """Return (all_entries, unreported) from the poller's self-heal log."""
    if not HEAL_LOG.exists():
        return [], []
    try:
        entries = json.loads(HEAL_LOG.read_text())
    except Exception:
        return [], []
    return entries, [e for e in entries if not e.get("reported")]


def mark_heals_reported(entries):
    for e in entries:
        e["reported"] = True
    try:
        HEAL_LOG.write_text(json.dumps(entries, indent=2))
    except Exception:
        pass


def load_ledger_for_date(mmddyyyy):
    key = mmddyyyy.replace("/", "").replace(".", "")
    path = LEDGER_DIR / f"{key}.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def date_from_csv_name(name):
    """MM/DD/YYYY out of 'MM.DD.YYYY_Daily.Square.Log.csv'.

    The transaction date normally comes from the rows themselves; this is the
    fallback for a CSV that parses to zero payments, so a bad export degrades to
    "no payments found" instead of silently reporting an empty backlog.
    """
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", name or "")
    return f"{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else ""


def find_csv(date_dotted):
    name = f"{date_dotted}_Daily.Square.Log.csv"
    for root in (bot.PROJECT_ROOT, LIVE_ROOT):
        for sub in ("drive-inbox", "Square Payment Archive"):
            p = root / sub / name
            if p.exists():
                return p
    return None


# =============================================================================
# WHY IT DIDN'T POST — plain-language translation for whoever posts it by hand
# =============================================================================

def _primary(reason):
    """The V2 attempt's message out of a chained failure reason.

    Reasons read "V2: ...; V2-retry: ...; V1: ..." — and the V1 leg almost always
    ends in "not found in autocomplete", which is the fallback path's own weaker
    lookup, not the real cause. Classifying on the whole string would tell staff
    "no TA client matched" for a client who is in TA and simply had no
    appointment that day. So classify on V2's message.
    """
    m = re.match(r"\s*V2:\s*(.*?)(?:;\s*V2-retry:|;\s*V1:|$)", reason or "", re.S)
    return m.group(1).strip() if m else (reason or "")


def explain(status, reason, name):
    """Translate a poller outcome into (why it didn't post, what to do about it).

    Deliberately staff-facing: no selectors, no file names, no stack traces. The
    raw reason still goes to the console for Travis.
    """
    r = _primary(reason).lower()

    if status == "SKIPPED_NO_NAME":
        return ("The Square payment has no customer attached, so the bot couldn't tell whose it was.",
                "Look the payment up in Square to identify the client, post it in TA, "
                "then attach the customer to the payment in Square.")
    if "not found in search results" in r or "not found in autocomplete" in r:
        return (f"No TherapyAppointment client matched the name Square has for them ({esc(name)}).",
                "Find the client in TA — it's usually a nickname, maiden name, or spelling "
                "difference — and post the payment. Reply to Travis with the correct TA name "
                "so the bot gets it right next time.")
    if "no appointment found" in r:
        return ("The client is in TA, but had no appointment on the date of the payment.",
                "Post the payment to the correct appointment, or to the client's open balance "
                "if there isn't one for that day.")
    if "multiple appointments" in r:
        return ("The client had more than one appointment that day, so the bot wouldn't guess which one.",
                "Pick the right appointment in TA and post the payment there.")
    if "multiple matches" in r:
        return ("More than one TA client matched that name.",
                "Confirm which client it is, then post the payment.")
    if "no active appointment" in r:
        return ("The client is in TA, but has no recent active appointment to attach the payment to.",
                "Check whether the appointment was cancelled or rescheduled, then post the payment to "
                "the right appointment or to the client's open balance.")
    if "no outstanding charges" in r or "prepayment" in r:
        return ("There was no outstanding charge to apply the payment to — often the session note "
                "hadn't been finalized yet.",
                "Check the client's balance in TA. Post it once the charge is there, or as a "
                "prepayment if that's what it really is.")
    if "intercepts pointer events" in r:
        return ("A popup covered the page while the bot was working.",
                "Post it in TA, and let Travis know — that one is a bot-side glitch.")
    if status == "GAP":
        return ("The bot never picked this payment up from Square.",
                "Post it in TA.")
    if status == "FLAGGED":
        return ("The bot flagged this one for a person to look at.",
                "Review it in TA and post the payment.")
    return ("The bot hit an error in TherapyAppointment and couldn't finish this one.",
            "Post it in TA.")


_LOG_REASONS = None


def reasons_from_logs():
    """Best-effort map of client name -> most recent failure reason from poller logs.

    Ledger entries only started carrying `reason` in Aug 2026, so older backlog
    rows have none. The poller log lines ("NOT POSTED <name>: <reason>") fill the
    gap. Matched by name, so it's the client's latest known failure reason — good
    enough to tell staff what to expect, not authoritative per payment.

    Cached: the poller log is multi-megabyte and every caller wants the same map.
    """
    global _LOG_REASONS
    if _LOG_REASONS is not None:
        return _LOG_REASONS
    out = {}
    pat = re.compile(r"NOT POSTED (.+?): (.+)$")
    for root in (bot.PROJECT_ROOT, LIVE_ROOT):
        for log in ("poll_live_stdout.log", "poll_shadow_stdout.log"):
            p = root / "logs" / log
            if not p.exists():
                continue
            try:
                for line in p.read_text(errors="replace").splitlines():
                    m = pat.search(line)
                    if m:
                        out[_norm(m.group(1))] = m.group(2).strip()
            except Exception:
                continue
    _LOG_REASONS = out
    return out


# =============================================================================
# CLEARED LIST — payments staff have confirmed they posted by hand
# =============================================================================

def key_items(items):
    """Stamp each outstanding payment with the identity used by the cleared list.

    The Square payment id when we have one: it doesn't change when the display
    name flips between the Square and CSV spellings, and it tells two identical
    payments apart (one client, one day, the same copay twice — clearing one of
    those must not silently drop the other). Payments the poller never saw have
    no id, so they fall back to date+name+amount plus an occurrence number.

    Numbered over the unfiltered list so a key stays stable as items clear.
    """
    seen = {}
    for i in items:
        if i.get("id"):
            i["clear_key"] = f"sq:{i['id']}"
            continue
        base = f"{i['date']}|{_norm(i['name'])}|{_amt(i['amount'])}"
        seen[base] = seen.get(base, 0) + 1
        i["clear_key"] = base if seen[base] == 1 else f"{base}#{seen[base]}"
    return items


def load_cleared():
    """The set of payment keys staff have confirmed they handled.

    A corrupt file is renamed rather than parsed as empty — silently starting
    over would resurrect the whole backlog and, worse, the next save would
    overwrite the only record of what had been cleared.
    """
    if not CLEARED_FILE.exists():
        return {"keys": []}
    try:
        d = json.loads(CLEARED_FILE.read_text())
        return {"keys": list(d.get("keys", []))}
    except Exception as e:
        bad = CLEARED_FILE.with_suffix(".json.corrupt")
        CLEARED_FILE.rename(bad)
        print(f"  reconcile: {CLEARED_FILE.name} is unreadable ({e}) — kept it as "
              f"{bad.name}; the outstanding list will be back to full until it's restored.")
        return {"keys": []}


def save_cleared(c):
    CLEARED_FILE.write_text(json.dumps({"keys": sorted(set(c["keys"]))}, indent=2))





# =============================================================================
# RECONCILIATION
# =============================================================================

def reconcile(csv_path):
    csv_payments = bot.read_csv(csv_path)            # [{name, date, amount}]
    txn_date = csv_payments[0]["date"] if csv_payments else date_from_csv_name(csv_path.name)
    ledger = load_ledger_for_date(txn_date) if txn_date else []

    led_by_name = {}
    for e in ledger:
        led_by_name.setdefault(_norm(e.get("name")), []).append(e)

    matched, gaps, errors, discrepancies = [], [], [], []
    used = set()
    for c in csv_payments:
        camt = _amt(c["amount"])
        cand = [e for e in led_by_name.get(_norm(c["name"]), []) if id(e) not in used]
        if not cand:
            gaps.append(dict(c))
            continue
        e = next((x for x in cand if _amt(x.get("amount")) == camt), cand[0])
        used.add(id(e))
        if e.get("status") not in POSTED_OK:
            # Nothing was posted, whatever the amounts say — this needs a human,
            # not a "the bot posted the wrong amount, go correct it" note.
            errors.append({**c, "status": e.get("status"), "reason": e.get("reason", ""),
                           "account": e.get("account", ""), "id": e.get("id")})
        elif _amt(e.get("amount")) != camt:
            discrepancies.append({**c, "ledger_amount": _amt(e.get("amount")), "status": e.get("status")})
        else:
            matched.append(c)

    # A CSV row lands in `gaps` whenever no ledger entry carries its name — but
    # that also happens when the poller DID handle the payment under a different
    # name (Square has the client as "William Stone IV", the CSV as "Cash Stone
    # IV") or under no name at all (no Square customer). Re-attach those by
    # date+amount, because "we tried and failed" is a different job for staff
    # than "we never saw it".
    #
    # Only when the pairing is unambiguous: exactly one unmatched gap and exactly
    # one unmatched failure at that date+amount. Two clients who each failed for
    # $25.00 that day stay separate rather than risk grafting one's reason,
    # account #, and alias onto the other's payment.
    leftover = [e for e in ledger if id(e) not in used and e.get("status") not in POSTED_OK]

    def _n(seq, date, amount):
        return sum(1 for x in seq if x.get("date") == date and _amt(x.get("amount")) == _amt(amount))

    for g in list(gaps):
        if _n(gaps, g["date"], g["amount"]) != 1 or _n(leftover, g["date"], g["amount"]) != 1:
            continue
        m = next(e for e in leftover
                 if e.get("date") == g["date"] and _amt(e.get("amount")) == _amt(g["amount"]))
        # One unclaimed CSV row and one unclaimed failure, same day, same cents:
        # almost always one payment under two spellings. Pair them rather than
        # print it twice — two blocks each saying "post it in TA" is how a client
        # gets charged twice. When the names don't look alike, pair it but say
        # the identification is uncertain.
        led_name = (m.get("name") or "").strip()
        uncertain = bool(led_name) and not _shares_name_token(led_name, g["name"])
        used.add(id(m))
        leftover.remove(m)
        gaps.remove(g)
        errors.append({**g, "status": m.get("status"), "reason": m.get("reason", ""),
                       "account": m.get("account", ""), "id": m.get("id"),
                       "square_name": m.get("name", ""), "uncertain": uncertain})

    # Anything the poller tried and failed that no CSV row claimed — a payment
    # missing from the export, a name too different to pair, an amount that
    # didn't line up. It is money that isn't in TA, so it belongs on the list
    # under whatever name Square had, rather than falling through the cracks.
    for e in ledger:
        if id(e) in used or e.get("status") in POSTED_OK or not e.get("status"):
            continue
        used.add(id(e))
        errors.append({"name": _clean(e.get("name")) or "(unknown client)",
                       "date": e.get("date") or txn_date, "amount": e.get("amount"),
                       "status": e.get("status"), "reason": e.get("reason", ""),
                       "account": e.get("account", ""), "id": e.get("id")})

    posted_ok = [e for e in ledger if e.get("status") in POSTED_OK]
    extras = [e for e in posted_ok if id(e) not in used]

    # Clients the poller handled but whose Square profile has no Account #
    # (reference_id). They had to match by name this time; adding the Account #
    # in Square lets future card payments match deterministically. Surfaced to
    # staff as an action item regardless of whether the reconciliation is clean.
    missing_account = [e for e in posted_ok if not (e.get("account") or "").strip()]

    return {
        "csv": csv_path.name, "txn_date": txn_date,
        "csv_count": len(csv_payments), "ledger_count": len(ledger),
        "matched": matched, "gaps": gaps, "errors": errors,
        "discrepancies": discrepancies, "extras": extras,
        "missing_account": missing_account,
    }


def reconcile_ledger_only(date_slashed):
    """The same result shape for a day whose Square report never arrived.

    Gaps can't be detected without the CSV — the poller's own record is all
    there is — but its failures are exactly the payments staff need to chase,
    and holding the whole email back over a missing export would hide them.
    """
    ledger = load_ledger_for_date(date_slashed)
    errors = [{"name": _clean(e.get("name")) or "(unknown client)",
               "date": e.get("date") or date_slashed, "amount": e.get("amount"),
               "status": e.get("status"), "reason": e.get("reason", ""),
               "account": e.get("account", ""), "id": e.get("id")}
              for e in ledger if e.get("status") and e.get("status") not in POSTED_OK]
    posted_ok = [e for e in ledger if e.get("status") in POSTED_OK]
    return {"csv": "(no Square report for this day)", "txn_date": date_slashed,
            "csv_count": 0, "ledger_count": len(ledger), "matched": [], "gaps": [],
            "errors": errors, "discrepancies": [], "extras": [],
            "missing_account": [e for e in posted_ok if not (e.get("account") or "").strip()],
            "no_csv": True}


def default_target_dates(now):
    """The days this morning's report should cover, as MM.DD.YYYY.

    Weekdays cover yesterday. Monday covers Friday, Saturday and Sunday in one
    email — the office isn't reading mail over the weekend, and three separate
    reports arriving Monday morning is three chances to work the wrong one.
    Saturday and Sunday send nothing; their payments wait for Monday.
    """
    wd = now.weekday()                      # Mon=0 ... Sun=6
    if wd >= 5:                             # Saturday, Sunday
        return []
    span = 3 if wd == 0 else 1              # Monday reaches back to Friday
    return [(now - timedelta(days=n)).strftime("%m.%d.%Y") for n in range(span, 0, -1)]


def result_for_date(date_dotted):
    """Reconcile one day, from its Square report if we have it, else the ledger."""
    csv_path = find_csv(date_dotted)
    if csv_path:
        return reconcile(csv_path)
    return reconcile_ledger_only(date_dotted.replace(".", "/"))


def _pretty(date_slashed):
    d = _dt(date_slashed)
    return f"{d.strftime('%a')} {d.strftime('%m/%d')}" if d else date_slashed


def combine(results):
    """Merge per-day reconciliations into one report payload.

    Concatenated rather than sectioned by day: the reader wants one to-do list,
    and a client who failed on both Friday and Sunday should appear once with
    two dated payments, not in two places. Every payment block already carries
    its own date.
    """
    dates = sorted((r["txn_date"] for r in results if r["txn_date"]),
                   key=lambda d: _dt(d) or datetime.min)
    out = {
        "csv": ", ".join(r["csv"] for r in results),
        "txn_date": dates[0] if len(dates) == 1 else f"{_pretty(dates[0])} – {_pretty(dates[-1])}"
                    if dates else "",
        "txn_dates": dates,
        "csv_count": sum(r["csv_count"] for r in results),
        "ledger_count": sum(r["ledger_count"] for r in results),
        "no_csv": all(r.get("no_csv") for r in results),
        "no_csv_dates": [r["txn_date"] for r in results if r.get("no_csv")],
    }
    for k in ("matched", "gaps", "errors", "discrepancies", "extras", "missing_account"):
        out[k] = [x for r in results for x in r[k]]
    return out


def unposted_for_day(date_dotted, log_reasons):
    """Every payment on one past day that never posted automatically.

    Uses the CSV when it's archived (catches payments the poller never saw) and
    always uses the ledger (catches the ones it tried and failed). Returns a list
    of dicts shaped like the daily action items.
    """
    date_slashed = date_dotted.replace(".", "/")
    csv_path = find_csv(date_dotted)
    items = []

    if csv_path:
        # reconcile() already folds in every ledger failure, matched or not, so
        # its gaps + errors are the complete picture for that day.
        r = reconcile(csv_path)
        for it in r["gaps"]:
            items.append({**it, "status": "GAP", "reason": "", "account": ""})
        for it in r["errors"]:
            items.append(dict(it))
    else:
        # No archived CSV for that day — the ledger is all we have, so gaps
        # (payments the poller never saw) can't be detected for it.
        for e in load_ledger_for_date(date_slashed):
            if e.get("status") in POSTED_OK or not e.get("status"):
                continue
            items.append({"name": e.get("name") or "(unknown client)",
                          "date": e.get("date") or date_slashed,
                          "amount": e.get("amount"), "status": e.get("status"),
                          "reason": e.get("reason", ""), "account": e.get("account", ""),
                          "id": e.get("id")})

    for i in items:
        if not i.get("reason") and i.get("status") != "GAP":
            i["reason"] = log_reasons.get(_norm(i.get("square_name") or i["name"]), "")
        i["name"] = _clean(i["name"])
    return items


def scan_backlog(before_date, days=BACKLOG_DAYS, cleared=None):
    """Payments from the `days` before `before_date` that still aren't posted.

    `before_date` is the day this report covers (MM/DD/YYYY) — that day's own
    misses are listed separately, so the backlog starts the day before it.
    """
    cleared = cleared or load_cleared()
    end = _dt(before_date)
    if not end:
        return []
    log_reasons = reasons_from_logs()
    out = []
    for n in range(1, days + 1):
        day = end - timedelta(days=n)
        try:
            items = unposted_for_day(day.strftime("%m.%d.%Y"), log_reasons)
        except Exception as e:
            # A single unreadable CSV or ledger costs that day, not the report.
            print(f"  reconcile: skipped {day.strftime('%m/%d/%Y')} in backlog scan — {e}")
            continue
        out.extend(items)
    out.sort(key=lambda i: (_dt(i["date"]) or datetime.min, i["name"]))
    key_items(out)
    return [i for i in out if i["clear_key"] not in cleared["keys"]]


# =============================================================================
# REPORT RENDERING
# =============================================================================

def action_items(r):
    """The day's payments that still need a human to post them in TA."""
    log_reasons = reasons_from_logs()
    items = []
    for g in r["gaps"]:
        items.append({**g, "status": "GAP", "reason": "", "account": ""})
    for e in r["errors"]:
        items.append(dict(e))
    for i in items:
        if not i.get("reason") and i.get("status") != "GAP":
            i["reason"] = log_reasons.get(_norm(i.get("square_name") or i["name"]), "")
        i["name"] = _clean(i["name"])
    items.sort(key=lambda i: i["name"])
    return items


def _total(items):
    total = 0.0
    for i in items:
        try:
            total += float(str(i["amount"]).replace("$", "").replace(",", ""))
        except (TypeError, ValueError):
            pass
    return total


def _group(items):
    """Group items by (client, why) so a repeat offender is one block, not five rows."""
    groups = OrderedDict()
    for i in items:
        why, todo = explain(i.get("status", ""), i.get("reason", ""), i.get("square_name") or i["name"])
        key = (i["name"], why)
        g = groups.setdefault(key, {"name": i["name"], "why": why, "todo": todo,
                                    "account": "", "square_name": "", "uncertain": False,
                                    "payments": []})
        g["uncertain"] = g["uncertain"] or bool(i.get("uncertain"))
        g["payments"].append(i)
        g["account"] = g["account"] or _account(i.get("account"))
        if i.get("square_name") and _norm(i["square_name"]) != _norm(i["name"]):
            g["square_name"] = i["square_name"]
    return list(groups.values())


def _blocks_html(items, accent="#c62828"):
    """Render grouped action items as checkable blocks (email-client safe).

    `accent` colours the left rule so today's list and the older backlog stay
    visually distinct at a glance.
    """
    out = []
    for g in _group(items):
        acct = (f'<span style="color:#666;font-weight:400;"> &middot; Account # '
                f'<span style="font-family:monospace;">{esc(g["account"])}</span></span>') if g["account"] else ""
        alias = (f'<span style="color:#666;font-weight:400;"> &middot; in Square as '
                 f'&ldquo;{esc(g["square_name"])}&rdquo;</span>') if g["square_name"] else ""
        caution = (
            f'<div style="font-size:13px;color:#e65100;margin-top:5px;">'
            f'<strong>Check before posting:</strong> the Square report calls this '
            f'&ldquo;{esc(g["name"])}&rdquo; and the bot&rsquo;s log calls it '
            f'&ldquo;{esc(g["square_name"])}&rdquo;. That is almost certainly one payment, not two — '
            f'look at both and post it once.</div>') if g["uncertain"] else ""
        rows = "".join(
            f'<div style="font-size:14px;color:#222;padding:3px 0 3px 4px;">'
            f'<span style="color:#888;">&#9744;</span> &nbsp;<strong>{esc(_money(p["amount"]))}</strong>'
            f'<span style="color:#555;"> &nbsp;paid {esc(p["date"])}</span></div>'
            for p in sorted(g["payments"], key=lambda p: _dt(p["date"]) or datetime.min)
        )
        out.append(
            f'<div style="border:1px solid #e0e0e0;border-left:4px solid {accent};'
            f'border-radius:3px;padding:12px 14px;margin:0 0 10px;">'
            f'<div style="font-size:15px;font-weight:700;color:#222;">{esc(g["name"])}{acct}{alias}</div>'
            f'<div style="margin:6px 0 8px;">{rows}</div>'
            f'<div style="font-size:13px;color:#555;"><strong>Why it didn\'t post:</strong> {g["why"]}</div>'
            f'<div style="font-size:13px;color:#1565c0;margin-top:3px;"><strong>What to do:</strong> {g["todo"]}</div>'
            f'{caution}</div>'
        )
    return "".join(out)


def _h2(text, color="#346756"):
    return (f'<h3 style="font-size:16px;color:{color};margin:26px 0 4px;'
            f'border-bottom:2px solid {color};padding-bottom:5px;">{text}</h3>')


HOW_TO_POST = (
    '<ol style="font-size:13px;color:#333;line-height:1.65;margin:8px 0 0;padding-left:20px;">'
    '<li>In TA, go to <strong>Clients</strong> and search for the client. The fastest way is to '
    'paste the <strong>Account #</strong> above into the Account Number field.</li>'
    '<li>Open the client, go to the <strong>Appointments</strong> tab, and click the appointment '
    'for the date the payment was made.</li>'
    '<li>Click <strong>Accept Payment</strong>.</li>'
    '<li>Enter the <strong>Payment Amount</strong> exactly as shown above. That figure is the base '
    'amount — the client&rsquo;s 3% card fee is already taken out, so don&rsquo;t add it back.</li>'
    '<li>Choose <strong>External Credit Card</strong> as the method, and type <strong>Square</strong> '
    'in the Reference / Check # box.</li>'
    '<li>Click <strong>Continue</strong>, then <strong>Save Payment</strong>.</li>'
    '<li>If the client has no appointment on that date, post it against their open balance instead '
    '(<strong>Billing &rarr; Take Payment</strong>) and reply to Travis so the appointment can be checked.</li>'
    '</ol>'
)


def build_report_html(r, heals=(), backlog=(), days=BACKLOG_DAYS, backlog_failed=False):
    """Build (subject, html, clean) for the morning report. Pure — no send."""
    today_items = action_items(r)
    backlog = list(backlog)
    verify = [("Amount doesn't match",
               f'{esc(d["name"])} — Square says {_money(d["amount"])}, the bot posted '
               f'{_money(d["ledger_amount"])}. Check the client&rsquo;s ledger in TA and correct it.')
              for d in r["discrepancies"]]
    verify += [("Posted, but not on the Square report",
                f'{esc(x.get("name"))} — {_money(x.get("amount"))} on {esc(x.get("date"))}. The bot posted this '
                f'in TA but it isn&rsquo;t on that day&rsquo;s Square report. Usually it just landed on the next '
                f'day&rsquo;s report — check Square, and remove it in TA only if it isn&rsquo;t a real payment.')
               for x in r["extras"]]
    clean = not (r["gaps"] or r["errors"] or r["discrepancies"] or r["extras"])
    date = r["txn_date"] or r["csv"]

    span = r.get("txn_dates") or []
    if len(span) > 1:
        first, last = (_dt(span[0]), _dt(span[-1]))
        covers = (f"{first.strftime('%A')} through {last.strftime('%A')}&rsquo;s"
                  if first and last else "These days&rsquo;")
    else:
        covers = "Yesterday&rsquo;s"

    bits = []
    if today_items:
        bits.append(f"{len(today_items)} to post")
    if backlog:
        bits.append(f"{len(backlog)} outstanding")
    if verify and not today_items:
        bits.append(f"{len(verify)} to verify")
    subject = f"PostIQ Daily Reconcile — {date} — " + (" · ".join(bits) if bits else "all caught up")

    # ── Headline ──
    if today_items:
        n = len(today_items)
        reports = "these days&rsquo; Square reports" if len(span) > 1 else "this day&rsquo;s Square report"
        if r["csv_count"]:
            sub = (f'The other {len(r["matched"])} of {r["csv_count"]} payments on {reports} '
                   f'posted automatically — nothing to do for those.')
        else:
            # No usable export, so this list is the bot's own record and can only
            # show what it tried — not a payment it never saw.
            sub = ('This day&rsquo;s Square report ' +
                   ('didn&rsquo;t arrive' if r.get("no_csv") else 'was empty') +
                   ', so this list is the bot&rsquo;s own record and may not be the whole story. '
                   'Tell Travis if you expected more.')
        headline = (
            f'<div style="background:#fdecea;border-left:5px solid #c62828;padding:14px 16px;margin:16px 0;">'
            f'<div style="font-size:17px;font-weight:700;color:#c62828;">'
            f'{n} payment{"s" if n != 1 else ""} ({_money(_total(today_items))}) '
            f'{"need" if n != 1 else "needs"} to be posted by hand in TherapyAppointment</div>'
            f'<div style="font-size:13px;color:#555;margin-top:5px;">{sub}</div></div>')
    elif r["csv_count"] == 0:
        what = ("didn&rsquo;t arrive" if r.get("no_csv") else "came through with no payments on it")
        headline = (
            f'<div style="background:#fff8e1;border-left:5px solid #e65100;padding:14px 16px;margin:16px 0;">'
            f'<div style="font-size:17px;font-weight:700;color:#e65100;">'
            f'This day&rsquo;s Square report {what}.</div>'
            f'<div style="font-size:13px;color:#555;margin-top:5px;">Nothing failed in the bot&rsquo;s '
            f'own record either, but it couldn&rsquo;t be checked against Square. If the practice took '
            f'card payments that day, tell Travis before assuming there&rsquo;s nothing to do.</div></div>')
    elif verify:
        headline = (
            f'<div style="background:#fff8e1;border-left:5px solid #6a1b9a;padding:14px 16px;margin:16px 0;">'
            f'<div style="font-size:17px;font-weight:700;color:#6a1b9a;">'
            f'Nothing new to post, but {len(verify)} payment'
            f'{"s" if len(verify) != 1 else ""} to check.</div>'
            f'<div style="font-size:13px;color:#555;margin-top:5px;">The rest of this day&rsquo;s '
            f'Square report posted automatically. See &ldquo;Check these&rdquo; below.</div></div>')
    else:
        headline = (
            f'<div style="background:#e8f5e9;border-left:5px solid #2e7d32;padding:14px 16px;margin:16px 0;">'
            f'<div style="font-size:17px;font-weight:700;color:#2e7d32;">'
            f'Nothing new to post — all {r["csv_count"]} payment'
            f'{"s" if r["csv_count"] != 1 else ""} on this day&rsquo;s Square report went into TA automatically.'
            f'</div></div>')

    parts = [headline]
    partial = [d for d in (r.get("no_csv_dates") or []) if not r.get("no_csv")]
    if partial:
        parts.append(
            f'<p style="background:#fff8e1;border-left:5px solid #e65100;padding:12px 14px;'
            f'margin:14px 0;font-size:13px;color:#555;">No Square report arrived for '
            f'{esc(", ".join(partial))}, so {"that day is" if len(partial) == 1 else "those days are"} '
            f'covered from the bot&rsquo;s own record only — a payment it never saw wouldn&rsquo;t '
            f'show up here. Travis has been told.</p>')

    # ── Today's manual postings ──
    if today_items:
        parts.append(_h2(f"Post these in TA — {date}", "#c62828"))
        parts.append(_blocks_html(today_items))

    # ── Rolling backlog ──
    if backlog_failed:
        parts.append(
            '<p style="background:#fff8e1;border-left:5px solid #e65100;padding:12px 14px;'
            'margin:20px 0;font-size:13px;color:#555;"><strong>The list of older outstanding '
            'payments couldn&rsquo;t be built this morning</strong>, so it isn&rsquo;t below — that '
            'does not mean there are none. Travis has been told.</p>')
    if backlog:
        parts.append(_h2(f"Still outstanding — last {days} days "
                         f"({len(backlog)} payments, {_money(_total(backlog))})", "#e65100"))
        parts.append(
            '<p style="font-size:13px;color:#555;margin:8px 0 12px;">These never posted automatically. '
            '<strong>Please check each one in TA.</strong> If it was already posted by hand, there&rsquo;s '
            'nothing to do — if not, post it using the steps below. When you&rsquo;ve worked through them, '
            'reply to this email and Travis will clear them off the list so they stop appearing.</p>')
        parts.append(_blocks_html(backlog, accent="#e65100"))

    # ── How to post ──
    if today_items or backlog:
        parts.append(_h2("How to post one of these in TA", "#1565c0"))
        parts.append(HOW_TO_POST)

    # ── Verify, don't post ──
    if verify:
        parts.append(_h2("Check these — don't post them", "#6a1b9a"))
        parts.append('<ul style="font-size:13px;color:#333;line-height:1.6;margin:8px 0 0;">'
                     + "".join(f'<li><strong>{esc(t)}:</strong> {body}</li>' for t, body in verify)
                     + '</ul>')

    # ── Housekeeping (admin) ──
    house = []
    if r.get("missing_account"):
        house.append(
            '<p style="font-size:13px;color:#333;margin:8px 0 4px;"><strong>Missing Square Account #</strong> — '
            'these clients posted fine, but Square has no Account # on their profile, so the bot had to match '
            'them by name. Adding it makes future payments match exactly:</p>'
            '<ul style="font-size:13px;color:#555;margin:0;">'
            + "".join(f'<li>{esc(m.get("name"))} — {_money(m.get("amount"))} on {esc(m.get("date"))}</li>'
                      for m in r["missing_account"]) + '</ul>')
    if heals:
        house.append(
            '<p style="font-size:13px;color:#333;margin:12px 0 4px;"><strong>Account #s the bot corrected '
            'in Square</strong> — already applied, just confirm they look right:</p>'
            '<ul style="font-size:13px;color:#555;margin:0;">'
            + "".join(f'<li>{esc(h.get("name"))} — {esc(str(h.get("before")))} &rarr; '
                      f'{esc(str(h.get("after")))}</li>' for h in heals) + '</ul>')
    if house:
        parts.append(_h2("Square housekeeping — no TA posting needed", "#1565c0"))
        parts.extend(house)

    html = (
        f'<html><body style="margin:0;padding:0;background:#f4f4f4;">'
        f'<div style="max-width:720px;margin:0 auto;background:#fff;padding:24px 28px;'
        f'font-family:Arial,Helvetica,sans-serif;color:#333;">'
        f'<h2 style="color:#346756;margin:0;">PostIQ Daily Reconcile — {esc(date)}</h2>'
        f'<p style="color:#666;font-size:13px;margin:4px 0 0;">{covers} Square card payments '
        f'vs. what the bot actually posted in TherapyAppointment.</p>'
        f'{"".join(parts)}'
        f'<p style="color:#999;font-size:12px;margin-top:28px;border-top:1px solid #eee;padding-top:12px;">'
        f'The bot posts Square card payments into TA automatically throughout the day. Each morning this '
        f'email checks the full Square daily report against what it actually posted, and lists anything '
        f'left for a person. Source: {esc(r["csv"])} · {r["csv_count"]} payments on the Square report · '
        f'{r["ledger_count"]} handled by the bot · {len(r["matched"])} matched. '
        f'Outstanding-list gap detection needs the archived Square report for a day; where it&rsquo;s '
        f'missing, only the bot&rsquo;s own failures are listed.<br>'
        f'Oakley, Great Oak Counseling&rsquo;s AI Assistant</p>'
        f'</div></body></html>')
    return subject, html, clean


def print_report(r, heals=(), backlog=(), days=BACKLOG_DAYS):
    """Console view — same content, plus the raw reasons and clear-keys for Travis."""
    clean = not (r["gaps"] or r["errors"] or r["discrepancies"] or r["extras"])
    print(f"=== Reconciliation: {r['csv']} (txn {r['txn_date']}) ===")
    print(f"CSV: {r['csv_count']} | ledger: {r['ledger_count']} | matched: {len(r['matched'])}")
    if clean:
        print("RESULT: CLEAN — every CSV payment is in the poller ledger, amounts match, no extras.")
    else:
        print("RESULT: EXCEPTIONS")
        for g in r["gaps"]:
            print(f"  GAP         {g['name']} ${g['amount']} on {g['date']} — poller never posted (manual)")
        for e in r["errors"]:
            print(f"  ERROR       {e['name']} ${e['amount']} on {e['date']} — poller status {e['status']}"
                  f"{' — ' + e['reason'] if e.get('reason') else ''} (manual)")
        for d in r["discrepancies"]:
            print(f"  DISCREPANCY {d['name']} CSV ${d['amount']} vs ledger ${d['ledger_amount']} (staff)")
        for x in r["extras"]:
            print(f"  EXTRA       {x.get('name')} ${x.get('amount')} on {x.get('date')} — poller posted, not in CSV (staff)")
    for m in r.get("missing_account", []):
        print(f"  NEEDS SQUARE ACCT#  {m.get('name')} ${m.get('amount')} on {m.get('date')} "
              f"(sq:{m.get('id')}) — add Account # in Square so it auto-matches next time")
    for h in heals:
        print(f"  SELF-HEALED  {h.get('name')} {h.get('before')} -> {h.get('after')} "
              f"— auto-corrected in Square (please confirm)")
    if backlog:
        print(f"\n--- Still outstanding, last {days} days ({len(backlog)} payments, "
              f"{_money(_total(backlog))}) ---")
        for b in backlog:
            print(f"  {b['date']}  {b['name']:<26} ${_amt(b['amount']):>8}  {b.get('status','')}"
                  f"{' — ' + b['reason'] if b.get('reason') else ''}")
            print(f"      clear key: {b['clear_key']}")
    return clean


def email_report(r, heals=(), backlog=(), days=BACKLOG_DAYS, backlog_failed=False):
    """Email the morning report to staff (Hannah) with Travis copied.

    Returns True only if it actually went out — the caller uses that to decide
    whether the self-heal confirmations can be retired.
    """
    subject, html, clean = build_report_html(r, heals, backlog, days, backlog_failed)
    sent = bot.send_email(to=STAFF_TO, cc=ADMIN_TO, subject=subject, body=html, html=True)
    if sent:
        print(f"  Emailed report to {STAFF_TO} (cc {ADMIN_TO}) — {'CLEAN' if clean else 'EXCEPTIONS'}")
    else:
        print(f"  Report NOT sent to {STAFF_TO} — see the email error above.")
    return sent


def main():
    ap = argparse.ArgumentParser(description="Daily poller-vs-CSV reconciliation + manual-posting report.")
    ap.add_argument("--csv", help="Path to the day's CSV.")
    ap.add_argument("--date", help="MM.DD.YYYY — find the CSV by date.")
    ap.add_argument("--email", action="store_true",
                    help="Email the report to staff. With no --date/--csv this covers yesterday, "
                         "or Friday+Saturday+Sunday when run on a Monday, and nothing on a weekend.")
    ap.add_argument("--backlog-days", type=int, default=BACKLOG_DAYS,
                    help=f"Look-back window for still-outstanding payments (default {BACKLOG_DAYS}).")
    ap.add_argument("--no-backlog", action="store_true", help="Skip the outstanding-payments scan.")
    ap.add_argument("--clear", action="append", default=[], metavar="KEY",
                    help="Mark one outstanding payment as handled (the clear key printed "
                         "beside it in the console report).")
    ap.add_argument("--clear-through", metavar="MM.DD.YYYY",
                    help="Mark everything on or before this date as handled.")
    args = ap.parse_args()

    # --clear / --clear-through are maintenance actions; do them and stop.
    if args.clear or args.clear_through:
        # Validate before writing anything, so a typo can't half-apply the batch.
        through = None
        if args.clear_through:
            through = _dt(args.clear_through)
            if not through:
                print(f"reconcile: --clear-through expects MM.DD.YYYY, got {args.clear_through}")
                sys.exit(1)
        c = load_cleared()
        for k in args.clear:
            if k not in c["keys"]:
                c["keys"].append(k)
                print(f"  cleared: {k}")
        if through:
            # Expand to the keys that are actually outstanding today rather than
            # keeping a date watermark. A watermark would also swallow anything
            # discovered later on those dates — exactly what happens when a late
            # Square report is archived and reveals a payment nobody has seen.
            # Anchored on today with a little slack: the morning report anchors
            # on yesterday, so its window reaches one day further back than a
            # today-anchored scan of the same length would.
            today = datetime.now().strftime("%m/%d/%Y")
            n = 0
            for it in scan_backlog(today, args.backlog_days + 2, cleared=c):
                if (_dt(it["date"]) or datetime.max) <= through:
                    c["keys"].append(it["clear_key"])
                    n += 1
            print(f"  cleared {n} outstanding payment(s) dated on or before "
                  f"{through.strftime('%m/%d/%Y')}")
        save_cleared(c)
        sys.exit(0)

    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            print(f"reconcile: no such CSV: {args.csv}")
            sys.exit(1)
        results = [reconcile(csv_path)]
    elif args.date:
        results = [result_for_date(args.date)]
    else:
        targets = default_target_dates(datetime.now())
        if not targets:
            print("reconcile: Saturday/Sunday — no report today; these payments go out "
                  "in Monday's email with Friday's.")
            sys.exit(0)
        print(f"reconcile: covering {', '.join(targets)}")
        results = [result_for_date(d) for d in targets]

    all_heals, unreported_heals = load_unreported_heals()
    r = combine(results)

    # A day whose Square report never arrived is still reported — from the
    # ledger — because this is the only daily staff email and its outstanding
    # list doesn't need the CSV. Travis hears about the missing export.
    missing = r.get("no_csv_dates") or []
    if missing:
        print(f"reconcile: no Square CSV for {', '.join(missing)} — using the poller ledger for "
              f"{'that day' if len(missing) == 1 else 'those days'}.")
        if args.email:
            try:
                bot.send_email(to=ADMIN_TO, cc=None,
                               subject=f"PostIQ — no Square report for {', '.join(missing)}",
                               body=f"No Square CSV was found for {', '.join(missing)}, so this "
                                    f"morning's staff report covers "
                                    f"{'that day' if len(missing) == 1 else 'those days'} from the "
                                    f"poller ledger alone and can't flag payments the poller never "
                                    f"saw. Check the S3 sync.",
                               html=False)
            except Exception:
                pass
    backlog, backlog_failed = [], False
    if not args.no_backlog:
        try:
            anchor = (r.get("txn_dates") or [r["txn_date"]])[0]
            backlog = scan_backlog(anchor, args.backlog_days)
        except Exception as e:
            backlog_failed = True
            print(f"  reconcile: backlog scan failed ({e}) — sending the day's report without it.")
    clean = print_report(r, unreported_heals, backlog, args.backlog_days)
    if args.email:
        sent = email_report(r, unreported_heals, backlog, args.backlog_days, backlog_failed)
        if sent and unreported_heals:
            mark_heals_reported(all_heals)  # exactly-once: don't re-report tomorrow
        if not sent:
            # This is the only daily staff email; a silent failure means nobody
            # is told about money that still needs posting.
            sys.exit(1)
    sys.exit(0 if clean else 2)


if __name__ == "__main__":
    main()
