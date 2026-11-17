provider "aws" {
  region = var.region

  # Emulated AWS (moto): no real credentials or account metadata exist.
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  skip_region_validation      = true
  s3_use_path_style           = true

  dynamic "endpoints" {
    for_each = [0]
    content {
      s3        = var.endpoint
      sqs       = var.endpoint
      dynamodb  = var.endpoint
      sts       = var.endpoint
      apigateway = var.endpoint
    }
  }
}

# ---------------------------------------------------------------------------
# Object storage
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "raw" {
  bucket = var.bucket_names.raw
}

resource "aws_s3_bucket" "artifacts" {
  bucket = var.bucket_names.artifacts
}

resource "aws_s3_bucket" "results" {
  bucket = var.bucket_names.results
}

# ---------------------------------------------------------------------------
# Job queue + dead-letter queue
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "jobs_dlq" {
  name = "jobs-dlq"

  visibility_timeout_seconds = 300
}

resource "aws_sqs_queue" "jobs" {
  name = "jobs"

  # 5 min default lease; workers extend it via heartbeats for long jobs.
  visibility_timeout_seconds = 300
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  # moto echoes the policy re-serialized with spaces, so exact-string matching
  # in Terraform reports perpetual whitespace drift. The wiring itself is
  # applied on first creation and asserted by scripts/verify-infra.sh.
  lifecycle {
    ignore_changes = [redrive_policy]
  }
}

# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "jobs" {
  name         = "jobs"
  hash_key     = "job_id"
  billing_mode = "PAY_PER_REQUEST"

  attribute {
    name = "job_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "alerts" {
  name         = "alerts"
  hash_key     = "alert_id"
  billing_mode = "PAY_PER_REQUEST"

  attribute {
    name = "alert_id"
    type = "S"
  }
}
