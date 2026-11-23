# Worker recovery and operations

This document is the operator companion to
`docs/decisions/0010-worker-state-and-leases.md`. Workers are separate
processes: one polls `jobs-cpu`, one polls `jobs-gpu`, and neither ever reads the
other's queue. All commands below are run from the repository root.

## Commands

```sh
# Consume one capability queue. Defaults to a loop until SIGTERM/SIGINT.
PYTHONPATH=src .venv/bin/python -m sentinel.worker.cli serve --backend cpu
PYTHONPATH=src .venv/bin/python -m sentinel.worker.cli serve --backend gpu

# Bounded single batch (useful for smoke tests and staged rollouts).
PYTHONPATH=src .venv/bin/python -m sentinel.worker.cli serve --backend cpu --once

# Republish old unpublished job/ingest records. --once is the only mode.
PYTHONPATH=src .venv/bin/python -m sentinel.worker.cli reconcile --once

# Inspect poison messages without consuming them (visibility timeout 0).
PYTHONPATH=src .venv/bin/python -m sentinel.worker.cli dlq-inspect --backend cpu

# Explicit, bounded replay. The reason is mandatory and auditable.
PYTHONPATH=src .venv/bin/python -m sentinel.worker.cli dlq-replay \
  --backend cpu --reason "rebuilt corrupt chunk" --limit 10 --operator alice
```

All commands print one JSON object to stdout and return `0` on a handled
outcome. `serve` also starts an in-process health/metrics HTTP server when
invoked through the CLI; the `--metrics-port` and `--worker-id` flags override
the corresponding environment variables. The `Worker` constructor itself starts
no threads, and importing the CLI starts nothing.

## Configuration

Resource names come from the existing `sentinel.api.config.Settings`. Worker-only
settings are read from the environment at startup:

| Variable | Default | Purpose |
|---|---:|---|
| `SENTINEL_WORKER_BACKEND` | `cpu` | Capability when `serve` omits `--backend` |
| `SENTINEL_WORKER_ID` | hostname-pid | Log/metric identity |
| `SENTINEL_RESULTS_BUCKET` | `results` | Immutable attempt results |
| `SENTINEL_WORKER_VISIBILITY_TIMEOUT` | `300` | SQS visibility and DynamoDB lease length (seconds) |
| `SENTINEL_WORKER_HEARTBEAT_INTERVAL` | `30` | Lease/SQS renewal interval |
| `SENTINEL_WORKER_LEASE_MARGIN_SECONDS` | `240` | Safety margin absorbed by renewal latency |
| `SENTINEL_WORKER_WAIT_SECONDS` | `20` | SQS long poll wait |
| `SENTINEL_WORKER_MAX_MESSAGES` | `1` | Messages per receive (1–10) |
| `SENTINEL_WORKER_MAX_RETRIES` | `3` | Transient retries before quarantine |
| `SENTINEL_WORKER_RETRY_BASE_SECONDS` / `..._MAX_SECONDS` | `0.5` / `30` | Jittered backoff bounds |
| `SENTINEL_WORKER_METRICS_HOST` / `..._PORT` | `127.0.0.1` / `9100` (CPU), `9101` (GPU) | HTTP bind address |
| `SENTINEL_WORKER_ARTIFACT_PATH` | unset | Local native library to verify/load |
| `SENTINEL_WORKER_ARTIFACT_SHA256` | unset | Pinned digest of that exact file |
| `SENTINEL_WORKER_EXPECTED_ABI_VERSION` | native ABI v2 (`2`) | Reject stale libraries |
| `SENTINEL_WORKER_RECONCILE_MIN_AGE_SECONDS` | `30` | Minimum age before outbox republish |
| `SENTINEL_WORKER_RECONCILE_PAGE_SIZE` / `..._MAX_PAGES` | `100` / `10` | Bounded outbox scan |
| `SENTINEL_WORKER_REPLAY_REASON_MIN_LENGTH` | `4` | Minimum operator reason length |
| `SENTINEL_WORKER_REPLAY_MAX_COUNT` | `1` | Maximum replays per message |
| `SENTINEL_WORKER_DLQ_INSPECT_LIMIT` | `50` | Requested inspection batch (SQS allows at most 10 per receive) |

Startup validation rejects `visibility_timeout < 2 * heartbeat_interval +
lease_margin_seconds`, an invalid message batch size, an invalid digest, or a
non-positive replay budget. A misconfigured worker fails loudly rather than
running with an unsafe lease.

## Health and metrics

* `GET /healthz` → compact JSON, `{"status":"ok"|"paused", ...}`. `paused` means
  the worker lost a lease and will not accept new work until restarted.
* `GET /metrics` → Prometheus text (`text/plain; version=0.0.4; charset=utf-8`).

S6 worker-local series:

| Metric | Type | Meaning |
|---|---|---|
| `sentinel_worker_info` | gauge | Capability and worker id labels |
| `sentinel_worker_active_state` | gauge | `idle`, `claiming`, `running`, `committing`, `stopped` |
| `sentinel_worker_accepting_work` | gauge | `0` after lease loss |
| `sentinel_worker_jobs_processed_total` | counter | Durable `SUCCEEDED` commits |
| `sentinel_worker_retries_total` | counter | Transient retries |
| `sentinel_worker_heartbeat_failures_total` | counter | Failed lease renewals |
| `sentinel_worker_lease_losses_total` | counter | Lost leases |
| `sentinel_worker_dlq_sends_total` | counter | Quarantined messages |
| `sentinel_worker_alerts_written_total` | counter | New alert rows |
| `sentinel_worker_outbox_republished_total` | counter | Reconciler sends |
| `sentinel_worker_run_completions_total` | counter | First run finalizations |
| `sentinel_worker_messages_ignored_total` | counter | Duplicate/terminal deliveries |
| `sentinel_worker_lease_age_seconds` / `sentinel_worker_heartbeat_age_seconds` | gauge | Current lease/heartbeat age |

Labels are capability and worker id only; run/job/chunk ids and log content are
never metric labels.

## Failure matrix

| Failure | Detection | Durable state / message | Worker action | Operator action |
|---|---|---|---|---|
| Malformed JSON or schema | Parse/validate before claim | No job write; structured DLQ message; original acked | `dlq` | Inspect, fix producer, replay if job record exists |
| Backend/capability mismatch | Message validation against queue capability | No job write; DLQ | `dlq` | Fix routing; do not replay into the wrong queue |
| Message/job/run identity mismatch | `validate_authoritative` | Non-terminal job may be quarantined with `last_error`; DLQ | `dlq` | Reconcile the producer record; replay only after repair |
| Chunk or manifest digest mismatch | Input verification | `DEAD_LETTERED`, `phase="execute"`; DLQ | `dlq` | Repair S3 object, then `dlq-replay` with reason |
| Artifact digest mismatch / wrong ABI / GPU load failure | Verifier before native call | `DEAD_LETTERED` (`gpu_unavailable` / `abi_mismatch`); DLQ | `dlq`, no CPU fallback | Deploy the correct v2 artifact and digest, then replay |
| Transient AWS error | `ClientError`/endpoint exception | `RETRYABLE_FAILED` with jittered backoff; message not acked until state recorded | `retryable` | Usually automatic; investigate repeated `job_retryable` logs |
| Retry budget exhausted | Attempt counter ≥ `max_retries + 1` | `DEAD_LETTERED`; DLQ | `dlq` | Fix root cause, then explicit replay |
| Lease/visibility loss | Heartbeat failure or injected `force_loss` | `RETRYABLE_FAILED` + `lease_lost_at`; message left in queue | `lease_lost`, worker pauses | Restart worker after dependency is healthy; redelivery gets a new fence |
| Kill before commit | Process exit during execution | Job still `RUNNING` with an expired lease; no result reference | None (redelivery) | Wait for visibility expiry; consumer reclaims with token+1 |
| Crash after job commit | Terminal `SUCCEEDED` before SQS delete | Result object + alerts + job + run finalized | `duplicate`, message acked | None |
| Result readback mismatch | HEAD/GET vs canonical digest | No job transition; no ack | `retryable` | Investigate S3 consistency/corruption |
| Run already finalized | Conditional `attribute_not_exists(run_completed_at)` fails | First counters retained | `committed` without another finalization | None |
| Outbox publish crash | `published=false` or stale duplicate message | Reconciler republishes identical logical message; conditional mark | Duplicate terminal delivery is acked | Run `reconcile --once` periodically |
| Replay budget exhausted | `replay.count >= replay_max_count` | Message stays in DLQ; `skipped` in replay report | `skipped` | Fix permanently or approve a policy change; do not loop manually |

`process_once()` returns the last `ProcessOutcome` (`committed`, `duplicate`,
`retryable`, `lease_lost`, `dlq`, `transient`, `claim_conflict`) or `None` when
the queue was empty. `serve --once` counts a delivery as one processed message.

## Recovery procedures

### Stuck `RUNNING` job with an expired lease

1. Check `/healthz`; a paused worker reports `paused`.
2. Restart the worker process so `accepting_work` is restored.
3. Wait one visibility timeout. SQS redelivers and `claim_job` increments
   `fencing_token`/`attempts`; the old worker cannot commit.
4. Confirm the job either becomes `SUCCEEDED` under the new attempt or
   `RETRYABLE_FAILED` again.

Do not manually clear `lease_lost_at` or decrement the fencing token.

### Poison message quarantine

1. `dlq-inspect --backend <cpu|gpu>` to see `job_id`, `last_error`, and replay
   metadata without consuming the message.
2. Repair the underlying artifact, S3 object, or producer record.
3. `dlq-replay --backend <cpu|gpu> --reason "<specific cause>"`. This requeues
   only terminal jobs, bumps the fence, strips `last_error`, and records the
   operator and replay count on the message.
4. Confirm `jobs_completed`/`jobs_failed` come from the job table, not from the
   replay command itself.

### Outbox backlog

Run `reconcile --once` on a schedule. It is bounded by page size/page count and
minimum age. A non-zero `errors` array means a record failed validation or an
AWS call; fix the record and rerun. Duplicate sends are expected after a
publish-then-crash and are harmless because ids are deterministic.

## Limitations

* `dlq-inspect` performs one SQS receive per invocation and SQS caps a receive
  at 10 messages, even if `SENTINEL_WORKER_DLQ_INSPECT_LIMIT` is higher.
* Reconciliation is periodic, not transactional: a crash immediately after
  publication can leave a duplicate until the terminal-state no-op handles it.
* GPU recovery requires a correct v2 native artifact on the host; no test or
  runtime path in this package loads CUDA hardware or silently falls back.
* The S6 metric names above are worker-local. A fleet-level adapter to the
  taxonomy in `docs/decisions/0011-artifact-observability.md` is a separate
  concern and is not performed by `sentinel.worker.metrics`.
