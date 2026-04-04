#!/usr/bin/env bash
# PostIQ AWS Infrastructure Setup
# Run this once from a machine with AWS CLI configured.
#
# Prerequisites:
#   - AWS CLI v2 installed and configured (aws configure)
#   - Sufficient IAM permissions for S3, RDS, Secrets Manager, EC2
#
# Usage:
#   chmod +x deploy/setup_aws.sh
#   ./deploy/setup_aws.sh

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# Configuration — edit these before running
# ──────────────────────────────────────────────────────────────────────
REGION="us-east-1"
PROJECT="postiq"

# S3
S3_BUCKET="${PROJECT}-data-$(aws sts get-caller-identity --query Account --output text)"

# RDS
DB_INSTANCE="${PROJECT}-db"
DB_NAME="postiq"
DB_USERNAME="postiq_admin"
DB_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"

# Secrets Manager
SECRET_TA_NAME="${PROJECT}/credentials"
SECRET_DB_NAME="${PROJECT}/database"

# EC2
KEY_PAIR_NAME="${PROJECT}-key"
INSTANCE_TYPE="t3.medium"

echo "============================================="
echo "  PostIQ AWS Setup"
echo "  Region: ${REGION}"
echo "  S3 Bucket: ${S3_BUCKET}"
echo "  RDS Instance: ${DB_INSTANCE}"
echo "============================================="
echo ""

# ──────────────────────────────────────────────────────────────────────
# Step 1: S3 Bucket
# ──────────────────────────────────────────────────────────────────────
echo ">>> Step 1: Creating S3 bucket..."
if aws s3api head-bucket --bucket "${S3_BUCKET}" 2>/dev/null; then
    echo "  Bucket already exists: ${S3_BUCKET}"
else
    aws s3api create-bucket \
        --bucket "${S3_BUCKET}" \
        --region "${REGION}" \
        --create-bucket-configuration LocationConstraint="${REGION}" 2>/dev/null || \
    aws s3api create-bucket \
        --bucket "${S3_BUCKET}" \
        --region "${REGION}"
    echo "  Created: ${S3_BUCKET}"
fi

# Enable server-side encryption
aws s3api put-bucket-encryption \
    --bucket "${S3_BUCKET}" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
echo "  Encryption enabled (AES-256)"

# Block public access
aws s3api put-public-access-block \
    --bucket "${S3_BUCKET}" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
echo "  Public access blocked"

# Create folder structure
aws s3api put-object --bucket "${S3_BUCKET}" --key "uploads/" --content-length 0
aws s3api put-object --bucket "${S3_BUCKET}" --key "screenshots/" --content-length 0
aws s3api put-object --bucket "${S3_BUCKET}" --key "reports/" --content-length 0
echo "  Created folder structure: uploads/, screenshots/, reports/"
echo ""

# ──────────────────────────────────────────────────────────────────────
# Step 2: RDS PostgreSQL
# ──────────────────────────────────────────────────────────────────────
echo ">>> Step 2: Creating RDS PostgreSQL instance..."

# Get default VPC
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
    --query "Vpcs[0].VpcId" --output text --region "${REGION}")
echo "  VPC: ${VPC_ID}"

# Create security group for RDS
RDS_SG_NAME="${PROJECT}-rds-sg"
RDS_SG_ID=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=${RDS_SG_NAME}" \
    --query "SecurityGroups[0].GroupId" --output text --region "${REGION}" 2>/dev/null) || true

if [ "${RDS_SG_ID}" = "None" ] || [ -z "${RDS_SG_ID}" ]; then
    RDS_SG_ID=$(aws ec2 create-security-group \
        --group-name "${RDS_SG_NAME}" \
        --description "PostIQ RDS access" \
        --vpc-id "${VPC_ID}" \
        --region "${REGION}" \
        --query "GroupId" --output text)
    echo "  Created security group: ${RDS_SG_ID}"

    # Allow PostgreSQL from within VPC
    VPC_CIDR=$(aws ec2 describe-vpcs --vpc-ids "${VPC_ID}" \
        --query "Vpcs[0].CidrBlock" --output text --region "${REGION}")
    aws ec2 authorize-security-group-ingress \
        --group-id "${RDS_SG_ID}" \
        --protocol tcp --port 5432 \
        --cidr "${VPC_CIDR}" \
        --region "${REGION}"
    echo "  Ingress rule: PostgreSQL from ${VPC_CIDR}"
else
    echo "  Security group already exists: ${RDS_SG_ID}"
fi

# Create RDS instance
DB_EXISTS=$(aws rds describe-db-instances \
    --db-instance-identifier "${DB_INSTANCE}" \
    --query "DBInstances[0].DBInstanceIdentifier" --output text \
    --region "${REGION}" 2>/dev/null) || true

if [ "${DB_EXISTS}" = "${DB_INSTANCE}" ]; then
    echo "  RDS instance already exists: ${DB_INSTANCE}"
else
    aws rds create-db-instance \
        --db-instance-identifier "${DB_INSTANCE}" \
        --db-instance-class db.t3.micro \
        --engine postgres \
        --engine-version "15" \
        --master-username "${DB_USERNAME}" \
        --master-user-password "${DB_PASSWORD}" \
        --db-name "${DB_NAME}" \
        --allocated-storage 20 \
        --storage-encrypted \
        --vpc-security-group-ids "${RDS_SG_ID}" \
        --backup-retention-period 7 \
        --no-publicly-accessible \
        --region "${REGION}" \
        --no-cli-pager
    echo "  RDS instance creating (this takes ~5 minutes)..."
    echo "  Waiting for availability..."
    aws rds wait db-instance-available \
        --db-instance-identifier "${DB_INSTANCE}" \
        --region "${REGION}"
    echo "  RDS instance available!"
fi

# Get RDS endpoint
DB_ENDPOINT=$(aws rds describe-db-instances \
    --db-instance-identifier "${DB_INSTANCE}" \
    --query "DBInstances[0].Endpoint.Address" --output text \
    --region "${REGION}")
DATABASE_URL="postgresql://${DB_USERNAME}:${DB_PASSWORD}@${DB_ENDPOINT}:5432/${DB_NAME}"
echo "  Endpoint: ${DB_ENDPOINT}"
echo ""

# ──────────────────────────────────────────────────────────────────────
# Step 3: Initialize database schema
# ──────────────────────────────────────────────────────────────────────
echo ">>> Step 3: Initializing database schema..."
echo "  Run this manually once the RDS instance is available:"
echo "    psql \"${DATABASE_URL}\" -f scripts/schema.sql"
echo ""

# ──────────────────────────────────────────────────────────────────────
# Step 4: Secrets Manager
# ──────────────────────────────────────────────────────────────────────
echo ">>> Step 4: Creating secrets..."

# Prompt for TherapyAppointment credentials
read -rp "  Enter TA_USERNAME: " TA_USER
read -rsp "  Enter TA_PASSWORD: " TA_PASS
echo ""

# Create TA credentials secret
aws secretsmanager create-secret \
    --name "${SECRET_TA_NAME}" \
    --description "TherapyAppointment login credentials" \
    --secret-string "{\"TA_USERNAME\":\"${TA_USER}\",\"TA_PASSWORD\":\"${TA_PASS}\"}" \
    --region "${REGION}" 2>/dev/null || \
aws secretsmanager update-secret \
    --secret-id "${SECRET_TA_NAME}" \
    --secret-string "{\"TA_USERNAME\":\"${TA_USER}\",\"TA_PASSWORD\":\"${TA_PASS}\"}" \
    --region "${REGION}"
echo "  Secret created: ${SECRET_TA_NAME}"

# Create database URL secret
aws secretsmanager create-secret \
    --name "${SECRET_DB_NAME}" \
    --description "PostIQ database connection string" \
    --secret-string "{\"DATABASE_URL\":\"${DATABASE_URL}\"}" \
    --region "${REGION}" 2>/dev/null || \
aws secretsmanager update-secret \
    --secret-id "${SECRET_DB_NAME}" \
    --secret-string "{\"DATABASE_URL\":\"${DATABASE_URL}\"}" \
    --region "${REGION}"
echo "  Secret created: ${SECRET_DB_NAME}"
echo ""

# ──────────────────────────────────────────────────────────────────────
# Step 5: EC2 Key Pair
# ──────────────────────────────────────────────────────────────────────
echo ">>> Step 5: Creating EC2 key pair..."
if aws ec2 describe-key-pairs --key-names "${KEY_PAIR_NAME}" \
    --region "${REGION}" &>/dev/null; then
    echo "  Key pair already exists: ${KEY_PAIR_NAME}"
else
    aws ec2 create-key-pair \
        --key-name "${KEY_PAIR_NAME}" \
        --query "KeyMaterial" --output text \
        --region "${REGION}" > "${KEY_PAIR_NAME}.pem"
    chmod 400 "${KEY_PAIR_NAME}.pem"
    echo "  Key pair saved: ${KEY_PAIR_NAME}.pem"
fi
echo ""

# ──────────────────────────────────────────────────────────────────────
# Step 6: IAM Role for EC2
# ──────────────────────────────────────────────────────────────────────
echo ">>> Step 6: Creating IAM role for EC2..."
ROLE_NAME="${PROJECT}-ec2-role"
INSTANCE_PROFILE="${PROJECT}-ec2-profile"

# Create role trust policy
cat > /tmp/trust-policy.json <<TRUST
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ec2.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
TRUST

aws iam create-role \
    --role-name "${ROLE_NAME}" \
    --assume-role-policy-document file:///tmp/trust-policy.json \
    2>/dev/null || echo "  Role already exists"

# Attach policies: S3, Secrets Manager
cat > /tmp/postiq-policy.json <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::${S3_BUCKET}",
        "arn:aws:s3:::${S3_BUCKET}/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": [
        "arn:aws:secretsmanager:${REGION}:*:secret:${PROJECT}/*"
      ]
    }
  ]
}
POLICY

aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name "${PROJECT}-access" \
    --policy-document file:///tmp/postiq-policy.json

# Create instance profile
aws iam create-instance-profile \
    --instance-profile-name "${INSTANCE_PROFILE}" 2>/dev/null || true
aws iam add-role-to-instance-profile \
    --instance-profile-name "${INSTANCE_PROFILE}" \
    --role-name "${ROLE_NAME}" 2>/dev/null || true
echo "  IAM role and instance profile ready"
echo ""

# ──────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────
echo "============================================="
echo "  Setup Complete!"
echo "============================================="
echo ""
echo "  S3 Bucket:      ${S3_BUCKET}"
echo "  RDS Endpoint:    ${DB_ENDPOINT}"
echo "  DB Password:     ${DB_PASSWORD}"
echo "  IAM Role:        ${ROLE_NAME}"
echo "  Key Pair:        ${KEY_PAIR_NAME}.pem"
echo ""
echo "  Next steps:"
echo "  1. Initialize the database:"
echo "     psql \"${DATABASE_URL}\" -f scripts/schema.sql"
echo ""
echo "  2. Launch EC2 instance:"
echo "     ./deploy/launch_ec2.sh"
echo ""
echo "  SAVE THESE VALUES — the DB password will not be shown again."
