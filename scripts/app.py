import csv
import io
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

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
    content = uploaded_file.getvalue().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
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
        preview.append({"Client": name, "Base Amount": f"${amount:.2f}"})

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
        # Save CSV to data/ for the bot to read
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = DATA_DIR / f"upload_{ts}.csv"
        csv_path.write_text(content)

        mode = "--dry-run" if dry_run else ""
        mode_label = "DRY RUN" if dry_run else "POSTING"

        st.divider()
        st.subheader(f"Running — {mode_label}")

        # Run the bot as a subprocess and stream output
        cmd = [sys.executable, str(BOT_SCRIPT), str(csv_path)]
        if dry_run:
            cmd.append("--dry-run")

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

        # Show the report if it was generated
        report_files = sorted(LOG_DIR.glob("*_report_*.txt"), reverse=True)
        if report_files:
            latest_report = report_files[0]
            st.subheader("Report")
            st.code(latest_report.read_text(), language="text")

else:
    st.info("Upload a CSV file with columns: **Full Name**, **Base Amount** "
            "(Total Charged and 3% Fee columns are optional and ignored).")

    # Show recent reports
    report_files = sorted(LOG_DIR.glob("*_report_*.txt"), reverse=True)
    if report_files:
        st.divider()
        st.subheader("Recent Reports")
        for report in report_files[:5]:
            with st.expander(report.name):
                st.code(report.read_text(), language="text")
