#!/bin/bash
# cleanup_archive.sh — Delete archived CSVs older than 30 days
# Called by LaunchAgent weekly

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE="$PROJECT_ROOT/drive-inbox/../Square Payment Archive"
LOG_DIR="$PROJECT_ROOT/logs"
LOGFILE="$LOG_DIR/cleanup.log"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"
}

log "=== Archive Cleanup ==="

# Find and delete CSV files older than 30 days
deleted=0
shopt -s nullglob
for f in "$ARCHIVE"/*.csv; do
    # Get file age in days from the filename date (MM.DD.YYYY)
    fname=$(basename "$f")
    date_part="${fname%%_Daily*}"

    # Skip files that don't match the expected naming pattern
    if [ "$date_part" = "$fname" ]; then
        continue
    fi

    IFS='.' read -r mm dd yyyy <<< "$date_part"
    file_date="${yyyy}-${mm}-${dd}"

    # Calculate age in days
    file_epoch=$(date -j -f "%Y-%m-%d" "$file_date" "+%s" 2>/dev/null) || continue
    now_epoch=$(date "+%s")
    age_days=$(( (now_epoch - file_epoch) / 86400 ))

    if [ "$age_days" -gt 30 ]; then
        log "Deleting (${age_days} days old): $fname"
        rm -f "$f"
        deleted=$((deleted + 1))
    fi
done
shopt -u nullglob

log "Deleted $deleted file(s) older than 30 days."
log "=== Done ==="
