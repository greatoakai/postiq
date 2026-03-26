import argparse
import csv
import os
import sys
from datetime import datetime
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

# Timeout for page actions (ms)
ACTION_TIMEOUT = 30000


def screenshot(page, name):
    """Save a timestamped screenshot to the logs directory."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOG_DIR / f"{ts}_{name}.png"
    page.screenshot(path=str(path))
    print(f"  Screenshot: {path.name}")
    return path


def read_csv(csv_path):
    """Read payment CSV and return list of dicts with name and amount."""
    payments = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("Full Name", "").strip()
            raw_amount = row.get("Base Amount", "").strip()
            if not name or not raw_amount:
                continue
            # Skip summary/totals rows
            if name.upper() in ("TOTALS", "TOTAL", "GRAND TOTAL", "SUM"):
                continue
            amount = raw_amount.replace("$", "").replace(",", "")
            try:
                amount = f"{float(amount):.2f}"
            except ValueError:
                print(f"  WARNING: Skipping row - invalid amount '{raw_amount}' for {name}")
                continue
            payments.append({"name": name, "amount": amount})
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


def navigate_to_billing(page):
    """Navigate to the Billing dashboard."""
    print("Navigating to Billing...")
    page.click("text=Billing")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)


def click_take_payment(page):
    """Click the Take Payment button on the Billing dashboard."""
    print("  Clicking Take Payment...")
    tp_btn = page.locator("text=Take Payment").first
    tp_btn.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)


def select_client(page, name):
    """Select a client from the Search Charges autocomplete."""
    print(f"  Searching for client: {name}")

    client_input = page.locator("#token-input-user_id_patient")
    client_input.click()

    # Type last name for autocomplete matching
    last_name = name.split()[-1]
    first_name = name.split()[0]
    page.keyboard.type(last_name, delay=100)
    page.wait_for_timeout(2000)

    # Find and click the matching client in the dropdown
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

    # Click Search to load the payment form
    print("  Clicking Search...")
    page.locator("button:has-text('Search')").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)


def fill_payment_form(page, amount):
    """Fill in the payment form fields."""
    # Find and fill Payment Amount (look for input with value "0.00")
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

    # Select External Credit Card
    print("  Selecting External Credit Card...")
    ext_cc = page.locator("text=External Credit Card")
    if ext_cc.is_visible():
        ext_cc.click()

    # Fill Reference/Check # with "Square"
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


def post_payment(page, name, amount, dry_run=False):
    """Post a single payment. Returns True on success, False on failure."""
    print(f"\n--- Payment: {name} — ${amount} ---")

    # Step 1: Click Take Payment
    click_take_payment(page)

    # Step 2: Select client and search
    select_client(page, name)
    screenshot(page, f"payment_{name.replace(' ', '_')}_01_form")

    # Step 3: Fill payment form
    fill_payment_form(page, amount)
    screenshot(page, f"payment_{name.replace(' ', '_')}_02_filled")

    if dry_run:
        print("  DRY RUN: Clicking Cancel instead of saving.")
        page.click("text=Cancel")
        page.wait_for_load_state("networkidle")
        return True

    # Step 4: Click Continue
    print("  Clicking Continue...")
    page.click("text=Continue")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    screenshot(page, f"payment_{name.replace(' ', '_')}_03_continue")

    # Step 5: Click Save Payment
    print("  Clicking Save Payment...")
    page.click("text=Save Payment")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    screenshot(page, f"payment_{name.replace(' ', '_')}_04_saved")

    print(f"  Payment saved for {name}.")
    return True


def generate_report(results, duplicates, dry_run=False):
    """Generate a summary report."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = "DRYRUN" if dry_run else "POSTED"
    report_path = LOG_DIR / f"{ts}_report_{mode}.txt"

    lines = []
    lines.append(f"PostIQ Payment Report — {mode}")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)

    if duplicates:
        lines.append("")
        lines.append("DUPLICATE NAMES — STAFF REVIEW REQUIRED:")
        for name in sorted(duplicates):
            count = sum(1 for r in results if r["name"] == name)
            lines.append(f"  {name} — {count} entries")
        lines.append("")

    lines.append(f"{'Status':<10} {'Name':<30} {'Amount':>10}")
    lines.append("-" * 52)

    total_success = 0
    total_amount = 0.0
    for r in results:
        status = r["status"]
        lines.append(f"{status:<10} {r['name']:<30} ${r['amount']:>9}")
        if status == "OK":
            total_success += 1
            total_amount += float(r["amount"])

    lines.append("-" * 52)
    lines.append(f"Total: {total_success}/{len(results)} payments — ${total_amount:.2f}")

    report_text = "\n".join(lines)
    report_path.write_text(report_text)
    print(f"\nReport saved: {report_path.name}")
    print(report_text)
    return report_path


def run():
    parser = argparse.ArgumentParser(description="PostIQ — Credit Card Payment Bot")
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
            navigate_to_billing(page)

            for i, payment in enumerate(payments, 1):
                name = payment["name"]
                amount = payment["amount"]
                print(f"\n[{i}/{len(payments)}]", end="")

                try:
                    post_payment(page, name, amount, dry_run=args.dry_run)
                    results.append({"name": name, "amount": amount, "status": "OK"})
                except PlaywrightTimeout:
                    screenshot(page, f"error_timeout_{name.replace(' ', '_')}")
                    print(f"  ERROR: Timed out for {name}")
                    results.append({"name": name, "amount": amount, "status": "TIMEOUT"})
                    try:
                        navigate_to_billing(page)
                    except Exception:
                        print("  Could not recover. Stopping.")
                        break
                except Exception as e:
                    screenshot(page, f"error_{name.replace(' ', '_')}")
                    print(f"  ERROR: {e}")
                    results.append({"name": name, "amount": amount, "status": "FAILED"})
                    try:
                        navigate_to_billing(page)
                    except Exception:
                        print("  Could not recover. Stopping.")
                        break

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

    generate_report(results, duplicates, dry_run=args.dry_run)


if __name__ == "__main__":
    run()
