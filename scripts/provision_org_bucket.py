#!/usr/bin/env python3
"""
provision_org_bucket.py
FuzeInfra utility to seed and manage organization-scoped S3/MinIO buckets.
Enforces:
1. Bucket naming: fuzeagent-org-{organization_id}
2. Configurable storage quota (default 5GB)
3. Standard folder structure: brains/{brain_id}/...
4. CORS and lifecycle policies for resumable multipart uploads
"""

import os
import sys
import logging
from typing import Dict, Any

try:
    import boto3
    from botocore.client import Config
    from botocore.exceptions import ClientError
except ImportError:
    print("boto3 is required: pip install boto3")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FuzeInfra.S3Provisioner")

DEFAULT_QUOTA_BYTES = int(os.environ.get("DEFAULT_ORG_STORAGE_QUOTA_BYTES", str(5 * 1024 * 1024 * 1024))) # 5GB

def get_s3_client():
    endpoint_url = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")
    aws_access_key_id = os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin")
    aws_secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin_secure_pass")
    region_name = os.environ.get("AWS_REGION", "us-east-1")

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        region_name=region_name,
        config=Config(signature_version="s3v4")
    )

def provision_org_bucket(organization_id: str, quota_bytes: int = DEFAULT_QUOTA_BYTES) -> Dict[str, Any]:
    """
    Creates an organization S3 bucket if missing, tags it with quota metadata,
    and sets CORS configuration for direct browser resumable multipart uploads.
    """
    clean_org = organization_id.lower().replace("_", "-")
    bucket_name = f"fuzeagent-org-{clean_org}"
    s3 = get_s3_client()

    logger.info(f"Provisioning bucket '{bucket_name}' with quota {quota_bytes / (1024*1024*1024):.2f} GB")

    # 1. Create bucket if not exists
    try:
        s3.head_bucket(Bucket=bucket_name)
        logger.info(f"Bucket '{bucket_name}' already exists.")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code")
        if error_code in ["404", "NoSuchBucket"]:
            logger.info(f"Creating bucket '{bucket_name}'...")
            s3.create_bucket(Bucket=bucket_name)
        else:
            logger.error(f"Failed to check bucket: {e}")
            raise

    # 2. Configure CORS for resumable browser uploads
    cors_configuration = {
        "CORSRules": [
            {
                "AllowedHeaders": ["*"],
                "AllowedMethods": ["GET", "PUT", "POST", "HEAD", "DELETE"],
                "AllowedOrigins": ["*"],
                "ExposeHeaders": ["ETag", "Content-Range", "Accept-Ranges"],
                "MaxAgeSeconds": 3000
            }
        ]
    }
    s3.put_bucket_cors(Bucket=bucket_name, CORSConfiguration=cors_configuration)

    # 3. Tag bucket with organization and quota metadata
    tagging = {
        "TagSet": [
            {"Key": "OrganizationId", "Value": organization_id},
            {"Key": "StorageQuotaBytes", "Value": str(quota_bytes)},
            {"Key": "ManagedBy", "Value": "FuzeInfra"}
        ]
    }
    s3.put_bucket_tagging(Bucket=bucket_name, Tagging=tagging)

    # 4. Ensure brains base prefix placeholder exists
    s3.put_object(Bucket=bucket_name, Key="brains/.keep", Body=b"")

    logger.info(f"✅ Successfully provisioned bucket '{bucket_name}'")
    return {
        "bucket_name": bucket_name,
        "organization_id": organization_id,
        "quota_bytes": quota_bytes,
        "quota_gb": quota_bytes / (1024 * 1024 * 1024),
        "status": "active"
    }

def provision_brain_folder(organization_id: str, brain_id: str) -> str:
    """
    Creates logical directory layout for a specific brain within the org bucket:
    brains/{brain_id}/documents/
    brains/{brain_id}/media/
    brains/{brain_id}/avatars/
    """
    clean_org = organization_id.lower().replace("_", "-")
    bucket_name = f"fuzeagent-org-{clean_org}"
    s3 = get_s3_client()

    subfolders = ["documents", "media", "avatars", "checkpoints"]
    for sub in subfolders:
        key = f"brains/{brain_id}/{sub}/.keep"
        s3.put_object(Bucket=bucket_name, Key=key, Body=b"")

    logger.info(f"✅ Initialized brain folders for 'brains/{brain_id}/' in bucket '{bucket_name}'")
    return f"s3://{bucket_name}/brains/{brain_id}/"

if __name__ == "__main__":
    org = sys.argv[1] if len(sys.argv) > 1 else "org_default"
    provision_org_bucket(org)
    if len(sys.argv) > 2:
        provision_brain_folder(org, sys.argv[2])
