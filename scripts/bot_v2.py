import argparse
import csv
import io
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from scripts.aws import (
    is_aws_enabled, get_credentials, upload_to_s3, upload_bytes_to_s3,
    download_from_s3, list_s3_prefix, list_processed_keys,
)

# Resolve paths relative to project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(PROJECT_ROOT / ".env")

# Name aliases: map Square names to TherapyAppointment names
ALIASES_FILE = Path(__file__).resolve().parent / "name_aliases.json"
NAME_ALIASES = {}
if ALIASES_FILE.exists():
    NAME_ALIASES = json.loads(ALIASES_FILE.read_text())


def resolve_name(name):
    """Return the TA name for a given Square name, using aliases if defined."""
    return NAME_ALIASES.get(name, name)

# Credentials: Secrets Manager on AWS, .env locally
USERNAME, PASSWORD = get_credentials()
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

ACTION_TIMEOUT = 30000

# Track whether appointment date filters have been set this session
_filters_set = False


def screenshot(page, name):
    """Save a timestamped screenshot locally and upload to S3 if configured."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{name}.png"
    path = LOG_DIR / filename
    page.screenshot(path=str(path))
    print(f"  Screenshot: {path.name}")
    s3_key = upload_to_s3(path, f"screenshots/{filename}")
    return s3_key or str(path)


def read_csv(csv_path):
    """Read payment CSV and return list of dicts with name, date, and amount."""
    payments = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        lines = f.readlines()
    # Skip label rows (e.g., "SUCCESSFUL PAYMENTS") that have no commas
    data_lines = [line for line in lines if "," in line]
    reader = csv.DictReader(io.StringIO("".join(data_lines)))
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


def recover_to_dashboard(page):
    """Navigate back to the dashboard to reset browser state between payments."""
    try:
        if "dashboard" not in (page.url or ""):
            page.goto("https://portal.therapyappointment.com/index.cfm/dashboard",
                      wait_until="domcontentloaded", timeout=15000)
            page.wait_for_load_state("networkidle")
    except Exception as e:
        print(f"  WARNING: Could not recover to dashboard: {e}")


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
    """Search for a client by first and last name. Returns True if a unique match was clicked."""
    first_name = name.split()[0]
    last_name = name.split()[-1]
    # Use first 3 letters for search to allow nickname/variation matching
    # e.g., "Mat" matches both "Matthew" and "Matt"
    first_search = first_name[:3]
    last_search = last_name[:3]

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

    # Check results — match using all name parts for compound/hyphenated names
    name_parts = [part.lower() for part in name.replace("-", " ").split()]
    all_links = page.locator("table a, .client-list a, a[href*='people']").all()
    matching = []
    for link in all_links:
        link_text = (link.text_content() or "").lower()
        if all(part in link_text for part in name_parts):
            matching.append(link)

    if len(matching) == 0:
        raise Exception(f"Client '{name}' not found in search results")
    elif len(matching) > 1:
        raise Exception(f"FLAG: Multiple matches for '{name}' — needs manual review")

    print(f"  Found client: {matching[0].text_content().strip()}")
    matching[0].click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    return True


def navigate_to_appointments(page):
    """Click the Appointments tab on the client profile."""
    print("  Clicking Appointments tab...")
    page.click("text=Appointments")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)


def ensure_date_filters(page):
    """Ensure the Appointments filter dates are set to the current year.

    Sets From = 01/01/{year} and To = 12/31/{year}. Once verified or set,
    skips on subsequent calls within the same login session.
    """
    global _filters_set
    if _filters_set:
        return

    year = datetime.now().year
    expected_from = f"01/01/{year}"
    expected_to = f"12/31/{year}"

    from_input = page.locator("input#span_startdate")
    to_input = page.locator("input#span_enddate")

    current_from = from_input.input_value()
    current_to = to_input.input_value()

    if current_from == expected_from and current_to == expected_to:
        print(f"  Filters already set: {expected_from} — {expected_to}")
        _filters_set = True
        return

    print(f"  Setting filters: From={expected_from}, To={expected_to}")

    # Masked inputs auto-insert slashes — type digits only
    from_digits = expected_from.replace("/", "")
    to_digits = expected_to.replace("/", "")

    if current_from != expected_from:
        from_input.click(click_count=3)
        page.keyboard.press("Backspace")
        page.keyboard.type(from_digits, delay=50)
        page.keyboard.press("Tab")
        page.wait_for_timeout(500)

    if current_to != expected_to:
        to_input.click(click_count=3)
        page.keyboard.press("Backspace")
        page.keyboard.type(to_digits, delay=50)
        page.keyboard.press("Tab")
        page.wait_for_timeout(500)

    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    screenshot(page, "filters_set")

    _filters_set = True
    print(f"  Filters set: {expected_from} — {expected_to}")


def click_appointment_by_date(page, date_str, name):
    """Find and click an appointment row matching the given date."""
    # Convert date to MM/DD/YYYY format to match TA's display
    ta_date = date_str
    if "-" in date_str:
        # Convert YYYY-MM-DD to MM/DD/YYYY
        parts = date_str.split("-")
        if len(parts) == 3:
            ta_date = f"{parts[1]}/{parts[2]}/{parts[0]}"
    print(f"  Looking for appointment on {ta_date}...")

    date_links = page.locator(f"a:has-text('{ta_date}')").all()

    if len(date_links) == 0:
        raise Exception(f"No appointment found on {date_str} for {name}")
    elif len(date_links) > 1:
        raise Exception(f"FLAG: Multiple appointments on {date_str} for {name} — needs manual review")

    print(f"  Found appointment: {date_links[0].text_content().strip()}")
    date_links[0].click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)


def click_accept_payment(page):
    """Click the Accept Payment button on the appointment summary."""
    print("  Clicking Accept Payment...")
    page.click("text=Accept Payment")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

    # Handle modal: "Would you like to accept payment for this one appointment?"
    yes_btn = page.locator("text=Yes, accept payment for this appointment")
    if yes_btn.is_visible(timeout=3000):
        print("  Confirming: Yes, accept payment for this appointment")
        yes_btn.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)


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
    """V2 flow: Clients > Appointments > Accept Payment."""
    print(f"  [V2] Clients > Appointments > Accept Payment")

    navigate_to_clients(page)
    search_client(page, name)
    navigate_to_appointments(page)
    ensure_date_filters(page)
    click_appointment_by_date(page, date, name)
    click_accept_payment(page)
    screenshot(page, f"payment_{name.replace(' ', '_')}_01_form")

    fill_payment_form(page, amount)
    screenshot(page, f"payment_{name.replace(' ', '_')}_02_filled")

    return submit_payment(page, name, dry_run)


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
    Returns (success: bool, method: str, error: str or None)
    """
    print(f"\n--- Payment: {name} — ${amount} on {date} ---")

    # --- Attempt 1: V2 flow (always try first) ---
    v2_error = None
    try:
        result = post_payment_v2(page, name, date, amount, dry_run)
        if not result:
            raise Exception("V2 submit_payment returned failure")
        return True, "V2", None
    except Exception as e:
        v2_error = str(e)
        # Reset filters flag since we're leaving the appointments page
        global _filters_set
        _filters_set = False
        if "FLAG" in v2_error:
            # Flagged items should NOT fallback — they need manual review
            return False, "FLAGGED", v2_error
        print(f"  V2 failed: {v2_error}")
        print(f"  Falling back to V1...")

    # --- Attempt 2: V1 fallback ---
    try:
        result = post_payment_v1(page, name, amount, dry_run)
        if not result:
            raise Exception("V1 submit_payment returned failure")
        return True, "V1", None
    except Exception as e:
        v1_error = str(e)
        if "FLAG" in v1_error:
            return False, "FLAGGED", v1_error
        print(f"  V1 also failed: {v1_error}")

    # --- Both failed ---
    return False, "FAILED", f"V2: {v2_error}; V1: {v1_error}"


def generate_report(results, duplicates, dry_run=False, source_s3_key=None):
    """Generate a summary report as both text (human) and JSON (dashboard)."""
    import json as _json

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = "DRYRUN" if dry_run else "POSTED"

    total_success = sum(1 for r in results if r["status"] == "OK")
    total_amount = sum(float(r["amount"]) for r in results if r["status"] == "OK")

    # ── JSON report (machine-readable, used by Streamlit dashboard) ──
    report_data = {
        "mode": mode,
        "generated": datetime.now().isoformat(),
        "source_s3_key": source_s3_key,
        "total_payments": len(results),
        "success_count": total_success,
        "total_amount": total_amount,
        "duplicates": sorted(duplicates) if duplicates else [],
        "payments": results,
    }
    json_str = _json.dumps(report_data, indent=2)
    json_path = LOG_DIR / f"{ts}_report_{mode}.json"
    json_path.write_text(json_str)

    # ── Text report (human-readable, printed to console) ──
    txt_path = LOG_DIR / f"{ts}_report_{mode}.txt"
    lines = []
    lines.append(f"PostIQ Payment Report — {mode}")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 72)

    if duplicates:
        lines.append("")
        lines.append("DUPLICATE NAMES — STAFF REVIEW REQUIRED:")
        for name in sorted(duplicates):
            count = sum(1 for r in results if r["name"] == name)
            lines.append(f"  {name} — {count} entries")
        lines.append("")

    lines.append(f"{'Status':<10} {'Method':<8} {'Name':<25} {'Date':<12} {'Amount':>10}")
    lines.append("-" * 68)

    for r in results:
        status = r["status"]
        method = r.get("method", "")
        date = r.get("date", "")
        lines.append(f"{status:<10} {method:<8} {r['name']:<25} {date:<12} ${r['amount']:>9}")

    lines.append("-" * 68)
    lines.append(f"Total: {total_success}/{len(results)} payments — ${total_amount:.2f}")

    flagged = [r for r in results if r["status"] == "FLAGGED"]
    if flagged:
        lines.append("")
        lines.append("FLAGGED FOR MANUAL REVIEW:")
        for r in flagged:
            lines.append(f"  {r['name']} — {r.get('reason', 'unknown')}")

    failed = [r for r in results if r["status"] == "FAILED"]
    if failed:
        lines.append("")
        lines.append("FAILED — COULD NOT POST:")
        for r in failed:
            lines.append(f"  {r['name']} — {r.get('reason', 'unknown')}")

    report_text = "\n".join(lines)
    txt_path.write_text(report_text)
    print(f"\nReport saved: {txt_path.name}")
    print(report_text)

    # Upload both to S3
    upload_bytes_to_s3(report_text, f"reports/{txt_path.name}", "text/plain")
    s3_key = upload_bytes_to_s3(json_str, f"reports/{json_path.name}", "application/json")
    return s3_key or str(json_path)


def resolve_csv(args):
    """Resolve the CSV source: S3 key, scheduled pickup, or local file.
    Returns (csv_path, s3_key) where csv_path is a local file and s3_key
    is the S3 key if applicable (None for local files).
    """
    # Mode 1: Scheduled — pick up latest unprocessed CSV from S3
    if args.scheduled:
        if not is_aws_enabled():
            print("ERROR: --scheduled requires S3_BUCKET to be set")
            sys.exit(1)

        # Find newest CSV in uploads/ that hasn't been processed yet.
        # A CSV is "processed" if a matching report exists in reports/.
        all_keys = list_s3_prefix("uploads/")
        csv_keys = [k for k in all_keys if k.endswith(".csv")]
        processed = list_processed_keys()
        unprocessed = [k for k in csv_keys if k not in processed]

        if not unprocessed:
            print("No unprocessed CSV files found in S3. Nothing to do.")
            sys.exit(0)
        s3_key = unprocessed[0]
        print(f"Found unprocessed CSV: {s3_key}")

        local_path = download_from_s3(s3_key)
        return Path(local_path), s3_key

    # Mode 2: Explicit S3 key
    if args.s3_key:
        local_path = download_from_s3(args.s3_key)
        if not local_path:
            print(f"ERROR: Could not download from S3: {args.s3_key}")
            sys.exit(1)
        return Path(local_path), args.s3_key

    # Mode 3: Local file path
    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        alt_path = DATA_DIR / csv_path.name
        if alt_path.exists():
            csv_path = alt_path
        else:
            print(f"ERROR: CSV file not found: {csv_path}")
            sys.exit(1)
    return csv_path, None


def run():
    parser = argparse.ArgumentParser(description="PostIQ — Payment Bot with Fallback")
    parser.add_argument("csv_file", nargs="?", default=None,
                        help="Path to the payment CSV file")
    parser.add_argument("--s3-key", default=None,
                        help="S3 key for the CSV file (e.g., uploads/file.csv)")
    parser.add_argument("--scheduled", action="store_true",
                        help="Scheduled mode: pick up latest unprocessed CSV from S3")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fill forms but don't submit (cancels instead of saving)")
    args = parser.parse_args()

    if not args.csv_file and not args.s3_key and not args.scheduled:
        parser.error("Provide a csv_file, --s3-key, or --scheduled")

    csv_path, s3_key = resolve_csv(args)

    if not USERNAME or not PASSWORD:
        print("ERROR: TA_USERNAME and TA_PASSWORD must be set")
        sys.exit(1)

    payments = read_csv(csv_path)
    if not payments:
        print("ERROR: No valid payment rows found in CSV.")
        sys.exit(1)

    duplicates = detect_duplicates(payments)

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
                csv_name = payment["name"]
                name = resolve_name(csv_name)
                if name != csv_name:
                    print(f"\n[{i}/{len(payments)}] (alias: {csv_name} -> {name})", end="")
                else:
                    print(f"\n[{i}/{len(payments)}]", end="")
                date = payment["date"]
                amount = payment["amount"]

                try:
                    success, method, error = post_payment(page, name, date, amount, dry_run=args.dry_run)

                    if success:
                        results.append({"name": csv_name, "date": date, "amount": amount,
                                        "status": "OK", "method": method})
                    elif method == "FLAGGED":
                        results.append({"name": csv_name, "date": date, "amount": amount,
                                        "status": "FLAGGED", "method": "", "reason": error})
                    else:
                        results.append({"name": csv_name, "date": date, "amount": amount,
                                        "status": "FAILED", "method": "", "reason": error})

                except PlaywrightTimeout:
                    screenshot(page, f"error_timeout_{name.replace(' ', '_')}")
                    print(f"  ERROR: Timed out for {name}")
                    results.append({"name": csv_name, "date": date, "amount": amount,
                                    "status": "TIMEOUT", "method": ""})
                except Exception as e:
                    screenshot(page, f"error_{name.replace(' ', '_')}")
                    print(f"  ERROR: {e}")
                    results.append({"name": csv_name, "date": date, "amount": amount,
                                    "status": "FAILED", "method": "", "reason": str(e)})
                finally:
                    recover_to_dashboard(page)

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

    generate_report(results, duplicates, dry_run=args.dry_run, source_s3_key=s3_key)

    # Send email report
    try:
        from scripts.email_report import send_report
        send_report(results, duplicates, dry_run=args.dry_run)
    except Exception as e:
        print(f"WARNING: Could not send email report: {e}")

    # Clean up temp file if downloaded from S3
    if s3_key and csv_path.exists() and str(csv_path).startswith(tempfile.gettempdir()):
        csv_path.unlink()
        print(f"Cleaned up temp file: {csv_path}")


if __name__ == "__main__":
    run()
