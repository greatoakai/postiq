#!/usr/bin/env python3
"""
sync_inbox.py — Download new Square payment CSVs from S3 into the local PostIQ inbox.

Replaces the old Google Drive sync. After successful download, the file is
DELETED from S3 — the bucket is a transient handoff, not long-term storage.

The permanent record of each daily report lives in two places:
  1. The HTML email Hannah and Travis receive (with both CSVs attached)
  2. The local archive folder (Square Payment Archive/) — pruned by the
     existing weekly cleanup job

Reads credentials from .env in the project root.
Logs to logs/sync.log.

Usage:
  python3 scripts/sync_inbox.py
  python3 scripts/sync_inbox.py --dry-run    (download but DO NOT delete from S3)
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_INBOX = PROJECT_ROOT / "drive-inbox"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "sync.log"

LOCAL_INBOX.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Load .env from project root
load_dotenv(PROJECT_ROOT / ".env")

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
S3_BUCKET = os.getenv("S3_BUCKET")
S3_REGION = os.getenv("S3_REGION", "us-east-1")
S3_PREFIX = os.getenv("S3_PREFIX", "uploads/")


def log(message):
    """Append a timestamped line to sync.log and print it."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line)


def main():
    parser = argparse.ArgumentParser(description="Download Square CSVs from S3")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Download files but do NOT delete them from S3 afterward.",
    )
    args = parser.parse_args()

    if not (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and S3_BUCKET):
        log("ERROR: Missing AWS credentials or S3_BUCKET in .env")
        sys.exit(1)

    log(f"Sync started — bucket={S3_BUCKET} prefix={S3_PREFIX}")
    if args.dry_run:
        log("DRY RUN — files will NOT be deleted from S3")

    s3 = boto3.client(
        "s3",
        region_name=S3_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )

    # List all objects under the prefix
    try:
        response = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=S3_PREFIX)
    except (BotoCoreError, ClientError) as err:
        log(f"ERROR: Could not list S3 bucket: {err}")
        sys.exit(1)

    objects = response.get("Contents", [])
    if not objects:
        log("No new files in S3. Nothing to sync.")
        return

    downloaded = 0
    for obj in objects:
        key = obj["Key"]
        filename = os.path.basename(key)

        # Skip the prefix itself if it shows up as a "folder" object
        if not filename or key.endswith("/"):
            continue

        # Skip if we already have this file locally
        local_path = LOCAL_INBOX / filename
        if local_path.exists():
            log(f"Skipping (already local): {filename}")
            # Still safe to delete from S3 — we already have it
            if not args.dry_run:
                try:
                    s3.delete_object(Bucket=S3_BUCKET, Key=key)
                    log(f"Deleted from S3: {key}")
                except (BotoCoreError, ClientError) as err:
                    log(f"WARN: Could not delete {key} from S3: {err}")
            continue

        # Download
        try:
            s3.download_file(S3_BUCKET, key, str(local_path))
            log(f"Downloaded: {filename}")
            downloaded += 1
        except (BotoCoreError, ClientError) as err:
            log(f"ERROR: Could not download {key}: {err}")
            continue

        # Delete from S3 after successful download
        if args.dry_run:
            log(f"DRY RUN — would delete from S3: {key}")
        else:
            try:
                s3.delete_object(Bucket=S3_BUCKET, Key=key)
                log(f"Deleted from S3: {key}")
            except (BotoCoreError, ClientError) as err:
                log(f"WARN: Could not delete {key} from S3: {err}")

    log(f"Sync complete — {downloaded} new file(s) downloaded.")


if __name__ == "__main__":
    main()
