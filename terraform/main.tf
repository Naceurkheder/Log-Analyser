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
      s3         = var.endpoint
      sqs        = var.endpoint
      dynamodb   = var.endpoint
      sts        = var.endpoint
      apigateway = var.endpoint
    }
  }
}
