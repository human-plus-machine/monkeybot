terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = var.tags
  }
}

data "aws_caller_identity" "current" {}

locals {
  name_prefix = var.project_name
}

# ---------------------------------------------------------------------------
# KMS — customer-managed CMK with automatic rotation for SSE-KMS.
# ---------------------------------------------------------------------------
resource "aws_kms_key" "emonk" {
  description             = "${local.name_prefix} CMK for S3 SSE-KMS and RDS storage encryption."
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_kms_alias" "emonk" {
  name          = "alias/${var.kms_key_alias}"
  target_key_id = aws_kms_key.emonk.key_id
}

# ---------------------------------------------------------------------------
# S3 — memory/, identity/, runpackages/ live under one bucket with versioning +
# SSE-KMS + a conservative lifecycle policy.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "emonk" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_versioning" "emonk" {
  bucket = aws_s3_bucket.emonk.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "emonk" {
  bucket = aws_s3_bucket.emonk.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.emonk.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "emonk" {
  bucket                  = aws_s3_bucket.emonk.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "emonk" {
  bucket = aws_s3_bucket.emonk.id

  rule {
    id     = "expire-old-runpackages"
    status = "Enabled"

    filter {
      prefix = "runpackages/"
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    expiration {
      days = 365
    }
  }
}

# ---------------------------------------------------------------------------
# RDS Postgres — db.t4g.small Prod-S default from 1C §3.1 sizing table.
# ---------------------------------------------------------------------------
resource "aws_db_subnet_group" "emonk" {
  name       = "${local.name_prefix}-db-subnets"
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "emonk_db" {
  name        = "${local.name_prefix}-db-sg"
  description = "Restrict Postgres ingress to resources within the VPC."
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_instance" "emonk" {
  identifier                          = "${local.name_prefix}-ckpt"
  engine                              = "postgres"
  engine_version                      = "15"
  instance_class                      = var.db_instance_class
  allocated_storage                   = 20
  max_allocated_storage               = 100
  storage_type                        = "gp3"
  storage_encrypted                   = true
  kms_key_id                          = aws_kms_key.emonk.arn
  db_name                             = "emonk_ckpt"
  username                            = var.db_username
  password                            = var.db_password
  db_subnet_group_name                = aws_db_subnet_group.emonk.name
  vpc_security_group_ids              = [aws_security_group.emonk_db.id]
  publicly_accessible                 = false
  backup_retention_period             = 7
  deletion_protection                 = true
  iam_database_authentication_enabled = true
  skip_final_snapshot                 = false
  final_snapshot_identifier           = "${local.name_prefix}-final-snapshot"
}

# ---------------------------------------------------------------------------
# Secrets Manager — placeholder secrets the consumer populates out-of-band.
# ---------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "ckpt_dsn" {
  name        = "${local.name_prefix}/ckpt-dsn"
  description = "Postgres DSN for the emonk checkpointer/job-storage pool."
  kms_key_id  = aws_kms_key.emonk.arn
}

resource "aws_secretsmanager_secret" "app_overrides" {
  name        = "${local.name_prefix}/app-overrides"
  description = "Free-form JSON blob of app-level secret overrides (LLM keys, etc.)."
  kms_key_id  = aws_kms_key.emonk.arn
}

# ---------------------------------------------------------------------------
# IAM — role + scoped policy the AgentCore runtime assumes at invocation time.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "emonk_runtime" {
  name               = "${local.name_prefix}-runtime"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
}

data "aws_iam_policy_document" "emonk_runtime" {
  statement {
    sid    = "BedrockInvokeClaude"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    resources = [
      "arn:aws:bedrock:${var.aws_region}::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0",
      "arn:aws:bedrock:${var.aws_region}::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0",
    ]
  }

  statement {
    sid       = "BedrockListFoundationModels"
    effect    = "Allow"
    actions   = ["bedrock:ListFoundationModels"]
    resources = ["*"]
  }

  statement {
    sid = "S3BucketRead"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [aws_s3_bucket.emonk.arn]
  }

  statement {
    sid    = "S3ObjectReadWrite"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = [
      "${aws_s3_bucket.emonk.arn}/memory/*",
      "${aws_s3_bucket.emonk.arn}/identity/*",
      "${aws_s3_bucket.emonk.arn}/runpackages/*",
    ]
  }

  statement {
    sid    = "SecretsManagerReadScoped"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = [
      aws_secretsmanager_secret.ckpt_dsn.arn,
      aws_secretsmanager_secret.app_overrides.arn,
    ]
  }

  statement {
    sid    = "KmsCmkOperations"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]
    resources = [aws_kms_key.emonk.arn]
  }

  statement {
    sid       = "StsWhoAmI"
    effect    = "Allow"
    actions   = ["sts:GetCallerIdentity"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "emonk_runtime" {
  name   = "${local.name_prefix}-runtime-policy"
  role   = aws_iam_role.emonk_runtime.id
  policy = data.aws_iam_policy_document.emonk_runtime.json
}

# ---------------------------------------------------------------------------
# VPC endpoints — keep Bedrock, S3, and Secrets Manager traffic on private net.
# ---------------------------------------------------------------------------
resource "aws_security_group" "emonk_vpce" {
  name        = "${local.name_prefix}-vpce-sg"
  description = "Allow HTTPS from the VPC to the interface endpoints."
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTPS from the VPC."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    self        = true
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.route_table_ids
}

resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.emonk_vpce.id]
  private_dns_enabled = true
}

resource "aws_vpc_endpoint" "bedrock_runtime" {
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.bedrock-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.emonk_vpce.id]
  private_dns_enabled = true
}
