#!/usr/bin/env python3
"""
capture_charges_modal.py — Observation-only capture of TA's "additional charges" modal.

Background
----------
When the posting bot clicks "Accept Payment" for a client who carries an
outstanding balance across other sessions, TherapyAppointment pops the
#show-other-charges-modal with (at least) two choices:

    • "Yes, accept payment for this appointment"  ← production bot always clicks this
    • "No, show all open charges"                 ← what we want to evaluate

Choosing "No" is expected to let the payment land against the client's open
charges (oldest balance) instead of pinning it to the current appointment.

This script does NOT post anything. For a single client it:
    1. Logs in and navigates to the chosen appointment (reusing the bot's helpers)
    2. Clicks "Accept Payment" but does NOT auto-confirm the modal
    3. Screenshots the modal and logs its exact button labels
    4. Clicks "No, show all open charges"
    5. Screenshots whatever TA presents next (the open-charges / distribution view)
    6. Optionally fills the amount so the distribution grid is visible
    7. CANCELS — it never clicks "Save Payment"; nothing is committed

Safe to run against live TA: no payment is ever saved.

Usage
-----
    python3 scripts/capture_charges_modal.py --name "Sophia Crocker" --date 06/02/2026
    python3 scripts/capture_charges_modal.py --name "Sophia Crocker" --date 06/02/2026 --amount 33.09

Pick a client who currently carries an outstanding balance (otherwise the modal
won't appear). Recent repeat hitters from the logs: Sophia Crocker, Eric Karsten,
Nathan Haney, Kayla Hanks, Tyler Eggimann.
"""

import argparse
import sys
from pathlib import Path

# Reuse the production bot's login/navigation helpers verbatim so behaviour matches.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import sync_playwright  # noqa: E402
import bot_v2 as bot  # noqa: E402

MODAL_SELECTOR = "#show-other-charges-modal.modal.in"


def _dump_visible_table(page):
    """Print a compact text view of any visible table (the distribution/charges grid)."""
    try:
        rows = page.evaluate(
            """
            () => {
                const out = [];
                for (const tbl of document.querySelectorAll('table')) {
                    if (tbl.offsetParent === null) continue;  // not visible
                    for (const tr of tbl.querySelectorAll('tr')) {
                        const cells = [...tr.querySelectorAll('th, td')]
                            .map(c => c.textContent.replace(/\\s+/g, ' ').trim())
                            .filter(Boolean);
                        if (cells.length) out.push(cells.join(' | '));
                    }
                }
                return out;
            }
            """
        )
    except Exception as e:
        print(f"  (could not read tables: {e})")
        return
    if rows:
        print("  --- visible table rows ---")
        for r in rows:
            print(f"    {r}")
        print("  --- end table rows ---")


def _dump_due_now_dom(page):
    """One-off: dump the DOM around 'Due From Client Now' so we can scrape it right."""
    try:
        info = page.evaluate(
            """
            () => {
                const norm = s => (s||'').replace(/\\s+/g,' ').trim();
                const label = [...document.querySelectorAll('div,span,label,p,strong')]
                    .find(el => norm(el.textContent) === 'Due From Client Now');
                if (!label) return {found:false};
                const out = {found:true, levels:[]};
                let node = label;
                for (let i=0;i<4 && node;i++){
                    const inputs = [...node.querySelectorAll('input')].map(inp => ({
                        name: inp.name||inp.id||'', value: inp.value,
                        type: inp.type, readOnly: inp.readOnly}));
                    out.levels.push({
                        i, tag: node.tagName,
                        text: norm(node.textContent).slice(0,120),
                        inputs});
                    node = node.parentElement;
                }
                return out;
            }
            """
        )
        print("  --- Due-From-Client-Now DOM probe ---")
        import json as _json
        print("  " + _json.dumps(info, indent=2).replace("\n", "\n  "))
        print("  --- end probe ---")
    except Exception as e:
        print(f"  (DOM probe failed: {e})")


def capture(name, date_str, amount=None):
    if not bot.USERNAME or not bot.PASSWORD:
        print("ERROR: TA_USERNAME and TA_PASSWORD must be set in .env")
        sys.exit(1)

    slug = name.replace(" ", "_")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=bot.HEADLESS)
        page = browser.new_page()
        page.set_default_timeout(bot.ACTION_TIMEOUT)
        saved = False  # tripwire — this script must never save

        try:
            bot.login(page)

            # Navigate to the client's appointment (mirrors post_payment_v2).
            bot.search_client(page, name)
            bot.navigate_to_appointments(page)
            bot.ensure_date_filters(page)
            bot.click_appointment_by_date(page, date_str, name)

            # Click Accept Payment WITHOUT auto-confirming the modal.
            print("  Clicking Accept Payment (capture mode — modal NOT auto-confirmed)...")
            bot.suppress_beacon_widget(page)
            bot.dismiss_popups(page)
            clicked = page.evaluate(
                """
                () => {
                    const els = [...document.querySelectorAll('a, button')];
                    const btn = els.find(el => el.textContent.trim().includes('Accept Payment'));
                    if (btn) { btn.click(); return true; }
                    return false;
                }
                """
            )
            if not clicked:
                page.click("text=Accept Payment")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)

            # Is the additional-charges modal present?
            modal = page.locator(MODAL_SELECTOR)
            if not modal.is_visible(timeout=4000):
                print(f"\n  NOTE: the additional-charges modal did NOT appear for {name}.")
                print("        This client has no extra open charges right now — pick one")
                print("        who currently carries an outstanding balance.")
                bot.screenshot(page, f"capture_{slug}_NO_MODAL")
                return

            print("  Modal appeared — capturing it.")
            bot.screenshot(page, f"capture_{slug}_1_modal")

            # Log the modal's exact button labels so we know the real selectors.
            try:
                btn_labels = page.evaluate(
                    """
                    () => {
                        const m = document.querySelector('#show-other-charges-modal.modal.in');
                        if (!m) return [];
                        return [...m.querySelectorAll('button, a.btn, a[role=button]')]
                            .map(b => b.textContent.replace(/\\s+/g, ' ').trim())
                            .filter(Boolean);
                    }
                    """
                )
                print(f"  Modal buttons: {btn_labels}")
            except Exception as e:
                print(f"  (could not read modal buttons: {e})")

            # Click "No, show all open charges". Try the labelled button first,
            # then fall back to the X / data-dismiss — per bot_v2's comment, the X
            # is semantically the "No, show all open charges" branch.
            print("  Clicking: No, show all open charges")
            result = page.evaluate(
                """
                () => {
                    const m = document.querySelector('#show-other-charges-modal.modal.in');
                    if (!m) return 'no-modal';
                    const btns = [...m.querySelectorAll('button, a.btn, a[role=button]')];
                    const no = btns.find(b => /no[,\\s]+show all open charges/i.test(b.textContent));
                    if (no) { no.click(); return 'clicked:no-button'; }
                    const x = m.querySelector('[data-dismiss="modal"], button.close');
                    if (x) { x.click(); return 'clicked:dismiss-x'; }
                    return 'no-target';
                }
                """
            )
            print(f"  -> {result}")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2500)

            # Capture whatever TA shows after choosing the open-charges branch.
            bot.screenshot(page, f"capture_{slug}_2_after_no")
            _dump_visible_table(page)
            _dump_due_now_dom(page)

            # Optionally fill the amount so the distribution grid populates.
            # Still never saves — we Cancel afterwards.
            if amount is not None:
                print(f"  Filling amount ${amount} to reveal the distribution (will NOT save)...")
                try:
                    bot.fill_payment_form(page, amount)
                    page.wait_for_timeout(1500)
                    bot.screenshot(page, f"capture_{slug}_3_filled")
                    _dump_visible_table(page)
                except Exception as e:
                    print(f"  (could not fill amount on the open-charges layout: {e})")
                    bot.screenshot(page, f"capture_{slug}_3_fill_failed")

            print("\n  CAPTURE COMPLETE — no payment saved.")
            print("  Review the screenshots in logs/:")
            print(f"    capture_{slug}_1_modal.png      (the choice modal)")
            print(f"    capture_{slug}_2_after_no.png   (TA's open-charges view)")
            if amount is not None:
                print(f"    capture_{slug}_3_filled.png     (distribution with amount entered)")

        finally:
            # Safety net: leave without saving. Click Cancel if it's on screen.
            try:
                cancel = page.locator("text=Cancel")
                if cancel.count() > 0 and cancel.first.is_visible():
                    print("  Clicking Cancel (safety — nothing saved).")
                    cancel.first.click()
                    page.wait_for_timeout(1000)
            except Exception:
                pass
            if saved:
                print("  !! WARNING: a save path executed — this should never happen.")
            print("Closing browser.")
            browser.close()


def main():
    parser = argparse.ArgumentParser(
        description="Observation-only capture of TA's additional-charges modal "
                    "via the 'No, show all open charges' branch. Never saves."
    )
    parser.add_argument("--name", required=True, help="Client full name, e.g. 'Sophia Crocker'")
    parser.add_argument("--date", required=True, help="Appointment date MM/DD/YYYY, e.g. 06/02/2026")
    parser.add_argument("--amount", default=None,
                        help="Optional payment amount to fill so the distribution grid shows "
                             "(still never saved). E.g. 33.09")
    args = parser.parse_args()
    capture(args.name, args.date, args.amount)


if __name__ == "__main__":
    main()
