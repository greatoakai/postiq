#!/bin/bash
# run_daily.sh — Sync the day's Square CSV from S3 and archive it for reconciliation.
# The live poller posts; com.greatoak.postiq-reconcile emails the single daily report.
# Called by LaunchAgent or manually

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INBOX="$PROJECT_ROOT/drive-inbox"
LOG_DIR="$PROJECT_ROOT/logs"
MARKER="$PROJECT_ROOT/.last_processed"
RECIPIENTS="hannah@greatoakcounseling.com,travis@greatoakcounseling.com,supportstaff@greatoakcounseling.com"

mkdir -p "$LOG_DIR"

DATE=$(date +%Y%m%d)
LOGFILE="$LOG_DIR/daily_${DATE}.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"
}

send_email() {
    local subject="$1"
    local body="$2"
    msmtp -t <<EOF
To: $RECIPIENTS
From: travis@greatoakcounseling.com
Subject: $subject

$body
EOF
}

log "=== PostIQ Daily Run ==="

# ── Step 0: Sync from S3 ──
# The Square Daily Report bot uploads new CSVs to S3 each morning at 6:00 AM.
# This step downloads them into the local inbox and deletes them from S3.
log "Syncing new CSVs from S3..."
SYNC_OUTPUT=$(/usr/bin/python3 "$PROJECT_ROOT/scripts/sync_inbox.py" 2>&1) || true
echo "$SYNC_OUTPUT" >> "$LOGFILE"

# Find CSV files matching the Daily.Square.Log pattern
shopt -s nullglob
csv_files=("$INBOX"/*_Daily.Square.Log.csv)
shopt -u nullglob

if [ ${#csv_files[@]} -eq 0 ]; then
    log "No CSV files found in inbox. Exiting."
    exit 0
fi

# Sort by filename (contains date as MM.DD.YYYY) — newest last
# Convert MM.DD.YYYY to YYYYMMDD for proper sorting
newest=""
newest_sort=""
for f in "${csv_files[@]}"; do
    fname=$(basename "$f")
    # Extract date from filename: MM.DD.YYYY_Daily.Square.Log.csv
    date_part="${fname%%_Daily*}"
    # Convert MM.DD.YYYY to YYYYMMDD
    IFS='.' read -r mm dd yyyy <<< "$date_part"
    sort_key="${yyyy}${mm}${dd}"
    if [ -z "$newest_sort" ] || [ "$sort_key" \> "$newest_sort" ]; then
        newest="$f"
        newest_sort="$sort_key"
    fi
done

newest_name=$(basename "$newest")
log "Newest CSV: $newest_name"

# Check if already processed
if [ -f "$MARKER" ] && grep -qF "$newest_name" "$MARKER"; then
    log "Already processed: $newest_name. Skipping."
    exit 0
fi

# Alert if multiple unprocessed files
unprocessed=()
for f in "${csv_files[@]}"; do
    fname=$(basename "$f")
    if [ -f "$MARKER" ] && grep -qF "$fname" "$MARKER" 2>/dev/null; then
        continue
    fi
    unprocessed+=("$fname")
done

if [ ${#unprocessed[@]} -gt 1 ]; then
    alert_msg="ALERT: Multiple unprocessed CSV files found in inbox:
$(printf '  - %s\n' "${unprocessed[@]}")

Processing only the newest: $newest_name
Please review the other files manually."
    log "$alert_msg"
    send_email "[PostIQ] ALERT: Multiple unprocessed CSV files" "$alert_msg"
fi

# NOTE (2026-06-22): this job no longer runs bot_v2.py.
# Since the shadow->live cutover, the live poller (com.greatoak.postiq-poll-live)
# does the real posting in near-real-time, and com.greatoak.postiq-reconcile emails
# the single daily report (poller ledger vs this CSV). The old report-only
# `bot_v2.py --dry-run` step was removed to stop the redundant "DRY RUN — ERRORS
# DETECTED" staff + tech emails (they re-simulated a batch that posts nothing).
# This job now only syncs the day's CSV from S3 (above) and archives it (below) so
# the reconcile has it to compare against.

# Mark as processed (even on failure, to avoid retry loops —
# failed files should be re-run manually after fixing the issue)
echo "$newest_name" >> "$MARKER"

# Move the processed CSV to the local archive folder
ARCHIVE="$PROJECT_ROOT/Square Payment Archive"
mkdir -p "$ARCHIVE"
log "Archiving processed CSV: $newest_name"
mv "$newest" "$ARCHIVE/$newest_name"
log "Archived: $newest_name → Square Payment Archive/"

log "=== Done ==="
