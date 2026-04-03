#!/bin/bash
# sync_inbox.sh — Copy new Square CSVs from Google Drive to local inbox
# Runs in the user GUI session (has Google Drive access)

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL_INBOX="$PROJECT_ROOT/drive-inbox"
DRIVE_SOURCE="/Users/travmegsam/Library/CloudStorage/GoogleDrive-travis@greatoakcounseling.com/My Drive/Claude Automations/Square (+) Payment CSV Files"
LOG_DIR="$PROJECT_ROOT/logs"
LOGFILE="$LOG_DIR/sync.log"

mkdir -p "$LOCAL_INBOX" "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOGFILE"
}

# Check if Google Drive is accessible
if [ ! -d "$DRIVE_SOURCE" ]; then
    log "ERROR: Google Drive folder not accessible: $DRIVE_SOURCE"
    exit 1
fi

# Copy new CSV files (only *_Daily.Square.Log.csv and *_Daily.Square.Denial.Log.csv)
copied=0
shopt -s nullglob
for f in "$DRIVE_SOURCE"/*_Daily.Square.Log.csv "$DRIVE_SOURCE"/*_Daily.Square.Denial.Log.csv; do
    fname=$(basename "$f")
    if [ ! -f "$LOCAL_INBOX/$fname" ]; then
        cp "$f" "$LOCAL_INBOX/$fname"
        log "Copied: $fname"
        copied=$((copied + 1))
    fi
done

# Also copy _REVIEW files if any
for f in "$DRIVE_SOURCE"/*_REVIEW.csv; do
    fname=$(basename "$f")
    if [ ! -f "$LOCAL_INBOX/$fname" ]; then
        cp "$f" "$LOCAL_INBOX/$fname"
        log "Copied: $fname"
        copied=$((copied + 1))
    fi
done
shopt -u nullglob

if [ "$copied" -gt 0 ]; then
    log "Synced $copied new file(s) to local inbox."
else
    log "No new files to sync."
fi
