#!/usr/bin/env bash
# Launch an EC2 instance for PostIQ.
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

echo ">>> Looking up latest Ubuntu 22.04 AMI..."
AMI_ID=$(aws ec2 describe-images \
    --owners 099720109477 \
    --filters "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*" \
              "Name=state,Values=available" \
    --query "Images | sort_by(@, &CreationDate) | [-1].ImageId" \
    --output text --region "${REGION}")
echo "  AMI: ${AMI_ID}"

# Security group for EC2
EC2_SG_NAME="${PROJECT}-ec2-sg"
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
    --query "Vpcs[0].VpcId" --output text --region "${REGION}")

EC2_SG_ID=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=${EC2_SG_NAME}" \
    --query "SecurityGroups[0].GroupId" --output text --region "${REGION}" 2>/dev/null) || true

if [ "${EC2_SG_ID}" = "None" ] || [ -z "${EC2_SG_ID}" ]; then
    EC2_SG_ID=$(aws ec2 create-security-group \
        --group-name "${EC2_SG_NAME}" \
        --description "PostIQ EC2 access" \
        --vpc-id "${VPC_ID}" \
        --region "${REGION}" \
        --query "GroupId" --output text)

    # SSH
    aws ec2 authorize-security-group-ingress \
        --group-id "${EC2_SG_ID}" --protocol tcp --port 22 --cidr 0.0.0.0/0 \
        --region "${REGION}"
    # Streamlit
    aws ec2 authorize-security-group-ingress \
        --group-id "${EC2_SG_ID}" --protocol tcp --port 8501 --cidr 0.0.0.0/0 \
        --region "${REGION}"
    echo "  Created security group: ${EC2_SG_ID}"
else
    echo "  Security group exists: ${EC2_SG_ID}"
fi

echo ">>> Launching EC2 instance..."
INSTANCE_ID=$(aws ec2 run-instances \
    --image-id "${AMI_ID}" \
    --instance-type "${INSTANCE_TYPE}" \
    --key-name "${KEY_PAIR_NAME}" \
    --security-group-ids "${EC2_SG_ID}" \
    --iam-instance-profile Name="${INSTANCE_PROFILE}" \
    --user-data file://deploy/user_data.sh \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${PROJECT}}]" \
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
echo "  EC2 Instance Launched!"
echo "============================================="
echo ""
echo "  Instance ID:  ${INSTANCE_ID}"
echo "  Public IP:    ${PUBLIC_IP}"
echo "  Streamlit UI: http://${PUBLIC_IP}:8501"
echo ""
echo "  SSH access:"
echo "    ssh -i ${KEY_PAIR_NAME}.pem ubuntu@${PUBLIC_IP}"
echo ""
echo "  The instance is bootstrapping (Docker build, etc.)."
echo "  Give it ~3-5 minutes, then visit the Streamlit URL."
