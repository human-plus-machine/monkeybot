# Terraform Coding Guidelines - Enterprise Grade

**Version:** 1.0  
**Last Updated:** 2026  
**Target:** Production Terraform infrastructure requiring high quality, security, and maintainability

---

## Table of Contents

1. [Philosophy](#philosophy)
2. [Code Style](#code-style)
3. [Type Safety](#type-safety)
4. [Documentation](#documentation)
5. [Architecture](#architecture)
6. [Error Handling](#error-handling)
7. [Testing](#testing)
8. [Security](#security)
9. [Performance](#performance)
10. [Dependencies](#dependencies)
11. [Logging & Observability](#logging--observability)
12. [Code Review](#code-review)
13. [Tooling](#tooling)

---

## Philosophy

Follow **Infrastructure as Code (IaC)** principles:
- Declarative over imperative: describe desired state, not steps
- Idempotency: running the same code twice produces the same result
- Immutable infrastructure: replace, don't patch
- Plan before apply: always review the execution plan
- Version everything: infrastructure code is treated like application code

**Core Principles:**
- Write code for humans first, machines second
- Optimize for readability and maintainability
- Modules should be opinionated and do one thing well
- Security is not optional: least privilege everywhere
- State is sacred: protect, encrypt, and back up state files
- Test infrastructure changes before applying to production
- Separate environments with clear boundaries

---

## Code Style

### Base Standard

Follow the **HashiCorp Terraform Style Guide** with `terraform fmt` as the baseline:

**Formatting:**
- Indentation: 2 spaces (never tabs)
- Encoding: UTF-8
- Line endings: LF (Unix style)
- Run `terraform fmt` before every commit
- Align equals signs for consecutive single-line arguments

**Naming Conventions:**
```hcl
# Resources - lowercase with underscores, singular nouns
# Do NOT repeat the resource type in the name
resource "aws_instance" "web_server" {     # [GOOD]
  # ...
}
resource "aws_instance" "web_server_instance" {  # [BAD] repeats "instance"
  # ...
}

# Single-instance resources - use "main" or "this"
resource "aws_vpc" "main" {
  # ...
}

# Multiple similar resources - use meaningful names
resource "aws_subnet" "public" { }
resource "aws_subnet" "private" { }
resource "aws_route_table" "public" { }
resource "aws_route_table" "private" { }

# Variables - lowercase with underscores
variable "instance_type" { }
variable "enable_monitoring" { }   # Boolean: positive name with enable/disable
variable "ram_size_gb" { }         # Numeric: include unit in name

# Outputs - lowercase with underscores
output "instance_id" { }
output "load_balancer_dns_name" { }

# Locals - lowercase with underscores
locals {
  common_tags = {
    Environment = var.environment
    ManagedBy   = "terraform"
    Project     = var.project_name
  }
}

# Data sources - lowercase with underscores
data "aws_ami" "ubuntu" { }
data "aws_caller_identity" "current" { }

# Modules - lowercase with underscores
module "vpc" {
  source = "./modules/vpc"
}
```

**Argument Ordering within Resource Blocks:**
```hcl
resource "aws_instance" "web_server" {
  # 1. Meta-arguments first
  count = var.instance_count

  # 2. Required arguments
  ami           = data.aws_ami.ubuntu.id
  instance_type = var.instance_type
  subnet_id     = aws_subnet.public.id

  # 3. Optional arguments
  associate_public_ip_address = true
  monitoring                  = true

  # 4. Tags (always last argument before blocks)
  tags = merge(local.common_tags, {
    Name = "web-server-${count.index}"
    Role = "web"
  })

  # 5. Nested blocks (separated by blank line)
  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  # 6. Meta-argument blocks last
  lifecycle {
    create_before_destroy = true
  }
}
```

**Comments:**
```hcl
# Use hash for single-line comments
# Explain the WHY, not the WHAT

# This security group allows internal traffic only because
# the ALB handles all external connections
resource "aws_security_group" "internal" {
  # ...
}

# TODO: Remove this after the 2026-Q3 migration is complete
```

**Avoid:**
- Dashes in resource/variable/output names (use underscores)
- Overly generic resource names: `resource`, `main1`, `temp`
- Deep nesting of ternary operators
- Hardcoded values that should be variables
- Repeated literal values (use locals)

---

## Type Safety

### Variable Types

**Always declare types and add validation:**
```hcl
# String with validation
variable "environment" {
  description = "Deployment environment (dev, staging, prod)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be one of: dev, staging, prod."
  }
}

# Number with validation
variable "instance_count" {
  description = "Number of instances to create"
  type        = number
  default     = 1

  validation {
    condition     = var.instance_count >= 1 && var.instance_count <= 10
    error_message = "Instance count must be between 1 and 10."
  }
}

# Boolean with positive naming
variable "enable_monitoring" {
  description = "Whether to enable detailed monitoring"
  type        = bool
  default     = true
}

# List of strings
variable "availability_zones" {
  description = "List of availability zones for deployment"
  type        = list(string)

  validation {
    condition     = length(var.availability_zones) >= 2
    error_message = "At least 2 availability zones are required for high availability."
  }
}

# Map of strings
variable "additional_tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}

# Complex object type
variable "database_config" {
  description = "Database instance configuration"
  type = object({
    engine         = string
    engine_version = string
    instance_class = string
    storage_gb     = number
    multi_az       = bool
    backup_retention_days = optional(number, 7)
  })

  validation {
    condition     = contains(["postgres", "mysql"], var.database_config.engine)
    error_message = "Database engine must be postgres or mysql."
  }

  validation {
    condition     = var.database_config.storage_gb >= 20
    error_message = "Minimum storage is 20 GB."
  }
}
```

**Sensitive variables:**
```hcl
variable "database_password" {
  description = "Master password for the database"
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.database_password) >= 16
    error_message = "Database password must be at least 16 characters."
  }
}
```

**Nullable variables:**
```hcl
variable "custom_domain" {
  description = "Optional custom domain for the application"
  type        = string
  default     = null
  nullable    = true
}

# Usage with conditional
resource "aws_route53_record" "custom" {
  count = var.custom_domain != null ? 1 : 0

  zone_id = data.aws_route53_zone.main.zone_id
  name    = var.custom_domain
  type    = "A"

  alias {
    name    = aws_lb.main.dns_name
    zone_id = aws_lb.main.zone_id
  }
}
```

---

## Documentation

### Variable and Output Descriptions

**Required for all variables and outputs (1-2 sentences):**
```hcl
variable "vpc_cidr_block" {
  description = "CIDR block for the VPC. Must be a /16 or larger network."
  type        = string
  default     = "10.0.0.0/16"
}

output "vpc_id" {
  description = "The ID of the VPC created by this module."
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "List of public subnet IDs for load balancer placement."
  value       = aws_subnet.public[*].id
}
```

### Module README

**Auto-generate with terraform-docs. Every module must include:**

```markdown
# VPC Module

Creates a VPC with public and private subnets, NAT gateways, and route tables.

## Usage

```hcl
module "vpc" {
  source = "./modules/vpc"

  environment        = "prod"
  vpc_cidr_block     = "10.0.0.0/16"
  availability_zones = ["us-east-1a", "us-east-1b", "us-east-1c"]
  enable_nat_gateway = true
}
```

## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.8.0 |
| aws | ~> 5.0 |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|----------|
| environment | Deployment environment | string | - | yes |
| vpc_cidr_block | CIDR block for the VPC | string | 10.0.0.0/16 | no |

## Outputs

| Name | Description |
|------|-------------|
| vpc_id | The ID of the VPC |
| public_subnet_ids | List of public subnet IDs |
```

### Inline Comments

```hcl
# [GOOD] Explain WHY, not WHAT
# Allow traffic from the ALB only, not directly from the internet.
# This ensures all traffic passes through WAF rules.
resource "aws_security_group_rule" "allow_alb" {
  type                     = "ingress"
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.alb.id
  security_group_id        = aws_security_group.app.id
}

# [BAD] Restating the obvious
# Allow ingress on port 80 from ALB security group
resource "aws_security_group_rule" "allow_alb" {
  # ...
}
```

### CHANGELOG for Modules

```markdown
# Changelog

## [1.2.0] - 2026-02-10
### Added
- Support for custom domain with Route53

### Changed
- Default instance type updated to t3.medium

## [1.1.0] - 2026-01-15
### Added
- NAT gateway high availability across AZs

### Fixed
- Security group rule ordering issue
```

---

## Architecture

### Standard Module Structure

```
modules/
├── vpc/
│   ├── main.tf           # Primary resources
│   ├── variables.tf      # Input variables
│   ├── outputs.tf        # Output values
│   ├── versions.tf       # Required providers and Terraform version
│   ├── locals.tf         # Local values (optional, if many)
│   ├── data.tf           # Data sources (optional, if many)
│   └── README.md         # Module documentation
├── ecs/
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── versions.tf
│   └── README.md
└── rds/
    ├── main.tf
    ├── variables.tf
    ├── outputs.tf
    ├── versions.tf
    └── README.md
```

### Root Module Structure

```
environments/
├── dev/
│   ├── main.tf           # Module calls
│   ├── variables.tf
│   ├── outputs.tf
│   ├── versions.tf
│   ├── backend.tf        # Remote state configuration
│   ├── terraform.tfvars  # Environment-specific values
│   └── providers.tf      # Provider configuration
├── staging/
│   ├── main.tf
│   ├── ...
└── prod/
    ├── main.tf
    ├── ...
```

### Feature-Based File Grouping

```hcl
# [GOOD] Group related resources in logically named files
# network.tf
resource "aws_vpc" "main" { }
resource "aws_subnet" "public" { }
resource "aws_subnet" "private" { }
resource "aws_internet_gateway" "main" { }
resource "aws_nat_gateway" "main" { }
resource "aws_route_table" "public" { }
resource "aws_route_table" "private" { }

# compute.tf
resource "aws_ecs_cluster" "main" { }
resource "aws_ecs_service" "app" { }
resource "aws_ecs_task_definition" "app" { }

# database.tf
resource "aws_db_instance" "main" { }
resource "aws_db_subnet_group" "main" { }

# security.tf
resource "aws_security_group" "alb" { }
resource "aws_security_group" "app" { }
resource "aws_security_group" "db" { }
```

### Module Composition

```hcl
# environments/prod/main.tf
module "vpc" {
  source = "../../modules/vpc"

  environment        = var.environment
  vpc_cidr_block     = "10.0.0.0/16"
  availability_zones = var.availability_zones
  enable_nat_gateway = true
}

module "ecs" {
  source = "../../modules/ecs"

  environment    = var.environment
  vpc_id         = module.vpc.vpc_id
  subnet_ids     = module.vpc.private_subnet_ids
  desired_count  = 3
  container_port = 8080
}

module "rds" {
  source = "../../modules/rds"

  environment       = var.environment
  vpc_id            = module.vpc.vpc_id
  subnet_ids        = module.vpc.private_subnet_ids
  database_config   = var.database_config
  database_password = var.database_password
}
```

### Remote State

```hcl
# backend.tf
terraform {
  backend "s3" {
    bucket         = "mycompany-terraform-state"
    key            = "prod/infrastructure.tfstate"
    region         = "us-east-1"
    dynamodb_table = "terraform-locks"
    encrypt        = true
  }
}

# Referencing remote state from another configuration
data "terraform_remote_state" "vpc" {
  backend = "s3"
  config = {
    bucket = "mycompany-terraform-state"
    key    = "prod/vpc.tfstate"
    region = "us-east-1"
  }
}

# Use outputs from remote state
resource "aws_ecs_service" "app" {
  network_configuration {
    subnets = data.terraform_remote_state.vpc.outputs.private_subnet_ids
  }
}
```

---

## Error Handling

### Preconditions and Postconditions

```hcl
# Precondition: validate assumptions before resource creation
resource "aws_instance" "web_server" {
  ami           = data.aws_ami.ubuntu.id
  instance_type = var.instance_type

  lifecycle {
    precondition {
      condition     = data.aws_ami.ubuntu.architecture == "x86_64"
      error_message = "The selected AMI must be x86_64 architecture."
    }

    postcondition {
      condition     = self.public_ip != ""
      error_message = "The instance must have a public IP assigned."
    }
  }
}
```

### Check Blocks (continuous validation)

```hcl
# check blocks validate assertions about infrastructure state
check "health_check" {
  data "http" "app_health" {
    url = "https://${aws_lb.main.dns_name}/health"
  }

  assert {
    condition     = data.http.app_health.status_code == 200
    error_message = "Application health check failed after deployment."
  }
}
```

### Variable Validation

```hcl
variable "cidr_block" {
  description = "CIDR block for the VPC"
  type        = string

  validation {
    condition     = can(cidrhost(var.cidr_block, 0))
    error_message = "Must be a valid CIDR block (e.g., 10.0.0.0/16)."
  }

  validation {
    condition     = tonumber(split("/", var.cidr_block)[1]) <= 24
    error_message = "CIDR block must be /24 or larger."
  }
}

variable "domain_name" {
  description = "Domain name for the application"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]+\\.[a-z]{2,}$", var.domain_name))
    error_message = "Must be a valid domain name."
  }
}
```

### Lifecycle Rules for Safety

```hcl
# Protect stateful resources from accidental destruction
resource "aws_db_instance" "main" {
  # ...

  lifecycle {
    prevent_destroy = true
  }
}

# Ignore externally managed changes
resource "aws_autoscaling_group" "app" {
  # ...

  lifecycle {
    ignore_changes = [desired_capacity]  # Managed by auto-scaling policies
  }
}

# Create replacement before destroying old resource
resource "aws_instance" "web_server" {
  # ...

  lifecycle {
    create_before_destroy = true
  }
}
```

### Moved Blocks for Refactoring

```hcl
# Safely rename resources without destroy/recreate
moved {
  from = aws_instance.web
  to   = aws_instance.web_server
}

# Move into a module
moved {
  from = aws_vpc.main
  to   = module.vpc.aws_vpc.main
}
```

---

## Testing

### Validation and Planning

```bash
# Always validate syntax first
terraform validate

# Always review the plan before applying
terraform plan -out=tfplan

# Apply only from a saved plan
terraform apply tfplan
```

### Native Terraform Tests

```hcl
# tests/vpc_test.tftest.hcl
run "creates_vpc_with_correct_cidr" {
  command = plan

  variables {
    environment    = "test"
    vpc_cidr_block = "10.0.0.0/16"
  }

  assert {
    condition     = aws_vpc.main.cidr_block == "10.0.0.0/16"
    error_message = "VPC CIDR block does not match expected value."
  }

  assert {
    condition     = aws_vpc.main.enable_dns_hostnames == true
    error_message = "DNS hostnames should be enabled."
  }
}

run "creates_correct_number_of_subnets" {
  command = plan

  variables {
    environment        = "test"
    availability_zones = ["us-east-1a", "us-east-1b"]
  }

  assert {
    condition     = length(aws_subnet.public) == 2
    error_message = "Should create one public subnet per AZ."
  }

  assert {
    condition     = length(aws_subnet.private) == 2
    error_message = "Should create one private subnet per AZ."
  }
}

run "rejects_invalid_environment" {
  command = plan

  variables {
    environment = "invalid"
  }

  expect_failures = [
    var.environment,
  ]
}
```

### Integration Tests (Terratest)

```go
// test/vpc_test.go
package test

import (
    "testing"

    "github.com/gruntwork-io/terratest/modules/terraform"
    "github.com/stretchr/testify/assert"
)

func TestVpcModule(t *testing.T) {
    t.Parallel()

    terraformOptions := terraform.WithDefaultRetryableErrors(t, &terraform.Options{
        TerraformDir: "../modules/vpc",
        Vars: map[string]interface{}{
            "environment":        "test",
            "vpc_cidr_block":     "10.99.0.0/16",
            "availability_zones": []string{"us-east-1a", "us-east-1b"},
        },
    })

    defer terraform.Destroy(t, terraformOptions)
    terraform.InitAndApply(t, terraformOptions)

    vpcId := terraform.Output(t, terraformOptions, "vpc_id")
    assert.NotEmpty(t, vpcId)

    publicSubnetIds := terraform.OutputList(t, terraformOptions, "public_subnet_ids")
    assert.Len(t, publicSubnetIds, 2)
}
```

### Static Analysis (Policy as Code)

```bash
# Checkov - static analysis for security
checkov -d . --framework terraform

# tfsec - security scanner
tfsec .

# OPA/Conftest - custom policy checks
conftest test . -p policies/

# Example OPA policy (policies/main.rego)
# package main
#
# deny[msg] {
#   resource := input.resource.aws_s3_bucket[name]
#   not resource.server_side_encryption_configuration
#   msg := sprintf("S3 bucket '%s' must have encryption enabled", [name])
# }
```

---

## Security

### Secrets Management

```hcl
# [GOOD] Mark sensitive variables
variable "database_password" {
  description = "Database master password"
  type        = string
  sensitive   = true
}

# [GOOD] Mark sensitive outputs
output "database_connection_string" {
  description = "Database connection string (contains credentials)"
  value       = "postgresql://${var.db_user}:${var.db_password}@${aws_db_instance.main.endpoint}"
  sensitive   = true
}

# [BAD] Never hardcode secrets
resource "aws_db_instance" "main" {
  password = "my-secret-password"  # NEVER DO THIS
}

# [GOOD] Use a secret manager
data "aws_secretsmanager_secret_version" "db_password" {
  secret_id = "prod/database/password"
}

resource "aws_db_instance" "main" {
  password = data.aws_secretsmanager_secret_version.db_password.secret_string
}
```

### Remote State Encryption

```hcl
# [GOOD] Encrypted remote state with locking
terraform {
  backend "s3" {
    bucket         = "mycompany-terraform-state"
    key            = "prod/infrastructure.tfstate"
    region         = "us-east-1"
    encrypt        = true                        # Encrypt state at rest
    dynamodb_table = "terraform-locks"           # State locking
    kms_key_id     = "alias/terraform-state-key" # Custom KMS key
  }
}
```

### IAM Least Privilege

```hcl
# [GOOD] Minimal permissions for each role
resource "aws_iam_role_policy" "app" {
  name = "app-policy"
  role = aws_iam_role.app.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
        ]
        Resource = "${aws_s3_bucket.app_data.arn}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
        ]
        Resource = aws_sqs_queue.app.arn
      },
    ]
  })
}

# [BAD] Overly permissive
resource "aws_iam_role_policy" "app" {
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "*"
      Resource = "*"
    }]
  })
}
```

### Encryption at Rest

```hcl
# Always enable encryption for data stores
resource "aws_s3_bucket_server_side_encryption_configuration" "main" {
  bucket = aws_s3_bucket.main.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
  }
}

resource "aws_db_instance" "main" {
  storage_encrypted = true
  kms_key_id        = aws_kms_key.main.arn
  # ...
}

resource "aws_ebs_volume" "data" {
  encrypted  = true
  kms_key_id = aws_kms_key.main.arn
  # ...
}
```

**Security Checklist:**
- [ ] No secrets in code, variables, or version control
- [ ] Remote state encrypted with KMS and locked with DynamoDB
- [ ] All `sensitive` flags set on variables and outputs containing credentials
- [ ] IAM policies follow least privilege principle
- [ ] All storage encrypted at rest (S3, RDS, EBS)
- [ ] All data in transit encrypted (TLS/SSL)
- [ ] Security groups restrict access to minimum needed
- [ ] Public access blocked on S3 buckets (unless explicitly required)
- [ ] Provider credentials managed via IAM roles (not static keys)
- [ ] Static analysis (tfsec/checkov) passes with no critical findings

---

## Performance

### Minimize Provider Calls

```hcl
# [GOOD] Use count/for_each for multiple similar resources
resource "aws_subnet" "public" {
  for_each = toset(var.availability_zones)

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr_block, 8, index(var.availability_zones, each.value))
  availability_zone = each.value

  tags = merge(local.common_tags, {
    Name = "public-${each.value}"
    Tier = "public"
  })
}

# [BAD] Separate resources for each (repetitive, hard to maintain)
resource "aws_subnet" "public_a" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.1.0/24"
  availability_zone = "us-east-1a"
}

resource "aws_subnet" "public_b" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.2.0/24"
  availability_zone = "us-east-1b"
}
```

### Targeted Operations

```bash
# Apply changes to specific resources only (use sparingly)
terraform apply -target=module.ecs

# Refresh specific resources
terraform apply -refresh-only -target=aws_instance.web_server
```

### State Splitting

```
# Split state by volatility and blast radius
# Long-lived infrastructure (VPC, RDS) - rarely changes
environments/prod/foundation/
├── main.tf      # VPC, subnets, NAT gateways, RDS
├── backend.tf   # Separate state file

# Short-lived infrastructure (ECS, Lambda) - changes frequently
environments/prod/application/
├── main.tf      # ECS services, Lambda functions, ALB
├── backend.tf   # Separate state file
```

### Parallelism Tuning

```bash
# Increase parallelism for faster applies (default: 10)
terraform apply -parallelism=20

# Decrease for rate-limited APIs
terraform apply -parallelism=5
```

### Limit Expression Complexity

```hcl
# [BAD] Complex nested ternary
locals {
  instance_type = var.environment == "prod" ? "m5.xlarge" : var.environment == "staging" ? "m5.large" : "t3.medium"
}

# [GOOD] Break into multiple locals
locals {
  instance_type_map = {
    prod    = "m5.xlarge"
    staging = "m5.large"
    dev     = "t3.medium"
  }
  instance_type = local.instance_type_map[var.environment]
}

# [BAD] Complex interpolation with many functions
locals {
  result = join(",", sort(distinct(flatten([for k, v in var.map : [for i in v : upper(trim(i))]]))))
}

# [GOOD] Break into steps
locals {
  flattened = flatten([for k, v in var.map : [for i in v : upper(trim(i))]])
  unique    = distinct(local.flattened)
  sorted    = sort(local.unique)
  result    = join(",", local.sorted)
}
```

---

## Dependencies

### Provider Version Pinning

```hcl
# versions.tf
terraform {
  required_version = ">= 1.8.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
```

### Module Version Pinning

```hcl
# [GOOD] Pin to specific version or version range
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.8.0"
}

# [GOOD] Pin to a Git tag
module "custom" {
  source = "git::https://github.com/myorg/terraform-module.git?ref=v1.2.0"
}

# [BAD] No version constraint
module "vpc" {
  source = "terraform-aws-modules/vpc/aws"
}

# [BAD] Pointing to branch (mutable reference)
module "custom" {
  source = "git::https://github.com/myorg/terraform-module.git?ref=main"
}
```

### Lock File

```bash
# Always commit .terraform.lock.hcl to version control
# It pins exact provider versions and checksums

# Update lock file when upgrading providers
terraform init -upgrade

# Verify lock file integrity
terraform providers lock \
  -platform=linux_amd64 \
  -platform=darwin_amd64 \
  -platform=darwin_arm64
```

### Dependency Updates

- Review provider changelogs before upgrading
- Test upgrades in dev/staging before production
- Update providers monthly for security patches
- Use Dependabot or Renovate for automated PRs

---

## Logging & Observability

### Terraform Logging

```bash
# Set log level for debugging
export TF_LOG=DEBUG        # TRACE, DEBUG, INFO, WARN, ERROR
export TF_LOG_PATH=terraform.log

# Provider-specific logging
export TF_LOG_PROVIDER=DEBUG
```

### Audit Trails via CI/CD

```yaml
# GitHub Actions example
- name: Terraform Plan
  run: terraform plan -out=tfplan -no-color 2>&1 | tee plan-output.txt

- name: Post Plan to PR
  uses: actions/github-script@v7
  with:
    script: |
      const plan = require('fs').readFileSync('plan-output.txt', 'utf8');
      github.rest.issues.createComment({
        issue_number: context.issue.number,
        owner: context.repo.owner,
        repo: context.repo.repo,
        body: `## Terraform Plan\n\`\`\`\n${plan}\n\`\`\``
      });
```

### Drift Detection

```bash
# Detect configuration drift
terraform plan -detailed-exitcode
# Exit code 0: No changes
# Exit code 1: Error
# Exit code 2: Changes detected (drift)

# Schedule drift detection in CI/CD (e.g., nightly)
# Alert if exit code is 2
```

### Resource Tagging for Observability

```hcl
locals {
  common_tags = {
    Environment = var.environment
    Project     = var.project_name
    ManagedBy   = "terraform"
    Team        = var.team_name
    CostCenter  = var.cost_center
    CreatedAt   = timestamp()
  }
}

# Apply to all resources
resource "aws_instance" "web_server" {
  # ...
  tags = merge(local.common_tags, {
    Name = "web-server"
    Role = "web"
  })
}
```

---

## Code Review

### Review Checklist

**Plan Output:**
- [ ] Reviewed `terraform plan` output thoroughly
- [ ] No unexpected resource deletions or replacements
- [ ] Changes match the intended scope
- [ ] Sensitive values not exposed in plan

**Code Quality:**
- [ ] `terraform fmt` applied (no formatting issues)
- [ ] `terraform validate` passes
- [ ] Naming follows conventions (underscores, singular, no type repetition)
- [ ] Variables have descriptions and type constraints
- [ ] Outputs have descriptions
- [ ] Validation blocks on variables where appropriate
- [ ] No hardcoded values (use variables or locals)

**Architecture:**
- [ ] Resources grouped logically in files
- [ ] Modules used for reusable patterns
- [ ] State separation appropriate (blast radius minimized)
- [ ] Dependencies between resources are clear

**Security:**
- [ ] No secrets in code or tfvars committed to VCS
- [ ] Sensitive flags on appropriate variables/outputs
- [ ] IAM follows least privilege
- [ ] Encryption enabled on all data stores
- [ ] Security groups restrict to minimum access
- [ ] tfsec/checkov passes

**Safety:**
- [ ] `prevent_destroy` on stateful resources (databases, S3 buckets)
- [ ] `create_before_destroy` where zero-downtime needed
- [ ] `moved` blocks for renames/refactors (no destroy/recreate)
- [ ] Backup/snapshot policies in place

### Review Process

**Requirements:**
- Minimum 2 approvals for production infrastructure
- `terraform plan` output reviewed by at least one reviewer
- All automated checks must pass (fmt, validate, tfsec, checkov)
- No unresolved comments
- Production applies gated behind manual approval

**Review Guidelines:**
- Be respectful and constructive
- Always check the plan output, not just the code diff
- Verify blast radius: what else is affected?
- Check for state management implications
- Distinguish between blocking and non-blocking comments

---

## Tooling

### Required Tools

**Code Formatting:**
- `terraform fmt` - Built-in formatter (run before every commit)

**Validation & Linting:**
- `terraform validate` - Syntax and type validation
- `TFLint` - Terraform linter with provider-specific rules

**Security Scanning:**
- `tfsec` - Security scanner for Terraform
- `checkov` - Policy-as-code static analysis
- `OPA/Conftest` - Custom policy validation (optional)

**Documentation:**
- `terraform-docs` - Auto-generate README from variables/outputs

**Testing:**
- `terraform test` - Native testing framework
- `Terratest` - Go-based integration testing

### Pre-Commit Configuration

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/antonbabenko/pre-commit-tf
    hooks:
      - id: terraform_fmt
      - id: terraform_validate
      - id: terraform_tflint
        args:
          - --args=--config=__GIT_WORKING_DIR__/.tflint.hcl
      - id: terraform_tfsec
      - id: terraform_docs
        args:
          - --args=--config=.terraform-docs.yml
      - id: terraform_checkov
        args:
          - --args=--quiet
          - --args=--skip-check CKV_AWS_18,CKV_AWS_21
```

### TFLint Configuration

```hcl
# .tflint.hcl
plugin "aws" {
  enabled = true
  version = "0.31.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}

rule "terraform_naming_convention" {
  enabled = true
}

rule "terraform_documented_variables" {
  enabled = true
}

rule "terraform_documented_outputs" {
  enabled = true
}

rule "terraform_typed_variables" {
  enabled = true
}

rule "terraform_unused_declarations" {
  enabled = true
}
```

### CI/CD Pipeline

**Required checks before merge:**
```bash
# Initialize
terraform init -backend=false

# Format check
terraform fmt -check -recursive

# Validate
terraform validate

# Lint
tflint --init && tflint

# Security scan
tfsec . --minimum-severity HIGH
checkov -d . --framework terraform --quiet

# Auto-generate docs
terraform-docs markdown table . --output-file README.md

# Plan (for each environment)
terraform plan -out=tfplan -detailed-exitcode

# Native tests
terraform test
```

**Production apply pipeline:**
```bash
# Plan with saved output
terraform plan -out=tfplan

# Manual approval gate (in CI/CD)

# Apply from saved plan only
terraform apply tfplan

# Post-apply validation
terraform output -json > outputs.json
# Run health checks against deployed infrastructure
```

---

## Quick Reference

### Daily Workflow

1. **Before coding:**
   - Pull latest changes
   - Review current state: `terraform plan`
   - Create feature branch

2. **While coding:**
   - Follow naming conventions (underscores, singular)
   - Add descriptions to all variables and outputs
   - Add validation blocks where appropriate
   - Use `terraform fmt` and `terraform validate` frequently

3. **Before pushing:**
   - Run `terraform fmt -check -recursive`
   - Run `terraform validate`
   - Run `tflint` and `tfsec`
   - Review `terraform plan` output carefully
   - Update module README with `terraform-docs`

4. **Code review:**
   - Share `terraform plan` output with reviewers
   - Address all comments
   - Ensure CI passes
   - Get 2 approvals
   - Squash and merge

5. **Applying:**
   - Apply to dev first, then staging, then production
   - Use saved plan files: `terraform plan -out=tfplan && terraform apply tfplan`
   - Verify deployment with health checks
   - Monitor for drift

### Common Commands

```bash
# Initialize working directory
terraform init

# Format all files
terraform fmt -recursive

# Validate configuration
terraform validate

# Plan changes
terraform plan

# Plan with saved output
terraform plan -out=tfplan

# Apply from saved plan
terraform apply tfplan

# Apply with auto-approve (CI/CD only, never interactive)
terraform apply -auto-approve

# Show current state
terraform show

# List resources in state
terraform state list

# Import existing resource
terraform import aws_instance.web_server i-1234567890abcdef0

# Move resource in state (refactoring)
terraform state mv aws_instance.old aws_instance.new

# Remove resource from state (without destroying)
terraform state rm aws_instance.legacy

# Destroy infrastructure
terraform destroy

# Lint
tflint

# Security scan
tfsec .

# Generate docs
terraform-docs markdown table . --output-file README.md

# Run tests
terraform test

# Upgrade providers
terraform init -upgrade

# Install pre-commit hooks
pre-commit install
pre-commit run --all-files
```

---

## References

- [HashiCorp Terraform Style Guide](https://developer.hashicorp.com/terraform/language/style)
- [HashiCorp Terraform Recommended Practices](https://developer.hashicorp.com/terraform/cloud-docs/recommended-practices)
- [HashiCorp Standard Module Structure](https://developer.hashicorp.com/terraform/language/modules/develop/structure)
- [Google Cloud Terraform Best Practices](https://docs.cloud.google.com/docs/terraform/best-practices/general-style-structure)
- [Terraform Best Practices (community)](https://www.terraform-best-practices.com/)
- [tfsec Documentation](https://aquasecurity.github.io/tfsec/)
- [Checkov Documentation](https://www.checkov.io/1.Welcome/What%20is%20Checkov.html)
- [Terratest Documentation](https://terratest.gruntwork.io/)

---

**Questions or suggestions?** Update this document through team discussion and code review.

**Version History:**
- v1.0 (2026) - Initial enterprise-grade guidelines
