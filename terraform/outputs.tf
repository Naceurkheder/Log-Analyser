output "buckets" {
  value = {
    incoming   = aws_s3_bucket.incoming.id
    raw_chunks = aws_s3_bucket.raw_chunks.id
    artifacts  = aws_s3_bucket.artifacts.id
    results    = aws_s3_bucket.results.id
  }
}

output "queues" {
  value = {
    ingest       = aws_sqs_queue.main["ingest"].url
    ingest_dlq   = aws_sqs_queue.dlq["ingest"].url
    jobs_gpu     = aws_sqs_queue.main["jobs_gpu"].url
    jobs_gpu_dlq = aws_sqs_queue.dlq["jobs_gpu"].url
    jobs_cpu     = aws_sqs_queue.main["jobs_cpu"].url
    jobs_cpu_dlq = aws_sqs_queue.dlq["jobs_cpu"].url
  }
}

output "tables" {
  value = {
    runs   = aws_dynamodb_table.runs.name
    jobs   = aws_dynamodb_table.jobs.name
    alerts = aws_dynamodb_table.alerts.name
  }
}

output "indexes" {
  value = {
    jobs_run   = "${aws_dynamodb_table.jobs.name}/run_id-index"
    alerts_run = "${aws_dynamodb_table.alerts.name}/run_id-index"
  }
}

output "endpoint" {
  value = var.endpoint
}
