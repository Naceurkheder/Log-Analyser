terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Local state: the fleet is ephemeral and reproducible (`make infra` rebuilds
  # everything). See docs/decisions/0003-terraform-local-state.md.
  backend "local" {}
}
