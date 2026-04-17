output "rds_endpoint" {
  description = "Hostname:port pair for the provisioned Postgres instance. Combine with the DSN stored in Secrets Manager."
  value       = aws_db_instance.emonk.endpoint
}

output "rds_database_name" {
  description = "Default database name inside the RDS instance."
  value       = aws_db_instance.emonk.db_name
}

output "s3_bucket_name" {
  description = "Bucket name used for memory/, identity/, and runpackages/ prefixes."
  value       = aws_s3_bucket.emonk.id
}

output "kms_key_id" {
  description = "ID of the CMK used for SSE-KMS. Plug into HarnessConfig.memory_store.kms_key_id."
  value       = aws_kms_key.emonk.key_id
}

output "kms_key_arn" {
  description = "ARN of the CMK used for SSE-KMS."
  value       = aws_kms_key.emonk.arn
}

output "iam_role_arn" {
  description = "IAM role ARN the AgentCore runtime assumes to reach every AWS surface."
  value       = aws_iam_role.emonk_runtime.arn
}

output "ckpt_dsn_secret_arn" {
  description = "ARN of the Secrets Manager entry holding the Postgres DSN."
  value       = aws_secretsmanager_secret.ckpt_dsn.arn
}
