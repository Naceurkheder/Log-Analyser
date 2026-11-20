# ---------------------------------------------------------------------------
# Object storage (see docs/decisions/0005-ingestion-resource-model.md)
#
#   incoming    original datasets uploaded by the CLI/API client
#   raw-chunks  immutable chunk objects and their JSON manifests
#   artifacts   versioned worker libraries and build manifests (S7)
#   results     attempt outputs and committed result manifests
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "incoming" {
  bucket = var.bucket_names.incoming
}

resource "aws_s3_bucket" "raw_chunks" {
  bucket = var.bucket_names.raw_chunks
}

resource "aws_s3_bucket" "artifacts" {
  bucket = var.bucket_names.artifacts
}

resource "aws_s3_bucket" "results" {
  bucket = var.bucket_names.results
}
