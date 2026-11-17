output "buckets" {
  value = {
    raw       = aws_s3_bucket.raw.id
    artifacts = aws_s3_bucket.artifacts.id
    results   = aws_s3_bucket.results.id
  }
}

output "queues" {
  value = {
    jobs    = aws_sqs_queue.jobs.url
    jobs_dlq = aws_sqs_queue.jobs_dlq.url
  }
}

output "tables" {
  value = {
    jobs   = aws_dynamodb_table.jobs.name
    alerts = aws_dynamodb_table.alerts.name
  }
}

output "endpoint" {
  value = var.endpoint
}
