# ADR-0014 — Optional Spark batch execution stage

* Status: accepted
* Date: 2026-11-23
* Context: Sprints 1–8 define immutable chunks, job identity, artifact identity
  and bounded observability. A batch/parallel replay use case remains: a run may
  contain many `QUEUED`/`PENDING` jobs that need a one-off sweep or replay. The
  repository has CUDA/WIP branches and a pre-integration S5 tag; choosing the
  wrong base would force the Spark stage to invent contracts that only exist
  after S6–S8.

## Decision: branch from the final release point

Implement the Spark stage on a dedicated branch named `feat/spark-processing`,
created from `v1.0-evaluated-release` / the final `main` **after** the current
S6/S7/S8 sprint integration. It is explicitly **not** based on
`cuda/k1-pattern-matcher`, `cuda/k2-window-statistics`,
`perf/cuda-memory-pipeline`, or the S5 tag.

Rationale:

* Spark consumes S3 chunk manifests and the job record written by the S3
  producer; the producer's final shape is part of the integrated mainline.
* Job identity (`job_id_for`), artifact identity (S7) and the detector config
  hash are the inputs to the deterministic attempt id. Building on a CUDA
  branch would couple the batch stage to unmerged GPU work and to an older
  artifact contract.
* Result commit boundary and observability naming are defined by S6/S7. The
  Spark stage must adapt to those contracts, not fork them.
* Keeping the work on its own branch keeps optional pyspark out of core CI and
  out of the durability path.

## Data flow

1. Driver (`sentinel.spark.job.plan_jobs` / `run_spark_job`) reads the run once
   and pages `SentinelStore.query_jobs` by `run_id-index`, capped by
   `SENTINEL_SPARK_PAGE_SIZE`, `SENTINEL_SPARK_MAX_PAGES` and `--limit`. No
   `scan`, no unbounded collection. Only `PENDING`/`QUEUED` CPU-routed jobs with
   a matching detector config hash are selected; GPU-routed jobs stay with the
   native worker path.
2. `sc.parallelize(refs, numSlices=min(max_parallelism, len(refs)))` distributes
   immutable `JobRef` values.
3. Each task constructs its own boto3 S3 client from explicit endpoint, region
   and credentials (`S3Connection`), calls `load_chunk` (head size + metadata
   check, then full-body sha256/size check), and runs `process_job_ref` against
   the CPU reference pipeline.
4. The driver writes `runs/{run_id}/attempts/{job_id}/{attempt_id}.json` to the
   results bucket with create-if-absent semantics and verifies head/metadata;
   a replay of an existing key verifies its bytes rather than overwriting them.
5. Before task submission, `FencedWorkerCommitter.prepare(...)` claims each
   planned job with `WorkerStore.claim_job` and records the fencing token.
   After task completion the driver calls `ResultCommitter.commit(...)`;
   `LoggingCommitter` remains the CLI default for advisory replay, while the
   lifecycle script selects `worker-fenced`.

## Determinism

* `attempt_id = "att-" + sha256(canonical_json({v: 1, job_id, source_sha256,
  detector_config_hash}))[:24]`. No timestamps, hostnames, Spark partition
  ids or task attempt numbers participate.
* `output_sha256` fingerprints canonical JSON of the detection payload
  (identity fields, algorithm version, sorted metrics and sorted alerts).
* The result object body is `canonical_json(result) + b"\n"`; its digest is
  stored in the object's `sha256` metadata alongside the worker-compatible
  `run-id`, `job-id`, `attempt-id` and `backend` fields.
* Detector settings are rebuilt from the run's detector config in every task
  and checked against the job's `detector_config_hash`. A mismatch fails the
  stage instead of silently computing with the wrong rules.

## Why boto3 per task instead of Hadoop S3A

* Spark runs locally against moto (`http://10.20.0.10:4566`). S3A would require
  Hadoop AWS jars, endpoint/credential bridging and filesystem provider
  configuration that moto local mode does not provide.
* boto3 is already a core dependency and `SentinelStore` already uses the same
  path-style, retry and endpoint settings. One client per task avoids sharing a
  non-thread-safe client across executors and makes the verification boundary
  explicit.
* The cost is that chunk bytes are materialised in the task; chunk sizes are
  already bounded by the S3-B chunker, and the driver bounds the number of
  tasks.

## Optional dependency policy

* `pyspark>=3.5,<4` lives only in `requirements-spark.txt`; core
  `requirements.txt` is unchanged.
* `sentinel.spark.core` and `sentinel.spark.config` never import pyspark;
  `sentinel.spark.job` imports it lazily inside `build_spark_session`. The
  package, the CLI `plan` mode and the tests import cleanly without pyspark.
* Missing pyspark is a distinct CLI exit code with an install command, not an
  import-time failure of the whole `sentinel` package.

## Result commit boundary

`ResultCommitter` is an explicit protocol with `prepare`, `commit` and `abort`
hooks. The default `LoggingCommitter` logs `spark_result_not_committed` and
returns `committed: false`, so advisory replay never forges a worker
acknowledgement. The integrated `FencedWorkerCommitter` uses the S6 store for
the real path: conditional `claim_job` before execution, `commit_job` with the
fencing token after the immutable object is verified, idempotent alert writes,
run finalisation, and bounded deletion of the already-dispatched SQS messages.
A failed batch moves claimed jobs to `RETRYABLE_FAILED` via `fail_job`, leaving
them eligible for normal worker redelivery.

## Consequences

* Large replay is possible without modifying S3 manifests or job records.
* Attempt keys are collision-free across replays with different source/config
  inputs and identical across replays with the same inputs.
* Stale Spark attempts cannot overwrite newer worker state: DynamoDB conditions
  on the fencing token and active attempt ID are authoritative.
* Spark has no per-task heartbeat and no DLQ of its own, so normal workers must
  be paused during a batch and the batch size must fit the lease window.
* Workers remain the authoritative steady-state execution path; Spark is an
  optional batch executor with the same result-object contract.
