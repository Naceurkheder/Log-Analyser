# ADR-0003 — Terraform runs on the host with local state

* Status: accepted
* Date: 2026-11-17
* Deciders: project owner, agent

## Context

The sprint plan places Terraform in a dedicated `provision` container and
flags the state-file location as an open decision. Provisioning is a
one-shot, human-driven operation; a container adds an image layer, RAM cap
and shell plumbing for no benefit on a single-host academic deployment.

## Decision

* Terraform executes on the host (`make infra`), targeting the emulated AWS
  endpoint over the fleet bridge (`http://10.20.0.10:4566`).
* State stays local in `terraform/` (backend `local`), and `*.tfstate*`,
  `.terraform/`, plans are gitignored. The fleet is fully reproducible:
  `make destroy && make up && make infra` rebuilds an identical environment,
  which is the real recovery mechanism rather than remote state.

## Consequences

* No state locking/backup story beyond git — acceptable because only one
  operator applies changes, and the environment is disposable.
* If a future sprint needs CI provisioning, revisit remote state (S3 backend
  would be trivially available through moto).
