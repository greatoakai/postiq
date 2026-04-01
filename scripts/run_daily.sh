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

# Run the bot — emails are sent automatically by bot_v2.py
log "Running bot on: $newest_name"
BOT_EXIT=0
BOT_OUTPUT=$(/usr/bin/python3 "$PROJECT_ROOT/scripts/bot_v2.py" "$newest" 2>&1) || BOT_EXIT=$?
echo "$BOT_OUTPUT" >> "$LOGFILE"

# If bot crashed before it could send its own report, alert Travis
if [ "$BOT_EXIT" -ne 0 ]; then
    log "ERROR: Bot exited with code $BOT_EXIT"
    # Grab the last 50 lines of output for context
    error_tail=$(echo "$BOT_OUTPUT" | tail -50)
    msmtp -t <<ERRMSG
To: travis@greatoakcounseling.com
From: Oakley, Great Oak AI Assistant <travis@greatoakcounseling.com>
Subject: [PostIQ] SYSTEM ERROR — bot did not complete
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

The PostIQ daily run failed before it could generate a report.

CSV file: $newest_name
Exit code: $BOT_EXIT
Time: $(date '+%Y-%m-%d %H:%M:%S')

--- Last 50 lines of output ---

$error_tail

--- End of output ---

Check the full log at: $LOGFILE

Oakley, Great Oak Counseling's AI Assistant
ERRMSG
    log "Crash alert emailed to Travis."
fi

# Mark as processed (even on failure, to avoid retry loops —
# failed files should be re-run manually after fixing the issue)
echo "$newest_name" >> "$MARKER"

# Move the processed CSV to the Archive folder in Google Drive
ARCHIVE="$INBOX/../Square Payment Archive"
log "Archiving processed CSV: $newest_name"
mv "$newest" "$ARCHIVE/$newest_name"
log "Archived: $newest_name → Square Payment Archive/"

log "=== Done ==="
