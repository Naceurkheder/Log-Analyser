# ---------------------------------------------------------------------------
# Capability-specific queues
#
#   ingest-queue  -> producer (S3-B)
#   jobs-gpu      -> CUDA worker (S6)
#   jobs-cpu      -> CPU worker (S6)
#
# Each main queue has a DLQ with maxReceiveCount = 3.  Visibility timeout is a
# placeholder until S6 measures real job durations; workers extend it with
# heartbeats, so it must stay comfortably above the heartbeat interval.
#
# moto echoes RedrivePolicy re-serialized with spaces, so exact-string matching
# in Terraform reports perpetual whitespace drift. The wiring is applied on
# first creation and asserted by scripts/verify-infra.sh.
# ---------------------------------------------------------------------------

locals {
  queues = {
    ingest = {
      name      = "ingest-queue"
      dlq       = "ingest-dlq"
      timeout   = 300
      retention = 86400
    }
    jobs_gpu = {
      name      = "jobs-gpu"
      dlq       = "jobs-gpu-dlq"
      timeout   = 300
      retention = 345600
    }
    jobs_cpu = {
      name      = "jobs-cpu"
      dlq       = "jobs-cpu-dlq"
      timeout   = 300
      retention = 345600
    }
  }
}

resource "aws_sqs_queue" "dlq" {
  for_each = local.queues

  name                       = each.value.dlq
  message_retention_seconds  = 1209600 # 14 days
  visibility_timeout_seconds = each.value.timeout
}

resource "aws_sqs_queue" "main" {
  for_each = local.queues

  name                       = each.value.name
  visibility_timeout_seconds = each.value.timeout
  receive_wait_time_seconds  = 20
  message_retention_seconds  = each.value.retention

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = var.max_receive_count
  })

  lifecycle {
    ignore_changes = [redrive_policy]
  }
}
