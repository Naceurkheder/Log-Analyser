# Sentinel-GPU — top-level task runner
#
# All local state lives in .env (see .env.example). Copy and edit if needed.

ifneq (,$(wildcard .env))
include .env
export
endif

SHELL      := /bin/bash
FLEET      := ./lxd/fleet.sh

.DEFAULT_GOAL := help
.PHONY: help up down status destroy shell dns-check

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---------------------------------------------------------------------------
# Fleet (LXD)
# ---------------------------------------------------------------------------
up: ## Launch the container fleet (idempotent)
	$(FLEET) up

down: ## Stop the fleet
	$(FLEET) down

status: ## Show fleet status and resource caps
	$(FLEET) status

destroy: ## Delete fleet + network + project (destructive)
	$(FLEET) destroy

shell: ## Open a shell in a container: make shell C=infra
	$(FLEET) shell $(C)

dns-check: ## Verify inter-container name resolution from infra
	$(FLEET) dns-check

# ---------------------------------------------------------------------------
# Emulated AWS (moto in infra) + Terraform (runs on the host)
# ---------------------------------------------------------------------------
infra: ## Bootstrap emulated AWS + apply Terraform resources (idempotent)
	./lxd/bootstrap-infra.sh infra
	terraform -chdir=$(TF_DIR) init -reconfigure -input=false
	terraform -chdir=$(TF_DIR) apply -auto-approve -input=false

infra-destroy: ## Tear down Terraform-managed emulated AWS resources
	terraform -chdir=$(TF_DIR) destroy -auto-approve -input=false

verify: ## Verify buckets/queues/tables/redrive from the host
	./scripts/verify-infra.sh
