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

# ── Step 0 REMOVED 2026-08-28: the S3 sync is gone ──
# The Square Daily Report bot used to upload the CSV to
# s3://greatoak-square-reports/uploads/ and this step downloaded it an hour
# later. Both ends run on the SAME Mac mini, so the round-trip cost an S3
# bucket, an IAM user and a long-lived access key to move a file between two
# local directories (#491). The bot now writes straight into drive-inbox/ —
# verified in its own log: "PostIQ hand-off OK ... (2341 bytes)", 2026-08-28.
#
# The sync call was wrapped in `|| true`, so once the bucket is deleted it
# would have failed every morning while this job still reported success.
# scripts/sync_inbox.py is now unreferenced and can be deleted with the bucket.

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
