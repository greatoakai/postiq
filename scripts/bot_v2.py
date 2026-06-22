import argparse
import csv
import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta, date as date_type
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Resolve paths relative to project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
PENDING_DIR = DATA_DIR / "pending_reports"
PENDING_ARCHIVE = PENDING_DIR / "archive"
LOG_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(PROJECT_ROOT / ".env")

USERNAME = os.getenv("TA_USERNAME")
PASSWORD = os.getenv("TA_PASSWORD")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

# Which button to click on TA's "Additional charges exist for this client" modal
# (#show-other-charges-modal), shown when a client carries open charges beyond the
# current appointment:
#   "this_appointment" (default) → "Yes, accept payment for this appointment"
#                                   Pins the payment to the current appointment.
#   "all_open_charges"           → "No, show all open charges"
#                                   Reveals the full ledger so TA's automatic
#                                   distribution applies the payment to the
#                                   client's oldest open balance first.
# Kept as an env flag so the behaviour can be A/B tested via a dry run before
# committing. Defaults to the historical behaviour.
CHARGES_MODAL_CHOICE = os.getenv("CHARGES_MODAL_CHOICE", "this_appointment").strip().lower()

# Account-Number-first client matching (search_client_by_account). TA's account
# field shows a fixed 'C' prefix icon, so the search types digits only (fixed +
# verified 2026-06-18 against Doud C007660727). Enable with ACCOUNT_MATCH=on in
# .env; default off falls back to the proven name matching.
ACCOUNT_MATCH_ENABLED = os.getenv("ACCOUNT_MATCH", "off").strip().lower() == "on"

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
    # Strategy 0: Close any leftover Bootstrap modal blocking clicks.
    # Generic safety net for unknown modals. We click the [data-dismiss=modal]
    # button (the × in the corner) — semantically equivalent to "close without
    # taking action", which is the correct default for an unrecognized modal.
    #
    # IMPORTANT: explicitly skip #show-other-charges-modal. For that modal,
    # the X is semantically equivalent to "No, show all open charges" — the
    # wrong branch. click_accept_payment() owns dismissing that one by clicking
    # "Yes". If we see it here, Fix #1 leaked — log it loudly so we notice.
    try:
        result = page.evaluate("""
            () => {
                const modals = document.querySelectorAll('div.modal.in[role="dialog"]');
                for (const m of modals) {
                    if (m.offsetParent === null) continue;  // not visible
                    if (m.id === 'show-other-charges-modal') {
                        return 'skipped:show-other-charges-modal';
                    }
                    const x = m.querySelector('[data-dismiss="modal"], button.close');
                    if (x) { x.click(); return 'dismissed:' + (m.id || 'unnamed'); }
                }
                return null;
            }
        """)
        if result and result.startswith("skipped:"):
            print(f"  WARNING: show-other-charges-modal leaked past click_accept_payment() — Fix #1 may have regressed")
        elif result and result.startswith("dismissed:"):
            print(f"  Dismissed Bootstrap modal ({result.split(':', 1)[1]})")
            page.wait_for_timeout(300)
    except Exception:
        pass

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


class UnrecoverableStateError(RuntimeError):
    """Raised when the browser can't be returned to a known-good dashboard state.

    Halts the batch run rather than letting a corrupted page silently fail every
    subsequent payment (see 2026-05-11 cascade: 72 payments lost to a stuck modal
    after recover_to_dashboard() swallowed the failure).
    """


def recover_to_dashboard(page):
    """Navigate back to the dashboard and verify we actually got there.

    Called after a failed payment so the next client starts from a clean slate
    instead of inheriting whatever modal/page state the previous failure left behind.

    Verification ladder — escalates until dashboard is confirmed reachable:
      1. goto(/dashboard) + assert sidebar (text=Clients) renders
      2. If sidebar missing: dismiss popups + re-assert
      3. If still missing: re-login + re-assert
      4. If still missing: raise UnrecoverableStateError to halt the batch

    Rationale: silently warning and pressing on caused the 2026-05-11 cascade
    where 72 consecutive payments failed against the same stuck modal. Better
    to halt loudly at payment N+1 than to mass-fail 72 in a row.
    """
    def _sidebar_visible() -> bool:
        try:
            page.wait_for_selector("text=Clients", timeout=10000)
            return True
        except PlaywrightTimeout:
            return False

    print("  Recovering to dashboard...")
    try:
        page.goto(
            "https://portal.therapyappointment.com/index.cfm/dashboard",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        page.wait_for_load_state("networkidle")
    except Exception as e:
        print(f"  WARNING: dashboard goto failed: {e}")

    suppress_beacon_widget(page)
    dismiss_popups(page)

    if _sidebar_visible():
        return

    # Sidebar didn't render — popups may still be blocking. Try again after a
    # second dismiss pass (Strategy 0 in dismiss_popups now handles generic
    # Bootstrap modals).
    print("  Sidebar not visible after recovery — retrying popup dismissal...")
    dismiss_popups(page)
    if _sidebar_visible():
        return

    # Still no sidebar — assume logged out or session expired. Try re-login.
    print("  Sidebar still not visible — attempting re-login...")
    try:
        login(page)
    except Exception as e:
        raise UnrecoverableStateError(
            f"Recovery failed: re-login raised {type(e).__name__}: {e}"
        )

    if _sidebar_visible():
        return

    raise UnrecoverableStateError(
        "Recovery failed: dashboard sidebar still not visible after re-login. "
        "Halting batch to avoid cascading failures."
    )


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


def _resolve_search_inputs(page, timeout_ms=8000):
    """Locate the First Name and Last Name search inputs on the Clients page.

    The Clients search form can lag a moment behind navigation (TA hydrates the
    page after networkidle), so a snap-second count of "visible text inputs"
    sometimes returned 0 and killed the V2 attempt. This helper waits for the
    form to be ready and uses accessibility-anchored selectors that survive
    layout changes.

    Resolution order (each step waits up to its share of timeout_ms):
      1. get_by_label('First Name' / 'Last Name') — accessibility-anchored
      2. get_by_placeholder('First Name' / 'Last Name')
      3. input[name='first_name'] / input[name='last_name'] (and 'first'/'last' loose attribute match)
      4. First two visible text inputs — positional heuristic (logs a warning)

    Returns (first_name_input, last_name_input) ready-to-fill Locators.
    Raises Exception('STAGE:search ...') if no usable pair resolves within the timeout.
    """
    import time
    per_step_timeout = max(1500, timeout_ms // 3)

    def _try(first_loc, last_loc):
        try:
            first_loc.first.wait_for(state="visible", timeout=per_step_timeout)
            last_loc.first.wait_for(state="visible", timeout=per_step_timeout)
            return first_loc.first, last_loc.first
        except Exception:
            return None

    # Step 1: label association (most resilient)
    result = _try(page.get_by_label("First Name"), page.get_by_label("Last Name"))
    if result:
        return result

    # Step 2: placeholder text
    result = _try(page.get_by_placeholder("First Name"), page.get_by_placeholder("Last Name"))
    if result:
        return result

    # Step 3: name / id attribute selectors
    first_attr = page.locator("input[name='first_name'], input[id='first_name'], input[name*='first' i]")
    last_attr = page.locator("input[name='last_name'], input[id='last_name'], input[name*='last' i]")
    result = _try(first_attr, last_attr)
    if result:
        return result

    # Step 4: positional heuristic, polled until the deadline
    print("  [search] WARNING: label/placeholder/attribute selectors all missed — using positional text-input fallback")
    deadline = time.monotonic() + (per_step_timeout / 1000.0)
    while time.monotonic() < deadline:
        visible = [inp for inp in page.locator("input[type='text']").all() if inp.is_visible()]
        if len(visible) >= 2:
            return visible[0], visible[1]
        page.wait_for_timeout(500)

    visible_count = len([inp for inp in page.locator("input[type='text']").all() if inp.is_visible()])
    raise Exception(
        f"STAGE:search Expected First Name + Last Name search inputs, found {visible_count} visible text inputs"
    )


def _do_search(page, first_search, last_search):
    """Fill the search form and submit. Returns visible table rows.

    Resolves the First Name + Last Name inputs via labels (with placeholder,
    attribute, and positional fallbacks) before filling, so a slow-hydrating
    page doesn't immediately kill the V2 attempt.
    """
    first_input, last_input = _resolve_search_inputs(page)

    first_input.fill(first_search)
    last_input.fill(last_search)

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

    # Search-stage retry: if _do_search raises STAGE:search (search form not
    # yet hydrated), retry the navigate+search pair up to 2 more times with a
    # stabilization wait between attempts. Without this, a flaky one-second
    # rendering delay used to bubble all the way up and trigger the V1
    # fallback, which can post payments as Prepayment / Credit.
    rows = None
    for attempt in range(3):
        try:
            navigate_to_clients(page)
            rows = _do_search(page, first_search, last_search)
            break
        except Exception as e:
            if "STAGE:search" in str(e) and attempt < 2:
                print(f"  Search attempt {attempt + 1} hit transient form-not-ready; stabilizing and retrying...")
                page.wait_for_timeout(2000)
                continue
            raise

    matching = _match_rows(rows, match_parts)

    # If no results among active clients, try including inactive clients
    if len(matching) == 0:
        inactive_rows = _try_inactive_clients(page)
        if inactive_rows is not None:
            matching = _match_rows(inactive_rows, match_parts)

    return matching


def _resolve_account_input(page, timeout_ms=8000):
    """Locate the 'Account Number' SEARCH INPUT on the Clients page.

    Must target the <input> specifically: the results table renders each account
    as a <td aria-label="Account Number for <name>">, which a loose
    get_by_label('Account Number') matches — then fill() fails on the <td>. So we
    select the input by attributes / exact label and verify the tag before use.
    Resolution order (id is dynamic Vuetify input-NNN — never keyed on):
      1. input[role='searchbox'][maxlength='10'] — the account box (C + 9 digits)
      2. get_by_label('Account Number', exact=True) — exact name excludes the
         'Account Number for <name>' result cells
      3. get_by_placeholder('Account Number')

    Returns a ready-to-fill <input> Locator, or None if none resolve in time.
    """
    per_step = max(1500, timeout_ms // 3)
    candidates = [
        page.locator("input[role='searchbox'][maxlength='10']"),
        page.get_by_label("Account Number", exact=True),
        page.get_by_placeholder("Account Number"),
    ]
    for loc in candidates:
        try:
            el = loc.first
            el.wait_for(state="visible", timeout=per_step)
            # Guard against matching a results <td> (or any non-field element).
            if (el.evaluate("e => e.tagName") or "").lower() != "input":
                continue
            return el
        except Exception:
            continue
    return None


def search_client_by_account(page, account):
    """Find a client by TA 'Account Number' (== the Square customer reference_id).

    This is the deterministic match path: account numbers are unique, so it
    sidesteps every name-matching failure (nicknames, misspellings, maiden
    names, Jr / multi-word surnames).

    Returns (True, note) on a unique match (client profile opened), or
    (False, reason) on a plain miss / unresolved field so the caller can fall
    back to name search. Raises only with a FLAG on the can't-happen case of
    multiple clients sharing one account number.
    """
    account = (account or "").strip()
    if not account:
        return False, "no account number"

    # TA's Account # field renders a fixed 'C' prefix icon, so the input takes
    # only the digits — filling the full 'C#########' searches for the wrong value
    # and returns nothing. Type the digits; still match the full account on the row.
    search_value = account[1:] if account[:1].upper() == "C" else account
    print(f"  [acct] Searching by Account Number: {account} (typing '{search_value}')")
    matching = []
    for attempt in range(3):
        try:
            navigate_to_clients(page)
            acct_input = _resolve_account_input(page)
            if acct_input is None:
                if attempt < 2:
                    page.wait_for_timeout(2000)
                    continue
                return False, "account field not found"
            acct_input.fill(search_value)
            page.locator("button:has-text('Search')").first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)
            screenshot(page, f"search_acct_{account}")
            matching = _rows_matching_account(page.locator("table tr").all(), account)
            if not matching:
                inactive_rows = _try_inactive_clients(page)
                if inactive_rows is not None:
                    matching = _rows_matching_account(inactive_rows, account)
            if matching:
                break
            # 0 results: TA's Clients search intermittently flakes to empty for a
            # valid query (same flake the backfill hit). Retry before giving up.
            if attempt < 2:
                print(f"  [acct] attempt {attempt + 1}: 0 results (TA search can flake); retrying...")
                page.wait_for_timeout(1500)
                continue
        except Exception as e:
            if attempt < 2:
                print(f"  [acct] attempt {attempt + 1} transient error ({e}); retrying...")
                page.wait_for_timeout(2000)
                continue
            return False, f"account search error: {e}"

    if len(matching) > 1:
        # Account numbers are unique; more than one match means something is wrong.
        raise Exception(f"FLAG: Multiple clients matched Account # {account} — needs manual review")
    if len(matching) == 0:
        return False, f"Account # {account} not found"

    row, link = matching[0]
    print(f"  [acct] Found client: {row.text_content().strip()[:60]}")
    link.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    return True, f"Matched by Account # {account}"


# TA Account # token: 'C' + 9 digits (the Clients search field caps at 10 chars).
_ACCOUNT_RE = re.compile(r"\bC\d{9}\b")


def _account_from_row(row):
    """Return the TA Account # (C#########) from a Clients results row, or ''.

    Matched against the row's concatenated cell text. Returns a value ONLY when
    exactly one account-shaped token is present — if zero or more than one match
    (e.g. another 'C#########' token slipped into the row), return '' so callers
    fail safe (fall back to name search / skip the backfill) rather than act on a
    guessed number.
    """
    found = set(_ACCOUNT_RE.findall(row.text_content() or ""))
    return found.pop() if len(found) == 1 else ""


def _rows_matching_account(rows, account):
    """Rows whose Account # exactly equals `account` (and that contain a link).

    Exact match (not substring) — avoids a shorter account matching inside a
    longer one, or the digits colliding with another numeric cell in the row.
    """
    out = []
    for row in rows:
        if _account_from_row(row) == account:
            links = row.locator("a")
            if links.count() > 0:
                out.append((row, links.first))
    return out


def scrape_account_for_name(page, name):
    """Name-search for a client and return their TA Account # (C#########), or ''.

    Reuses the same name-resolution chain as search_client (alias → as-is →
    normalized → nickname variations) but does NOT open the profile — it only
    reads the Account # from the unique results row. Returns '' if the client
    can't be uniquely matched (so a wrong number is never written back).
    Used by the Square reference_id backfill (poll_square --backfill-missing).
    """
    resolved = resolve_name(name)
    norm = normalize_name(resolved)

    # Ordered candidate search names: as-is, normalized (if different), then
    # nickname variations — the same ladder search_client() walks.
    candidates = [resolved]
    if norm != resolved:
        candidates.append(norm)
    candidates.extend(var_name for var_name, _ in get_name_variations(resolved))

    for candidate in candidates:
        rows = _try_search(page, candidate)
        if len(rows) == 1:
            return _account_from_row(rows[0][0])

    return ""


def search_client(page, name, account=None):
    """Search for a client and open their profile. Returns (True, note) on a unique match.

    When an `account` (TA Account Number == Square reference_id) is supplied, try
    it first — it's a deterministic, unique key. Fall back to the name-resolution
    chain below on any miss or unresolved field, so behavior is unchanged when no
    account is available (e.g. the daily CSV batch passes none).

    Name-resolution order (stops at first unique match):
        1. Explicit alias from name_aliases.json (resolve_name)
        2. Original name as-is
        3. Normalized name (strip accents / fix encoding)
        4. Nickname variations (Bob ↔ Robert, Liz ↔ Elizabeth, etc.)

    Also returns a note string if the CSV name contains a middle name or
    extra name part, or if the bot had to use an alternate name to find the client.
    """
    # Step 0: Deterministic match by Account Number (Square reference_id), when
    # available AND enabled. Falls through to name resolution on any miss. Gated
    # by ACCOUNT_MATCH_ENABLED — off until TA's account search is fixed/verified.
    if account and ACCOUNT_MATCH_ENABLED:
        try:
            ok, acct_note = search_client_by_account(page, account)
            if ok:
                return True, acct_note
            print(f"  [acct] {acct_note} — falling back to name search")
        except Exception as e:
            if "FLAG" in str(e):
                raise
            print(f"  [acct] account search errored ({e}) — falling back to name search")

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


# How far back click_appointment_by_date() will match an appointment to a payment.
# The Appointments filter (ensure_date_filters) MUST be wider than this, or
# matchable appointments get hidden from the list — e.g. Cassie Chitty 6/19: her
# 6/2 appointment was 17 days back, but a 15-day filter started 6/4 and hid it,
# producing a false "no appointment found". Defined once so the two windows can't
# drift apart again.
APPT_MATCH_LOOKBACK_DAYS = 60


def ensure_date_filters(page):
    """Set the Appointments filter dates on every client visit.

    Sets From = (APPT_MATCH_LOOKBACK_DAYS + 10) days before today, To = 12/31/{year}.
    The filter is derived from the matcher's lookback so it's always wider; the
    +10 buffer absorbs the gap between when a payment is processed (anchored to
    'now') and the payment's own date (the matcher anchors to that — the batch
    runs the next morning, and the poller can lag if the Mac was off). Always
    re-applies because TA resets filters when navigating between clients.
    """
    from_date = datetime.now() - timedelta(days=APPT_MATCH_LOOKBACK_DAYS + 10)
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


def _appt_target_eligible(link):
    """Is this TA appointment row a valid payment target?

    Post to Active appointments and to chargeable cancellations (no-show /
    late-cancel fees). Skip 'Rescheduled to ...' (the slot was moved — the real
    appointment is a different row) and plain 'Cancelled' (no charge). Keep
    anything unreadable/unknown so we never drop a real appointment on a status
    we couldn't parse.
    """
    try:
        t = (link.locator("xpath=ancestor::tr").text_content() or "").lower()
    except Exception:
        return True
    if "reschedul" in t:
        return False  # moved to another date — the real appointment is elsewhere
    if "no show" in t or "noshow" in t or "late cancel" in t or "latecancel" in t:
        return True   # chargeable cancellation fee
    if "cancel" in t:
        return False  # plain cancellation, no charge
    return True       # active or unrecognized status


def click_appointment_by_date(page, date_str, name):
    """Find and click an appointment row matching the given date.

    Converts CSV date (YYYY-MM-DD or MM/DD/YYYY) to TA display format
    (MM/DD/YYYY) before searching.

    Resolution order:
      1. Exact date match on the transaction date
      2. If no exact match, scan visible appointment links within
         APPT_MATCH_LOOKBACK_DAYS prior, keep only valid posting targets
         (_appt_target_eligible — Active / chargeable cancellation, never a
         rescheduled or plainly-cancelled slot), then pick the closest.
      3. If multiple valid appointments share the closest date, flag for review.

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
        # Multiple rows on the same date — keep only valid posting targets
        # (Active or chargeable cancellation; drop Rescheduled / plain Cancelled).
        eligible = [l for l in date_links if _appt_target_eligible(l)]
        if len(eligible) == 1:
            print(f"  Multiple rows on {ta_date}, picking the eligible appointment")
            eligible[0].click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1000)
            return None  # resolved via status

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

            # Only consider dates within the lookback window BEFORE the target (not after)
            days_diff = (target_dt - link_dt).days
            if 0 < days_diff <= APPT_MATCH_LOOKBACK_DAYS:
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

    # Keep only valid posting targets BEFORE choosing the closest date, so a
    # payment skips e.g. a "Rescheduled to ..." slot (Travis Friga 6/18 -> both
    # rescheduled to 6/16) and falls through to the real Active appointment.
    candidates = [c for c in candidates if _appt_target_eligible(c["link"])]
    if not candidates:
        raise Exception(
            f"No active appointment within {APPT_MATCH_LOOKBACK_DAYS} days of {ta_date} for {name}"
        )

    # Sort by proximity (closest first)
    candidates.sort(key=lambda c: c["days_diff"])

    # Flag only if multiple VALID appointments share the closest date.
    closest_date = candidates[0]["date_str"]
    same_date = [c for c in candidates if c["date_str"] == closest_date]
    if len(same_date) > 1:
        raise Exception(
            f"FLAG: Multiple active appointments near {ta_date} on {closest_date} "
            f"for {name} — needs manual review"
        )

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


def _scrape_due_now(page):
    """Scrape the 'Due From Client Now' figure from the Client Payment form.

    TA holds this figure in an input named 'patientResponsibility' and renders
    the visible '$' separately as text — so a textContent scrape grabs the
    co-pay note ("has a 30.00 co-pay…") instead of the real balance. Read the
    input value directly.

    Returns a string like '20.00' (no $ / commas), or None if it can't be found.
    Best-effort: on any miss it returns None so the report falls back to '—'
    rather than showing a wrong number.
    """
    import re
    try:
        loc = page.locator("input[name='patientResponsibility']")
        if loc.count() == 0:
            return None
        raw = (loc.first.input_value() or "").replace("$", "").replace(",", "").strip()
        if not raw:
            return None
        m = re.search(r"\d+(?:\.\d{1,2})?", raw)
        return f"{float(m.group(0)):.2f}" if m else None
    except Exception as e:
        print(f"  WARNING: could not scrape Due From Client Now: {e}")
        return None


def click_accept_payment(page, name):
    """Click the Accept Payment button on the appointment summary.

    Returns (balance_note, balance_amount):
      balance_note:   a note string if the client has an outstanding balance
                      (additional-charges modal appeared), else None.
      balance_amount: the 'Due From Client Now' figure as a string (e.g.
                      '20.00') for the report's Amount-due column, else None.
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
    # (#show-other-charges-modal) — the client carries open charges beyond this
    # appointment. Key off the modal's stable id rather than a button class — TA
    # has shipped the buttons with different classes across releases, and scoping
    # the locator inside the modal prevents matching stray text on the page.
    # Which branch we take is governed by CHARGES_MODAL_CHOICE (see top of file).
    balance_note = None
    balance_amount = None
    modal = page.locator("#show-other-charges-modal.modal.in")
    if modal.is_visible(timeout=3000):
        print(f"  NOTE: {name} has additional charges / outstanding balance")
        balance_note = "Client has outstanding balance — additional charges exist"
        # Capture the modal for the audit trail (no screenshot of it existed before).
        screenshot(page, f"payment_{name.replace(' ', '_')}_00_charges_modal")
        if CHARGES_MODAL_CHOICE == "all_open_charges":
            print("  Clicking: No, show all open charges")
            btn = modal.locator("button:has-text('No, show all open charges')")
        else:
            print("  Clicking: Yes, accept payment for this appointment")
            btn = modal.locator("button:has-text('Yes, accept payment for this appointment')")
        btn.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)
        # Capture what TA now reports as owed, for the report's Amount-due column.
        # On the "all_open_charges" path this is the client's total open
        # client-responsible balance; on the "this_appointment" path it is the
        # current appointment's balance.
        balance_amount = _scrape_due_now(page)
        if balance_amount:
            print(f"  Outstanding (Due From Client Now): ${balance_amount}")

    return balance_note, balance_amount


def _resolve_payment_amount_input(page, timeout_ms=4000):
    """Locate the Payment Amount input on the Client Payment form.

    The V2 form shows two side-by-side amount fields: "Due From Client Now"
    (the displayed balance) and "Payment Amount" (the editable input). When
    both are pre-filled with the same value via Accept Payment, a value-based
    or positional heuristic can pick the wrong one. Use label/attribute
    selectors first and fall back only with a warning.
    """
    try:
        loc = page.get_by_label("Payment Amount", exact=True)
        if loc.count() > 0:
            loc.first.wait_for(state="visible", timeout=timeout_ms)
            return loc.first
    except Exception:
        pass

    try:
        loc = page.locator(
            "input[name='payment_amount'], input[id='payment_amount'], "
            "input[name*='payment_amount' i], input[name*='amount' i]:not([readonly]):not([disabled])"
        )
        if loc.count() > 0:
            loc.first.wait_for(state="visible", timeout=timeout_ms)
            return loc.first
    except Exception:
        pass

    return None


def _resolve_reference_input(page, timeout_ms=4000):
    """Locate the Reference / Check # input on the Client Payment form."""
    try:
        loc = page.get_by_placeholder("Reference / Check #")
        if loc.count() > 0:
            loc.first.wait_for(state="visible", timeout=timeout_ms)
            return loc.first
    except Exception:
        pass

    try:
        loc = page.get_by_placeholder("Reference", exact=False)
        if loc.count() > 0:
            loc.first.wait_for(state="visible", timeout=timeout_ms)
            return loc.first
    except Exception:
        pass

    try:
        loc = page.get_by_label("Reference", exact=False)
        if loc.count() > 0:
            loc.first.wait_for(state="visible", timeout=timeout_ms)
            return loc.first
    except Exception:
        pass

    return None


def fill_payment_form(page, amount):
    """Fill in the payment form fields.

    Uses label/placeholder-anchored selectors for the Payment Amount and
    Reference inputs (resilient to layout shuffles and to both-fields-prefilled
    states). Falls back to the legacy value/attribute heuristic only if those
    miss, and logs when the fallback fires.
    """
    print(f"  Entering amount: ${amount}")

    payment_input = _resolve_payment_amount_input(page)
    if payment_input is None:
        print("  [form] WARNING: Payment Amount label/attribute lookup missed — using value-based heuristic")
        all_inputs = page.locator("input[type='text'], input:not([type])").all()
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
        if payment_input is None and all_inputs:
            payment_input = all_inputs[1] if len(all_inputs) > 1 else all_inputs[0]

    payment_input.click(click_count=3)
    payment_input.fill(amount)

    print("  Selecting External Credit Card...")
    page.click("text=External Credit Card")

    print("  Entering reference: Square")
    ref_input = _resolve_reference_input(page)
    if ref_input is None:
        print("  [form] WARNING: Reference label/placeholder lookup missed — using attribute heuristic")
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


def post_payment_v2(page, name, date, amount, dry_run=False, account=None):
    """V2 flow: Clients > Appointments > Accept Payment.

    Returns (success: bool, note: str or None). Raises on hard failure
    so the caller can fall back to V1. `account` is the TA Account Number
    (Square reference_id) used for deterministic client matching when present.
    """
    print(f"  [V2] Clients > Appointments > Accept Payment")

    # search_client() handles its own navigation to the Clients page
    _ok, name_note = search_client(page, name, account=account)
    navigate_to_appointments(page)
    ensure_date_filters(page)
    date_note = click_appointment_by_date(page, date, name)
    balance_note, balance_amount = click_accept_payment(page, name)
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

    return True, note, balance_amount


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

    Returns (date_str, has_real_charge):
      date_str: MM/DD/YYYY of the first non-Unapplied row, or None
      has_real_charge: True if any row in the distribution is NOT an
        'Unapplied Payment' — i.e., a real outstanding charge exists.
        When False, V1 would post the payment as Prepayment / Credit
        with no DOS attached, and the caller should bail.
    """
    import re
    date_pattern = re.compile(r'(\d{2}/\d{2}/\d{4})')
    has_real_charge = False
    found_date = None
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
            has_real_charge = True
            if found_date is None:
                match = date_pattern.match(first_cell)
                if match:
                    found_date = match.group(1)
    except Exception as e:
        print(f"  WARNING: Could not scrape allocation date: {e}")
    return found_date, has_real_charge


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

    # Scrape the allocation date and check whether real charges exist.
    # If every row in the Payment Distribution is an "Unapplied Payment"
    # (no outstanding charge yet — typical when a session note hasn't been
    # finalized), submitting here would post as Prepayment / Credit with no
    # DOS attached. Bail with a FLAG so the row surfaces for manual review
    # instead of silently posting a misallocated payment.
    posted_date, has_real_charge = scrape_allocation_date(page)
    if not has_real_charge:
        raise Exception(
            f"FLAG: V1 would post as Prepayment for {name} — Payment Distribution "
            f"shows no outstanding charges (only Unapplied rows). Session note may "
            f"not yet be finalized. Needs manual review."
        )
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

    # Return a status string (not posted_date) — per cb13f95, scrape_allocation_date
    # is unreliable across clients with multiple historical charges, so its value
    # must not flow into the report's date column. The has_real_charge gate above
    # is what prevents Prepayment posts; this string just signals V1 success.
    return True, "Posted ✓"


# =============================================================================
# MAIN LOGIC: Try V2, fallback to V1, then fail
# =============================================================================

def post_payment(page, name, date, amount, dry_run=False, account=None):
    """
    Post a payment with retry and fallback logic:
    1. Try V2 (Clients > Appointments > Accept Payment)
    2. If V2 fails (not flagged), retry V2 once with fresh navigation
    3. If V2 retry fails, try V1 (Billing > Take Payment > Search Charges)
    4. If all fail, mark as FAILED
    Returns (success, method, error, note, posted_date, v2_error, balance_amount)
    - posted_date: the appointment date TA allocated the V1 payment to (V1 only)
    - v2_error: why V2 failed, so the report can show it (V1 only)
    - balance_amount: 'Due From Client Now' for the report's Amount-due column
      (V2 outstanding-balance clients only; None otherwise)
    `account` is the TA Account Number (Square reference_id); when present, V2
    matches the client by it deterministically and only falls back to name on a
    miss. V1 (the billing-autocomplete fallback) remains name-based.
    """
    print(f"\n--- Payment: {name} — ${amount} on {date} ---")

    # --- Attempt 1: V2 flow ---
    v2_error = None
    try:
        ok, note, balance_amount = post_payment_v2(page, name, date, amount, dry_run, account=account)
        if not ok:
            raise Exception("V2 returned ok=False")
        return True, "V2", None, note, None, None, balance_amount
    except Exception as e:
        v2_error = str(e)
        if "FLAG" in v2_error:
            return False, "FLAGGED", v2_error, None, None, None, None
        print(f"  V2 failed: {v2_error}")

    # --- Attempt 2: Retry V2 with fresh navigation ---
    print(f"  Retrying V2...")
    try:
        ok, note, balance_amount = post_payment_v2(page, name, date, amount, dry_run, account=account)
        if not ok:
            raise Exception("V2-retry returned ok=False")
        return True, "V2-retry", None, note, None, None, balance_amount
    except Exception as e:
        v2_retry_error = str(e)
        if "FLAG" in v2_retry_error:
            return False, "FLAGGED", v2_retry_error, None, None, None, None
        print(f"  V2 retry failed: {v2_retry_error}")
        print(f"  Falling back to V1...")

    # Combine V2 errors for reporting
    combined_v2_error = f"V2: {v2_error}; V2-retry: {v2_retry_error}"

    # --- Attempt 3: V1 fallback ---
    try:
        v1_ok, posted_date = post_payment_v1(page, name, amount, dry_run)
        if not v1_ok:
            raise Exception("V1 submit_payment returned failure")
        return True, "V1", None, None, posted_date, combined_v2_error, None
    except Exception as e:
        v1_error = str(e)
        if "FLAG" in v1_error:
            return False, "FLAGGED", v1_error, None, None, None, None
        print(f"  V1 also failed: {v1_error}")

    # --- All attempts failed ---
    return False, "FAILED", f"{combined_v2_error}; V1: {v1_error}", None, None, None, None


def _stat_box(value, label, color):
    """Return HTML for a single summary stat box."""
    return f'''<td style="text-align:center;padding:12px 24px;">
      <div style="font-size:36px;font-weight:700;color:{color};">{value}</div>
      <div style="font-size:12px;color:#666;margin-top:4px;">{label}</div>
    </td>'''


def _day_subheading_row(payload, colspan):
    """Render a per-day subheading row to insert inside a multi-day action table.

    Returns HTML for a single `<tr>` that spans all columns of the table,
    showing 'Saturday, May 16' (etc.). Callers should only emit this when the
    report covers more than one day; for single-day reports it adds visual noise.
    """
    label = _format_day_header(payload.get("run_date"))
    return (
        f'<tr><td colspan="{colspan}" style="background:#f4f4f4;padding:6px 12px;'
        f'font-size:12px;font-weight:700;color:#555;border-top:1px solid #ddd;'
        f'border-bottom:1px solid #ddd;">{label}</td></tr>'
    )


def _header_date_range(payloads):
    """Build the 'Square Payments — ...' header line.

    Examples:
      One day:        'Saturday, May 16, 2026'
      Multi-day:      'Saturday, May 16 — Monday, May 18, 2026'
      Cross-month:    'Friday, May 29 — Monday, June 1, 2026'
    """
    def _parse(p):
        return datetime.strptime(p["run_date"], "%Y-%m-%d").date()
    dates = sorted({_parse(p) for p in payloads})
    if not dates:
        return "Unknown date"
    if len(dates) == 1:
        d = dates[0]
        return f"{d.strftime('%A, %B')} {d.day}, {d.year}"
    first, last = dates[0], dates[-1]
    if first.year == last.year:
        return (
            f"{first.strftime('%A, %B')} {first.day} — "
            f"{last.strftime('%A, %B')} {last.day}, {last.year}"
        )
    return (
        f"{first.strftime('%A, %B')} {first.day}, {first.year} — "
        f"{last.strftime('%A, %B')} {last.day}, {last.year}"
    )


def generate_report(payloads, dry_run=False):
    """Generate the HTML staff report (for Hannah) and save to logs.

    Accepts a list of payload dicts — one per day in the reporting range.
    For single-day reports (len(payloads) == 1) the rendering matches the
    historical layout; for multi-day reports each action section emits a
    short 'Saturday, May 16' subheading row before each day's rows.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = "DRYRUN" if dry_run else "POSTED"
    report_path = LOG_DIR / f"{ts}_report_{mode}.html"

    multi_day = len(payloads) > 1
    # Flatten across all days for stat boxes + overall totals. Each row gets
    # tagged with its payload's run_date so per-day grouping can find it later.
    results = []
    for p in payloads:
        for r in p["results"]:
            results.append({**r, "run_date": p["run_date"]})

    def _by_day(section_rows):
        """Yield (payload, [rows]) in chronological order. Empty days are skipped."""
        for p in payloads:
            day_rows = [r for r in section_rows if r.get("run_date") == p["run_date"]]
            if day_rows:
                yield p, day_rows

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
    any_duplicates = any(p.get("duplicates") for p in payloads)
    has_actions = bool(manual or v1_clients or date_mismatch or balance_clients or name_noted or any_duplicates)

    display_date = _header_date_range(payloads)

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

    # --- Extra-emphasis banner for Hannah when the new distribution mode is on ---
    # Gated on CHARGES_MODAL_CHOICE so it appears only on runs that used
    # "No, show all open charges" and disappears automatically once reverted.
    if CHARGES_MODAL_CHOICE == "all_open_charges":
        h.append('''<tr><td style="padding:12px 32px 4px;">
          <div style="background:#fff3cd;border:2px solid #e0a800;border-radius:4px;padding:16px 20px;font-size:14px;color:#5a4a00;">
            <div style="font-size:17px;font-weight:800;color:#9a6a00;margin-bottom:6px;">&#9888;&#65039; Hannah &mdash; please cross-check this run</div>
            <p style="margin:6px 0;">This run posted payments with a <strong>new distribution method</strong> (&ldquo;show all open charges&rdquo;): each payment was distributed across the client&rsquo;s open charges, <strong>oldest balance first</strong>, instead of being applied to a single appointment.</p>
            <p style="margin:6px 0;">Please verify in TherapyAppointment that <strong>each payment landed on the correct line item(s)</strong> &mdash; especially the <strong>Outstanding balances</strong> clients below, and anyone with multiple payments or multiple open charges. Confirm nothing was over-applied to one appointment or left unapplied, and flag anything that looks off.</p>
          </div>
        </td></tr>''')

    # --- "Action required" banner introduces the action sections ---
    if has_actions:
        h.append('''<tr><td style="padding:8px 32px 4px;">
          <div style="background:#fdecea;border-left:6px solid #c62828;padding:14px 18px;font-size:14px;color:#5a1a1a;">
            <div style="font-size:16px;font-weight:700;color:#c62828;margin-bottom:4px;">Action required</div>
            The sections below need staff attention. Completed payments summary is at the bottom.
          </div>
        </td></tr>''')

    # ============================================================
    # ACTION-REQUIRED SECTIONS (ordered by urgency)
    # ============================================================

    # --- 1. Duplicate names (verify each payment landed on the right account) ---
    if any_duplicates:
        h.append('''<tr><td style="padding:16px 32px 8px;">
          <div style="font-size:18px;font-weight:700;color:#b8860b;border-bottom:3px solid #ffc107;padding-bottom:6px;">
            Duplicate names &mdash; verify correct client</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;font-size:13px;">
          <p style="color:#666;margin:8px 0;">Two or more Square transactions share the same client name, so the bot
          can&rsquo;t tell which person in TherapyAppointment each payment belongs to. Please confirm in TA that
          each payment was posted to the correct client&rsquo;s account.</p>
          <div style="background:#fff3cd;border-left:4px solid #ffc107;padding:12px 16px;">''')
        # Duplicates are detected per CSV (per day) — render each day's set
        # under its own subheading when the report covers multiple days.
        for p in payloads:
            if not p.get("duplicates"):
                continue
            if multi_day:
                h.append(
                    f'<div style="font-weight:700;color:#555;margin:10px 0 4px;">'
                    f'{_format_day_header(p["run_date"])}</div>'
                )
            for name in sorted(p["duplicates"]):
                count = sum(1 for r in p["results"] if r["name"] == name)
                h.append(f'{name} ({count} entries)<br>')
        h.append('</div></td></tr>')

    # --- 2. Manual posting needed (must do — bot did not post) ---
    if manual:
        h.append('''<tr><td style="padding:16px 32px 8px;">
          <div style="font-size:18px;font-weight:700;color:#c62828;border-bottom:3px solid #c62828;padding-bottom:6px;">
            Manual posting needed</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;font-size:13px;">
          <p style="color:#666;margin:8px 0;">The bot was unable to post these payments. Please post them manually in TherapyAppointment.</p>
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:13px;border-collapse:collapse;">
            <tr style="background:#c62828;color:#fff;">
              <th style="text-align:left;padding:10px 12px;">Client</th>
              <th style="text-align:right;padding:10px 12px;">Amount</th>
              <th style="text-align:left;padding:10px 12px;">Reason</th>
            </tr>''')
        for p, day_rows in _by_day(manual):
            if multi_day:
                h.append(_day_subheading_row(p, colspan=3))
            for i, r in enumerate(day_rows):
                bg = "#fff5f5" if i % 2 else "#fff"
                reason = r.get("reason", r["status"])
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

    # --- 2. Outstanding balances (follow up — bot posted, older charges remain) ---
    if balance_clients:
        h.append('''<tr><td style="padding:16px 32px 8px;">
          <div style="font-size:18px;font-weight:700;color:#e65100;border-bottom:3px solid #e65100;padding-bottom:6px;">
            Outstanding balances &mdash; follow up needed</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;font-size:13px;">
          <p style="color:#666;margin:8px 0;">The bot posted the payment, but additional charges exist for these clients.
          Look up each client in TherapyAppointment to see the remaining balance and follow up.</p>
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:13px;border-collapse:collapse;">
            <tr style="background:#e65100;color:#fff;">
              <th style="text-align:left;padding:10px 12px;">Client</th>
              <th style="text-align:right;padding:10px 12px;">Amount due</th>
            </tr>''')
        for p, day_rows in _by_day(balance_clients):
            if multi_day:
                h.append(_day_subheading_row(p, colspan=2))
            for i, r in enumerate(day_rows):
                bg = "#fff8f0" if i % 2 else "#fff"
                amount_due = r.get("balance_amount")
                if amount_due in (None, ""):
                    amount_due = "—"
                else:
                    try:
                        amount_due = f"${float(amount_due):,.2f}"
                    except (ValueError, TypeError):
                        amount_due = str(amount_due)
                h.append(f'''<tr style="background:{bg};">
                  <td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>
                  <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;color:#999;">{amount_due}</td>
                </tr>''')
        h.append('</table></td></tr>')

    # --- 3. Alternate date postings (verify allocation in TA) ---
    if v1_clients:
        h.append('''<tr><td style="padding:16px 32px 8px;">
          <div style="font-size:18px;font-weight:700;color:#e67e22;border-bottom:3px solid #e67e22;padding-bottom:6px;">
            Alternate date postings</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;font-size:13px;">
          <p style="color:#666;margin:8px 0;">These payments were posted via an alternate method because
          the Square transaction date did not match an appointment on the same day. Please verify in TherapyAppointment
          that each payment is allocated to the correct appointment.</p>
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:13px;border-collapse:collapse;">
            <tr style="background:#e67e22;color:#fff;">
              <th style="text-align:left;padding:10px 12px;">Client</th>
              <th style="text-align:right;padding:10px 12px;">Amount</th>
              <th style="text-align:left;padding:10px 12px;">Expected appt date</th>
              <th style="text-align:left;padding:10px 12px;">Actual date posted</th>
            </tr>''')
        for p, day_rows in _by_day(v1_clients):
            if multi_day:
                h.append(_day_subheading_row(p, colspan=4))
            for i, r in enumerate(day_rows):
                bg = "#fef5eb" if i % 2 else "#fff"
                appt_date = r.get("date", "")
                try:
                    dt = datetime.strptime(appt_date, "%Y-%m-%d")
                    appt_date = dt.strftime("%m/%d/%Y")
                except ValueError:
                    pass
                raw_posted = r.get("posted_date", "") or ""
                if raw_posted:
                    posted_cell = raw_posted
                    row_bg = bg
                else:
                    posted_cell = '<span style="color:#c62828;font-weight:700;">&#9888; No DOS detected &mdash; verify allocation (possible Prepayment)</span>'
                    row_bg = "#fdecea"
                h.append(f'''<tr style="background:{row_bg};">
                  <td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>
                  <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"]):,.2f}</td>
                  <td style="padding:8px 12px;border-bottom:1px solid #eee;">{appt_date}</td>
                  <td style="padding:8px 12px;border-bottom:1px solid #eee;">{posted_cell}</td>
                </tr>''')
        h.append('</table></td></tr>')

    # --- 4. Date mismatch (count + name list only — no per-transaction table) ---
    if date_mismatch:
        h.append(f'''<tr><td style="padding:16px 32px 8px;">
          <div style="font-size:18px;font-weight:700;color:#d84315;border-bottom:3px solid #d84315;padding-bottom:6px;">
            Date mismatch &mdash; {len(date_mismatch)} payment{'s' if len(date_mismatch) != 1 else ''} to verify</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;font-size:13px;">
          <p style="color:#666;margin:8px 0;">These payments were <strong>successfully posted</strong> to each client&rsquo;s account &mdash; this is not a failure.
          The Square transaction date didn&rsquo;t match an appointment exactly, so the bot allocated each one to the <strong>closest appointment within 60 days</strong>.
          Please confirm in TherapyAppointment that each payment landed on the right session.</p>''')
        if multi_day:
            for p, day_rows in _by_day(date_mismatch):
                day_label = _format_day_header(p["run_date"])
                day_names = ", ".join(r["name"] for r in day_rows)
                h.append(
                    f'<p style="color:#333;margin:8px 0;">'
                    f'<strong>{day_label}:</strong> {day_names}</p>'
                )
        else:
            names = ", ".join(r["name"] for r in date_mismatch)
            h.append(f'<p style="color:#333;margin:8px 0;"><strong>Verify in TA:</strong> {names}</p>')
        h.append('</td></tr>')

    # --- 5. Name notes (lowest urgency — informational verification) ---
    if name_noted:
        h.append('''<tr><td style="padding:16px 32px 8px;">
          <div style="font-size:18px;font-weight:700;color:#1565c0;border-bottom:3px solid #1565c0;padding-bottom:6px;">
            Name notes &mdash; please verify in TherapyAppointment</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;font-size:13px;">
          <p style="color:#666;margin:8px 0;">These clients have middle or extra names in Square that may not match
          TherapyAppointment. Payments posted OK, but the names should be reviewed. These could also be two-part last names.</p>
          <table width="100%" cellpadding="8" cellspacing="0" style="font-size:13px;border-collapse:collapse;">
            <tr style="background:#1565c0;color:#fff;">
              <th style="text-align:left;padding:10px 12px;">Client</th>
              <th style="text-align:left;padding:10px 12px;">Extra name in Square</th>
            </tr>''')
        for p, day_rows in _by_day(name_noted):
            if multi_day:
                h.append(_day_subheading_row(p, colspan=2))
            for i, r in enumerate(day_rows):
                bg = "#f0f4ff" if i % 2 else "#fff"
                note = r.get("note", "")
                extra = note.split("'")[1] if "'" in note else note
                h.append(f'''<tr style="background:{bg};">
                  <td style="padding:8px 12px;border-bottom:1px solid #eee;">{r["name"]}</td>
                  <td style="padding:8px 12px;border-bottom:1px solid #eee;">{extra}</td>
                </tr>''')
        h.append('</table></td></tr>')

    # ============================================================
    # COMPLETED PAYMENTS — one-line summary at the bottom
    # (Multi-day reports break down per day above the grand total.)
    # ============================================================
    if succeeded:
        h.append('''<tr><td style="padding:24px 32px 8px;">
          <div style="font-size:15px;font-weight:700;color:#346756;border-bottom:2px solid #346756;padding-bottom:6px;">
            Completed payments</div>
        </td></tr>
        <tr><td style="padding:0 32px 24px;font-size:14px;color:#333;">''')
        if multi_day:
            for p, day_rows in _by_day(succeeded):
                day_total = sum(float(r["amount"]) for r in day_rows)
                day_label = _format_day_header(p["run_date"])
                h.append(
                    f'<div style="margin:4px 0;">'
                    f'<strong>{day_label}:</strong> '
                    f'{len(day_rows)} payment{"s" if len(day_rows) != 1 else ""} '
                    f'&mdash; ${day_total:,.2f}'
                    f'</div>'
                )
            h.append(
                f'<div style="margin-top:10px;padding-top:8px;border-top:1px solid #ddd;">'
                f'<strong style="color:#2e7d32;font-size:16px;">'
                f'Total: {len(succeeded)} payment{"s" if len(succeeded) != 1 else ""} '
                f'posted successfully &mdash; ${total_amount:,.2f}'
                f'</strong>'
                f'</div>'
            )
        else:
            h.append(
                f'<strong style="color:#2e7d32;font-size:16px;">'
                f'{len(succeeded)} payment{"s" if len(succeeded) != 1 else ""} posted successfully '
                f'&mdash; ${total_amount:,.2f} total.</strong>'
            )
        h.append('</td></tr>')

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


def generate_tech_report(payloads):
    """Generate a comprehensive HTML tech report for Travis.

    Accepts a list of payload dicts (one per day in the reporting range).
    Categorizes ALL non-perfect outcomes (failures, flagged, V1 fallbacks,
    encoding issues, name notes, popup blocks) into actionable groups so they
    can be reviewed and fixed. Sent only to Travis, never to Hannah.

    Returns (path, html) or (None, None) if there is genuinely nothing to report.
    """
    # Flatten across days, tagging each row with its run_date for context.
    results = []
    for p in payloads:
        for r in p["results"]:
            results.append({**r, "run_date": p["run_date"]})
    csv_date = _header_date_range(payloads)
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
              <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Status</th>
              <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">V2 Failure</th>
            </tr>''')
        for r in v1_fallbacks:
            raw_posted = r.get("posted_date", "") or ""
            if raw_posted:
                posted_cell = raw_posted
            else:
                posted_cell = '<span style="color:#c62828;font-weight:700;">&#9888; No DOS &mdash; verify (possible Prepayment)</span>'
            v2_err = r.get("v2_error", "") or ""
            # Shorten V2 error for readability
            if len(v2_err) > 120:
                v2_err = v2_err[:120] + "…"
            h.append(f'''<tr>
              <td style="padding:8px;border-bottom:1px solid #eee;">{r["name"]}</td>
              <td style="padding:8px;border-bottom:1px solid #eee;text-align:right;">${float(r["amount"]):,.2f}</td>
              <td style="padding:8px;border-bottom:1px solid #eee;">{r.get("date", "")}</td>
              <td style="padding:8px;border-bottom:1px solid #eee;">{posted_cell}</td>
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


# =============================================================================
# WEEKEND REPORT AGGREGATION
# =============================================================================
#
# The bot runs every day, but no one's at a desk on Saturday or Sunday to act
# on the action items (V1 fallbacks, FLAGs, balance alerts). Sending a report
# nobody reads splits Hannah's attention. Instead we:
#   - Run the bot every day (Sat/Sun included) so payments post promptly.
#   - On Sat/Sun, persist the run's results as JSON and skip the email.
#   - On Mon-Fri, merge any pending weekend runs with today's results into a
#     single combined report that covers the full date range.
#
# Failure handling: pending JSONs only get archived after a successful email
# send. If msmtp fails on Monday, the JSONs stay in place and Tuesday's run
# picks them up. No data is lost.

def _save_pending_results(results, duplicates, csv_date_display, run_date):
    """Persist this run's results so a later weekday run can include them."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_date": run_date.isoformat(),
        "csv_date": csv_date_display,
        "results": results,
        "duplicates": sorted(duplicates) if duplicates else [],
    }
    path = PENDING_DIR / f"results_{run_date.strftime('%Y%m%d')}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"  Results saved to pending: {path.name}")
    return path


def _load_pending_payloads():
    """Load all pending result JSONs in chronological run-date order."""
    if not PENDING_DIR.exists():
        return []
    files = sorted(PENDING_DIR.glob("results_*.json"))
    payloads = []
    for f in files:
        try:
            payloads.append(json.loads(f.read_text()))
        except Exception as e:
            print(f"  WARNING: Could not load pending {f.name}: {e}")
    return payloads


def _archive_pending_payloads():
    """Move all pending JSONs to the archive folder. Called after a successful send."""
    if not PENDING_DIR.exists():
        return
    PENDING_ARCHIVE.mkdir(parents=True, exist_ok=True)
    for f in PENDING_DIR.glob("results_*.json"):
        try:
            f.rename(PENDING_ARCHIVE / f.name)
        except Exception as e:
            print(f"  WARNING: Could not archive {f.name}: {e}")


def _format_combined_subject(payloads, has_errors):
    """Build a date-range subject line.

    Examples:
      One day:        'Oakley's PostIQ Report — Mon, May 18'
      Sat-Sun-Mon:    'Oakley's PostIQ Report covering Sat–Mon, May 16–18'
      Cross-month:    'Oakley's PostIQ Report covering Fri–Mon, May 29–Jun 1'
    """
    def _parse(p):
        return datetime.strptime(p["run_date"], "%Y-%m-%d").date()

    dates = sorted({_parse(p) for p in payloads})
    subject_tag = " — ERRORS DETECTED" if has_errors else ""

    if len(dates) == 1:
        d = dates[0]
        body = f"{d.strftime('%a')}, {d.strftime('%b')} {d.day}"
        return f"Oakley's PostIQ Report — {body}{subject_tag}"

    first, last = dates[0], dates[-1]
    if first.month == last.month:
        date_part = f"{first.strftime('%b')} {first.day}–{last.day}"
    else:
        date_part = f"{first.strftime('%b %-d')}–{last.strftime('%b %-d')}"
    dow_part = f"{first.strftime('%a')}–{last.strftime('%a')}"
    return f"Oakley's PostIQ Report covering {dow_part}, {date_part}{subject_tag}"


def _format_day_header(run_date_str):
    """Render a per-day section header like 'Saturday, May 16'."""
    try:
        d = datetime.strptime(run_date_str, "%Y-%m-%d").date()
        return f"{d.strftime('%A')}, {d.strftime('%b')} {d.day}"
    except (ValueError, TypeError):
        return run_date_str or "Unknown date"


def send_email(to, cc, subject, body, html=True):
    """Send an email via msmtp. Sends as HTML by default.

    Returns True on success, False on failure or skip — callers depend on
    this to know whether it's safe to archive pending state.
    """
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
            return True
        else:
            print(f"  Email FAILED: {proc.stderr.strip()}")
            return False
    except FileNotFoundError:
        print("  Email SKIPPED: msmtp not installed")
        return False
    except Exception as e:
        print(f"  Email ERROR: {e}")
        return False


def send_reports(results, duplicates, csv_date, dry_run=False):
    """Save this run's results, then on weekdays generate and email a combined
    report covering today + any pending weekend runs.

    Sat/Sun: results are persisted as JSON and no email goes out. Mon-Fri:
    all pending JSONs are loaded, results are flattened across days, the
    combined report is emailed, and the pending JSONs are archived only if
    the email succeeded.
    """
    run_date = datetime.now().date()

    # Persist this run's results so a later weekday run can include them — but
    # only when there's something to persist. An empty (no-payment) day saves
    # nothing, yet on a weekday it still proceeds below to flush any deferred
    # weekend reports that were waiting. Dry runs persist too.
    if results:
        _save_pending_results(results, duplicates, csv_date, run_date)

    # Sat (5) / Sun (6) — defer email; the next weekday picks this up.
    if run_date.weekday() >= 5:
        if results:
            print(f"  Weekend run ({run_date.strftime('%A')}) — results saved, email deferred to next weekday.")
        else:
            print(f"  Weekend run ({run_date.strftime('%A')}), no payments — nothing to defer.")
        return

    # Weekday: load every pending payload (today's, if saved, plus any deferred
    # weekend runs). If there's nothing at all, send no email (don't crash, and
    # don't spam an empty report).
    payloads = _load_pending_payloads()
    if not payloads:
        print("  No payments today and no deferred reports pending — nothing to send.")
        return

    # Flatten across days for the error check and the dry-run flag.
    all_results = [r for p in payloads for r in p["results"]]
    has_errors = any(r["status"] in ("FAILED", "FLAGGED", "TIMEOUT") for r in all_results)

    if dry_run:
        # Override subject to make dry-run obvious; date range still follows below.
        staff_subject = "Oakley's PostIQ Report — DRY RUN"
        if has_errors:
            staff_subject += " — ERRORS DETECTED"
    else:
        staff_subject = _format_combined_subject(payloads, has_errors)

    # Staff report → Hannah
    staff_path, staff_body = generate_report(payloads, dry_run=dry_run)
    staff_sent = send_email(
        to="hannah@greatoakcounseling.com",
        cc="travis@greatoakcounseling.com, supportstaff@greatoakcounseling.com",
        subject=staff_subject,
        body=staff_body,
    )

    # Tech report → Travis only (when there are technical issues)
    tech_path, tech_body = generate_tech_report(payloads)
    tech_sent = True  # default True so a None body doesn't block archive
    if tech_body:
        tech_subject = staff_subject.replace("Oakley's PostIQ Report", "PostIQ Tech Report")
        tech_sent = send_email(
            to="travis@greatoakcounseling.com",
            cc=None,
            subject=tech_subject,
            body=tech_body,
        )

    # Only archive pending JSONs if the staff email actually got out. If it
    # failed, the JSONs stay in place and the next weekday run will retry.
    if staff_sent:
        _archive_pending_payloads()
    else:
        print("  Staff email did not send — pending results NOT archived; next run will retry.")


def run():
    parser = argparse.ArgumentParser(description="PostIQ — Payment Bot with Fallback")
    parser.add_argument("csv_file", help="Path to the payment CSV file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fill forms but don't submit (cancels instead of saving)")
    parser.add_argument("--no-email", action="store_true",
                        help="Skip send_reports (no staff email, no pending-state changes); "
                             "just generate and save the report HTML to logs/ for review. "
                             "Use for test/dry-run batches.")
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
        # No payments for this date (e.g. a quiet weekend day with an empty
        # Square export). This is NOT a failure, and we must NOT crash — on a
        # weekday this run is what flushes any deferred weekend reports, so
        # exiting here would strand them (see 2026-06-15: empty Sunday CSV
        # crashed Monday before the weekend report could send). Skip posting
        # (no browser/login needed) and still run the report step.
        import re as _re
        _m = _re.match(r"(\d{2})\.(\d{2})\.(\d{4})", csv_path.name)
        csv_date_display = f"{_m.group(1)}/{_m.group(2)}/{_m.group(3)}" if _m else "unknown"
        print(f"No valid payment rows in {csv_path.name} — nothing to post today. "
              f"Running report step to flush any deferred reports.")
        send_reports([], set(), csv_date_display, dry_run=args.dry_run)
        return

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
                    success, method, error, note, posted_date, v2_error, balance_amount = post_payment(page, name, date, amount, dry_run=args.dry_run)

                    if success:
                        results.append({"name": name, "date": date, "amount": amount,
                                        "status": "OK", "method": method, "note": note,
                                        "posted_date": posted_date, "v2_error": v2_error,
                                        "balance_amount": balance_amount})
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

                except UnrecoverableStateError:
                    # Recovery layer has decided the browser is wedged. Let this
                    # propagate to the outer handler so the batch halts loudly
                    # instead of mass-failing every remaining payment.
                    raise
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

    if args.no_email:
        # Test/dry-run mode: build the report locally for review without emailing
        # staff or touching the pending-JSON state.
        payload = {
            "run_date": datetime.now().date().isoformat(),
            "csv_date": csv_date_display,
            "results": results,
            "duplicates": sorted(duplicates) if duplicates else [],
        }
        report_path, _ = generate_report([payload], dry_run=args.dry_run)
        print(f"  --no-email: report saved locally, no email sent → {report_path.name}")
    else:
        send_reports(results, duplicates, csv_date_display, dry_run=args.dry_run)


if __name__ == "__main__":
    run()
