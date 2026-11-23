# Optional Spark batch stage

Sentinel's durable execution path is the S6 SQS worker fleet. The Spark stage
in `sentinel.spark` is an **optional batch executor**: it replays or sweeps a
large bounded set of already-published chunks in parallel and writes the same
attempt-scoped result objects the workers write. It never replaces worker
fencing, lease handling, alert ordering or run finalisation.

## When to use it

* Large one-off replay: reprocess every `QUEUED`/`PENDING` job of a run after a
  detector-config correction, without re-publishing the dataset.
* Batch backfill / verification: run a bounded sweep over a run and compare
  attempt outputs with worker-produced results.
* Local-mode parallelism on one host (default `local[*]`) when a run has many
  chunks and the CPU oracle dominates wall time.

Do **not** use it for steady-state ingestion. The `logging` committer is
advisory only; the `worker-fenced` committer uses the S6 conditional store for
leases and commits, but it has no per-task heartbeat and no DLQ. Pause the
normal workers while a Spark batch runs so the two executors do not race for the
same jobs. Workers remain the durable steady-state path.

## Install

pyspark is an optional dependency and is intentionally absent from
`requirements.txt`:

```sh
source .venv/bin/activate
pip install -r requirements.txt          # unchanged core environment
pip install -r requirements-spark.txt    # only where Spark is needed
```

The package imports without pyspark. `plan` and every helper in
`sentinel.spark.core` work in the base environment; only `run` requires the
optional install and reports a clear error otherwise.

## Commands

From the repository root:

```sh
# Read-only: show the exact job refs a run would execute (no pyspark needed).
PYTHONPATH=src python -m sentinel.spark.cli plan --run-id run_...

# Execute a bounded batch (defaults: local[*], limit 200, results bucket "results").
PYTHONPATH=src python -m sentinel.spark.cli run \
  --run-id run_... \
  --master 'local[*]' \
  --limit 200 \
  --endpoint-url http://10.20.0.10:4566 \
  --results-bucket results \
  --committer worker-fenced
```

`plan` prints one JSON object with `run_id`, `backend`, `jobs_selected` and
`job_refs` (the projected immutable job references, including chunk bucket/key,
checksum, size and starting line number). `run` prints a JSON summary with the
per-attempt result key and sha256. Both write structured log lines with fixed
event names and `run_id`/`job_id`/`attempt_id` fields.

`--committer worker-fenced` claims each planned job with a fencing token before
execution, then commits the result, alerts and run state after the immutable
attempt object is written. `--committer logging` (the CLI default) writes only
the attempt object. The one-shot script defaults to `worker-fenced`.

Exit codes: `0` success, `2` verification/config/store error (including chunk
checksum mismatch), `3` missing pyspark, `1` any other failure.

## Configuration

The stage reuses `sentinel.api.config.Settings` (so the existing
`AWS_*`/`SENTINEL_*` resource names, table names and credentials apply) and adds:

| Variable | Default | Meaning |
|---|---|---|
| `AWS_ENDPOINT_URL` | `http://10.20.0.10:4566` | moto/LocalStack endpoint; `--endpoint-url` overrides it for both the store and task clients |
| `SENTINEL_RESULTS_BUCKET` | `results` | bucket for attempt objects; `--results-bucket` overrides it |
| `SENTINEL_SPARK_MASTER` | `local[*]` | Spark master; `--master` overrides it |
| `SENTINEL_SPARK_APP_NAME` | `sentinel-spark` | Spark application name |
| `SENTINEL_SPARK_SHUFFLE_PARTITIONS` | `8` | `spark.sql.shuffle.partitions` |
| `SENTINEL_SPARK_MAX_PARALLELISM` | `8` | `spark.default.parallelism` and the `sc.parallelize` slice cap |
| `SENTINEL_SPARK_PAGE_SIZE` | `100` | DynamoDB `query` page limit (max 1000) |
| `SENTINEL_SPARK_MAX_PAGES` | `10` | hard cap on query pages; combined with `--limit` this prevents unbounded scans |
| `SENTINEL_SPARK_LIMIT` | `200` | maximum jobs selected; `--limit` overrides it |
| `SENTINEL_SPARK_COMMITTER` | `logging` | result committer: `logging` or `worker-fenced` (the lifecycle script sets `worker-fenced`) |
| `SPARK_LOCAL_IP` | unset | when set, pins Spark driver host/bind address |

The driver does one `get_run` and a bounded sequence of
`SentinelStore.query_jobs` calls using the `run_id-index` GSI. It never calls
`scan`. Selection is restricted to `PENDING` and `QUEUED` jobs whose
`detector_config_hash` matches the run's detector configuration and whose
`backend` is `cpu`; GPU-routed jobs stay with the native worker path.

## Data flow and determinism

1. Driver loads the run's detector config and a bounded page of eligible jobs.
2. Each Spark task builds **its own boto3 S3 client** from the explicit
   endpoint/credentials, downloads `chunk.key`, verifies size and sha256 from
   the job item, and runs the CPU detector. There is no Hadoop S3A dependency.
3. The task returns a compact result: `job_id`, `run_id`, `chunk_id`,
   `actual_backend="spark-cpu"`, `algorithm_version`, `metrics`, `alerts`,
   `output_sha256` and `attempt_id`.
4. The driver writes each result to
   `s3://<results>/runs/{run_id}/attempts/{job_id}/{attempt_id}.json` with
   create-if-absent semantics, verifies the head/metadata (and an existing body
   on replay), then invokes the result committer.

`attempt_id = "att-" + sha256(canonical_json({v, job_id, source_sha256,
detector_config_hash}))[:24]`. No wall clock is involved, so replay of the
same job produces the same object key. `output_sha256` fingerprints the
canonical detection payload. The result object body is canonical JSON plus a
trailing newline, with its own digest in the `sha256` metadata field — the same
shape as `sentinel.worker.result`.

## Result commit boundary

`ResultCommitter` has three hooks: `prepare` (before tasks), `commit` (per
result), and `abort` (on a failed batch).

- `LoggingCommitter` is the CLI default. It only logs
  `spark_result_not_committed` and returns `{"committed": false, ...}`. Treat
  its attempt objects as batch evidence, not as worker acknowledgements.
- `FencedWorkerCommitter` (`--committer worker-fenced`) requires a
  `WorkerStore`. Before submission it calls `claim_job` for every planned CPU
  job, receiving a conditional `fencing_token`. After the attempt object is
  verified, it calls `commit_job` with that token, writes alerts idempotently,
  finalises the run when all jobs are terminal, and deletes the already
  dispatched SQS messages for the committed job IDs. If a task fails, claimed
  jobs are moved to `RETRYABLE_FAILED` so the worker path can redeliver them.

A stale attempt cannot commit: `WorkerStore.commit_job` checks the fencing
token and active attempt ID in DynamoDB, not just the local state. Bounded queue
draining prevents an old SQS message from later failing a claim against a job
Spark has already completed.

## Limitations

* Single-host batch by default (`local[*]`); the parallelism cap and bounded
  page size keep executor count and memory predictable. No cluster scheduler,
  autoscaling or cross-host shuffle contract is provided.
* Verified against moto/S3 only. Real AWS S3 and DynamoDB should use the same
  boto3 path, but no ECS/IAM role story is implemented here.
* No Hadoop S3A, no `spark.read` on S3, and no filesystem connector setup.
  Chunk bytes always come from boto3 inside the task.
* No GPU scheduling through Spark. `actual_backend` is always `spark-cpu`;
  GPU work stays with the native worker path, and GPU-routed jobs are not
  selected by the driver.
* Chunk checksum/size verification failures abort the stage with a non-zero
  exit. The fenced committer releases claimed jobs as `RETRYABLE_FAILED`, but
  this optional path has no per-task heartbeat or DLQ of its own.
