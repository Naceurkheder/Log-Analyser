# ADR-0005 — Ingestion-plane resource model and LXD project scoping

* Status: accepted
* Date: 2026-11-20
* Deciders: project owner, agent

## Context

Sprint 2 provisioned a minimal plane (`raw`, `artifacts`, `results`; one `jobs`
queue; `jobs`/`alerts` tables) that was sufficient for the infrastructure
acceptance checks. Sprint 3 introduces separate ingestion and execution
contracts:

* source datasets must be immutable and referenced by identity (`incoming`),
* chunks and their manifests need their own bucket (`raw-chunks`),
* GPU work and CPU baseline work must not consume each other's messages
  (`jobs-gpu` / `jobs-cpu`),
* the API needs per-run and per-job records with run-level pagination
  (`SentinelRuns`, `SentinelJobs`, `SentinelAlerts`).

The previous names were placeholders; keeping them would force the API,
producer and workers to encode capability inside a shared queue, which the
plan explicitly forbids.

Separately, `lxd/fleet.sh` stored its LXD command as a bash array but invoked
it as `$LXC`, which expands to the first element only. The `--project sentinel`
argument was silently dropped, so every container was created in LXD's
`default` project while the documentation claimed `sentinel`. Commands that
hard-coded `lxc exec` happened to work; anything using the array did not.

## Decision

* Use the plan's resource model exactly:
  * buckets `incoming`, `raw-chunks`, `artifacts`, `results`;
  * queues `ingest-queue`/`ingest-dlq`, `jobs-gpu`/`jobs-gpu-dlq`,
    `jobs-cpu`/`jobs-cpu-dlq`, all with `maxReceiveCount = 3`;
  * tables `SentinelRuns` (`run_id`), `SentinelJobs` (`job_id`, GSI
    `run_id-index` with range key `job_id`), `SentinelAlerts` (`alert_id`,
    GSI `run_id-index` with range key `alert_id`).
* Terraform is split into `buckets.tf`, `queues.tf`, `tables.tf`,
  `outputs.tf`, keeping the provider in `main.tf`.
* The GSI range keys are the item ids, not timestamps: DynamoDB only
  guarantees order inside a hash key when a sort key exists, and the API must
  paginate deterministically. Alerts therefore page in `alert_id` order
  (content-addressed, stable) rather than insertion order.
* Visibility timeouts are 300 s for ingest/job queues until Sprint 6 measures
  real job durations and tunes them together with heartbeat intervals.
* Queue message retention: 4 days for job queues, 1 day for ingestion, 14 days
  for all DLQs.
* All helper scripts source `lxd/common.sh`, which owns the `lxd --project`
  invocation. The four containers were migrated from `default` to the
  `sentinel` project (stopped, `lxc move --target-project`, restarted), so the
  scripts and the documentation now agree.

## Consequences

* Old resources are destroyed by the first apply and recreated with the new
  names. This is safe: the emulator holds only synthetic test data and
  Terraform state is local/reproducible.
* Consumers must be updated together with the queue names; until Sprint 6
  only the producer consumes (ingest queue), so no worker code breaks now.
* Any future command that addresses an instance must go through
  `lxd/common.sh`; adding a bare `lxc` call is a review rejection.
* DLQ name for ingestion is `ingest-dlq` (not `ingest-queue-dlq`); the
  verification script encodes this mapping explicitly.

## Revisit when

* Sprint 6 measures job durations and finalises visibility timeouts/retries.
* Pagination access patterns justify narrower GSI projections.
