import csv
import io
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

from scripts.aws import is_aws_enabled, upload_bytes_to_s3, get_s3_text
from scripts.db import (
    is_db_enabled, create_batch, get_recent_batches, get_batch_payments,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
BOT_SCRIPT = PROJECT_ROOT / "scripts" / "bot_v2.py"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

st.set_page_config(page_title="PostIQ", page_icon="💳", layout="wide")

st.title("PostIQ — Credit Card Payment Posting")
st.caption("Upload a CSV to post credit card payments to TherapyAppointment")

# --- CSV Upload ---
uploaded_file = st.file_uploader("Upload payment CSV", type=["csv"])

if uploaded_file:
    # Parse and preview the CSV
    raw_content = uploaded_file.getvalue().decode("utf-8-sig")

    # Skip label rows (e.g., "SUCCESSFUL PAYMENTS") before parsing
    content_lines = raw_content.splitlines(keepends=True)
    data_lines = [line for line in content_lines if "," in line]
    content_for_preview = "".join(data_lines)

    reader = csv.DictReader(io.StringIO(content_for_preview))
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
        # Skip summary/totals rows
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

        # Save CSV — to S3 if configured, otherwise local
        if is_aws_enabled():
            s3_key = f"uploads/upload_{ts}.csv"
            upload_bytes_to_s3(raw_content, s3_key, "text/csv")
            csv_ref = s3_key

            # Create pending batch in database
            if is_db_enabled():
                create_batch(s3_key, source="upload", dry_run=dry_run,
                             total_rows=len(preview))

            # Build bot command with S3 key
            cmd = [sys.executable, str(BOT_SCRIPT), "--s3-key", s3_key]
        else:
            csv_path = DATA_DIR / f"upload_{ts}.csv"
            csv_path.write_text(raw_content)
            csv_ref = str(csv_path)
            cmd = [sys.executable, str(BOT_SCRIPT), str(csv_path)]

        if dry_run:
            cmd.append("--dry-run")

        mode_label = "DRY RUN" if dry_run else "POSTING"

        st.divider()
        st.subheader(f"Running — {mode_label}")

        # Run the bot as a subprocess and stream output
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

        # Show the report — from database or local file
        if is_db_enabled():
            batches = get_recent_batches(limit=1)
            if batches:
                batch = batches[0]
                payments = get_batch_payments(batch["id"])
                if payments:
                    st.subheader("Report")
                    report_data = []
                    for pay in payments:
                        report_data.append({
                            "Status": pay["status"],
                            "Method": pay["method"] or "",
                            "Client": pay["client_name"],
                            "Amount": f"${pay['amount']:.2f}",
                        })
                    st.dataframe(report_data, use_container_width=True, hide_index=True)
        else:
            report_files = sorted(LOG_DIR.glob("*_report_*.txt"), reverse=True)
            if report_files:
                latest_report = report_files[0]
                st.subheader("Report")
                st.code(latest_report.read_text(), language="text")

else:
    st.info("Upload a CSV file with columns: **Full Name**, **Base Amount**, "
            "**Transaction Date** (Total Charged and 3% Fee columns are optional).")

    # Show recent batches from database, or local reports as fallback
    if is_db_enabled():
        batches = get_recent_batches(limit=10)
        if batches:
            st.divider()
            st.subheader("Recent Batches")
            for batch in batches:
                label = (f"{batch['created_at'].strftime('%Y-%m-%d %H:%M')} — "
                         f"{batch['status'].upper()} — "
                         f"{batch['success_count'] or 0}/{batch['total_rows'] or 0} payments")
                if batch["dry_run"]:
                    label += " (DRY RUN)"
                with st.expander(label):
                    payments = get_batch_payments(batch["id"])
                    if payments:
                        pay_data = []
                        for pay in payments:
                            pay_data.append({
                                "Status": pay["status"],
                                "Method": pay["method"] or "",
                                "Client": pay["client_name"],
                                "Date": str(pay["payment_date"] or ""),
                                "Amount": f"${pay['amount']:.2f}",
                                "Error": pay["error_message"] or "",
                            })
                        st.dataframe(pay_data, use_container_width=True, hide_index=True)
                    # Show text report if available
                    if batch.get("report_s3_key") and is_aws_enabled():
                        report_text = get_s3_text(batch["report_s3_key"])
                        if report_text:
                            st.code(report_text, language="text")
    else:
        report_files = sorted(LOG_DIR.glob("*_report_*.txt"), reverse=True)
        if report_files:
            st.divider()
            st.subheader("Recent Reports")
            for report in report_files[:5]:
                with st.expander(report.name):
                    st.code(report.read_text(), language="text")
