import argparse
import csv
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Resolve paths relative to project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR.mkdir(exist_ok=True)

load_dotenv(PROJECT_ROOT / ".env")

USERNAME = os.getenv("TA_USERNAME")
PASSWORD = os.getenv("TA_PASSWORD")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

ACTION_TIMEOUT = 30000



def screenshot(page, name):
    """Save a timestamped screenshot to the logs directory."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOG_DIR / f"{ts}_{name}.png"
    page.screenshot(path=str(path))
    print(f"  Screenshot: {path.name}")
    return path


def read_csv(csv_path):
    """Read payment CSV and return list of dicts with name, date, and amount."""
    payments = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        # Skip title row (e.g., "SUCCESSFUL PAYMENTS") if present
        first_line = f.readline().strip().strip('"')
        if "Full Name" not in first_line:
            pass  # title row — DictReader starts from the real header
        else:
            f.seek(0)  # first line was the header — rewind
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("Full Name", "").strip()
            date = row.get("Transaction Date", "").strip()
            raw_amount = row.get("Base Amount", "").strip()
            if not name or not raw_amount:
                continue
            if name.upper() in ("TOTALS", "TOTAL", "GRAND TOTAL", "SUM"):
                continue
            amount = raw_amount.replace("$", "").replace(",", "")
            try:
                amount = f"{float(amount):.2f}"
            except ValueError:
                print(f"  WARNING: Skipping row - invalid amount '{raw_amount}' for {name}")
                continue
            payments.append({"name": name, "date": date, "amount": amount})
    return payments


def detect_duplicates(payments):
    """Find names that appear more than once and return a set of them."""
    seen = {}
    for p in payments:
        seen[p["name"]] = seen.get(p["name"], 0) + 1
    return {name for name, count in seen.items() if count > 1}


def login(page):
    """Log in to TherapyAppointment."""
    print("Opening login portal...")
    page.goto("https://portal.therapyappointment.com/index.cfm/public:auth?fw1pk=1",
              wait_until="domcontentloaded")
    screenshot(page, "01_loginform")

    print("Entering credentials...")
    page.fill("input[type='text']", USERNAME)
    page.fill("input[type='password']", PASSWORD)

    print("Clicking Sign In...")
    page.click("text=Sign In")
    page.wait_for_url("**/dashboard/**", timeout=30000)
    page.wait_for_load_state("networkidle")
    screenshot(page, "02_dashboard")
    print("Login successful.")


# =============================================================================
# V2 FLOW: Clients > Search > Appointments > Accept Payment
# =============================================================================

def navigate_to_clients(page):
    """Click Clients in the sidebar."""
    print("  Navigating to Clients...")
    page.click("text=Clients")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)


def search_client(page, name):
    """Search for a client by first and last name. Returns True if a unique match was clicked.

    Also returns a note string if the CSV name contains a middle name or extra
    name part that was ignored during matching (so it can be flagged in the report).
    """
    parts = name.split()
    first_name = parts[0]
    last_name = parts[-1]
    middle_parts = parts[1:-1] if len(parts) > 2 else []
    # Use first 3 letters for search to allow nickname/variation matching
    # e.g., "Mat" matches both "Matthew" and "Matt"
    first_search = first_name[:3]
    last_search = last_name[:3]

    note = None
    if middle_parts:
        extra = " ".join(middle_parts)
        note = f"Middle/extra name '{extra}' in CSV — verify in TherapyAppointment"
        print(f"  NOTE: {note}")

    print(f"  Searching: First={first_search} (from {first_name}), Last={last_search} (from {last_name})")

    visible_text_inputs = []
    for inp in page.locator("input[type='text']").all():
        if inp.is_visible():
            visible_text_inputs.append(inp)

    if len(visible_text_inputs) < 2:
        raise Exception(f"Expected at least 2 visible text inputs, found {len(visible_text_inputs)}")

    first_input = visible_text_inputs[0]
    last_input = visible_text_inputs[1]

    first_input.fill(first_search)
    last_input.fill(last_search)

    page.locator("button:has-text('Search')").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    screenshot(page, f"search_{first_search}_{last_search}")

    # Match on first and last name only (middle names are not reliable in TA)
    match_parts = [first_name.lower(), last_name.lower()]
    # Also split hyphenated last names so "Nowicki-Gamadia" matches both parts
    expanded = []
    for part in match_parts:
        expanded.extend(part.split("-"))
    match_parts = expanded

    # TA splits first/last name across separate cells in the same row.
    # Match against the full row text, then click the link within that row.
    rows = page.locator("table tr").all()
    matching_rows = []
    for row in rows:
        row_text = (row.text_content() or "").lower()
        if all(part in row_text for part in match_parts):
            links = row.locator("a")
            if links.count() > 0:
                matching_rows.append((row, links.first))

    if len(matching_rows) == 0:
        raise Exception(f"Client '{name}' not found in search results")
    elif len(matching_rows) > 1:
        raise Exception(f"FLAG: Multiple matches for '{name}' — needs manual review")

    row, link = matching_rows[0]
    print(f"  Found client: {row.text_content().strip()[:60]}")
    link.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    return True, note


def navigate_to_appointments(page):
    """Click the Appointments tab on the client profile."""
    print("  Clicking Appointments tab...")
    page.click("text=Appointments")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)


def ensure_date_filters(page):
    """Set the Appointments filter dates on every client visit.

    Sets From = 15 days before today, To = 12/31/{year}.
    Always re-applies because TA resets filters when navigating between clients.
    """
    from_date = datetime.now() - timedelta(days=15)
    year = datetime.now().year
    expected_from = from_date.strftime("%m/%d/%Y")
    expected_to = f"12/31/{year}"

    from_input = page.locator("input#span_startdate")
    to_input = page.locator("input#span_enddate")

    current_from = from_input.input_value()
    current_to = to_input.input_value()

    if current_from == expected_from and current_to == expected_to:
        print(f"  Filters already set: {expected_from} — {expected_to}")
        return

    print(f"  Setting filters: From={expected_from}, To={expected_to}")

    # Masked inputs auto-insert slashes — type digits only
    from_digits = expected_from.replace("/", "")
    to_digits = expected_to.replace("/", "")

    if current_from != expected_from:
        from_input.click()
        page.keyboard.press("Meta+a")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(200)
        page.keyboard.type(from_digits, delay=50)
        page.keyboard.press("Tab")
        page.wait_for_timeout(500)

    if current_to != expected_to:
        to_input.click()
        page.keyboard.press("Meta+a")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(200)
        page.keyboard.type(to_digits, delay=50)
        page.keyboard.press("Tab")
        page.wait_for_timeout(500)

    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    screenshot(page, "filters_set")
    print(f"  Filters set: {expected_from} — {expected_to}")


def click_appointment_by_date(page, date_str, name):
    """Find and click an appointment row matching the given date.

    Converts CSV date (YYYY-MM-DD) to TA display format (MM/DD/YYYY)
    before searching.
    """
    # Convert YYYY-MM-DD → MM/DD/YYYY to match TA's display format
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        ta_date = dt.strftime("%m/%d/%Y")
    except ValueError:
        ta_date = date_str  # fallback to original if format is unexpected

    print(f"  Looking for appointment on {ta_date}...")

    date_links = page.locator(f"a:has-text('{ta_date}')").all()

    if len(date_links) == 0:
        raise Exception(f"No appointment found on {ta_date} for {name}")
    elif len(date_links) > 1:
        raise Exception(f"FLAG: Multiple appointments on {ta_date} for {name} — needs manual review")

    print(f"  Found appointment: {date_links[0].text_content().strip()}")
    date_links[0].click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)


def click_accept_payment(page, name):
    """Click the Accept Payment button on the appointment summary.

    Returns a note string if the client has an outstanding balance (additional
    charges modal appeared), or None if clean.
    """
    print("  Clicking Accept Payment...")
    page.click("text=Accept Payment")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    # Handle modal: "Additional charges exist for this client"
    # This means the client has an outstanding balance from older sessions.
    balance_note = None
    yes_btn = page.locator("button.btn-action:has-text('Yes, accept payment for this appointment')")
    if yes_btn.is_visible(timeout=3000):
        print(f"  NOTE: {name} has additional charges / outstanding balance")
        balance_note = "Client has outstanding balance — additional charges exist"
        print("  Clicking: Yes, accept payment for this appointment")
        yes_btn.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

    return balance_note


def fill_payment_form(page, amount):
    """Fill in the payment form fields."""
    print(f"  Entering amount: ${amount}")

    all_inputs = page.locator("input[type='text'], input:not([type])").all()
    payment_input = None
    for inp in all_inputs:
        try:
            val = inp.get_attribute("value") or ""
            name_attr = inp.get_attribute("name") or ""
            placeholder = inp.get_attribute("placeholder") or ""
            combined = (placeholder + name_attr).lower()
            if val == "0.00" or "amount" in combined or "payment" in combined:
                payment_input = inp
                break
        except Exception:
            continue

    if payment_input is None:
        payment_input = all_inputs[1] if len(all_inputs) > 1 else all_inputs[0]

    payment_input.click(click_count=3)
    payment_input.fill(amount)

    print("  Selecting External Credit Card...")
    page.click("text=External Credit Card")

    print("  Entering reference: Square")
    ref_input = None
    for inp in page.locator("input[type='text'], input:not([type])").all():
        placeholder = inp.get_attribute("placeholder") or ""
        name_attr = inp.get_attribute("name") or ""
        combined = (placeholder + name_attr).lower()
        if "reference" in combined or "check" in combined:
            ref_input = inp
            break
    if ref_input is None:
        ref_input = page.locator("input[placeholder*='Reference'], input[placeholder*='Check']").first
    ref_input.fill("Square")


def submit_payment(page, name, dry_run=False):
    """Click Continue then Save Payment, or Cancel if dry run."""
    if dry_run:
        print("  DRY RUN: Clicking Cancel instead of saving.")
        page.click("text=Cancel")
        page.wait_for_load_state("networkidle")
        return True

    print("  Clicking Continue...")
    page.click("text=Continue")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    screenshot(page, f"payment_{name.replace(' ', '_')}_03_continue")

    print("  Clicking Save Payment...")
    page.click("text=Save Payment")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    screenshot(page, f"payment_{name.replace(' ', '_')}_04_saved")

    print(f"  Payment saved for {name}.")
    return True


def post_payment_v2(page, name, date, amount, dry_run=False):
    """V2 flow: Clients > Appointments > Accept Payment.

    Returns (success: bool, note: str or None).
    """
    print(f"  [V2] Clients > Appointments > Accept Payment")

    navigate_to_clients(page)
    _ok, name_note = search_client(page, name)
    navigate_to_appointments(page)
    ensure_date_filters(page)
    click_appointment_by_date(page, date, name)
    balance_note = click_accept_payment(page, name)
    screenshot(page, f"payment_{name.replace(' ', '_')}_01_form")

    # Combine notes (middle name + outstanding balance)
    notes = [n for n in (name_note, balance_note) if n]
    note = "; ".join(notes) if notes else None

    fill_payment_form(page, amount)
    screenshot(page, f"payment_{name.replace(' ', '_')}_02_filled")

    submit_payment(page, name, dry_run)
    return True, note


# =============================================================================
# V1 FALLBACK: Billing > Take Payment > Search Charges
# =============================================================================

def navigate_to_billing(page):
    """Navigate to the Billing dashboard."""
    print("  Navigating to Billing...")
    page.click("text=Billing")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)


def select_client_v1(page, name):
    """Select a client via the Search Charges token-input autocomplete."""
    print(f"  Searching for client: {name}")

    client_input = page.locator("#token-input-user_id_patient")
    client_input.click()

    last_name = name.split()[-1]
    first_name = name.split()[0]
    page.keyboard.type(last_name, delay=100)
    page.wait_for_timeout(2000)

    dropdown_items = page.locator("[class*='token-input-dropdown'] li, "
                                  ".token-input-dropdown li, "
                                  "div.token-input-dropdown-facebook li")
    count = dropdown_items.count()

    selected = False
    for i in range(count):
        item = dropdown_items.nth(i)
        item_text = item.text_content() or ""
        if "type in" in item_text.lower() or "search" in item_text.lower():
            continue
        if first_name.lower() in item_text.lower() and last_name.lower() in item_text.lower():
            print(f"  Selected: {item_text.strip()}")
            item.click()
            page.wait_for_timeout(500)
            selected = True
            break

    if not selected:
        raise Exception(f"Client '{name}' not found in autocomplete ({count} results)")

    print("  Clicking Search...")
    page.locator("button:has-text('Search')").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)


def post_payment_v1(page, name, amount, dry_run=False):
    """V1 fallback: Billing > Take Payment > Search Charges."""
    print(f"  [V1 FALLBACK] Billing > Take Payment > Search Charges")

    navigate_to_billing(page)

    # Click Take Payment
    print("  Clicking Take Payment...")
    page.locator("text=Take Payment").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

    # Select client and search
    select_client_v1(page, name)
    screenshot(page, f"payment_{name.replace(' ', '_')}_v1_01_form")

    # Fill payment form
    fill_payment_form(page, amount)
    screenshot(page, f"payment_{name.replace(' ', '_')}_v1_02_filled")

    return submit_payment(page, name, dry_run)


# =============================================================================
# MAIN LOGIC: Try V2, fallback to V1, then fail
# =============================================================================

def post_payment(page, name, date, amount, dry_run=False):
    """
    Post a payment with fallback logic:
    1. Try V2 (Clients > Appointments > Accept Payment)
    2. If V2 fails, try V1 (Billing > Take Payment > Search Charges)
    3. If both fail, mark as FAILED
    Returns (success: bool, method: str, error: str or None, note: str or None)
    """
    print(f"\n--- Payment: {name} — ${amount} on {date} ---")

    # --- Attempt 1: V2 flow (always try first) ---
    v2_error = None
    try:
        _ok, note = post_payment_v2(page, name, date, amount, dry_run)
        return True, "V2", None, note
    except Exception as e:
        v2_error = str(e)
        if "FLAG" in v2_error:
            # Flagged items should NOT fallback — they need manual review
            return False, "FLAGGED", v2_error, None
        print(f"  V2 failed: {v2_error}")
        print(f"  Falling back to V1...")

    # --- Attempt 2: V1 fallback ---
    try:
        post_payment_v1(page, name, amount, dry_run)
        return True, "V1", None, None
    except Exception as e:
        v1_error = str(e)
        if "FLAG" in v1_error:
            return False, "FLAGGED", v1_error, None
        print(f"  V1 also failed: {v1_error}")

    # --- Both failed ---
    return False, "FAILED", f"V2: {v2_error}; V1: {v1_error}", None


def _stat_box(value, label, color):
    """Return HTML for a single summary stat box."""
    return f'''<td style="text-align:center;padding:12px 24px;">
      <div style="font-size:36px;font-weight:700;color:{color};">{value}</div>
      <div style="font-size:12px;color:#666;margin-top:4px;">{label}</div>
    </td>'''


def generate_report(results, duplicates, csv_date, dry_run=False):
    """Generate the HTML staff report (for Hannah) and save to logs."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = "DRYRUN" if dry_run else "POSTED"
    report_path = LOG_DIR / f"{ts}_report_{mode}.html"

    succeeded = [r for r in results if r["status"] == "OK"]
    failed = [r for r in results if r["status"] == "FAILED"]
    flagged = [r for r in results if r["status"] == "FLAGGED"]
    timed_out = [r for r in results if r["status"] == "TIMEOUT"]
    manual = failed + flagged + timed_out
    v1_clients = [r for r in succeeded if r.get("method") == "V1"]
    balance_clients = [r for r in results if r.get("note") and "outstanding balance" in r["note"]]
    name_noted = [r for r in results if r.get("note") and "Middle/extra name" in r["note"]]

    total_amount = sum(float(r["amount"]) for r in succeeded)

    # Pretty date for header
    try:
        dt = datetime.strptime(csv_date, "%m/%d/%Y")
        display_date = dt.strftime("%A, %B %d, %Y")
    except ValueError:
        display_date = csv_date

    # --- Build HTML ---
    h = []
    h.append(f'''<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;background:#f4f4f4;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;margin:0 auto;background:#fff;">

  <!-- Header -->
  <tr><td style="background:#346756;padding:24px 32px;">
    <div style="font-size:22px;font-weight:700;color:#fff;">PostIQ Payment Report</div>
    <div style="font-size:13px;color:#a8d4c0;margin-top:4px;">{"DRY RUN — " if dry_run else ""}Square Payments — {display_date}</div>
  </td></tr>

  <!-- Stats -->
  <tr><td style="padding:20px 0;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      {_stat_box(len(succeeded), "Posted", "#2e7d32")}
      {_stat_box(len(manual), "Need Manual Posting", "#c62828" if manual else "#999")}
      {_stat_box(len(v1_clients), "Verify Allocation", "#7b1fa2" if v1_clients else "#999")}
      {_stat_box(len(balance_clients), "Outstanding Balances", "#e65100" if balance_clients else "#999")}
    </tr></table>
  </td></tr>''')

    # --- Duplicates warning ---
    if duplicates:
        h.append('''<tr><td style="padding:0 32px 16px;">
          <div style="background:#fff3cd;border-left:4px solid #ffc107;padding:12px 16px;font-size:14px;">
            <strong>Duplicate Names — Staff Review Required</strong><br>''')
        for name in sorted(duplicates):
            count = sum(1 for r in results if r["name"] == name)
            h.append(f'{name} ({count} entries)<br>')
        h.append('</div></td></tr>')

    # --- Successful payments table ---
    if succeeded:
        h.append('''<tr><td style="padding:0 32px 8px;">
          <div style="font-size:15px;font-weight:700;color:#346756;border-bottom:2px solid #346756;padding-bottom:6px;margin-bottom:0;">
            Completed Payments</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;">
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:13px;border-collapse:collapse;">
            <tr style="background:#346756;color:#fff;">
              <th style="text-align:left;padding:10px 12px;">Client</th>
              <th style="text-align:right;padding:10px 12px;">Amount</th>
            </tr>''')
        for i, r in enumerate(succeeded):
            bg = "#f9f9f9" if i % 2 else "#fff"
            h.append(f'''<tr style="background:{bg};">
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"]):,.2f}</td>
            </tr>''')
        h.append(f'''<tr style="background:#346756;color:#fff;font-weight:700;">
              <td style="padding:10px 12px;">Total</td>
              <td style="padding:10px 12px;text-align:right;">${total_amount:,.2f}</td>
            </tr>
          </table>
        </td></tr>''')

    # --- Manual posting needed ---
    if manual:
        h.append('''<tr><td style="padding:0 32px 8px;">
          <div style="font-size:15px;font-weight:700;color:#c62828;border-bottom:2px solid #c62828;padding-bottom:6px;">
            Action Required — Manual Posting Needed</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;">
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:13px;border-collapse:collapse;">
            <tr style="background:#c62828;color:#fff;">
              <th style="text-align:left;padding:10px 12px;">Client</th>
              <th style="text-align:right;padding:10px 12px;">Amount</th>
              <th style="text-align:left;padding:10px 12px;">Reason</th>
            </tr>''')
        for i, r in enumerate(manual):
            bg = "#fff5f5" if i % 2 else "#fff"
            reason = r.get("reason", r["status"])
            # Shorten long reasons for readability
            if "Multiple appointments" in reason:
                short_reason = "Multiple appointments on same date"
            elif "not found in search" in reason:
                short_reason = "Client not found in system"
            else:
                short_reason = reason[:80]
            h.append(f'''<tr style="background:{bg};">
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"]):,.2f}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{short_reason}</td>
            </tr>''')
        h.append('</table></td></tr>')

    # --- V1 fallback — verify allocation ---
    if v1_clients:
        h.append('''<tr><td style="padding:0 32px 8px;">
          <div style="font-size:15px;font-weight:700;color:#7b1fa2;border-bottom:2px solid #7b1fa2;padding-bottom:6px;">
            Verify Allocation — Payments Posted via Fallback</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;font-size:13px;">
          <p style="color:#666;margin:8px 0;">These payments were posted to the correct client but could <strong>not</strong>
          be matched to a specific appointment date. Please verify in TherapyAppointment that each payment
          is allocated to the correct clinician and appointment.</p>
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:13px;border-collapse:collapse;">
            <tr style="background:#7b1fa2;color:#fff;">
              <th style="text-align:left;padding:10px 12px;">Client</th>
              <th style="text-align:right;padding:10px 12px;">Amount</th>
              <th style="text-align:left;padding:10px 12px;">Expected Appt Date</th>
            </tr>''')
        for i, r in enumerate(v1_clients):
            bg = "#f5f0ff" if i % 2 else "#fff"
            appt_date = r.get("date", "")
            try:
                dt = datetime.strptime(appt_date, "%Y-%m-%d")
                appt_date = dt.strftime("%m/%d/%Y")
            except ValueError:
                pass
            h.append(f'''<tr style="background:{bg};">
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"]):,.2f}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{appt_date}</td>
            </tr>''')
        h.append('</table></td></tr>')

    # --- Outstanding balances ---
    if balance_clients:
        h.append('''<tr><td style="padding:0 32px 8px;">
          <div style="font-size:15px;font-weight:700;color:#e65100;border-bottom:2px solid #e65100;padding-bottom:6px;">
            Outstanding Balances — Follow Up Needed</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;font-size:13px;">
          <p style="color:#666;margin:8px 0;">These clients had additional charges from older sessions.
          Today's payment was posted to the correct appointment, but the remaining balance needs attention.</p>
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:13px;border-collapse:collapse;">
            <tr style="background:#e65100;color:#fff;">
              <th style="text-align:left;padding:10px 12px;">Client</th>
            </tr>''')
        for i, r in enumerate(balance_clients):
            bg = "#fff8f0" if i % 2 else "#fff"
            h.append(f'''<tr style="background:{bg};">
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>
            </tr>''')
        h.append('</table></td></tr>')

    # --- Name notes ---
    if name_noted:
        h.append('''<tr><td style="padding:0 32px 8px;">
          <div style="font-size:15px;font-weight:700;color:#1565c0;border-bottom:2px solid #1565c0;padding-bottom:6px;">
            Name Notes — Please Verify in TherapyAppointment</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;font-size:13px;">
          <p style="color:#666;margin:8px 0;">These clients have middle or extra names in Square that may not match
          TherapyAppointment. Payments posted OK, but the names should be reviewed. These could also be two-part last names.</p>
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:13px;border-collapse:collapse;">
            <tr style="background:#1565c0;color:#fff;">
              <th style="text-align:left;padding:10px 12px;">Client</th>
              <th style="text-align:left;padding:10px 12px;">Extra Name in Square</th>
            </tr>''')
        for i, r in enumerate(name_noted):
            bg = "#f0f4ff" if i % 2 else "#fff"
            # Extract the middle name from the note
            note = r.get("note", "")
            extra = note.split("'")[1] if "'" in note else note
            h.append(f'''<tr style="background:{bg};">
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{extra}</td>
            </tr>''')
        h.append('</table></td></tr>')

    # --- Footer ---
    h.append(f'''<tr><td style="padding:24px 32px;font-size:13px;color:#666;">
      Thanks for your attention to detail and getting these tasks completed.<br><br>
      <strong>Oakley</strong>, Great Oak Counseling's AI Assistant
    </td></tr>
    <tr><td style="background:#346756;padding:12px 32px;font-size:11px;color:#a8d4c0;text-align:center;">
      PostIQ — automated payment posting by Great Oak Counseling
    </td></tr>
</table></body></html>''')

    html = "\n".join(h)
    report_path.write_text(html)
    print(f"\nStaff report saved: {report_path.name}")
    return report_path, html


def generate_tech_report(results, csv_date):
    """Generate an HTML tech report for Travis — only when there are technical issues.

    Returns (path, html) or (None, None) if no issues to report.
    """
    failed = [r for r in results if r["status"] in ("FAILED", "TIMEOUT")]

    encoding_issues = []
    for r in results:
        if any(ord(c) > 127 for c in r["name"]):
            encoding_issues.append(r)

    if not failed and not encoding_issues:
        return None, None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = LOG_DIR / f"{ts}_tech_report.html"

    h = []
    h.append(f'''<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;background:#f4f4f4;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;margin:0 auto;background:#fff;">
  <tr><td style="background:#333;padding:24px 32px;">
    <div style="font-size:22px;font-weight:700;color:#fff;">PostIQ Tech Report</div>
    <div style="font-size:13px;color:#aaa;margin-top:4px;">{csv_date}</div>
  </td></tr>''')

    if failed:
        h.append('''<tr><td style="padding:24px 32px 8px;">
          <div style="font-size:15px;font-weight:700;color:#c62828;border-bottom:2px solid #c62828;padding-bottom:6px;">Failures</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;">
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:13px;border-collapse:collapse;">
            <tr style="background:#c62828;color:#fff;">
              <th style="text-align:left;padding:10px 12px;">Client</th>
              <th style="text-align:left;padding:10px 12px;">Status</th>
              <th style="text-align:left;padding:10px 12px;">Detail</th>
            </tr>''')
        for i, r in enumerate(failed):
            bg = "#fff5f5" if i % 2 else "#fff"
            h.append(f'''<tr style="background:{bg};">
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["status"]}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;">{r.get("reason", "unknown")}</td>
            </tr>''')
        h.append('</table></td></tr>')

    if encoding_issues:
        h.append('''<tr><td style="padding:24px 32px 8px;">
          <div style="font-size:15px;font-weight:700;color:#e65100;border-bottom:2px solid #e65100;padding-bottom:6px;">Encoding Issues</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;font-size:13px;">
          <p style="color:#666;">Likely cause: Square CSV export using wrong encoding (UTF-8 vs Latin-1).
          Consider adding normalization to read_csv if this recurs.</p>
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:13px;border-collapse:collapse;">
            <tr style="background:#e65100;color:#fff;">
              <th style="text-align:left;padding:10px 12px;">Client</th>
              <th style="text-align:left;padding:10px 12px;">Hex</th>
            </tr>''')
        for i, r in enumerate(encoding_issues):
            bg = "#fff8f0" if i % 2 else "#fff"
            hex_repr = " ".join(f"{ord(c):02x}" for c in r["name"])
            h.append(f'''<tr style="background:{bg};">
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;font-family:monospace;font-size:11px;">{hex_repr}</td>
            </tr>''')
        h.append('</table></td></tr>')

    h.append('''<tr><td style="background:#333;padding:12px 32px;font-size:11px;color:#aaa;text-align:center;">
      PostIQ Tech Report — Great Oak Counseling
    </td></tr>
</table></body></html>''')

    html = "\n".join(h)
    report_path.write_text(html)
    print(f"\nTech report saved: {report_path.name}")
    return report_path, html


def send_email(to, cc, subject, body, html=True):
    """Send an email via msmtp. Sends as HTML by default."""
    cc_header = f"Cc: {cc}\n" if cc else ""
    content_type = "text/html" if html else "text/plain"
    message = (
        f"To: {to}\n"
        f"{cc_header}"
        f"From: Oakley, Great Oak AI Assistant <travis@greatoakcounseling.com>\n"
        f"Subject: {subject}\n"
        f"MIME-Version: 1.0\n"
        f"Content-Type: {content_type}; charset=utf-8\n"
        f"\n"
        f"{body}"
    )
    try:
        proc = subprocess.run(
            ["msmtp", "-t"],
            input=message, text=True, capture_output=True, timeout=30,
        )
        if proc.returncode == 0:
            print(f"  Email sent: {subject}")
        else:
            print(f"  Email FAILED: {proc.stderr.strip()}")
    except FileNotFoundError:
        print("  Email SKIPPED: msmtp not installed")
    except Exception as e:
        print(f"  Email ERROR: {e}")


def send_reports(results, duplicates, csv_date, dry_run=False):
    """Generate and email all reports."""
    mode = "DRY RUN" if dry_run else csv_date

    # Staff report → Hannah (cc Travis, support staff)
    staff_path, staff_body = generate_report(results, duplicates, csv_date, dry_run)

    has_errors = any(r["status"] in ("FAILED", "FLAGGED", "TIMEOUT") for r in results)
    subject_tag = "ERRORS DETECTED" if has_errors else "Success"
    staff_subject = f"PostIQ Payment Report — {mode} — {subject_tag}"

    send_email(
        to="hannah@greatoakcounseling.com",
        cc="travis@greatoakcounseling.com, supportstaff@greatoakcounseling.com",
        subject=staff_subject,
        body=staff_body,
    )

    # Tech report → Travis only (when there are technical issues)
    tech_path, tech_body = generate_tech_report(results, csv_date)
    if tech_body:
        send_email(
            to="travis@greatoakcounseling.com",
            cc=None,
            subject=f"PostIQ Tech Report — {csv_date}",
            body=tech_body,
        )


def run():
    parser = argparse.ArgumentParser(description="PostIQ — Payment Bot with Fallback")
    parser.add_argument("csv_file", help="Path to the payment CSV file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fill forms but don't submit (cancels instead of saving)")
    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        alt_path = DATA_DIR / csv_path.name
        if alt_path.exists():
            csv_path = alt_path
        else:
            print(f"ERROR: CSV file not found: {csv_path}")
            sys.exit(1)

    if not USERNAME or not PASSWORD:
        print("ERROR: TA_USERNAME and TA_PASSWORD must be set in .env")
        sys.exit(1)

    payments = read_csv(csv_path)
    if not payments:
        print("ERROR: No valid payment rows found in CSV.")
        sys.exit(1)

    duplicates = detect_duplicates(payments)

    # Extract the date from the CSV for report subject lines
    csv_date = payments[0].get("date", "") if payments else "unknown"
    # Format as MM/DD/YYYY for readability
    try:
        dt = datetime.strptime(csv_date, "%Y-%m-%d")
        csv_date_display = dt.strftime("%m/%d/%Y")
    except ValueError:
        csv_date_display = csv_date

    print(f"Loaded {len(payments)} payments from {csv_path.name}")
    if duplicates:
        print(f"Duplicate names detected: {', '.join(sorted(duplicates))}")
    if args.dry_run:
        print("MODE: DRY RUN (will fill forms but cancel instead of saving)")
    print()

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()
        page.set_default_timeout(ACTION_TIMEOUT)

        try:
            login(page)

            for i, payment in enumerate(payments, 1):
                name = payment["name"]
                date = payment["date"]
                amount = payment["amount"]
                print(f"\n[{i}/{len(payments)}]", end="")

                try:
                    success, method, error, note = post_payment(page, name, date, amount, dry_run=args.dry_run)

                    if success:
                        results.append({"name": name, "date": date, "amount": amount,
                                        "status": "OK", "method": method, "note": note})
                    elif method == "FLAGGED":
                        results.append({"name": name, "date": date, "amount": amount,
                                        "status": "FLAGGED", "method": "", "reason": error, "note": note})
                    else:
                        results.append({"name": name, "date": date, "amount": amount,
                                        "status": "FAILED", "method": "", "reason": error, "note": note})

                except PlaywrightTimeout:
                    screenshot(page, f"error_timeout_{name.replace(' ', '_')}")
                    print(f"  ERROR: Timed out for {name}")
                    results.append({"name": name, "date": date, "amount": amount,
                                    "status": "TIMEOUT", "method": ""})
                except Exception as e:
                    screenshot(page, f"error_{name.replace(' ', '_')}")
                    print(f"  ERROR: {e}")
                    results.append({"name": name, "date": date, "amount": amount,
                                    "status": "FAILED", "method": "", "reason": str(e)})

        except PlaywrightTimeout as e:
            screenshot(page, "error_timeout")
            print(f"ERROR: Timed out during setup - {e}")
            sys.exit(1)
        except Exception as e:
            screenshot(page, "error_unexpected")
            print(f"ERROR: {e}")
            sys.exit(1)
        finally:
            print("\nClosing browser.")
            browser.close()

    send_reports(results, duplicates, csv_date_display, dry_run=args.dry_run)


if __name__ == "__main__":
    run()
