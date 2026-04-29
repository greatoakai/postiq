import argparse
import csv
import os
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Resolve paths relative to project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(PROJECT_ROOT / ".env")

USERNAME = os.getenv("TA_USERNAME")
PASSWORD = os.getenv("TA_PASSWORD")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

ACTION_TIMEOUT = 30000


# ─────────────────────────────────────────────
# NAME MATCHING — nicknames + explicit aliases
# ─────────────────────────────────────────────
# Square sometimes stores a client's preferred/nickname while TA stores their
# legal/formal name (or vice versa). When the exact name doesn't match, the bot
# tries variations from this dictionary before giving up.
#
# To add a missing nickname mapping, just add it here. To override a specific
# client's name with a one-off mapping, use scripts/name_aliases.json instead.

NICKNAMES = {
    "alexander": ["alex"],
    "alexandra": ["alex", "lexi"],
    "barbara": ["barb", "barbi", "babs"],
    "benjamin": ["ben"],
    "catherine": ["cathy", "cat", "kate"],
    "christine": ["chris", "christy", "tina"],
    "christina": ["chris", "christy", "tina"],
    "christopher": ["chris"],
    "daniel": ["dan", "danny"],
    "david": ["dave", "davey"],
    "deborah": ["deb", "debbie"],
    "donald": ["don", "donny"],
    "dorothy": ["dot", "dottie"],
    "edward": ["ed", "eddie", "ted"],
    "elizabeth": ["liz", "beth", "lizzy", "eliza"],
    "evelyn": ["eve", "evie"],
    "frederick": ["fred", "freddy"],
    "gregory": ["greg"],
    "james": ["jim", "jimmy", "jamie"],
    "jennifer": ["jen", "jenny"],
    "jonathan": ["jon", "john"],
    "joseph": ["joe", "joey"],
    "joshua": ["josh"],
    "judith": ["judy", "judi"],
    "katherine": ["kate", "kathy", "katie", "kat"],
    "lawrence": ["larry"],
    "leonard": ["leo", "lenny"],
    "madeline": ["maddy", "maddie"],
    "margaret": ["maggie", "meg", "peggy", "marge"],
    "matthew": ["matt"],
    "mercedes": ["cede", "mercy", "sadie"],
    "michael": ["mike", "mikey"],
    "nathaniel": ["nate", "nathan"],
    "nicholas": ["nick", "nicky"],
    "pamela": ["pam"],
    "patricia": ["pat", "patty", "trish"],
    "raelyn": ["rae"],
    "rebecca": ["becca", "becky"],
    "rebekah": ["becca", "becky"],
    "richard": ["rick", "rich", "dick"],
    "robert": ["rob", "bob", "bobby", "robbie"],
    "samuel": ["sam", "sammy"],
    "stephanie": ["steph"],
    "susan": ["sue", "susie"],
    "theodore": ["ted", "teddy", "theo"],
    "thomas": ["tom", "tommy"],
    "timothy": ["tim", "timmy"],
    "victoria": ["vicky", "tori"],
    "william": ["will", "bill", "billy", "liam"],
    "zachary": ["zach", "zack"],
}

# Reverse lookup: nickname → list of formal names
# (e.g., "ted" → ["edward", "theodore"])
_NICK_REVERSE = {}
for _formal, _nicks in NICKNAMES.items():
    for _nick in _nicks:
        _NICK_REVERSE.setdefault(_nick, []).append(_formal)


# ─────────────────────────────────────────────
# Explicit name aliases (one-off overrides)
# ─────────────────────────────────────────────
# scripts/name_aliases.json maps specific Square names to specific TA names.
# Use this for clients whose nicknames aren't in the NICKNAMES dictionary
# (e.g., legal name changes, uncommon nicknames, or typos in either system).
#
# Format: { "Square Full Name": "TA Full Name", ... }
# Keys starting with "_" (e.g., "_comment") are ignored.

_ALIASES_FILE = Path(__file__).resolve().parent / "name_aliases.json"
NAME_ALIASES = {}
if _ALIASES_FILE.exists():
    try:
        import json
        _raw = json.loads(_ALIASES_FILE.read_text())
        # Drop comment keys
        NAME_ALIASES = {k: v for k, v in _raw.items() if not k.startswith("_")}
        if NAME_ALIASES:
            print(f"Loaded {len(NAME_ALIASES)} name alias(es) from name_aliases.json")
    except Exception as e:
        print(f"WARNING: Could not load name_aliases.json: {e}")


def resolve_name(name):
    """Return the TA-side name for a given Square name.

    If the name is in NAME_ALIASES, return the override.
    Otherwise return the original name unchanged.
    """
    return NAME_ALIASES.get(name, name)


# Common name suffixes that should not be treated as the last name
_NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v", "esq", "esq."}


def split_first_last(name):
    """Split a full name into (first_name, last_name), ignoring suffixes.

    Strips common suffixes like Jr, Sr, II, III so they aren't mistaken for
    the last name.

    Examples:
        "Jeffrey Paul Keck Jr"    → ("Jeffrey", "Keck")
        "Christopher Holland Jr"  → ("Christopher", "Holland")
        "Landon Michael Thorne"   → ("Landon", "Thorne")
        "Jane Doe"                → ("Jane", "Doe")
    """
    parts = name.split()
    if len(parts) < 2:
        return (name, "")

    first = parts[0]

    # Walk backwards from the end, skipping suffixes
    last = parts[-1]
    for i in range(len(parts) - 1, 0, -1):
        if parts[i].lower().rstrip(".") in _NAME_SUFFIXES:
            continue
        last = parts[i]
        break

    return (first, last)


def get_name_variations(name):
    """Generate alternate names to try if the exact name doesn't match.

    Returns a list of (variation_name, variation_type) tuples in priority order.
    Used by search_client() and select_client_v1() when the original name fails.

    Examples:
        get_name_variations("Bob Smith")
            → [("Robert Smith", "formal name")]
        get_name_variations("Robert Smith")
            → [("Rob Smith", "nickname"), ("Bob Smith", "nickname"), ...]
    """
    first, last = split_first_last(name)
    if not first or not last:
        return []

    first_lower = first.lower()
    variations = []

    # 1. Try common nicknames of the first name (e.g., Robert → Bob, Rob, Robbie)
    if first_lower in NICKNAMES:
        for nick in NICKNAMES[first_lower]:
            variations.append((nick.capitalize() + " " + last, "nickname"))

    # 2. Try formal names if the given first name is itself a nickname
    #    (e.g., Bob → Robert; Ted → Edward, Theodore)
    if first_lower in _NICK_REVERSE:
        for formal in _NICK_REVERSE[first_lower]:
            variations.append((formal.capitalize() + " " + last, "formal name"))

    return variations


def normalize_name(name):
    """Normalize a name by stripping accents and fixing common encoding issues.

    Handles cases like 'ChloÃ©' (UTF-8 bytes decoded as Latin-1) by
    re-encoding and decoding, then stripping to ASCII-friendly form.
    e.g., 'ChloÃ© Ray' → 'Chloe Ray'
    """
    # First, try to fix mojibake (UTF-8 bytes misread as Latin-1)
    try:
        fixed = name.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        fixed = name

    # Strip accents: é → e, ñ → n, etc.
    nfkd = unicodedata.normalize('NFKD', fixed)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_name


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
            if name.upper().startswith(("TOTAL", "GRAND TOTAL", "SUM")):
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

    # Permanently disable the Beacon (HelpScout) chat widget for this session.
    # Even when "closed" via JS API, the iframe stays in the DOM and intercepts
    # pointer events. We hide the entire container with CSS so it can never
    # block clicks. Re-injected on every recover_to_dashboard() in case TA
    # reloads the widget.
    suppress_beacon_widget(page)
    dismiss_popups(page)


def suppress_beacon_widget(page):
    """Inject CSS to hide the HelpScout Beacon widget entirely.

    The Beacon iframe overlays the page and intercepts clicks even when the
    visible popup has been dismissed. The bot never needs the chat widget,
    so we hide it permanently for the duration of the session by injecting
    a <style> tag that forces display:none on the container.

    Idempotent — safe to call multiple times. Re-call after any page reload
    that might re-inject Beacon (e.g., recover_to_dashboard navigation).
    """
    try:
        page.add_style_tag(content="""
            #beacon-container,
            #beacon-container *,
            iframe[title*="Help Scout"],
            iframe[title*="Beacon"],
            div[class*="BeaconContainer"] {
                display: none !important;
                visibility: hidden !important;
                pointer-events: none !important;
            }
        """)
    except Exception as e:
        # Don't fail the run if style injection fails — fall back to dismiss_popups
        print(f"  WARNING: Could not suppress Beacon widget: {e}")


def dismiss_popups(page):
    """Close any overlay/chat widgets that intercept clicks.

    TherapyAppointment uses a Beacon (HelpScout) widget that occasionally
    pops up with announcements. The widget overlays the page and blocks
    clicks elsewhere with "subtree intercepts pointer events" errors.

    The Beacon close button is hidden until the user hovers over the popup,
    so a normal click_if_visible check won't see it. This function uses
    multiple strategies in order:
      1. Beacon's JavaScript API (cleanest if available)
      2. Force-click the hidden close button via JS
      3. Force-click via Playwright with force=True

    Safe to call repeatedly — does nothing if no popup is present.
    """
    # Strategy 1: Use Beacon's JS API to close the widget directly.
    try:
        result = page.evaluate("""
            () => {
                if (typeof window.Beacon === 'function') {
                    try { window.Beacon('close'); return 'closed-via-api'; }
                    catch (e) { return 'api-error: ' + e.message; }
                }
                return null;
            }
        """)
        if result == "closed-via-api":
            print("  Dismissed Beacon popup via JS API")
            page.wait_for_timeout(300)
    except Exception:
        pass

    # Strategy 2: Force-click any known close button via JS, regardless of CSS visibility.
    try:
        clicked = page.evaluate("""
            () => {
                const selectors = [
                    '[data-cy="beacon-close-button"]',
                    '[data-cy="beacon-message-close-button"]',
                    'button.BeaconCloseButton',
                    'button[aria-label="Close message"]',
                    'button[aria-label="Close"]'
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) { el.click(); return sel; }
                }
                return null;
            }
        """)
        if clicked:
            print(f"  Dismissed popup via force JS click ({clicked})")
            page.wait_for_timeout(300)
    except Exception:
        pass

    # Strategy 3: Playwright force-click as last resort.
    selectors = [
        '[data-cy="beacon-close-button"]',
        '[data-cy="beacon-message-close-button"]',
        'button.BeaconCloseButton',
        'button[aria-label="Close message"]',
        'button[aria-label="Close"]',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(force=True, timeout=1000)
                print(f"  Dismissed popup via force click ({sel})")
                page.wait_for_timeout(300)
                break
        except Exception:
            pass


def recover_to_dashboard(page):
    """Navigate back to the dashboard to reset browser state between payments.

    Called after a failed payment so the next client starts from a clean slate
    instead of inheriting whatever modal/page state the previous failure left behind.
    Also re-suppresses the Beacon widget (it can come back after navigation).
    Failure to recover is logged but does not raise — the next payment attempt will
    fall back through its own retry/fallback logic.
    """
    try:
        if "dashboard" not in (page.url or ""):
            print("  Recovering to dashboard...")
            page.goto(
                "https://portal.therapyappointment.com/index.cfm/dashboard",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            page.wait_for_load_state("networkidle")
        suppress_beacon_widget(page)
        dismiss_popups(page)
    except Exception as e:
        print(f"  WARNING: Could not recover to dashboard: {e}")


# =============================================================================
# V2 FLOW: Clients > Search > Appointments > Accept Payment
# =============================================================================

def navigate_to_clients(page):
    """Click Clients in the sidebar."""
    print("  Navigating to Clients...")
    dismiss_popups(page)  # Beacon widget can intercept the sidebar click
    page.click("text=Clients")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)


def _do_search(page, first_search, last_search):
    """Fill the search form and submit. Returns visible table rows."""
    visible_text_inputs = []
    for inp in page.locator("input[type='text']").all():
        if inp.is_visible():
            visible_text_inputs.append(inp)

    if len(visible_text_inputs) < 2:
        raise Exception(f"Expected at least 2 visible text inputs, found {len(visible_text_inputs)}")

    visible_text_inputs[0].fill(first_search)
    visible_text_inputs[1].fill(last_search)

    page.locator("button:has-text('Search')").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    screenshot(page, f"search_{first_search}_{last_search}")
    return page.locator("table tr").all()


def _match_rows(rows, match_parts):
    """Find table rows where all match_parts appear in the row text."""
    matching = []
    for row in rows:
        row_text = (row.text_content() or "").lower()
        if all(part in row_text for part in match_parts):
            links = row.locator("a")
            if links.count() > 0:
                matching.append((row, links.first))
    return matching


def _try_inactive_clients(page):
    """If the search returned 'We didn't find any results', click the
    'Inactive Clients' button to re-search including inactive clients.

    Returns the new table rows if the button was found and clicked,
    or None if the button isn't present (meaning the search did return results,
    or TA didn't offer the inactive fallback).
    """
    try:
        inactive_btn = page.locator("button:has-text('Inactive Clients')")
        if inactive_btn.is_visible(timeout=1000):
            print("  No active results — clicking 'Inactive Clients' to expand search...")
            inactive_btn.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)
            screenshot(page, "search_inactive")
            return page.locator("table tr").all()
    except Exception:
        pass
    return None


def _try_search(page, search_name):
    """Run one search attempt with the given name. Returns matching_rows list.

    If the initial search returns no results and TA offers an 'Inactive Clients'
    fallback button, clicks it and re-checks for matches.

    Helper used by search_client() to try multiple variations without duplicating
    the parsing/matching logic.
    """
    first, last = split_first_last(search_name)
    first_search = first[:3]
    last_search = last[:3]

    match_parts = []
    for part in [first.lower(), last.lower()]:
        match_parts.extend(part.split("-"))

    print(f"  Searching: First={first_search} (from {first}), Last={last_search} (from {last})")
    navigate_to_clients(page)
    rows = _do_search(page, first_search, last_search)
    matching = _match_rows(rows, match_parts)

    # If no results among active clients, try including inactive clients
    if len(matching) == 0:
        inactive_rows = _try_inactive_clients(page)
        if inactive_rows is not None:
            matching = _match_rows(inactive_rows, match_parts)

    return matching


def search_client(page, name):
    """Search for a client by first and last name. Returns True if a unique match was clicked.

    Resolution order (stops at first unique match):
        1. Explicit alias from name_aliases.json (resolve_name)
        2. Original name as-is
        3. Normalized name (strip accents / fix encoding)
        4. Nickname variations (Bob ↔ Robert, Liz ↔ Elizabeth, etc.)

    Also returns a note string if the CSV name contains a middle name or
    extra name part, or if the bot had to use an alternate name to find the client.
    """
    first, last = split_first_last(name)
    parts = name.split()
    # Middle parts: everything between first and last, excluding suffixes
    middle_parts = [p for p in parts[1:] if p != last and p.lower().rstrip(".") not in _NAME_SUFFIXES]

    note = None
    if middle_parts:
        extra = " ".join(middle_parts)
        note = f"Middle/extra name '{extra}' in CSV — verify in TherapyAppointment"
        print(f"  NOTE: {note}")

    # Step 1: Apply explicit alias if one exists.
    resolved = resolve_name(name)
    if resolved != name:
        print(f"  Alias applied: '{name}' → '{resolved}'")
        if note:
            note += f"; Name resolved via alias: '{name}' → '{resolved}'"
        else:
            note = f"Name resolved via alias: '{name}' → '{resolved}'"

    matched_name = None  # tracks which variation actually matched

    # Step 2: Try the resolved name as-is.
    matching_rows = _try_search(page, resolved)
    if len(matching_rows) == 1:
        matched_name = resolved

    # Step 3: If still no match, try the normalized version (encoding fix).
    if len(matching_rows) != 1:
        norm = normalize_name(resolved)
        if norm != resolved:
            print(f"  No match for '{resolved}', trying normalized: '{norm}'")
            matching_rows = _try_search(page, norm)
            if len(matching_rows) == 1:
                matched_name = norm
                if note:
                    note += f"; Name normalized: '{resolved}' → '{norm}'"
                else:
                    note = f"Name normalized: '{resolved}' → '{norm}'"

    # Step 4: If still no match, try nickname variations.
    if len(matching_rows) != 1:
        variations = get_name_variations(resolved)
        for var_name, var_type in variations:
            print(f"  No match yet, trying {var_type}: '{var_name}'")
            var_matches = _try_search(page, var_name)
            if len(var_matches) == 1:
                matching_rows = var_matches
                matched_name = var_name
                if note:
                    note += f"; Matched via {var_type}: '{name}' → '{var_name}'"
                else:
                    note = f"Matched via {var_type}: '{name}' → '{var_name}'"
                break
            elif len(var_matches) > 1:
                # Multiple matches on a variation — flag rather than guess.
                raise Exception(f"FLAG: Multiple matches for variation '{var_name}' of '{name}' — needs manual review")

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

    Converts CSV date (YYYY-MM-DD or MM/DD/YYYY) to TA display format
    (MM/DD/YYYY) before searching.

    Resolution order:
      1. Exact date match on the transaction date
      2. If no exact match, scan all visible appointment links for dates
         within 60 days prior. Pick the closest date to the original.
      3. If multiple appointments on the same closest date, flag for review.

    Returns a note string if a nearby (non-exact) date was used, or None
    if the exact date matched.
    """
    import re

    # Convert YYYY-MM-DD → MM/DD/YYYY to match TA's display format
    try:
        target_dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        try:
            target_dt = datetime.strptime(date_str, "%m/%d/%Y")
        except ValueError:
            target_dt = None

    ta_date = target_dt.strftime("%m/%d/%Y") if target_dt else date_str

    print(f"  Looking for appointment on {ta_date}...")

    # --- Step 1: Try exact date match ---
    date_links = page.locator(f"a:has-text('{ta_date}')").all()

    if len(date_links) == 1:
        print(f"  Found appointment: {date_links[0].text_content().strip()}")
        date_links[0].click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)
        return None  # exact match, no note needed

    if len(date_links) > 1:
        # Multiple rows on the same date — filter to only "Active" appointments
        # (rescheduled slots stay as rows with "Rescheduled to..." status)
        active_links = []
        for link in date_links:
            try:
                row = link.locator("xpath=ancestor::tr")
                row_text = (row.text_content() or "").strip()
                # Check that the row contains "Active" as a status,
                # but NOT "Rescheduled" or "Cancelled"
                if "\tActive" in row_text or row_text.endswith("Active"):
                    active_links.append(link)
            except Exception:
                continue

        if len(active_links) == 1:
            print(f"  Multiple rows on {ta_date}, picking Active appointment")
            active_links[0].click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1000)
            return None  # resolved via Active status

        raise Exception(
            f"FLAG: Multiple appointments on {ta_date} for {name} — needs manual review"
        )

    # --- Step 2: No exact match — scan for nearby dates (up to 60 days prior) ---
    if target_dt is None:
        raise Exception(f"No appointment found on {ta_date} for {name}")

    print(f"  No exact match on {ta_date} — searching within 60 days prior...")

    # Find all date links on the Appointments page
    # TA shows dates in format: MM/DD/YYYY (HH:MM AM/PM - HH:MM AM/PM)
    date_pattern = re.compile(r"(\d{2}/\d{2}/\d{4})")
    all_links = page.locator("a").all()

    # Parse each link's text for a date and measure distance from target
    candidates = []
    for link in all_links:
        try:
            text = (link.text_content() or "").strip()
            match = date_pattern.search(text)
            if not match:
                continue
            link_date_str = match.group(1)
            link_dt = datetime.strptime(link_date_str, "%m/%d/%Y")

            # Only consider dates within 60 days BEFORE the target (not after)
            days_diff = (target_dt - link_dt).days
            if 0 < days_diff <= 60:
                candidates.append({
                    "link": link,
                    "date_str": link_date_str,
                    "date": link_dt,
                    "days_diff": days_diff,
                    "text": text,
                })
        except Exception:
            continue

    if not candidates:
        raise Exception(f"No appointment found on {ta_date} for {name}")

    # Sort by proximity (closest first)
    candidates.sort(key=lambda c: c["days_diff"])

    # Check if the closest date has multiple appointments
    closest_date = candidates[0]["date_str"]
    same_date = [c for c in candidates if c["date_str"] == closest_date]

    if len(same_date) > 1:
        # Filter to only Active appointments (exclude Rescheduled/Cancelled)
        active_candidates = []
        for c in same_date:
            try:
                row = c["link"].locator("xpath=ancestor::tr")
                row_text = (row.text_content() or "").strip()
                if "\tActive" in row_text or row_text.endswith("Active"):
                    active_candidates.append(c)
            except Exception:
                continue

        if len(active_candidates) == 1:
            same_date = active_candidates
        else:
            raise Exception(
                f"FLAG: Multiple appointments near {ta_date} on {closest_date} "
                f"for {name} — needs manual review"
            )

    # Click the closest appointment (use same_date which may be active-filtered)
    chosen = same_date[0]
    print(
        f"  Nearest appointment found: {chosen['text'][:60]} "
        f"({chosen['days_diff']} day(s) before Square date)"
    )
    chosen["link"].click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

    note = (
        f"Date mismatch: Square={ta_date}, TA={chosen['date_str']} "
        f"({chosen['days_diff']}d prior) — please verify correct appointment"
    )
    return note


def click_accept_payment(page, name):
    """Click the Accept Payment button on the appointment summary.

    Returns a note string if the client has an outstanding balance (additional
    charges modal appeared), or None if clean.
    """
    print("  Clicking Accept Payment...")
    suppress_beacon_widget(page)
    dismiss_popups(page)
    # Use JS click as primary — Playwright's normal click fails ~10% of the time
    # because TA has overlays (Beacon iframe, notification banners, etc.) that
    # intercept pointer events even after suppression. JS click bypasses all of that.
    try:
        page.evaluate("""
            () => {
                const links = [...document.querySelectorAll('a, button')];
                const btn = links.find(el => el.textContent.trim().includes('Accept Payment'));
                if (btn) { btn.click(); return true; }
                return false;
            }
        """)
    except Exception:
        # Fallback to Playwright click if JS fails
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

    Returns (success: bool, note: str or None). Raises on hard failure
    so the caller can fall back to V1.
    """
    print(f"  [V2] Clients > Appointments > Accept Payment")

    # search_client() handles its own navigation to the Clients page
    _ok, name_note = search_client(page, name)
    navigate_to_appointments(page)
    ensure_date_filters(page)
    date_note = click_appointment_by_date(page, date, name)
    balance_note = click_accept_payment(page, name)
    screenshot(page, f"payment_{name.replace(' ', '_')}_01_form")

    # Combine notes (middle name + date mismatch + outstanding balance)
    notes = [n for n in (name_note, date_note, balance_note) if n]
    note = "; ".join(notes) if notes else None

    fill_payment_form(page, amount)
    screenshot(page, f"payment_{name.replace(' ', '_')}_02_filled")

    # submit_payment may return False if Save Payment didn't take.
    # Treat that as a hard failure so the caller can fall back.
    if not submit_payment(page, name, dry_run):
        raise Exception("V2 submit_payment returned failure")

    return True, note


# =============================================================================
# V1 FALLBACK: Billing > Take Payment > Search Charges
# =============================================================================

def navigate_to_billing(page):
    """Navigate to the Billing dashboard."""
    print("  Navigating to Billing...")
    dismiss_popups(page)  # Beacon widget can intercept the sidebar click
    page.click("text=Billing")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)


def _search_v1_autocomplete(page, first_name, last_name):
    """Type last name into autocomplete and try to find a match. Returns True if selected."""
    client_input = page.locator("#token-input-user_id_patient")
    client_input.click()

    page.keyboard.type(last_name, delay=100)
    page.wait_for_timeout(2000)

    dropdown_items = page.locator("[class*='token-input-dropdown'] li, "
                                  ".token-input-dropdown li, "
                                  "div.token-input-dropdown-facebook li")
    count = dropdown_items.count()

    for i in range(count):
        item = dropdown_items.nth(i)
        item_text = item.text_content() or ""
        if "type in" in item_text.lower() or "search" in item_text.lower():
            continue
        if first_name.lower() in item_text.lower() and last_name.lower() in item_text.lower():
            print(f"  Selected: {item_text.strip()}")
            item.click()
            page.wait_for_timeout(500)
            return True, count
    return False, count


def select_client_v1(page, name):
    """Select a client via the Search Charges token-input autocomplete.

    Resolution order (stops at first match):
        1. Explicit alias from name_aliases.json (resolve_name)
        2. Original name as-is
        3. Normalized name (strip accents / fix encoding)
        4. Nickname variations (Bob ↔ Robert, etc.)
    """
    print(f"  Searching for client: {name}")

    # Step 1: Apply explicit alias if one exists.
    resolved = resolve_name(name)
    if resolved != name:
        print(f"  Alias applied: '{name}' → '{resolved}'")

    def _try_select(try_name):
        """Try one variation. Returns (selected, count). Clears the input first."""
        client_input = page.locator("#token-input-user_id_patient")
        client_input.click(click_count=3)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(500)
        first, last = split_first_last(try_name)
        return _search_v1_autocomplete(page, first, last)

    # Step 2: Try the resolved name as-is.
    selected, count = _try_select(resolved)

    # Step 3: If no match, try normalized name.
    if not selected:
        norm = normalize_name(resolved)
        if norm != resolved:
            print(f"  No match for '{resolved}', trying normalized: '{norm}'")
            selected, count = _try_select(norm)

    # Step 4: If still no match, try nickname variations.
    if not selected:
        variations = get_name_variations(resolved)
        for var_name, var_type in variations:
            print(f"  No match yet, trying {var_type}: '{var_name}'")
            selected, count = _try_select(var_name)
            if selected:
                break

    if not selected:
        raise Exception(f"Client '{name}' not found in autocomplete ({count} results)")

    print("  Clicking Search...")
    page.locator("button:has-text('Search')").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)


def scrape_allocation_date(page):
    """Scrape the appointment date from the V1 Payment Distribution table.

    After select_client_v1() searches for charges, the Payment Distribution
    table shows outstanding appointments. The first row that is NOT an
    'Unapplied Payment' contains the appointment date the payment will be
    allocated to.

    Returns the date string (MM/DD/YYYY) or None if not found.
    """
    import re
    date_pattern = re.compile(r'(\d{2}/\d{2}/\d{4})')
    try:
        rows = page.locator("table tr").all()
        for row in rows:
            cells = row.locator("td").all()
            if len(cells) < 3:
                continue
            first_cell = (cells[0].text_content() or "").strip()
            second_cell = (cells[1].text_content() or "").strip()
            if "Unapplied" in second_cell:
                continue
            match = date_pattern.match(first_cell)
            if match:
                return match.group(1)
    except Exception as e:
        print(f"  WARNING: Could not scrape allocation date: {e}")
    return None


def scrape_confirmation_date(page):
    """Scrape Date of Svc from the payment confirmation page.

    After Save Payment, TA shows a confirmation with a Distribution table
    listing which appointment(s) the payment was allocated to. This is the
    definitive source — it shows where TA actually put the money.

    Returns the date string (MM/DD/YYYY) or None if not found.
    """
    import re
    date_pattern = re.compile(r'(\d{2}/\d{2}/\d{4})')
    try:
        rows = page.locator("table tr").all()
        for row in rows:
            text = (row.text_content() or "").strip()
            if "TOTAL" in text or "Date of Svc" in text:
                continue
            cells = row.locator("td").all()
            if len(cells) < 2:
                continue
            first_cell = (cells[0].text_content() or "").strip()
            match = date_pattern.match(first_cell)
            if match:
                return match.group(1)
    except Exception as e:
        print(f"  WARNING: Could not scrape confirmation date: {e}")
    return None


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

    # Scrape the allocation date from the Payment Distribution table
    # before submitting — this is the appointment TA will allocate to
    posted_date = scrape_allocation_date(page)
    if posted_date:
        print(f"  Allocation target: appointment on {posted_date}")

    # Fill payment form
    fill_payment_form(page, amount)
    screenshot(page, f"payment_{name.replace(' ', '_')}_v1_02_filled")

    if not submit_payment(page, name, dry_run):
        raise Exception("V1 submit_payment returned failure")

    # After save, scrape the confirmation page for the definitive Date of Svc
    if not dry_run:
        confirmation_date = scrape_confirmation_date(page)
        if confirmation_date:
            posted_date = confirmation_date
            print(f"  Confirmed: payment posted to {posted_date}")

    return True, posted_date


# =============================================================================
# MAIN LOGIC: Try V2, fallback to V1, then fail
# =============================================================================

def post_payment(page, name, date, amount, dry_run=False):
    """
    Post a payment with retry and fallback logic:
    1. Try V2 (Clients > Appointments > Accept Payment)
    2. If V2 fails (not flagged), retry V2 once with fresh navigation
    3. If V2 retry fails, try V1 (Billing > Take Payment > Search Charges)
    4. If all fail, mark as FAILED
    Returns (success, method, error, note, posted_date, v2_error)
    - posted_date: the appointment date TA allocated the V1 payment to (V1 only)
    - v2_error: why V2 failed, so the report can show it (V1 only)
    """
    print(f"\n--- Payment: {name} — ${amount} on {date} ---")

    # --- Attempt 1: V2 flow ---
    v2_error = None
    try:
        ok, note = post_payment_v2(page, name, date, amount, dry_run)
        if not ok:
            raise Exception("V2 returned ok=False")
        return True, "V2", None, note, None, None
    except Exception as e:
        v2_error = str(e)
        if "FLAG" in v2_error:
            return False, "FLAGGED", v2_error, None, None, None
        print(f"  V2 failed: {v2_error}")

    # --- Attempt 2: Retry V2 with fresh navigation ---
    print(f"  Retrying V2...")
    try:
        ok, note = post_payment_v2(page, name, date, amount, dry_run)
        if not ok:
            raise Exception("V2-retry returned ok=False")
        return True, "V2-retry", None, note, None, None
    except Exception as e:
        v2_retry_error = str(e)
        if "FLAG" in v2_retry_error:
            return False, "FLAGGED", v2_retry_error, None, None, None
        print(f"  V2 retry failed: {v2_retry_error}")
        print(f"  Falling back to V1...")

    # Combine V2 errors for reporting
    combined_v2_error = f"V2: {v2_error}; V2-retry: {v2_retry_error}"

    # --- Attempt 3: V1 fallback ---
    try:
        v1_ok, posted_date = post_payment_v1(page, name, amount, dry_run)
        if not v1_ok:
            raise Exception("V1 submit_payment returned failure")
        return True, "V1", None, None, posted_date, combined_v2_error
    except Exception as e:
        v1_error = str(e)
        if "FLAG" in v1_error:
            return False, "FLAGGED", v1_error, None, None, None
        print(f"  V1 also failed: {v1_error}")

    # --- All attempts failed ---
    return False, "FAILED", f"{combined_v2_error}; V1: {v1_error}", None, None, None


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
    date_mismatch = [r for r in results if r.get("note") and "Date mismatch" in r["note"]]
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

  <!-- Logo -->
  <tr><td style="padding:24px 32px;text-align:center;">
    <img src="https://greatoakcounseling.com/wp-content/uploads/2025/02/great-oak-logo-horizontal.png" alt="Great Oak Counseling" width="200" style="display:inline-block;" />
  </td></tr>

  <!-- Header -->
  <tr><td style="background:#346756;padding:24px 32px;">
    <div style="font-size:22px;font-weight:700;color:#fff;">Oakley's PostIQ Payment Report</div>
    <div style="font-size:13px;color:#a8d4c0;margin-top:4px;">{"DRY RUN — " if dry_run else ""}Square Payments — {display_date}</div>
  </td></tr>

  <!-- Stats -->
  <tr><td style="padding:20px 0;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      {_stat_box(len(succeeded), "Posted", "#2e7d32")}
      {_stat_box(len(manual), "Need Manual Posting", "#c62828" if manual else "#999")}
      {_stat_box(len(v1_clients), "Alternate Date", "#e67e22" if v1_clients else "#999")}
      {_stat_box(len(date_mismatch), "Date Mismatch", "#d84315" if date_mismatch else "#999")}
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

    # --- V1 fallback — alternate date postings ---
    if v1_clients:
        h.append('''<tr><td style="padding:0 32px 8px;">
          <div style="font-size:15px;font-weight:700;color:#e67e22;border-bottom:2px solid #e67e22;padding-bottom:6px;">
            Alternate Date Postings</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;font-size:13px;">
          <p style="color:#666;margin:8px 0;">These payments were posted successfully. The Square transaction date
          did not match an appointment on the same day — this is normal when a client's payment comes in
          a few days after their appointment. The actual appointment date each payment was posted to is shown below.</p>
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:13px;border-collapse:collapse;">
            <tr style="background:#e67e22;color:#fff;">
              <th style="text-align:left;padding:10px 12px;">Client</th>
              <th style="text-align:right;padding:10px 12px;">Amount</th>
              <th style="text-align:left;padding:10px 12px;">Expected Appt Date</th>
              <th style="text-align:left;padding:10px 12px;">Actual Posted Date</th>
            </tr>''')
        for i, r in enumerate(v1_clients):
            bg = "#fef5eb" if i % 2 else "#fff"
            appt_date = r.get("date", "")
            try:
                dt = datetime.strptime(appt_date, "%Y-%m-%d")
                appt_date = dt.strftime("%m/%d/%Y")
            except ValueError:
                pass
            posted_date = r.get("posted_date", "") or ""
            h.append(f'''<tr style="background:{bg};">
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"]):,.2f}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{appt_date}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{posted_date}</td>
            </tr>''')
        h.append('</table></td></tr>')

    # --- Date mismatch — posted to a nearby appointment ---
    if date_mismatch:
        h.append('''<tr><td style="padding:0 32px 8px;">
          <div style="font-size:15px;font-weight:700;color:#d84315;border-bottom:2px solid #d84315;padding-bottom:6px;">
            Date Mismatch — Please Confirm Correct Appointment</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;font-size:13px;">
          <p style="color:#666;margin:8px 0;">These payments were posted, but the Square transaction date
          did not match any appointment in TherapyAppointment. The bot posted to the <strong>closest
          appointment within 60 days</strong>. Please verify each one is allocated to the correct session.</p>
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:13px;border-collapse:collapse;">
            <tr style="background:#d84315;color:#fff;">
              <th style="text-align:left;padding:10px 12px;">Client</th>
              <th style="text-align:right;padding:10px 12px;">Amount</th>
              <th style="text-align:left;padding:10px 12px;">Square Date</th>
              <th style="text-align:left;padding:10px 12px;">Posted To</th>
            </tr>''')
        for i, r in enumerate(date_mismatch):
            bg = "#fff3e0" if i % 2 else "#fff"
            note = r.get("note", "")
            # Extract the TA date from the note: "Date mismatch: Square=MM/DD/YYYY, TA=MM/DD/YYYY ..."
            ta_posted = ""
            if "TA=" in note:
                ta_posted = note.split("TA=")[1].split(" ")[0]
            square_date = r.get("date", "")
            try:
                dt = datetime.strptime(square_date, "%Y-%m-%d")
                square_date = dt.strftime("%m/%d/%Y")
            except ValueError:
                pass
            h.append(f'''<tr style="background:{bg};">
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"]):,.2f}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{square_date}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;">{ta_posted}</td>
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


def _classify_issue(reason):
    """Categorize a failure reason string into an actionable group + suggested fix."""
    r = (reason or "").lower()

    if "timeout" in r and ("click" in r or "intercepts pointer events" in r):
        return ("Click blocked by overlay",
                "A popup (often the Beacon chat widget) was covering the page. "
                "The dismiss_popups() helper should catch this — if it persists, "
                "add the new selector to dismiss_popups().")
    if "no appointment found" in r:
        return ("Appointment not found on date",
                "Client exists in TA but has no appointment matching the Square "
                "transaction date. Check for: timezone mismatch, no-show status, "
                "canceled status, or missing TA appointment record.")
    if "not found in search results" in r or "not found in autocomplete" in r:
        return ("Client name mismatch",
                "Square name doesn't match TA. Add an entry to "
                "scripts/name_aliases.json mapping the Square name to the TA name.")
    if "multiple appointments" in r:
        return ("Multiple appointments same date",
                "Client has 2+ appointments on the same date. Bot can't pick safely "
                "without amount-matching logic.")
    if "multiple matches" in r:
        return ("Multiple client matches",
                "More than one TA client matches the search. May need a more "
                "specific search or an alias entry.")
    if "list index out of range" in r:
        return ("Form input not found",
                "fill_payment_form() couldn't find the expected text input. "
                "TA may have changed the form layout.")
    if "submit_payment returned failure" in r:
        return ("Save Payment click failed",
                "submit_payment returned False without raising. Could be a stuck "
                "modal or a UI change blocking the Save Payment button.")
    return ("Other", "")


def generate_tech_report(results, csv_date):
    """Generate a comprehensive HTML tech report for Travis.

    Categorizes ALL non-perfect outcomes (failures, flagged, V1 fallbacks,
    encoding issues, name notes, popup blocks) into actionable groups so they
    can be reviewed and fixed. Sent only to Travis, never to Hannah.

    Returns (path, html) or (None, None) if there is genuinely nothing to report.
    """
    # Categorize results
    failed = [r for r in results if r["status"] in ("FAILED", "TIMEOUT")]
    flagged = [r for r in results if r["status"] == "FLAGGED"]
    v1_fallbacks = [r for r in results if r.get("method") == "V1"]
    v2_retries = [r for r in results if r.get("method") == "V2-retry"]
    name_notes = [r for r in results if r.get("note") and "Middle/extra name" in (r.get("note") or "")]
    auto_corrected = [r for r in results
                      if r.get("note") and ("auto-corrected" in (r.get("note") or "")
                                            or "Matched via" in (r.get("note") or "")
                                            or "normalized" in (r.get("note") or ""))]
    aliased = [r for r in results
               if r.get("note") and "Name resolved via alias" in (r.get("note") or "")]

    encoding_issues = []
    for r in results:
        if any(ord(c) > 127 for c in r["name"]):
            encoding_issues.append(r)

    # Group failed/flagged by category for actionable summary
    issues_by_category = {}
    for r in failed + flagged:
        category, suggestion = _classify_issue(r.get("reason", ""))
        issues_by_category.setdefault(category, {"clients": [], "suggestion": suggestion})
        issues_by_category[category]["clients"].append(r)

    # If absolutely nothing to report, skip the email
    has_anything = (failed or flagged or v1_fallbacks or v2_retries or
                    name_notes or auto_corrected or aliased or encoding_issues)
    if not has_anything:
        return None, None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = LOG_DIR / f"{ts}_tech_report.html"

    total_issues = len(failed) + len(flagged)

    h = []
    h.append(f'''<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;background:#f4f4f4;color:#333;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:760px;margin:0 auto;background:#fff;">

  <!-- Header -->
  <tr><td style="background:#333;padding:24px 32px;">
    <div style="font-size:22px;font-weight:700;color:#fff;">PostIQ Tech Report</div>
    <div style="font-size:13px;color:#aaa;margin-top:4px;">{csv_date} — for Travis only</div>
  </td></tr>

  <!-- Summary stat row -->
  <tr><td style="padding:20px 0;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      {_stat_box(total_issues, "Hard Issues", "#c62828" if total_issues else "#999")}
      {_stat_box(len(v1_fallbacks), "V1 Fallbacks", "#7b1fa2" if v1_fallbacks else "#999")}
      {_stat_box(len(v2_retries), "V2 Retries", "#e65100" if v2_retries else "#999")}
      {_stat_box(len(auto_corrected) + len(aliased), "Name Fixes", "#1565c0" if (auto_corrected or aliased) else "#999")}
    </tr></table>
  </td></tr>''')

    # ─── Section 1: Hard issues by category ───
    if issues_by_category:
        h.append('''<tr><td style="padding:0 32px 8px;">
          <div style="font-size:16px;font-weight:700;color:#c62828;border-bottom:2px solid #c62828;padding-bottom:6px;">
            Issues to Fix</div>
        </td></tr>''')

        for category, data in issues_by_category.items():
            h.append(f'''<tr><td style="padding:16px 32px 8px;font-size:14px;">
              <strong style="color:#c62828;">{category}</strong> ({len(data["clients"])} client{"s" if len(data["clients"]) != 1 else ""})
            </td></tr>''')

            if data["suggestion"]:
                h.append(f'''<tr><td style="padding:0 32px 8px;font-size:12px;color:#666;font-style:italic;">
                  Suggested fix: {data["suggestion"]}
                </td></tr>''')

            h.append('''<tr><td style="padding:0 32px 16px;">
              <table width="100%" cellpadding="8" cellspacing="0" style="font-size:12px;border-collapse:collapse;border:1px solid #ddd;">
                <tr style="background:#fff5f5;">
                  <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Client</th>
                  <th style="text-align:right;padding:8px;border-bottom:1px solid #ddd;">Amount</th>
                  <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Date</th>
                  <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Raw Error</th>
                </tr>''')
            for r in data["clients"]:
                reason = (r.get("reason") or "")[:200]
                h.append(f'''<tr>
                  <td style="padding:8px;border-bottom:1px solid #eee;">{r["name"]}</td>
                  <td style="padding:8px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"]):,.2f}</td>
                  <td style="padding:8px;border-bottom:1px solid #eee;">{r.get("date", "")}</td>
                  <td style="padding:8px;border-bottom:1px solid #eee;font-family:monospace;font-size:10px;color:#666;">{reason}</td>
                </tr>''')
            h.append('</table></td></tr>')

    # ─── Section 2: V1 fallbacks ───
    if v1_fallbacks:
        h.append(f'''<tr><td style="padding:16px 32px 8px;">
          <div style="font-size:14px;font-weight:700;color:#7b1fa2;border-bottom:1px solid #7b1fa2;padding-bottom:4px;">
            V1 Fallbacks ({len(v1_fallbacks)})
          </div>
          <div style="font-size:12px;color:#666;margin-top:6px;">
            These succeeded via the V1 fallback path — they did NOT match an appointment date and need manual verification.
            Frequent V1 fallbacks suggest the appointment-date matching logic needs attention.
          </div>
        </td></tr>
        <tr><td style="padding:0 32px 16px;">
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:12px;border-collapse:collapse;border:1px solid #ddd;">
            <tr style="background:#f5f0ff;">
              <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Client</th>
              <th style="text-align:right;padding:8px;border-bottom:1px solid #ddd;">Amount</th>
              <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Expected Date</th>
              <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Posted Date</th>
              <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">V2 Failure</th>
            </tr>''')
        for r in v1_fallbacks:
            posted_date = r.get("posted_date", "") or ""
            v2_err = r.get("v2_error", "") or ""
            # Shorten V2 error for readability
            if len(v2_err) > 120:
                v2_err = v2_err[:120] + "…"
            h.append(f'''<tr>
              <td style="padding:8px;border-bottom:1px solid #eee;">{r["name"]}</td>
              <td style="padding:8px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"]):,.2f}</td>
              <td style="padding:8px;border-bottom:1px solid #eee;">{r.get("date", "")}</td>
              <td style="padding:8px;border-bottom:1px solid #eee;">{posted_date}</td>
              <td style="padding:8px;border-bottom:1px solid #eee;font-family:monospace;font-size:10px;color:#666;">{v2_err}</td>
            </tr>''')
        h.append('</table></td></tr>')

    # ─── Section 3: V2 retries (succeeded after first failure) ───
    if v2_retries:
        h.append(f'''<tr><td style="padding:16px 32px 8px;">
          <div style="font-size:14px;font-weight:700;color:#e65100;border-bottom:1px solid #e65100;padding-bottom:4px;">
            V2 Retries ({len(v2_retries)})
          </div>
          <div style="font-size:12px;color:#666;margin-top:6px;">
            These succeeded on the second attempt. First attempt failed for transient reasons (likely a popup or timing).
          </div>
        </td></tr>
        <tr><td style="padding:0 32px 16px;">
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:12px;border-collapse:collapse;border:1px solid #ddd;">
            <tr style="background:#fff8f0;">
              <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Client</th>
            </tr>''')
        for r in v2_retries:
            h.append(f'''<tr><td style="padding:8px;border-bottom:1px solid #eee;">{r["name"]}</td></tr>''')
        h.append('</table></td></tr>')

    # ─── Section 4: Auto-corrections (name normalized, nickname matched, etc.) ───
    if auto_corrected or aliased:
        h.append(f'''<tr><td style="padding:16px 32px 8px;">
          <div style="font-size:14px;font-weight:700;color:#1565c0;border-bottom:1px solid #1565c0;padding-bottom:4px;">
            Name Auto-Corrections ({len(auto_corrected) + len(aliased)})
          </div>
          <div style="font-size:12px;color:#666;margin-top:6px;">
            The bot had to use an alternate name to find these clients. Consider updating Square or TA so the names match.
          </div>
        </td></tr>
        <tr><td style="padding:0 32px 16px;">
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:12px;border-collapse:collapse;border:1px solid #ddd;">
            <tr style="background:#f0f4ff;">
              <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Client</th>
              <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Note</th>
            </tr>''')
        for r in (auto_corrected + aliased):
            h.append(f'''<tr>
              <td style="padding:8px;border-bottom:1px solid #eee;">{r["name"]}</td>
              <td style="padding:8px;border-bottom:1px solid #eee;font-size:11px;color:#666;">{r.get("note", "")}</td>
            </tr>''')
        h.append('</table></td></tr>')

    # ─── Section 5: Encoding issues ───
    if encoding_issues:
        h.append('''<tr><td style="padding:16px 32px 8px;">
          <div style="font-size:14px;font-weight:700;color:#e65100;border-bottom:1px solid #e65100;padding-bottom:4px;">
            Encoding Issues
          </div>
          <div style="font-size:12px;color:#666;margin-top:6px;">
            Likely cause: Square CSV export using wrong encoding. The new Square Daily Report bot
            should produce clean UTF-8 — if these still appear, check the source CSV pipeline.
          </div>
        </td></tr>
        <tr><td style="padding:0 32px 16px;">
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:12px;border-collapse:collapse;border:1px solid #ddd;">
            <tr style="background:#fff8f0;">
              <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Client (raw)</th>
              <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Hex</th>
            </tr>''')
        for r in encoding_issues:
            hex_repr = " ".join(f"{ord(c):02x}" for c in r["name"])
            h.append(f'''<tr>
              <td style="padding:8px;border-bottom:1px solid #eee;">{r["name"]}</td>
              <td style="padding:8px;border-bottom:1px solid #eee;font-family:monospace;font-size:10px;">{hex_repr}</td>
            </tr>''')
        h.append('</table></td></tr>')

    # ─── Section 6: Name notes (middle name issues) ───
    if name_notes:
        h.append(f'''<tr><td style="padding:16px 32px 8px;">
          <div style="font-size:14px;font-weight:700;color:#666;border-bottom:1px solid #ccc;padding-bottom:4px;">
            Middle Name Notes ({len(name_notes)})
          </div>
          <div style="font-size:12px;color:#666;margin-top:6px;">
            Square has middle/extra names that aren't in TA. Bot matched on first/last only — may want to clean up Square or TA.
          </div>
        </td></tr>
        <tr><td style="padding:0 32px 16px;">
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:12px;border-collapse:collapse;border:1px solid #ddd;">
            <tr style="background:#f5f5f5;">
              <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Client</th>
              <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Note</th>
            </tr>''')
        for r in name_notes:
            h.append(f'''<tr>
              <td style="padding:8px;border-bottom:1px solid #eee;">{r["name"]}</td>
              <td style="padding:8px;border-bottom:1px solid #eee;font-size:11px;color:#666;">{r.get("note", "")}</td>
            </tr>''')
        h.append('</table></td></tr>')

    # ─── Footer ───
    h.append(f'''<tr><td style="padding:24px 32px;font-size:12px;color:#666;border-top:1px solid #ddd;">
      Full screenshots and run log are in <code>~/Developer/postiq/logs/</code> on the Mac mini.<br>
      Generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}.
    </td></tr>
    <tr><td style="background:#333;padding:12px 32px;font-size:11px;color:#aaa;text-align:center;">
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
    subject_tag = " — ERRORS DETECTED" if has_errors else ""
    staff_subject = f"Oakley's PostIQ Report — {mode}{subject_tag}"

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
                    success, method, error, note, posted_date, v2_error = post_payment(page, name, date, amount, dry_run=args.dry_run)

                    if success:
                        results.append({"name": name, "date": date, "amount": amount,
                                        "status": "OK", "method": method, "note": note,
                                        "posted_date": posted_date, "v2_error": v2_error})
                    elif method == "FLAGGED":
                        results.append({"name": name, "date": date, "amount": amount,
                                        "status": "FLAGGED", "method": "", "reason": error, "note": note})
                        # Failed/flagged payments may leave the browser mid-flow.
                        # Reset to a known clean state before the next client.
                        recover_to_dashboard(page)
                    else:
                        results.append({"name": name, "date": date, "amount": amount,
                                        "status": "FAILED", "method": "", "reason": error, "note": note})
                        recover_to_dashboard(page)

                except PlaywrightTimeout:
                    screenshot(page, f"error_timeout_{name.replace(' ', '_')}")
                    print(f"  ERROR: Timed out for {name}")
                    results.append({"name": name, "date": date, "amount": amount,
                                    "status": "TIMEOUT", "method": ""})
                    recover_to_dashboard(page)
                except Exception as e:
                    screenshot(page, f"error_{name.replace(' ', '_')}")
                    print(f"  ERROR: {e}")
                    results.append({"name": name, "date": date, "amount": amount,
                                    "status": "FAILED", "method": "", "reason": str(e)})
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

    send_reports(results, duplicates, csv_date_display, dry_run=args.dry_run)


if __name__ == "__main__":
    run()
