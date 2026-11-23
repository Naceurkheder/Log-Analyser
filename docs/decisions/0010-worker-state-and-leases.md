# ADR-0010 — Worker state, fenced leases, and durable result commits

* Status: accepted (Sprint 6 / S6)
* Date: 2026-11-23
* Context: S3, DynamoDB, and SQS do not share a transaction. A job can be
  delivered more than once, a worker can die after any durable write and before
  acknowledging the message, and a slow attempt can continue running after its
  visibility timeout. Sprint 6 must therefore make "exactly once *effects*"
  achievable with idempotent writes, conditional state transitions, and a
  fencing token, without adding a distributed lock service or a new dependency.

## Decision

### 1. One authoritative job state machine

`src/sentinel/worker/state.py` is the only place that declares legal
transitions. Every state-changing method in `WorkerStore` validates locally
with `assert_transition` and then issues a conditional DynamoDB write; the
condition is the concurrency authority, not the in-process check.

```text
                    claim (token+1, attempt+1)
  PENDING/QUEUED/RETRYABLE_FAILED ───────────────► RUNNING
          ▲                                          │
          │ retry backlog                            ├─► SUCCEEDED              (terminal)
          │                                          ├─► FAILED_PERMANENT       (terminal)
          └─ RETRYABLE_FAILED ◄───────────────────────┤
              (transient / lost lease)               └─► DEAD_LETTERED          (terminal)

  FAILED_PERMANENT / DEAD_LETTERED / SUCCEEDED
        ── explicit operator replay only ──► QUEUED, token+1
```

Terminal states have no automatic outgoing transitions. `requeue_for_replay`
is deliberately outside the automatic machine: it requires an auditable reason
from `dlq-replay` and bumps the fencing token so a stale worker cannot commit a
late result.

### 2. Fenced lease ownership

A claim is one conditional `UpdateItem` that requires the job to be claimable
and that the message identity (`run_id`, `backend`, detector config hash,
artifact id) matches the durable job record. It increments `fencing_token`
and `attempts` together and stores a fresh `attempt_id`, `lease_started_at`,
`lease_expires_at`, and `heartbeat_at`.

Every later write carries the token and attempt id:

| Operation | Condition (all must hold) |
|---|---|
| `heartbeat` | `status = RUNNING`, token/attempt match, no `lease_lost_at` |
| `mark_lease_lost` | same as heartbeat; writes `RETRYABLE_FAILED` + `lease_lost_at` |
| `fail_job` | same guard; writes `RETRYABLE_FAILED`, `FAILED_PERMANENT`, or `DEAD_LETTERED` |
| `commit_job` | same guard **plus** `lease_expires_at > now`; writes `SUCCEEDED` |

The heartbeat thread renews both the DynamoDB lease deadline and the SQS message
visibility. Configuration enforces
`visibility_timeout >= 2 * heartbeat_interval + lease_margin_seconds`, so a
single failed renewal cannot race with expiry. If renewal fails or an injected
loss occurs, `Lease` records the loss, sets `lease_lost_at`, marks the job
retryable, and pauses the worker. The message is intentionally **not** deleted:
redelivery claims a new token after the old visibility window ends.

A stale token never falls back to a different worker or backend. `GPU` jobs
require the verified v2 native ABI; a missing, corrupt, or ABI-mismatched
library is a deterministic failure, never a CPU substitution.

### 3. Validate before claiming

The consumer accepts only schema-v1 `job` messages addressed to its own
capability queue. It cross-checks the message against the authoritative job and
run records before any claim. Malformed or mismatched messages go straight to
the capability DLQ with a structured `last_error`; no job record is mutated.

Input bytes are fetched from S3 only after validation: the chunk digest and its
manifest digest are verified against the job record. A checksum mismatch is a
permanent, quarantined failure with `phase="execute"`.

### 4. Durable result commit ordering

The commit is deliberately ordered so every crash point is recoverable:

1. Write the attempt-specific result document to
   `s3://<results>/runs/{run_id}/attempts/{job_id}/{attempt_id}.json` with an
   immutable put and `sha256` metadata. The object key is unique per attempt,
   so a retried job never overwrites another attempt.
2. `HeadObject` and `GetObject` read it back; length, bytes, and metadata digest
   must all match. A mismatch is transient and is never acknowledged.
3. Write alerts with `PutItem(ConditionExpression="attribute_not_exists(alert_id)")`.
   Alert ids are deterministic, so duplicate deliveries cannot double-count.
4. Conditionally transition the job `RUNNING -> SUCCEEDED` with the active fence,
   storing the result reference, backend, and completion time. A replay of an
   already committed attempt is a no-op (`False`), not a rewrite.
5. Derive run counters by scanning the authoritative job and alert rows for the
   run, then finalize with
   `attribute_not_exists(run_completed_at)`. The first completion wins.
6. Only after all durable writes succeed, delete the SQS message.

If the process dies after step 4 but before step 6, redelivery sees a terminal
job and acks it as a duplicate. If it dies before step 4, there is no job
transition, the result object is orphaned under an unused attempt id, and the
job is reclaimed with a new fence. Orphaned attempt objects are harmless and
bounded by the number of attempts.

### 5. Outbox reconciliation

DynamoDB and SQS cannot be updated atomically. The producer/API pattern is:

1. persist the job or run with `published=false` / `ingest_published=false`;
2. send the logical message;
3. conditionally flip the flag.

A crash between 1 and 2 leaves an unpublished record; a crash between 2 and 3
leaves a duplicate in SQS. `reconcile_outbox` scans bounded pages for records
older than `reconcile_min_age_seconds`, rebuilds the **same** deterministic job
or ingest message, sends it, and then conditionally marks it published. The
mark is atomic, so concurrent reconcilers cannot inflate counters. Duplicate
publication is safe because job ids are deterministic and terminal-state
commits are no-ops.

### 6. Failure classes and recovery

* **Transient** (AWS errors, unverifiable result readback): fenced
  `RETRYABLE_FAILED` with jittered bounded backoff; the message is not acked
  until state is recorded.
* **Permanent** (schema, identity, artifact, checksum): fenced
  `DEAD_LETTERED`, structured poison message to the capability DLQ, then ack.
* **Lease lost**: no commit, `RETRYABLE_FAILED`, worker pauses, message not
  acked.
* **Operator replay**: `dlq-replay` requires a reason, a bounded limit, and a
  terminal job; it increments the replay count and the fencing token. Messages
  over `replay_max_count` are skipped back into the DLQ.

Operational commands, the failure matrix, and configuration are documented in
`docs/worker-recovery.md`.

## Consequences

* Workers are horizontally safe in-process; DynamoDB conditions are the only
  authority for ownership and terminal results.
* A lost lease is treated as an uncertain attempt, not as a permission to
  commit. This trades a little latency (visibility wait) for correctness.
* Result objects are immutable per attempt and may accumulate; lifecycle
  tooling can delete orphaned attempts, but no worker deletes another attempt's
  bytes.
* The DLQ is a quarantine plus a replay audit log, not a second work queue.
  Replay is explicit, bounded, and fenced.
* Health and Prometheus text endpoints are in-process and dependency-free;
  the S6 worker-local metric names are listed in `docs/worker-recovery.md`.

## Revisit when

* A native SDK adds real transactional outbox support.
* Result objects need retention automation beyond S3 lifecycle rules.
* A second scheduler replaces SQS redrive and can supply monotonic delivery
  receipts.
