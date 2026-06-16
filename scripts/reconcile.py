#!/usr/bin/env python3
"""
reconcile.py — daily poller-vs-CSV reconciliation (dev-test).

Compares the poller's per-day ledger (data/poll_ledger/MMDDYYYY.json — what it
posted, or in shadow mode would post) against the FULL daily CSV (the
authoritative list of that day's Square payments) and reports any discrepancy.
If everything lines up, the result is CLEAN.

Each CSV payment is classified:
  MATCHED      poller posted it (or, in shadow, would post it) with matching amount
  GAP          in the CSV, poller never handled it — a true miss (auto-fill candidate)
  ERROR        poller tried but failed/skipped (no name, client not found, ...) — staff
  DISCREPANCY  name matches but the amount differs — staff
Plus:
  EXTRA        poller (would-)posted something not in the CSV — staff

Report-only for now (auto-fill of clean GAPs is a later, deliberate step). During
the shadow proving window, a CLEAN result across several days means the poller's
detection matches the CSV and it's safe to cut over.

Usage:
  python3 scripts/reconcile.py --csv "Square Payment Archive/06.13.2026_Daily.Square.Log.csv"
  python3 scripts/reconcile.py --date 06.13.2026
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot_v2 as bot  # noqa: E402

LEDGER_DIR = bot.DATA_DIR / "poll_ledger"
POSTED_OK = ("OK", "WOULD_POST")
# CSVs land in the live repo; the ledger lives wherever this runs (dev worktree
# during the shadow window). Search both so reconcile works from either tree.
LIVE_ROOT = Path("/Users/travmegsam/Developer/postiq")
RECONCILE_TO = "travis@greatoakcounseling.com"


def _norm(name):
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _amt(a):
    try:
        return f"{float(str(a).replace('$', '').replace(',', '')):.2f}"
    except Exception:
        return str(a)


def load_ledger_for_date(mmddyyyy):
    key = mmddyyyy.replace("/", "").replace(".", "")
    path = LEDGER_DIR / f"{key}.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def find_csv(date_dotted):
    name = f"{date_dotted}_Daily.Square.Log.csv"
    for root in (bot.PROJECT_ROOT, LIVE_ROOT):
        for sub in ("drive-inbox", "Square Payment Archive"):
            p = root / sub / name
            if p.exists():
                return p
    return None


def reconcile(csv_path):
    csv_payments = bot.read_csv(csv_path)            # [{name, date, amount}]
    txn_date = csv_payments[0]["date"] if csv_payments else ""
    ledger = load_ledger_for_date(txn_date) if txn_date else []

    led_by_name = defaultdict(list)
    for e in ledger:
        led_by_name[_norm(e.get("name"))].append(e)

    matched, gaps, errors, discrepancies = [], [], [], []
    used = set()
    for c in csv_payments:
        camt = _amt(c["amount"])
        cand = [e for e in led_by_name.get(_norm(c["name"]), []) if id(e) not in used]
        if not cand:
            gaps.append(c)
            continue
        e = next((x for x in cand if _amt(x.get("amount")) == camt), cand[0])
        used.add(id(e))
        if _amt(e.get("amount")) != camt:
            discrepancies.append({**c, "ledger_amount": _amt(e.get("amount")), "status": e.get("status")})
        elif e.get("status") in POSTED_OK:
            matched.append(c)
        else:
            errors.append({**c, "status": e.get("status")})

    extras = [e for e in ledger if id(e) not in used and e.get("status") in POSTED_OK]

    # Clients the poller handled but whose Square profile has no Account #
    # (reference_id). They had to match by name this time; adding the Account #
    # in Square lets future card payments match deterministically. Surfaced to
    # staff as an action item regardless of whether the reconciliation is clean.
    missing_account = [
        e for e in ledger
        if e.get("status") in POSTED_OK and not (e.get("account") or "").strip()
    ]

    return {
        "csv": csv_path.name, "txn_date": txn_date,
        "csv_count": len(csv_payments), "ledger_count": len(ledger),
        "matched": matched, "gaps": gaps, "errors": errors,
        "discrepancies": discrepancies, "extras": extras,
        "missing_account": missing_account,
    }


def print_report(r):
    clean = not (r["gaps"] or r["errors"] or r["discrepancies"] or r["extras"])
    print(f"=== Reconciliation: {r['csv']} (txn {r['txn_date']}) ===")
    print(f"CSV: {r['csv_count']} | ledger: {r['ledger_count']} | matched: {len(r['matched'])}")
    if clean:
        print("RESULT: CLEAN — every CSV payment is in the poller ledger, amounts match, no extras.")
    else:
        print("RESULT: EXCEPTIONS")
        for g in r["gaps"]:
            print(f"  GAP         {g['name']} ${g['amount']} on {g['date']} — poller never posted (auto-fill candidate)")
        for e in r["errors"]:
            print(f"  ERROR       {e['name']} ${e['amount']} on {e['date']} — poller status {e['status']} (staff)")
        for d in r["discrepancies"]:
            print(f"  DISCREPANCY {d['name']} CSV ${d['amount']} vs ledger ${d['ledger_amount']} (staff)")
        for x in r["extras"]:
            print(f"  EXTRA       {x.get('name')} ${x.get('amount')} on {x.get('date')} — poller posted, not in CSV (staff)")
    # Maintenance action item — independent of CLEAN/EXCEPTIONS, doesn't flip the result.
    for m in r.get("missing_account", []):
        print(f"  NEEDS SQUARE ACCT#  {m.get('name')} ${m.get('amount')} on {m.get('date')} "
              f"(sq:{m.get('id')}) — add Account # in Square so it auto-matches next time")
    return clean


def build_report_html(r):
    """Build (subject, html, clean) for the reconcile report. Pure — no send."""
    clean = not (r["gaps"] or r["errors"] or r["discrepancies"] or r["extras"])
    status = "CLEAN" if clean else "EXCEPTIONS"
    subject = f"PostIQ Shadow Reconcile — {r['txn_date'] or r['csv']} — {status}"
    parts = []

    def sect(title, items, fmt, color):
        if not items:
            return
        lis = "".join(f"<li>{fmt(it)}</li>" for it in items)
        parts.append(f'<p style="color:{color};font-weight:700;margin:12px 0 2px;">{title} '
                     f'({len(items)})</p><ul style="margin:0;font-size:13px;color:#333;">{lis}</ul>')

    if clean:
        headline = (f'<p style="font-size:16px;color:#2e7d32;font-weight:700;">CLEAN — all '
                    f'{r["csv_count"]} CSV payment(s) matched the poller ledger.</p>')
    else:
        headline = '<p style="font-size:15px;color:#c62828;font-weight:700;">EXCEPTIONS — review below.</p>'
        sect("Gaps — poller missed (auto-fill candidates)", r["gaps"],
             lambda g: f"{g['name']} — ${g['amount']} on {g['date']}", "#e65100")
        sect("Errors — poller failed/skipped (staff)", r["errors"],
             lambda e: f"{e['name']} — ${e['amount']} ({e['status']})", "#c62828")
        sect("Discrepancies — amount mismatch (staff)", r["discrepancies"],
             lambda d: f"{d['name']} — CSV ${d['amount']} vs ledger ${d['ledger_amount']}", "#d84315")
        sect("Extras — poller posted, not in CSV (staff)", r["extras"],
             lambda x: f"{x.get('name')} — ${x.get('amount')} on {x.get('date')}", "#6a1b9a")

    # Maintenance section — rendered whether clean or not. These clients need a
    # one-time Account # added in Square so future card payments match by
    # Account # instead of by name.
    if r.get("missing_account"):
        sect("Missing Square Account # — add in Square so future payments auto-match (staff action)",
             r["missing_account"],
             lambda m: f"{m.get('name')} — ${m.get('amount')} on {m.get('date')} (Square payment {m.get('id')})",
             "#1565c0")
        subject += f" · {len(r['missing_account'])} need Acct#"

    html = (f'<html><body style="font-family:Arial,sans-serif;color:#333;">'
            f'<h2 style="color:#346756;">Shadow Reconcile — {r["txn_date"]}</h2>'
            f'<p style="color:#666;font-size:13px;">{r["csv"]} · {r["csv_count"]} CSV payment(s) · '
            f'{r["ledger_count"]} ledger · {len(r["matched"])} matched</p>{headline}{"".join(parts)}'
            f'<p style="color:#999;font-size:12px;margin-top:20px;">Poller proving window — shadow mode, '
            f'the poller posted nothing. This reconciles its would-post ledger against the day’s CSV.</p>'
            f'</body></html>')
    return subject, html, clean


def email_report(r):
    """Email the reconciliation outcome to RECONCILE_TO (proving-window report)."""
    subject, html, clean = build_report_html(r)
    bot.send_email(to=RECONCILE_TO, cc=None, subject=subject, body=html, html=True)
    print(f"  Emailed reconcile report to {RECONCILE_TO} ({'CLEAN' if clean else 'EXCEPTIONS'})")


def main():
    ap = argparse.ArgumentParser(description="Daily poller-vs-CSV reconciliation (dev-test).")
    ap.add_argument("--csv", help="Path to the day's CSV.")
    ap.add_argument("--date", help="MM.DD.YYYY — find the CSV by date.")
    ap.add_argument("--email", action="store_true", help="Email the result to Travis.")
    args = ap.parse_args()

    if args.csv:
        csv_path = Path(args.csv)
    else:
        # Default to yesterday (what the morning launchd job reconciles).
        date_dotted = args.date or (datetime.now() - timedelta(days=1)).strftime("%m.%d.%Y")
        csv_path = find_csv(date_dotted)

    if not csv_path or not csv_path.exists():
        target = args.csv or args.date or "yesterday"
        print(f"reconcile: CSV not found for {target}")
        if args.email:
            try:
                bot.send_email(to=RECONCILE_TO, cc=None,
                               subject=f"PostIQ Shadow Reconcile — {target} — CSV NOT FOUND",
                               body=f"No CSV found to reconcile for {target}.", html=False)
            except Exception:
                pass
        sys.exit(1)

    r = reconcile(csv_path)
    clean = print_report(r)
    if args.email:
        email_report(r)
    sys.exit(0 if clean else 2)


if __name__ == "__main__":
    main()
