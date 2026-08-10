#!/usr/bin/env python3
"""check_posted.py — read-only spot-check (posts NOTHING).

For each (name, amount, date) row, log into TA, open the client's Billing
ledger, and look for a Client Payment matching the amount within ~3 weeks of the
date. Reports POSTED? per client.

Two sources:

  --from-outstanding   the morning report's own outstanding list. Answers "did
                       staff already post these by hand?" with evidence instead
                       of a reply, and prints the reconcile --clear command for
                       everything it finds. This is the one to use.
  --file PATH          a tab-separated name/amount/date list (default
                       /tmp/sample.tsv), for ad-hoc checks.

Slow — a browser round trip per client, so a full outstanding list takes a
while. It posts nothing and changes nothing.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from playwright.sync_api import sync_playwright  # noqa: E402
import bot_v2 as bot  # noqa: E402
import reconcile as rec  # noqa: E402

WINDOW_DAYS = 21


def parse_date(s):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None


def load_samples(path):
    out = []
    with open(path) as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 3 and not p[0].lower().startswith("unknown"):
                out.append((p[0], p[1].replace("$", "").replace(",", ""), p[2], ""))
    return out


def load_outstanding(days):
    """The morning report's outstanding list: (name, amount, date, clear_key)."""
    anchor = datetime.now().strftime("%m/%d/%Y")
    hist = rec.scan_history(anchor, days)
    return [(i["name"], rec._amt(i["amount"]), i["date"], i["clear_key"])
            for i in hist["unposted"]
            if not i["name"].lower().startswith("(unknown")]


def scrape_client_payments(page):
    return page.evaluate(
        """() => {
            const out=[];
            for (const tr of document.querySelectorAll('tr')) {
                const t=(tr.textContent||'').replace(/\\s+/g,' ').trim();
                if(!/client payment/i.test(t)) continue;
                const dm=t.match(/(\\d{1,2}\\/\\d{1,2}\\/\\d{2,4})/);
                const am=[...t.matchAll(/([\\d,]+\\.\\d{2})/g)].map(m=>m[1].replace(/,/g,''));
                out.push({date:dm?dm[1]:'', amounts:am});
            }
            return out;
        }"""
    )


def check(page, name, amt, date):
    bot.search_client(page, name)
    bot.dismiss_popups(page)
    # Click the CLIENT's Billing TAB (a.v-tab; its href is URL-encoded so a plain
    # href*='billing/account' won't match). It bounces to the billing ledger page.
    page.click("a.v-tab:has-text('Billing')")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2800)
    rows = scrape_client_payments(page)
    target_dt = parse_date(date)
    target_amt = f"{float(amt):.2f}"
    near = []
    same_amt = 0
    for r in rows:
        # A payment row carries several figures (amount, charge, running
        # balance); match on any of them rather than whichever came first.
        if target_amt not in r["amounts"]:
            continue
        same_amt += 1
        rdt = parse_date(r["date"])
        if rdt and target_dt and abs((rdt - target_dt).days) <= WINDOW_DAYS:
            near.append(r)
    return len(rows), same_amt, near


def main():
    ap = argparse.ArgumentParser(description="Read-only: is this payment already in TA? Posts nothing.")
    ap.add_argument("--from-outstanding", action="store_true",
                    help="Check the morning report's outstanding list.")
    ap.add_argument("--days", type=int, default=rec.BACKLOG_DAYS,
                    help=f"Look-back for --from-outstanding (default {rec.BACKLOG_DAYS}).")
    ap.add_argument("--file", default="/tmp/sample.tsv",
                    help="Tab-separated name/amount/date list (default /tmp/sample.tsv).")
    ap.add_argument("--limit", type=int, help="Stop after this many clients.")
    args = ap.parse_args()

    samples = load_outstanding(args.days) if args.from_outstanding else load_samples(args.file)
    if args.limit:
        samples = samples[:args.limit]
    if not samples:
        print("Nothing to check.")
        return
    print(f"Checking {len(samples)} client(s) in TA (read-only, posts nothing)...\n")

    posted = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=bot.HEADLESS)
        page = browser.new_page()
        page.set_default_timeout(bot.ACTION_TIMEOUT)
        try:
            bot.login(page)
            for name, amt, date, key in samples:
                try:
                    total, same_amt, near = check(page, name, amt, date)
                    if near:
                        m = near[0]
                        verdict = f"LIKELY POSTED — ${amt} Client Payment on {m['date']} (within {WINDOW_DAYS}d)"
                        if key:
                            posted.append((key, name, amt, date))
                    elif same_amt:
                        verdict = f"UNCERTAIN — has {same_amt} ${amt} payment(s) but none within {WINDOW_DAYS}d of {date}"
                    else:
                        verdict = f"NOT FOUND — no ${amt} Client Payment in ledger ({total} payments total)"
                    print(f"  {name:22} ${amt:>7} ~{date}  ->  {verdict}")
                except Exception as e:
                    print(f"  {name:22} ${amt:>7} ~{date}  ->  ERROR: {str(e)[:80]}")
                    try:
                        bot.recover_to_dashboard(page)
                    except Exception:
                        pass
        finally:
            browser.close()

    # LIKELY POSTED is evidence, not proof — a same-amount payment inside a
    # three-week window can be a different session. So print the command rather
    # than clearing anything, and let a person look before running it.
    if posted:
        print(f"\n{len(posted)} look already posted in TA:")
        for _key, name, amt, date in posted:
            print(f"  {name} — ${amt} ({date})")
        print("\nIf those are right, take them off the outstanding list with:")
        print("  python3 scripts/reconcile.py \\\n    "
              + " \\\n    ".join(f'--clear "{k}"' for k, _n, _a, _d in posted))


if __name__ == "__main__":
    main()
