# ---------------------------------------------------------------------------
# Metadata tables
#
#   SentinelRuns    one item per logical run (idempotent run creation)
#   SentinelJobs    one item per deterministic chunk job (+ run_id GSI)
#   SentinelAlerts  deduplicated alert metadata (+ run_id GSI)
#
# The GSIs are projections of the pagination queries served by the API; keep
# the key names in sync with src/sentinel/api/store.py.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "runs" {
  name         = "SentinelRuns"
  hash_key     = "run_id"
  billing_mode = "PAY_PER_REQUEST"

  attribute {
    name = "run_id"
    type = "S"
  }
}

resource "aws_dynamodb_table" "jobs" {
  name         = "SentinelJobs"
  hash_key     = "job_id"
  billing_mode = "PAY_PER_REQUEST"

  attribute {
    name = "job_id"
    type = "S"
  }

  attribute {
    name = "run_id"
    type = "S"
  }

  global_secondary_index {
    name            = "run_id-index"
    hash_key        = "run_id"
    projection_type = "ALL"
  }
}

resource "aws_dynamodb_table" "alerts" {
  name         = "SentinelAlerts"
  hash_key     = "alert_id"
  billing_mode = "PAY_PER_REQUEST"

  attribute {
    name = "alert_id"
    type = "S"
  }

  attribute {
    name = "run_id"
    type = "S"
  }

  global_secondary_index {
    name            = "run_id-index"
    hash_key        = "run_id"
    projection_type = "ALL"
  }
}
