variable "endpoint" {
  description = "Base URL of the emulated AWS API (moto in the infra container)."
  type        = string
  default     = "http://10.20.0.10:4566"
}

variable "region" {
  description = "Emulated AWS region. Any value is accepted; pinned for determinism."
  type        = string
  default     = "us-east-1"
}

variable "bucket_names" {
  description = "Object storage buckets: raw input chunks, build artifacts, results."
  type = object({
    raw       = string
    artifacts = string
    results   = string
  })
  default = {
    raw       = "raw"
    artifacts = "artifacts"
    results   = "results"
  }
}

variable "max_receive_count" {
  description = "Receive attempts before a job message is moved to the dead-letter queue."
  type        = number
  default     = 3
}
