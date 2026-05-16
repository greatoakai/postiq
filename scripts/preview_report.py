#!/usr/bin/env python3
"""preview_report.py — Re-render the most recent PostIQ report with the
current generate_report() layout and email it to Travis for review.

Reads today's logs/<ts>_report_POSTED.html, reconstructs a `results` list
by parsing the section tables, then calls generate_report() and ships
the HTML via msmtp. Production runs are untouched; this is for layout
review only.
"""

import re
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot_v2  # noqa: E402


REPORT_DIR = Path(__file__).resolve().parent.parent / "logs"


class TableExtractor(HTMLParser):
    """Pull tables out of the PostIQ report HTML.

    Each table becomes a list of rows, each row a list of cell strings.
    """

    def __init__(self):
        super().__init__()
        self.tables = []
        self._current_table = None
        self._current_row = None
        self._current_cell = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._current_table = []
        elif tag == "tr" and self._current_table is not None:
            self._current_row = []
        elif tag in ("td", "th") and self._current_row is not None:
            self._current_cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._current_cell is not None:
            text = "".join(self._current_cell).strip()
            text = re.sub(r"\s+", " ", text)
            self._current_row.append(text)
            self._current_cell = None
        elif tag == "tr" and self._current_row is not None:
            if self._current_row:
                self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._current_table is not None:
            self.tables.append(self._current_table)
            self._current_table = None

    def handle_data(self, data):
        if self._current_cell is not None:
            self._current_cell.append(data)


def parse_amount(s):
    return float(s.replace("$", "").replace(",", "").strip())


def reconstruct_results(html):
    """Walk the rendered HTML and rebuild a results list + duplicates set."""
    parser = TableExtractor()
    parser.feed(html)

    # Pull section blocks by header text scan
    sections = re.split(r'<div style="font-size:15px;font-weight:700;', html)
    section_titles = []
    for chunk in sections[1:]:
        title_match = re.search(r">\s*([^<]+?)\s*</div>", chunk)
        section_titles.append(title_match.group(1).strip() if title_match else "")

    # Walk every table and bucket rows by which heading they follow
    # Simpler: re-find each table region in source order with its preceding heading
    table_blocks = re.findall(
        r'<div style="font-size:15px;font-weight:700;[^>]+>\s*([^<]+?)\s*</div>'
        r'(.*?)(?=<div style="font-size:15px;font-weight:700;|</table></body></html>)',
        html, flags=re.DOTALL,
    )

    results = []
    duplicates = set()
    by_name = {}

    def upsert(name, **fields):
        if name in by_name:
            by_name[name].update({k: v for k, v in fields.items() if v is not None})
        else:
            row = {"name": name, **fields}
            by_name[name] = row
            results.append(row)

    # Duplicates warning is rendered as a div, not a table — scrape it directly
    dup_match = re.search(
        r'Duplicate Names — Staff Review Required</strong><br>(.*?)</div>',
        html, flags=re.DOTALL,
    )
    if dup_match:
        for line in dup_match.group(1).splitlines():
            m = re.match(r"\s*(.+?)\s*\(\d+\s*entries\)\s*<br>\s*", line)
            if m:
                duplicates.add(m.group(1).strip())

    for title, block in table_blocks:
        sub = TableExtractor()
        sub.feed(block)
        if not sub.tables:
            continue
        rows = sub.tables[0]
        if len(rows) < 2:
            continue
        header = [c.lower() for c in rows[0]]
        body = rows[1:]

        if title.lower().startswith("completed"):
            for row in body:
                if row[0].lower() == "total":
                    continue
                name = row[0]
                amount = parse_amount(row[1])
                upsert(name, status="OK", amount=amount, date="2026-05-15")
        elif "manual posting" in title.lower():
            for row in body:
                upsert(
                    row[0], status="FAILED",
                    amount=parse_amount(row[1]),
                    reason=row[2], date="2026-05-15",
                )
        elif "alternate date" in title.lower():
            for row in body:
                name = row[0]
                amount = parse_amount(row[1])
                # MM/DD/YYYY → YYYY-MM-DD for date field
                try:
                    appt_iso = datetime.strptime(row[2], "%m/%d/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    appt_iso = row[2]
                posted = row[3] if len(row) > 3 else ""
                upsert(name, status="OK", method="V1", amount=amount,
                       date=appt_iso, posted_date=posted)
        elif "date mismatch" in title.lower():
            for row in body:
                name = row[0]
                amount = parse_amount(row[1])
                try:
                    sq_iso = datetime.strptime(row[2], "%m/%d/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    sq_iso = row[2]
                ta_posted = row[3] if len(row) > 3 else ""
                upsert(
                    name, status="OK", amount=amount, date=sq_iso,
                    note=f"Date mismatch: Square={row[2]}, TA={ta_posted}",
                )
        elif "outstanding balances" in title.lower():
            for row in body:
                name = row[0]
                upsert(name, status="OK",
                       note="Client has outstanding balance — additional charges exist")
                if "amount" not in by_name[name]:
                    by_name[name]["amount"] = 0.0
                if "date" not in by_name[name]:
                    by_name[name]["date"] = "2026-05-15"
        elif "name notes" in title.lower():
            for row in body:
                name = row[0]
                extra = row[1] if len(row) > 1 else ""
                upsert(name, status="OK",
                       note=f"Middle/extra name '{extra}' may not match TA")
                if "amount" not in by_name[name]:
                    by_name[name]["amount"] = 0.0
                if "date" not in by_name[name]:
                    by_name[name]["date"] = "2026-05-15"

    # Default any leftover required fields
    for r in results:
        r.setdefault("amount", 0.0)
        r.setdefault("date", "2026-05-15")

    return results, duplicates


def find_latest_posted_report():
    """Pick the newest report that came from a real bot run.

    Reports we render in this preview script also save under
    *_report_POSTED.html, so filter those out by checking for the
    "Completed Payments" section (only present in the pre-redesign layout
    that real production runs still write — until generate_report is
    called next from a real run, the only files with the old layout are
    production outputs).
    """
    candidates = sorted(REPORT_DIR.glob("*_report_POSTED.html"))
    for path in reversed(candidates):
        text = path.read_text()
        if "Completed Payments</div>" in text and "Action required" not in text:
            return path
    sys.exit("No production *_report_POSTED.html found (only preview outputs).")


def main():
    src = find_latest_posted_report()
    print(f"Source report: {src.name}")
    html = src.read_text()

    results, duplicates = reconstruct_results(html)
    print(f"Reconstructed {len(results)} results, {len(duplicates)} duplicate names")

    # Render with the current (new) layout
    csv_date = "05/15/2026"
    _, body = bot_v2.generate_report(results, duplicates, csv_date, dry_run=False)

    subject = "[SAMPLE — REVIEW] Oakley's PostIQ Payment Report — new layout"
    intro = (
        '<div style="max-width:680px;margin:16px auto;padding:14px 18px;'
        'background:#fffbe6;border-left:6px solid #ffc107;'
        'font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#5a4a00;">'
        '<strong>Sample for review.</strong> This is a re-render of the '
        f'{src.name} report using the new layout. Reply with feedback or '
        '“approved” to ship.</div>'
    )
    bot_v2.send_email(
        to="travis@greatoakcounseling.com",
        cc=None,
        subject=subject,
        body=intro + body,
    )
    print("Sample emailed to travis@greatoakcounseling.com")


if __name__ == "__main__":
    main()
