#!/bin/bash
# run_daily.sh — Watch for new Square CSV, run PostIQ bot, email report
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

# Run the bot (dry-run mode for safety — remove --dry-run when ready for live)
log "Running bot on: $newest_name"
BOT_OUTPUT=$(/usr/bin/python3 "$PROJECT_ROOT/scripts/bot_v2.py" "$newest" --dry-run 2>&1) || true
echo "$BOT_OUTPUT" >> "$LOGFILE"

# Mark as processed
echo "$newest_name" >> "$MARKER"

# Find the report file (most recent)
report=$(ls -t "$LOG_DIR"/*_report_*.txt 2>/dev/null | head -1)
if [ -n "$report" ]; then
    report_content=$(cat "$report")
else
    report_content="$BOT_OUTPUT"
fi

# Check for failures in the output
if echo "$BOT_OUTPUT" | grep -qiE "ERROR|FAILED|TIMEOUT"; then
    send_email "[PostIQ] Payment Report — ERRORS DETECTED (DRY RUN)" "$report_content"
    log "Email sent (with errors)."
else
    send_email "[PostIQ] Payment Report — Success (DRY RUN)" "$report_content"
    log "Email sent (success)."
fi

# Move the processed CSV to the Archive folder in Google Drive
ARCHIVE="$INBOX/../Square Payment Archive"
log "Archiving processed CSV: $newest_name"
mv "$newest" "$ARCHIVE/$newest_name"
log "Archived: $newest_name → Square Payment Archive/"

log "=== Done ==="
