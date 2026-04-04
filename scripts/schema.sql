-- PostIQ Database Schema
-- Run this once on your RDS PostgreSQL instance to set up tables.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS batch (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source        VARCHAR(20) NOT NULL,
    status        VARCHAR(20) NOT NULL DEFAULT 'pending',
    csv_s3_key    TEXT NOT NULL,
    report_s3_key TEXT,
    total_rows    INT,
    success_count INT,
    fail_count    INT,
    dry_run       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS payment (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id        UUID NOT NULL REFERENCES batch(id),
    client_name     VARCHAR(200) NOT NULL,
    payment_date    DATE,
    amount          NUMERIC(10,2) NOT NULL,
    status          VARCHAR(20) NOT NULL,
    method          VARCHAR(10),
    error_message   TEXT,
    screenshot_key  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payment_batch ON payment(batch_id);
CREATE INDEX IF NOT EXISTS idx_batch_created ON batch(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_batch_status ON batch(status);
