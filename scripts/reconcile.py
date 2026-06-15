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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot_v2 as bot  # noqa: E402

LEDGER_DIR = bot.DATA_DIR / "poll_ledger"
POSTED_OK = ("OK", "WOULD_POST")


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
    for d in (bot.PROJECT_ROOT / "drive-inbox", bot.PROJECT_ROOT / "Square Payment Archive"):
        if (d / name).exists():
            return d / name
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

    return {
        "csv": csv_path.name, "txn_date": txn_date,
        "csv_count": len(csv_payments), "ledger_count": len(ledger),
        "matched": matched, "gaps": gaps, "errors": errors,
        "discrepancies": discrepancies, "extras": extras,
    }


def print_report(r):
    clean = not (r["gaps"] or r["errors"] or r["discrepancies"] or r["extras"])
    print(f"=== Reconciliation: {r['csv']} (txn {r['txn_date']}) ===")
    print(f"CSV: {r['csv_count']} | ledger: {r['ledger_count']} | matched: {len(r['matched'])}")
    if clean:
        print("RESULT: CLEAN — every CSV payment is in the poller ledger, amounts match, no extras.")
        return True
    print("RESULT: EXCEPTIONS")
    for g in r["gaps"]:
        print(f"  GAP         {g['name']} ${g['amount']} on {g['date']} — poller never posted (auto-fill candidate)")
    for e in r["errors"]:
        print(f"  ERROR       {e['name']} ${e['amount']} on {e['date']} — poller status {e['status']} (staff)")
    for d in r["discrepancies"]:
        print(f"  DISCREPANCY {d['name']} CSV ${d['amount']} vs ledger ${d['ledger_amount']} (staff)")
    for x in r["extras"]:
        print(f"  EXTRA       {x.get('name')} ${x.get('amount')} on {x.get('date')} — poller posted, not in CSV (staff)")
    return False


def main():
    ap = argparse.ArgumentParser(description="Daily poller-vs-CSV reconciliation (dev-test).")
    ap.add_argument("--csv", help="Path to the day's CSV.")
    ap.add_argument("--date", help="MM.DD.YYYY — find the CSV by date instead.")
    args = ap.parse_args()
    csv_path = Path(args.csv) if args.csv else (find_csv(args.date) if args.date else None)
    if not csv_path or not csv_path.exists():
        ap.error("provide a valid --csv path or --date MM.DD.YYYY")
    sys.exit(0 if print_report(reconcile(csv_path)) else 2)


if __name__ == "__main__":
    main()
