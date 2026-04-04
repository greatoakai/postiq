"""Database helpers for batch and payment tracking."""

import os
import uuid
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor

from scripts.aws import get_database_url, is_aws_enabled

_conn = None


def _get_conn():
    """Get or create a database connection."""
    global _conn
    if _conn is not None and not _conn.closed:
        return _conn
    db_url = get_database_url()
    if not db_url:
        return None
    _conn = psycopg2.connect(db_url)
    _conn.autocommit = True
    return _conn


def is_db_enabled():
    """Return True if a database connection is available."""
    return get_database_url() is not None


def create_batch(csv_s3_key, source="upload", dry_run=False, total_rows=0):
    """Insert a new batch record. Returns the batch ID."""
    conn = _get_conn()
    if not conn:
        return None
    batch_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO batch (id, source, status, csv_s3_key, dry_run, total_rows, created_at)
               VALUES (%s, %s, 'running', %s, %s, %s, %s)""",
            (batch_id, source, csv_s3_key, dry_run, total_rows, datetime.now())
        )
    print(f"  Batch created: {batch_id}")
    return batch_id


def update_batch(batch_id, status, success_count=0, fail_count=0, report_s3_key=None):
    """Update batch status and counts."""
    conn = _get_conn()
    if not conn or not batch_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE batch
               SET status = %s, success_count = %s, fail_count = %s,
                   report_s3_key = %s, completed_at = %s
               WHERE id = %s""",
            (status, success_count, fail_count, report_s3_key, datetime.now(), batch_id)
        )


def insert_payment(batch_id, client_name, payment_date, amount, status,
                    method=None, error_message=None, screenshot_key=None):
    """Insert a payment result record."""
    conn = _get_conn()
    if not conn or not batch_id:
        return
    payment_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO payment
               (id, batch_id, client_name, payment_date, amount, status,
                method, error_message, screenshot_key, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (payment_id, batch_id, client_name, payment_date, amount, status,
             method, error_message, screenshot_key, datetime.now())
        )


def get_recent_batches(limit=20):
    """Get recent batch records for the dashboard."""
    conn = _get_conn()
    if not conn:
        return []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT id, source, status, csv_s3_key, report_s3_key,
                      total_rows, success_count, fail_count, dry_run,
                      created_at, completed_at
               FROM batch ORDER BY created_at DESC LIMIT %s""",
            (limit,)
        )
        return cur.fetchall()


def get_batch_payments(batch_id):
    """Get all payment records for a batch."""
    conn = _get_conn()
    if not conn:
        return []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT client_name, payment_date, amount, status, method,
                      error_message, screenshot_key, created_at
               FROM payment WHERE batch_id = %s ORDER BY created_at""",
            (batch_id,)
        )
        return cur.fetchall()


def get_pending_batch():
    """Get the most recent pending batch (for scheduled runs)."""
    conn = _get_conn()
    if not conn:
        return None
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT id, csv_s3_key, dry_run
               FROM batch WHERE status = 'pending'
               ORDER BY created_at DESC LIMIT 1"""
        )
        return cur.fetchone()


def get_processed_s3_keys():
    """Get all S3 keys that have already been processed (for dedup in scheduled mode)."""
    conn = _get_conn()
    if not conn:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT csv_s3_key FROM batch")
        return {row[0] for row in cur.fetchall()}
