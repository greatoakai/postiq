import csv
import io
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

from scripts.aws import is_aws_enabled, upload_bytes_to_s3, list_s3_prefix, get_s3_json

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
BOT_SCRIPT = PROJECT_ROOT / "scripts" / "bot_v2.py"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)


# ─── Report helpers ──────────────────────────────────────────────────────────

def _load_recent_reports(limit=5):
    """Load recent JSON reports from S3 or local filesystem."""
    reports = []
    if is_aws_enabled():
        keys = list_s3_prefix("reports/")
        json_keys = [k for k in keys if k.endswith(".json")]
        for key in json_keys[:limit]:
            data = get_s3_json(key)
            if data:
                reports.append(data)
    else:
        json_files = sorted(LOG_DIR.glob("*_report_*.json"), reverse=True)
        for f in json_files[:limit]:
            try:
                reports.append(json.loads(f.read_text()))
            except Exception:
                continue
    return reports


def _render_report(report):
    """Render a JSON report as a Streamlit table."""
    payments = report.get("payments", [])
    if not payments:
        return
    table = []
    for p in payments:
        table.append({
            "Status": p.get("status", ""),
            "Method": p.get("method", ""),
            "Client": p.get("name", ""),
            "Date": p.get("date", ""),
            "Amount": f"${float(p.get('amount', 0)):.2f}",
        })
    st.dataframe(table, use_container_width=True, hide_index=True)

    dups = report.get("duplicates", [])
    if dups:
        st.warning(f"Duplicates: {', '.join(dups)}")


# ─── Page layout ─────────────────────────────────────────────────────────────

st.set_page_config(page_title="PostIQ", page_icon="💳", layout="wide")

st.title("PostIQ — Credit Card Payment Posting")
st.caption("Upload a CSV to post credit card payments to TherapyAppointment")

uploaded_file = st.file_uploader("Upload payment CSV", type=["csv"])

if uploaded_file:
    raw_content = uploaded_file.getvalue().decode("utf-8-sig")

    # Skip label rows (e.g., "SUCCESSFUL PAYMENTS") before parsing
    content_lines = raw_content.splitlines(keepends=True)
    data_lines = [line for line in content_lines if "," in line]

    reader = csv.DictReader(io.StringIO("".join(data_lines)))
    rows = list(reader)

    if not rows:
        st.error("CSV is empty.")
        st.stop()

    # Validate required columns
    required = {"Full Name", "Base Amount", "Transaction Date"}
    headers = set(rows[0].keys())
    missing = required - headers
    if missing:
        st.error(f"Missing required columns: {', '.join(missing)}")
        st.stop()

    # Build preview table
    preview = []
    total = 0.0
    for row in rows:
        name = row.get("Full Name", "").strip()
        raw_amount = row.get("Base Amount", "").strip()
        date = row.get("Transaction Date", "").strip()
        if not name or not raw_amount:
            continue
        if name.upper() in ("TOTALS", "TOTAL", "GRAND TOTAL", "SUM"):
            continue
        amount_str = raw_amount.replace("$", "").replace(",", "")
        try:
            amount = float(amount_str)
        except ValueError:
            amount = 0.0
        total += amount
        preview.append({"Client": name, "Date": date, "Base Amount": f"${amount:.2f}"})

    # Detect duplicates
    name_counts = {}
    for p in preview:
        name_counts[p["Client"]] = name_counts.get(p["Client"], 0) + 1
    duplicates = {n for n, c in name_counts.items() if c > 1}

    st.subheader(f"Preview — {len(preview)} payments, ${total:.2f} total")

    if duplicates:
        st.warning(f"Duplicate names detected: {', '.join(sorted(duplicates))}. "
                   "Both entries will be posted. Staff should verify these.")

    st.dataframe(preview, use_container_width=True, hide_index=True)

    # --- Action Buttons ---
    col1, col2, col3 = st.columns([1, 1, 3])

    with col1:
        dry_run = st.button("🔍 Dry Run", use_container_width=True)
    with col2:
        post = st.button("💳 Post Payments", type="primary", use_container_width=True)

    if dry_run or post:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        if is_aws_enabled():
            s3_key = f"uploads/upload_{ts}.csv"
            upload_bytes_to_s3(raw_content, s3_key, "text/csv")
            cmd = [sys.executable, str(BOT_SCRIPT), "--s3-key", s3_key]
        else:
            csv_path = DATA_DIR / f"upload_{ts}.csv"
            csv_path.write_text(raw_content)
            cmd = [sys.executable, str(BOT_SCRIPT), str(csv_path)]

        if dry_run:
            cmd.append("--dry-run")

        mode_label = "DRY RUN" if dry_run else "POSTING"

        st.divider()
        st.subheader(f"Running — {mode_label}")

        output_area = st.empty()
        output_lines = []

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(PROJECT_ROOT),
        )

        for line in process.stdout:
            output_lines.append(line.rstrip())
            output_area.code("\n".join(output_lines), language="text")

        process.wait()

        if process.returncode == 0:
            st.success(f"{mode_label} complete!")
        else:
            st.error(f"Bot exited with code {process.returncode}")

        # Show latest report
        reports = _load_recent_reports(limit=1)
        if reports:
            st.subheader("Report")
            _render_report(reports[0])
        else:
            report_files = sorted(LOG_DIR.glob("*_report_*.txt"), reverse=True)
            if report_files:
                st.subheader("Report")
                st.code(report_files[0].read_text(), language="text")

else:
    st.info("Upload a CSV file with columns: **Full Name**, **Base Amount**, "
            "**Transaction Date** (Total Charged and 3% Fee columns are optional).")

    # Show recent reports
    reports = _load_recent_reports(limit=5)
    if reports:
        st.divider()
        st.subheader("Recent Reports")
        for report in reports:
            generated = report.get("generated", "unknown")
            mode = report.get("mode", "")
            success = report.get("success_count", 0)
            total_pay = report.get("total_payments", 0)
            label = f"{generated[:16]} — {mode} — {success}/{total_pay} payments"
            with st.expander(label):
                _render_report(report)
    else:
        # Fallback to local text reports
        report_files = sorted(LOG_DIR.glob("*_report_*.txt"), reverse=True)
        if report_files:
            st.divider()
            st.subheader("Recent Reports")
            for report_file in report_files[:5]:
                with st.expander(report_file.name):
                    st.code(report_file.read_text(), language="text")
