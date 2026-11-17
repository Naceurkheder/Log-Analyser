# ADR-0002 — moto instead of LocalStack for the emulated AWS plane

* Status: accepted
* Date: 2026-11-17
* Deciders: project owner, agent

## Context

The sprint plan calls for LocalStack (community edition) inside the `infra`
container to emulate S3, SQS and DynamoDB. LocalStack's community runtime
expects to launch its services through a Docker daemon. Docker inside an LXD
container means either nested virtualization complexity or running LocalStack
on the host — both heavier than the project needs. The host already carries a
Docker daemon whose nftables rules conflict with LXD NAT (see
scripts/host-firewall.sh), so adding more Docker coupling is undesirable.

## Decision

Use **moto** (`pip install moto[server]`) inside the `infra` container, served
by `moto_server` (threaded werkzeug server) as a systemd service
(`sentinel-moto.service`) on `0.0.0.0:4566`.

* Installs in ~100 MB, no Docker required.
* Covers the exact API surface this project uses: S3, SQS (incl. long polling
  and redrive policy), DynamoDB.
* Terraform targets it through the standard `endpoints` block, same as it
  would target LocalStack — so swapping back is a configuration change, not a
  code change.

## Consequences

* moto returns some serialized attributes (e.g. SQS `RedrivePolicy`) with
  whitespace differences from what Terraform writes; handled in
  terraform/main.tf with a documented `ignore_changes` lifecycle block, and
  the actual wiring is asserted by scripts/verify-infra.sh.
* moto implements a subset of each service; anything outside S3/SQS/DynamoDB
  basics must be re-checked before use (noted per feature from Sprint 3 on).
