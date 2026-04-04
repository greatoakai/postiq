#!/usr/bin/env bash
# PostIQ AWS Infrastructure Setup (HIPAA Compliant)
# Run this once from a machine with AWS CLI configured.
#
# Prerequisites:
#   - AWS CLI v2 installed and configured (aws configure)
#   - Sufficient IAM permissions for S3, Secrets Manager, EC2, KMS, CloudWatch
#   - AWS BAA signed in AWS Artifact
#
# Usage:
#   chmod +x deploy/setup_aws.sh
#   ./deploy/setup_aws.sh

set -euo pipefail

# ----------------------------------------------------------------------
# Configuration — edit these before running
# ----------------------------------------------------------------------
REGION="us-east-1"
PROJECT="postiq"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# S3
S3_BUCKET="${PROJECT}-data-${ACCOUNT_ID}"
S3_LOG_BUCKET="${PROJECT}-access-logs-${ACCOUNT_ID}"

# Secrets Manager
SECRET_TA_NAME="${PROJECT}/credentials"

# EC2
KEY_PAIR_NAME="${PROJECT}-key"
INSTANCE_TYPE="t3.medium"

echo "============================================="
echo "  PostIQ AWS Setup (HIPAA Compliant)"
echo "  Region:  ${REGION}"
echo "  Account: ${ACCOUNT_ID}"
echo "  Bucket:  ${S3_BUCKET}"
echo "============================================="
echo ""

# ----------------------------------------------------------------------
# Step 1: KMS Key for encryption
# ----------------------------------------------------------------------
echo ">>> Step 1: Creating KMS key for PHI encryption..."
KMS_KEY_ID=$(aws kms list-aliases --region "${REGION}" \
    --query "Aliases[?AliasName=='alias/${PROJECT}-key'].TargetKeyId" --output text)

if [ -z "${KMS_KEY_ID}" ] || [ "${KMS_KEY_ID}" = "None" ]; then
    KMS_KEY_ID=$(aws kms create-key \
        --description "PostIQ PHI encryption key" \
        --region "${REGION}" \
        --query "KeyMetadata.KeyId" --output text)
    aws kms create-alias \
        --alias-name "alias/${PROJECT}-key" \
        --target-key-id "${KMS_KEY_ID}" \
        --region "${REGION}"
    # Enable automatic key rotation (HIPAA requirement)
    aws kms enable-key-rotation \
        --key-id "${KMS_KEY_ID}" \
        --region "${REGION}"
    echo "  Created KMS key: ${KMS_KEY_ID}"
    echo "  Automatic key rotation enabled"
else
    echo "  KMS key already exists: ${KMS_KEY_ID}"
fi
echo ""

# ----------------------------------------------------------------------
# Step 2: S3 access logging bucket
# ----------------------------------------------------------------------
echo ">>> Step 2: Creating S3 access logging bucket..."
if aws s3api head-bucket --bucket "${S3_LOG_BUCKET}" 2>/dev/null; then
    echo "  Log bucket already exists: ${S3_LOG_BUCKET}"
else
    aws s3api create-bucket \
        --bucket "${S3_LOG_BUCKET}" \
        --region "${REGION}" \
        --create-bucket-configuration LocationConstraint="${REGION}" 2>/dev/null || \
    aws s3api create-bucket \
        --bucket "${S3_LOG_BUCKET}" \
        --region "${REGION}"
    echo "  Created: ${S3_LOG_BUCKET}"
fi

# Block public access on log bucket
aws s3api put-public-access-block \
    --bucket "${S3_LOG_BUCKET}" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# Encrypt log bucket with KMS
aws s3api put-bucket-encryption \
    --bucket "${S3_LOG_BUCKET}" \
    --server-side-encryption-configuration \
    "{\"Rules\":[{\"ApplyServerSideEncryptionByDefault\":{\"SSEAlgorithm\":\"aws:kms\",\"KMSMasterKeyID\":\"${KMS_KEY_ID}\"},\"BucketKeyEnabled\":true}]}"
echo "  Log bucket encrypted with KMS"
echo ""

# ----------------------------------------------------------------------
# Step 3: S3 data bucket (PHI storage)
# ----------------------------------------------------------------------
echo ">>> Step 3: Creating S3 data bucket..."
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

# KMS encryption (auditable via CloudTrail, supports key rotation)
aws s3api put-bucket-encryption \
    --bucket "${S3_BUCKET}" \
    --server-side-encryption-configuration \
    "{\"Rules\":[{\"ApplyServerSideEncryptionByDefault\":{\"SSEAlgorithm\":\"aws:kms\",\"KMSMasterKeyID\":\"${KMS_KEY_ID}\"},\"BucketKeyEnabled\":true}]}"
echo "  Encryption: KMS (key rotation enabled)"

# Block public access
aws s3api put-public-access-block \
    --bucket "${S3_BUCKET}" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
echo "  Public access blocked"

# Enable S3 access logging (HIPAA audit trail)
aws s3api put-bucket-logging \
    --bucket "${S3_BUCKET}" \
    --bucket-logging-status \
    "{\"LoggingEnabled\":{\"TargetBucket\":\"${S3_LOG_BUCKET}\",\"TargetPrefix\":\"s3-access-logs/\"}}"
echo "  Access logging enabled -> ${S3_LOG_BUCKET}"

# Enforce SSL/TLS only (no unencrypted access)
cat > /tmp/s3-ssl-policy.json <<SSLPOLICY
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "DenyUnencryptedTransport",
    "Effect": "Deny",
    "Principal": "*",
    "Action": "s3:*",
    "Resource": [
      "arn:aws:s3:::${S3_BUCKET}",
      "arn:aws:s3:::${S3_BUCKET}/*"
    ],
    "Condition": {
      "Bool": {"aws:SecureTransport": "false"}
    }
  }]
}
SSLPOLICY
aws s3api put-bucket-policy \
    --bucket "${S3_BUCKET}" \
    --policy file:///tmp/s3-ssl-policy.json
echo "  SSL/TLS enforced (HTTP denied)"

# Lifecycle policy: delete PHI after 90 days
cat > /tmp/s3-lifecycle.json <<LIFECYCLE
{
  "Rules": [
    {
      "ID": "DeleteScreenshotsAfter90Days",
      "Filter": {"Prefix": "screenshots/"},
      "Status": "Enabled",
      "Expiration": {"Days": 90}
    },
    {
      "ID": "DeleteUploadsAfter90Days",
      "Filter": {"Prefix": "uploads/"},
      "Status": "Enabled",
      "Expiration": {"Days": 90}
    }
  ]
}
LIFECYCLE
aws s3api put-bucket-lifecycle-configuration \
    --bucket "${S3_BUCKET}" \
    --lifecycle-configuration file:///tmp/s3-lifecycle.json
echo "  Lifecycle: screenshots and uploads auto-delete after 90 days"

# Enable versioning (protect against accidental deletion)
aws s3api put-bucket-versioning \
    --bucket "${S3_BUCKET}" \
    --versioning-configuration Status=Enabled
echo "  Versioning enabled"

# Create folder structure
aws s3api put-object --bucket "${S3_BUCKET}" --key "uploads/" --content-length 0
aws s3api put-object --bucket "${S3_BUCKET}" --key "screenshots/" --content-length 0
aws s3api put-object --bucket "${S3_BUCKET}" --key "reports/" --content-length 0
echo "  Created folder structure: uploads/, screenshots/, reports/"
echo ""

# ----------------------------------------------------------------------
# Step 4: Secrets Manager
# ----------------------------------------------------------------------
echo ">>> Step 4: Creating secrets..."

read -rp "  Enter TA_USERNAME: " TA_USER
read -rsp "  Enter TA_PASSWORD: " TA_PASS
echo ""

aws secretsmanager create-secret \
    --name "${SECRET_TA_NAME}" \
    --description "TherapyAppointment login credentials" \
    --kms-key-id "${KMS_KEY_ID}" \
    --secret-string "{\"TA_USERNAME\":\"${TA_USER}\",\"TA_PASSWORD\":\"${TA_PASS}\"}" \
    --region "${REGION}" 2>/dev/null || \
aws secretsmanager update-secret \
    --secret-id "${SECRET_TA_NAME}" \
    --kms-key-id "${KMS_KEY_ID}" \
    --secret-string "{\"TA_USERNAME\":\"${TA_USER}\",\"TA_PASSWORD\":\"${TA_PASS}\"}" \
    --region "${REGION}"
echo "  Secret created: ${SECRET_TA_NAME} (KMS encrypted)"
echo ""

# ----------------------------------------------------------------------
# Step 5: VPC Flow Logs (network audit trail)
# ----------------------------------------------------------------------
echo ">>> Step 5: Enabling VPC Flow Logs..."
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
    --query "Vpcs[0].VpcId" --output text --region "${REGION}")

# Create CloudWatch log group for flow logs
aws logs create-log-group \
    --log-group-name "/postiq/vpc-flow-logs" \
    --region "${REGION}" 2>/dev/null || true
aws logs put-retention-policy \
    --log-group-name "/postiq/vpc-flow-logs" \
    --retention-in-days 90 \
    --region "${REGION}"

# Create IAM role for flow logs
cat > /tmp/flow-logs-trust.json <<FLTRUST
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "vpc-flow-logs.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
FLTRUST

FLOW_LOG_ROLE="${PROJECT}-flow-log-role"
aws iam create-role \
    --role-name "${FLOW_LOG_ROLE}" \
    --assume-role-policy-document file:///tmp/flow-logs-trust.json \
    2>/dev/null || true

cat > /tmp/flow-logs-policy.json <<FLPOLICY
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams"
    ],
    "Resource": "*"
  }]
}
FLPOLICY
aws iam put-role-policy \
    --role-name "${FLOW_LOG_ROLE}" \
    --policy-name "flow-logs-cloudwatch" \
    --policy-document file:///tmp/flow-logs-policy.json

FLOW_LOG_ROLE_ARN=$(aws iam get-role --role-name "${FLOW_LOG_ROLE}" \
    --query "Role.Arn" --output text)

# Check if flow logs already exist
EXISTING_FL=$(aws ec2 describe-flow-logs \
    --filter "Name=resource-id,Values=${VPC_ID}" \
    --query "FlowLogs[0].FlowLogId" --output text --region "${REGION}" 2>/dev/null) || true

if [ -z "${EXISTING_FL}" ] || [ "${EXISTING_FL}" = "None" ]; then
    aws ec2 create-flow-logs \
        --resource-type VPC \
        --resource-ids "${VPC_ID}" \
        --traffic-type ALL \
        --log-destination-type cloud-watch-logs \
        --log-group-name "/postiq/vpc-flow-logs" \
        --deliver-logs-permission-arn "${FLOW_LOG_ROLE_ARN}" \
        --region "${REGION}"
    echo "  VPC Flow Logs enabled -> CloudWatch /postiq/vpc-flow-logs"
else
    echo "  VPC Flow Logs already enabled: ${EXISTING_FL}"
fi
echo ""

# ----------------------------------------------------------------------
# Step 6: EC2 Key Pair
# ----------------------------------------------------------------------
echo ">>> Step 6: Creating EC2 key pair..."
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

# ----------------------------------------------------------------------
# Step 7: IAM Role for EC2
# ----------------------------------------------------------------------
echo ">>> Step 7: Creating IAM role for EC2..."
ROLE_NAME="${PROJECT}-ec2-role"
INSTANCE_PROFILE="${PROJECT}-ec2-profile"

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

# Least-privilege: S3, Secrets Manager, KMS, CloudWatch Logs
cat > /tmp/postiq-policy.json <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3Access",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::${S3_BUCKET}",
        "arn:aws:s3:::${S3_BUCKET}/*"
      ]
    },
    {
      "Sid": "SecretsAccess",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": [
        "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:${PROJECT}/*"
      ]
    },
    {
      "Sid": "KMSAccess",
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
      "Resource": [
        "arn:aws:kms:${REGION}:${ACCOUNT_ID}:key/${KMS_KEY_ID}"
      ]
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/postiq/*"
    }
  ]
}
POLICY

aws iam put-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-name "${PROJECT}-access" \
    --policy-document file:///tmp/postiq-policy.json

aws iam create-instance-profile \
    --instance-profile-name "${INSTANCE_PROFILE}" 2>/dev/null || true
aws iam add-role-to-instance-profile \
    --instance-profile-name "${INSTANCE_PROFILE}" \
    --role-name "${ROLE_NAME}" 2>/dev/null || true
echo "  IAM role ready (S3 + Secrets + KMS + CloudWatch)"
echo ""

# ----------------------------------------------------------------------
# Step 8: CloudWatch log group for bot
# ----------------------------------------------------------------------
echo ">>> Step 8: Creating CloudWatch log groups..."
aws logs create-log-group \
    --log-group-name "/postiq/bot" \
    --region "${REGION}" 2>/dev/null || true
aws logs put-retention-policy \
    --log-group-name "/postiq/bot" \
    --retention-in-days 90 \
    --region "${REGION}"

aws logs create-log-group \
    --log-group-name "/postiq/streamlit" \
    --region "${REGION}" 2>/dev/null || true
aws logs put-retention-policy \
    --log-group-name "/postiq/streamlit" \
    --retention-in-days 90 \
    --region "${REGION}"
echo "  Log groups created with 90-day retention"
echo ""

# ----------------------------------------------------------------------
# Cleanup temp files
# ----------------------------------------------------------------------
rm -f /tmp/trust-policy.json /tmp/postiq-policy.json /tmp/s3-ssl-policy.json \
      /tmp/s3-lifecycle.json /tmp/flow-logs-trust.json /tmp/flow-logs-policy.json

# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------
echo "============================================="
echo "  HIPAA-Compliant Setup Complete!"
echo "============================================="
echo ""
echo "  S3 Data Bucket:   ${S3_BUCKET}"
echo "  S3 Log Bucket:    ${S3_LOG_BUCKET}"
echo "  KMS Key:          ${KMS_KEY_ID}"
echo "  IAM Role:         ${ROLE_NAME}"
echo "  Key Pair:         ${KEY_PAIR_NAME}.pem"
echo "  VPC Flow Logs:    /postiq/vpc-flow-logs"
echo "  Bot Logs:         /postiq/bot"
echo "  Streamlit Logs:   /postiq/streamlit"
echo ""
echo "  HIPAA controls applied:"
echo "    - KMS encryption with automatic key rotation"
echo "    - S3 access logging"
echo "    - S3 SSL/TLS enforced (HTTP denied)"
echo "    - S3 lifecycle: PHI auto-deletes after 90 days"
echo "    - S3 versioning enabled"
echo "    - Secrets Manager encrypted with KMS"
echo "    - VPC Flow Logs to CloudWatch"
echo "    - CloudWatch log retention: 90 days"
echo ""
echo "  Next step:"
echo "    ./deploy/launch_ec2.sh"
