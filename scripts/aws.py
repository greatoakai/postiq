"""AWS helpers for S3 and Secrets Manager."""

import json
import os
import tempfile

import boto3
from botocore.exceptions import ClientError

# Bucket name from environment, or None if running locally
S3_BUCKET = os.getenv("S3_BUCKET")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

_s3_client = None
_secrets_client = None


def _get_s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=AWS_REGION)
    return _s3_client


def _get_secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)
    return _secrets_client


def is_aws_enabled():
    """Return True if S3_BUCKET is configured (running on AWS)."""
    return bool(S3_BUCKET)


# ─── Secrets Manager ────────────────────────────────────────────────────────

def get_secret(secret_name):
    """Fetch a secret from AWS Secrets Manager. Returns dict of key-value pairs."""
    client = _get_secrets()
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def get_credentials():
    """Get TherapyAppointment credentials from Secrets Manager or .env fallback."""
    if is_aws_enabled():
        secret = get_secret("postiq/credentials")
        return secret["TA_USERNAME"], secret["TA_PASSWORD"]
    # Local fallback: use .env
    return os.getenv("TA_USERNAME"), os.getenv("TA_PASSWORD")


def get_database_url():
    """Get the database connection string from Secrets Manager or env."""
    if is_aws_enabled():
        secret = get_secret("postiq/database")
        return secret["DATABASE_URL"]
    return os.getenv("DATABASE_URL")


# ─── S3 ─────────────────────────────────────────────────────────────────────

def upload_to_s3(local_path, s3_key):
    """Upload a local file to S3. No-op if S3 not configured."""
    if not is_aws_enabled():
        return None
    _get_s3().upload_file(str(local_path), S3_BUCKET, s3_key)
    print(f"  Uploaded to S3: {s3_key}")
    return s3_key


def upload_bytes_to_s3(data, s3_key, content_type="application/octet-stream"):
    """Upload bytes or string to S3. No-op if S3 not configured."""
    if not is_aws_enabled():
        return None
    if isinstance(data, str):
        data = data.encode("utf-8")
    _get_s3().put_object(Bucket=S3_BUCKET, Key=s3_key, Body=data, ContentType=content_type)
    print(f"  Uploaded to S3: {s3_key}")
    return s3_key


def download_from_s3(s3_key):
    """Download a file from S3 to a temp file. Returns the temp file path."""
    if not is_aws_enabled():
        return None
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
    _get_s3().download_file(S3_BUCKET, s3_key, tmp.name)
    print(f"  Downloaded from S3: {s3_key}")
    return tmp.name


def list_s3_prefix(prefix):
    """List object keys under a prefix. Returns list of keys sorted newest first."""
    if not is_aws_enabled():
        return []
    client = _get_s3()
    response = client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
    objects = response.get("Contents", [])
    objects.sort(key=lambda o: o["LastModified"], reverse=True)
    return [o["Key"] for o in objects]


def get_s3_text(s3_key):
    """Read a text file from S3 and return its contents as string."""
    if not is_aws_enabled():
        return None
    response = _get_s3().get_object(Bucket=S3_BUCKET, Key=s3_key)
    return response["Body"].read().decode("utf-8")
