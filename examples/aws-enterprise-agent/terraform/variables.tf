variable "project_name" {
  description = "Short, lowercase project identifier used to prefix every provisioned resource."
  type        = string
  default     = "emonk-enterprise"
}

variable "aws_region" {
  description = "AWS region where every resource is provisioned (Bedrock + RDS + S3 + KMS)."
  type        = string
  default     = "us-east-1"
}

variable "vpc_id" {
  description = "Existing VPC id the RDS instance + VPC endpoints attach to. Must contain at least two private subnets."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet ids used by the DB subnet group and interface VPC endpoints."
  type        = list(string)
  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "Provide at least two private subnets across distinct AZs."
  }
}

variable "route_table_ids" {
  description = "Route table ids the S3 gateway VPC endpoint associates with."
  type        = list(string)
  default     = []
}

variable "db_username" {
  description = "Master username for the emonk_ckpt Postgres database."
  type        = string
  default     = "emonk_admin"
}

variable "db_password" {
  description = "Master password for the Postgres DB. Store in Secrets Manager; pass in via TF_VAR_db_password."
  type        = string
  sensitive   = true
}

variable "db_instance_class" {
  description = "RDS instance class. Prod-S default per 1C §3.1 RDS sizing table."
  type        = string
  default     = "db.t4g.small"
}

variable "bucket_name" {
  description = "S3 bucket holding memory/, identity/, and runpackages/ prefixes. Must be globally unique."
  type        = string
}

variable "kms_key_alias" {
  description = "KMS alias (without the 'alias/' prefix) applied to the CMK used for SSE-KMS."
  type        = string
  default     = "emonk-enterprise-cmk"
}

variable "tags" {
  description = "Tags applied to every resource. Merge with provider default_tags for consistency."
  type        = map(string)
  default = {
    Project   = "emonk-enterprise"
    ManagedBy = "terraform"
  }
}
