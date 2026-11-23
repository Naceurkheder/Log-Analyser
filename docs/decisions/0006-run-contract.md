# ADR-0006 — Run API contract, idempotency, and cursor pagination

* Status: accepted
* Date: 2026-11-22
* Deciders: project owner, agent

## Context

Sprint 3 introduces the platform boundary. Before chunking, workers, or
detection semantics exist, the system needs one stable contract for:

* how a dataset is referenced without sending log bytes through JSON,
* how retries of the same submission avoid duplicate runs,
* how job and alert collections are read without unbounded payloads,
* how failures are reported so clients can act on them.

The API also has to run both locally (uvicorn on the host) and inside the
`api` LXD container, against the emulated AWS plane, without code changes.

## Decision

### Request contract

* `POST /v1/runs` accepts a versioned `RunRequest`
  (`schema_version = 1`) with exactly:
  * `dataset`: an object reference (`bucket`, `key`, optional `size_bytes`,
    optional `sha256`, encoding/format). The bucket must be the configured
    `incoming` bucket; raw log content is rejected because every model sets
    `extra = "forbid"`.
  * `detector`: 1–64 rules with unique `rule_id`s, a single-line pattern
    (≤256 UTF-8 bytes), severity, window size, baseline length, z-score
    threshold, and max line length — all bounded.
  * `backend_policy`: `gpu`, `cpu`, or `auto`.
* The dataset object is checked with `HeadObject`; the authoritative size is
  stored. A declared size mismatch, an empty object, a missing object, or a
  size above `SENTINEL_MAX_DATASET_BYTES` (5 GiB default) is rejected with a
  machine-readable error code.
* The detector configuration is canonicalised (sorted JSON) and hashed. The
  hash is stored on the run and will be part of deterministic job IDs in
  Sprint 3-B.

### Idempotency

* `Idempotency-Key` is a required header, 8–128 characters of
  `[A-Za-z0-9._:-]`.
* `run_id = "run_" + sha256("sentinel-run-v1:" + key)[:32]`. The derivation
  lives in `src/sentinel/ids.py` so the API, producer, and future workers
  cannot drift.
* Run creation is a conditional `PutItem` (`attribute_not_exists(run_id)`).
  The canonical request fingerprint is stored with the run.
  * same key + same fingerprint → `200`, original run, no second ingest
    message;
  * same key + different fingerprint → `409 idempotency_key_reuse`;
  * new key → `201`.
* The ingest message is published after the record is written. If publishing
  fails the run is marked `INGEST_FAILED` and the request returns `503`; a
  replay with the same key retries publication and returns `200` once it
  succeeds. Messages contain references and metadata only.

### Status and pagination

* Run states: `PENDING`, `QUEUED`, `CHUNKING`, `RUNNING`, `SUCCEEDED`,
  `FAILED`, `CANCELLED`, `INGEST_FAILED`.
* Jobs and alerts are queried through `run_id-index` with the item id as range
  key (`job_id` / `alert_id`). This is why ADR-0005 made the range keys the
  item ids: page order is deterministic.
* Pagination is cursor-based. The cursor is base64url-encoded JSON of the
  DynamoDB `LastEvaluatedKey`, is opaque to clients, is validated on decode,
  and is scoped to the collection. `limit` is 1–200 (default 50). No endpoint
  scans a table.
* Unknown runs return `404` before a query, so an empty page cannot be
  confused with a missing run.

### Errors, logging, and documentation

* Every failure uses one envelope:
  `{"error": {"code", "message", "details", "request_id"}}`.
* A middleware generates a request id, returns it as `X-Request-ID`, and logs
  one JSON line per request with method, path, status, duration, and request
  id. Headers, bodies, and raw log content are never logged.
* `/healthz` reports process liveness. `/readyz` checks all three DynamoDB
  tables, the `incoming` bucket, and that the ingest queue exists; it returns
  `503` when any dependency is missing.
* The OpenAPI document is generated from the implementation and committed at
  `docs/api/openapi.json`; a test fails if the file is stale.

### Operator path

* `sentinel.api.cli` (also installed as `sentinelctl`) provides `upload`,
  `submit`, `submit-file` (upload + submit), `status`, `jobs`, `alerts`,
  `serve`, and `openapi`.
* When no idempotency key is supplied, the CLI derives one from the canonical
  request body, so re-running the same command is safe.
* The ingest queue may be configured by URL (`SENTINEL_INGEST_QUEUE_URL`) or
  by name (`SENTINEL_INGEST_QUEUE`, default `ingest-queue`); the name path
  resolves the URL at runtime because moto's account id is not fixed.

## Consequences

* Sprint 3-B must consume ingest messages idempotently using `run_id` and
  `request_hash`; duplicate publication is allowed and expected on retries.
* Changing detector semantics or bounds requires a schema-version bump (or an
  algorithm-version field once Sprint 4 defines it). Silently widening the
  contract is a review rejection.
* The alert response model is intentionally permissive until Sprint 4 defines
  the canonical alert schema; fields are additive and extra fields are
  preserved.
* The `api` and `producer` containers add 1 GiB of caps (total 5.5 GiB), well
  inside ADR-0001's 16 GB budget.
* The API cannot create a run if object storage is unavailable; this is
  deliberate — accepting references that do not exist would defer a
  deterministic validation error into the worker.
* Readiness checks the queue by resolving it. A URL that points at a deleted
  queue therefore fails readiness instead of silently accepting messages.

## Revisit when

* Sprint 4 fixes the alert schema and algorithm version.
* Sprint 6 adds the outbox: publication will move from "after create" to a
  durable pending-dispatch record, and the `INGEST_FAILED` retry path can be
  simplified.
* Sprint 7 adds Prometheus metrics; `/metrics` is intentionally absent today.
* Real AWS checksum semantics are exercised, which may justify verifying the
  declared `sha256` at submission time.
