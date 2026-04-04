#!/usr/bin/env bash
# Launch an EC2 instance for PostIQ (HIPAA Compliant).
# Run after setup_aws.sh has completed.
#
# Usage:
#   chmod +x deploy/launch_ec2.sh
#   ./deploy/launch_ec2.sh

set -euo pipefail

REGION="us-east-1"
PROJECT="postiq"
KEY_PAIR_NAME="${PROJECT}-key"
INSTANCE_PROFILE="${PROJECT}-ec2-profile"
INSTANCE_TYPE="t3.medium"

# ----------------------------------------------------------------------
# IMPORTANT: Replace with your office IP before running.
# Find it by visiting https://checkip.amazonaws.com from your office.
# Format: x.x.x.x/32  (the /32 means "this one IP only")
# ----------------------------------------------------------------------
OFFICE_IP="REPLACE_WITH_YOUR_OFFICE_IP/32"

if [ "${OFFICE_IP}" = "REPLACE_WITH_YOUR_OFFICE_IP/32" ]; then
    echo "ERROR: Edit deploy/launch_ec2.sh and set OFFICE_IP to your office IP."
    echo "       Find it at: https://checkip.amazonaws.com"
    exit 1
fi

echo ">>> Looking up latest Ubuntu 22.04 AMI..."
AMI_ID=$(aws ec2 describe-images \
    --owners 099720109477 \
    --filters "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*" \
              "Name=state,Values=available" \
    --query "Images | sort_by(@, &CreationDate) | [-1].ImageId" \
    --output text --region "${REGION}")
echo "  AMI: ${AMI_ID}"

VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
    --query "Vpcs[0].VpcId" --output text --region "${REGION}")

# ----------------------------------------------------------------------
# Security group (HIPAA: restricted access)
# ----------------------------------------------------------------------
EC2_SG_NAME="${PROJECT}-ec2-sg"

EC2_SG_ID=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=${EC2_SG_NAME}" \
    --query "SecurityGroups[0].GroupId" --output text --region "${REGION}" 2>/dev/null) || true

if [ "${EC2_SG_ID}" = "None" ] || [ -z "${EC2_SG_ID}" ]; then
    EC2_SG_ID=$(aws ec2 create-security-group \
        --group-name "${EC2_SG_NAME}" \
        --description "PostIQ EC2 - HIPAA restricted" \
        --vpc-id "${VPC_ID}" \
        --region "${REGION}" \
        --query "GroupId" --output text)

    # SSH: office IP only
    aws ec2 authorize-security-group-ingress \
        --group-id "${EC2_SG_ID}" --protocol tcp --port 22 \
        --cidr "${OFFICE_IP}" \
        --region "${REGION}"
    echo "  SSH restricted to: ${OFFICE_IP}"

    # HTTPS (443): office IP only
    aws ec2 authorize-security-group-ingress \
        --group-id "${EC2_SG_ID}" --protocol tcp --port 443 \
        --cidr "${OFFICE_IP}" \
        --region "${REGION}"
    echo "  HTTPS restricted to: ${OFFICE_IP}"

    echo "  Created security group: ${EC2_SG_ID}"
else
    echo "  Security group exists: ${EC2_SG_ID}"
fi

# ----------------------------------------------------------------------
# Launch EC2 with encrypted EBS volume
# ----------------------------------------------------------------------
echo ">>> Launching EC2 instance (encrypted EBS)..."

KMS_KEY_ID=$(aws kms list-aliases --region "${REGION}" \
    --query "Aliases[?AliasName=='alias/${PROJECT}-key'].TargetKeyId" --output text)

INSTANCE_ID=$(aws ec2 run-instances \
    --image-id "${AMI_ID}" \
    --instance-type "${INSTANCE_TYPE}" \
    --key-name "${KEY_PAIR_NAME}" \
    --security-group-ids "${EC2_SG_ID}" \
    --iam-instance-profile Name="${INSTANCE_PROFILE}" \
    --user-data file://deploy/user_data.sh \
    --block-device-mappings "[{\"DeviceName\":\"/dev/sda1\",\"Ebs\":{\"VolumeSize\":30,\"VolumeType\":\"gp3\",\"Encrypted\":true,\"KmsKeyId\":\"${KMS_KEY_ID}\"}}]" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${PROJECT}},{Key=HIPAA,Value=true}]" \
    --region "${REGION}" \
    --query "Instances[0].InstanceId" --output text)
echo "  Instance ID: ${INSTANCE_ID}"

echo ">>> Waiting for instance to enter running state..."
aws ec2 wait instance-running --instance-ids "${INSTANCE_ID}" --region "${REGION}"

PUBLIC_IP=$(aws ec2 describe-instances \
    --instance-ids "${INSTANCE_ID}" \
    --query "Reservations[0].Instances[0].PublicIpAddress" --output text \
    --region "${REGION}")

echo ""
echo "============================================="
echo "  EC2 Instance Launched (HIPAA Compliant)"
echo "============================================="
echo ""
echo "  Instance ID:   ${INSTANCE_ID}"
echo "  Public IP:     ${PUBLIC_IP}"
echo "  EBS:           Encrypted (KMS)"
echo ""
echo "  Streamlit UI:  https://${PUBLIC_IP}"
echo "  SSH access:    ssh -i ${KEY_PAIR_NAME}.pem ubuntu@${PUBLIC_IP}"
echo ""
echo "  Access restricted to: ${OFFICE_IP}"
echo ""
echo "  The instance is bootstrapping (Docker build, etc.)."
echo "  Give it ~5 minutes, then visit the Streamlit URL."
echo ""
echo "  NOTE: Your browser will show a certificate warning"
echo "  because we're using a self-signed cert. This is expected."
echo "  For a trusted cert, set up a domain name and use ACM."
