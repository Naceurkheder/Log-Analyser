# Sentinel-GPU

**A reproducible GPU/CPU log-anomaly platform with a distributed worker plane, read-only operations console, and optional Apache Spark batch stage.**

Sentinel-GPU turns a large log object into immutable chunks, publishes capability-routed jobs, processes them with either a pinned CUDA artifact or the CPU reference, commits attempt-scoped results with fenced DynamoDB writes, and exposes the outcome through a FastAPI control plane and a Streamlit/Tailwind dashboard. The local environment uses LXD containers and moto (S3/SQS/DynamoDB) on one Linux host.

> **Status:** Sprints 1–8 are implemented and integrated on `main`. The optional Spark stage lives on `feat/spark-processing`, branched from the final release point. Saved benchmark files are evidence for the machine described in them, not a promise for every workload. Items that were not run are labelled `Not run`.

---

## What is actually implemented

| Area | Shipped code | Evidence / docs |
| --- | --- | --- |
| Run contract and API | Idempotent `POST /v1/runs`, bounded job/alert pagination, `/healthz`, `/readyz`, `/metrics`, committed OpenAPI | `docs/api/openapi.json`, `docs/decisions/0006-run-contract.md` |
| Streaming producer | Bounded S3 reads, record-boundary chunking, SHA-256 manifests, progress checkpoints, outbox publish intents | `docs/decisions/0007-streaming-chunker.md` |
| CPU detector | Python oracle + C/C++ ABI v2, golden/edge/property tests, deterministic alert identity | `docs/algorithm-semantics.md`, `docs/native-abi.md` |
| CUDA kernels | K1 matcher, K2 statistics and windowed scoring; randomized CPU/GPU parity; bounded VRAM staging | `docs/decisions/0009-native-abi-v2.md`, `docs/benchmarks/cuda-memory-pipeline.json` |
| S6 reliability | Capability-specific workers, leases, heartbeats, fencing tokens, conditional commits, DLQ inspect/replay, reconciliation | `docs/decisions/0010-worker-state-and-leases.md`, `docs/worker-recovery.md` |
| S7 artifacts/observability | Digest-pinned artifacts, metrics registry, Prometheus/Grafana, CI quality gates, repository hygiene | `docs/decisions/0011-artifact-observability.md` |
| S8 evaluation | TP/FP/TN/FN, precision/recall/F1/FPR, strata and coverage, benchmark report, demo runbook | `docs/decisions/0012-evaluation.md`, `docs/evaluation/` |
| Dashboard | Streamlit + Tailwind read-only console with live API pages and click interactions | `docs/dashboard.md`, `docs/decisions/0013-dashboard-design.md` |
| Spark batch stage | Optional pyspark job, boto3-per-task chunk fetch, deterministic attempts, S6-fenced committer | `docs/spark.md`, `docs/decisions/0014-spark-stage.md` |
| One-shot lifecycle | `scripts/sentinel.sh install/run/status/close/restart/spark` | `docs/one-shot-script.md` |

**Planned / explicitly not done here:** real AWS IAM and multi-host deployment, Spark cluster scheduling, GPU scheduling through Spark, PR-AUC, the 5-repetition workload matrix, pageable-memory baselines, and evaluation on a non-synthetic corpus.

---

## Quickstart on a fresh Linux host

Prerequisites: `python3` with venv support, `terraform`, `lxc`, `curl`, plus `docker` only for monitoring and `nvidia-smi` only for GPU execution.

```bash
# One command installs dependencies, creates .env, writes a 100 MB synthetic dataset,
# starts LXD + moto + Terraform + API + producer + workers + dashboard.
scripts/sentinel.sh install --spark
scripts/sentinel.sh run --with-gpu --monitoring

# Inspect, then close everything.
scripts/sentinel.sh status
scripts/sentinel.sh close
```

Everything is parameterised by flags or environment variables:

```bash
API_PORT=8100 DASH_PORT=8600 BACKEND=cpu \
  scripts/sentinel.sh install --spark --dataset-mb 250
API_PORT=8100 DASH_PORT=8600 \
  scripts/sentinel.sh run --monitoring
SPARK_COMMITTER=worker-fenced \
  scripts/sentinel.sh spark --run-id run_abc123
scripts/sentinel.sh close --keep-fleet
```

Full flag/env reference and troubleshooting: `docs/one-shot-script.md`. Make aliases: `make install-all`, `make run-all`, `make status-all`, `make close-all`, `make restart-all`, `make spark-batch`.

Runtime PIDs and logs live in `.local/sentinel/` (gitignored). The script only signals PIDs it recorded; it never kills unrelated processes. API and dashboard bind to `127.0.0.1` by default.

---

## Manual development commands

```bash
make up                 # LXD fleet (idempotent)
make infra              # provision moto buckets/queues/tables with Terraform
make verify             # verify queue/redrive wiring
make dataset            # 100 MB deterministic synthetic dataset + labels
make dataset-labels     # small derived dataset + separate ground-truth manifest

make test               # full Python suite (243 tests in final verification)
make test-dashboard     # Streamlit interaction tests
make test-cpu           # CPU reference correctness
make test-cpp           # native CPU/GPU parity on the fixed fixture
make test-cuda          # CUDA smoke + randomized K1/K2 + window parity (17 tests)
make bench-kernels      # 100 MB native CPU vs GPU benchmark
make cuda-memory-pipeline
make check-cmake-cpu    # CPU-only CMake build, no CUDA toolkit required
terraform fmt -check -recursive terraform && terraform -chdir=terraform validate
bash scripts/check_repo_hygiene.sh
```

Final verification on this host: `243 passed` Python tests, `17 passed` CUDA tests, native parity `PASS`, CPU-only CMake build OK, Terraform valid, repository hygiene clean. The lifecycle script was syntax- and help-tested here; the full LXD boot was not executed in this checkout.

---

## How the pipeline works

1. A client submits a run referencing an S3 object and detector configuration. The API validates it, enforces the idempotency key, records the run and publishes one ingest message. Raw log bytes never pass through the API.
2. The producer consumes `ingest-queue`, streams the source in bounded blocks, writes immutable chunks plus manifests to `raw-chunks`, checkpoints progress, and publishes one deterministic job per chunk to `jobs-cpu` or `jobs-gpu`.
3. A worker receives a job, validates its schema, chunk checksum and pinned artifact digest, then executes the CPU or CUDA detector. A lease and monotonically increasing fencing token protect the attempt.
4. The worker writes the result to `runs/{run_id}/attempts/{job_id}/{attempt_id}.json`, verifies the object, writes deterministic alerts, then conditionally commits the job and finalises the run. The SQS message is acknowledged only after that commit.
5. The API exposes run/job/alert pages; Prometheus scrapes `/metrics`; the dashboard reads those APIs and committed evidence files only.

### State and idempotency

```text
PENDING -> QUEUED -> RUNNING -> SUCCEEDED
                \      |  \
                 \     |   -> RETRYABLE_FAILED -> QUEUED
                  \    |
                   \   -> FAILED_PERMANENT / DEAD_LETTERED
```

SQS is at-least-once. Duplicate delivery is safe because `job_id`, `attempt_id`, alert IDs and result keys are deterministic, and every state change is conditional in DynamoDB. A stale worker whose lease expired cannot commit: `WorkerStore.commit_job` checks the current fencing token and active attempt ID. An outbox reconciliation command republishes jobs whose queue publish was never confirmed.

### Local topology

| Component | Address / resource | Role |
| --- | --- | --- |
| `infra` | `10.20.0.10:4566` | moto S3/SQS/DynamoDB emulator |
| `api` | `10.20.0.11:8080` | control API on the private bridge |
| `producer` | `10.20.0.12` | chunking and job publication |
| `gpu-worker-1` | `10.20.0.21` | CUDA execution, pinned artifact |
| `cpu-worker-1` | `10.20.0.22` | CPU reference execution |
| `dashboard` | `10.20.0.23` | read-only Streamlit surface |

Buckets: `incoming`, `raw-chunks`, `artifacts`, `results`. Queues: `ingest-queue`, `jobs-cpu`, `jobs-gpu` and their DLQs. Tables: `SentinelRuns`, `SentinelJobs`, `SentinelAlerts`.

This is a distributed *application* topology on one physical host: independent processes, namespaces, network identities and restart boundaries, but no host-level failover. One physical GPU is shared; containers do not create independent VRAM.

---

## CPU vs GPU: what the comparison means

The CPU detector is the correctness oracle. CUDA results must match it on match flags, per-window cell counts, means, standard deviations and anomaly records. Integer outputs match exactly; floating-point K2 scores use the documented `abs=1e-6, rel=1e-6` tolerance.

Saved kernel-scope evidence from `make bench-kernels` on the fixed 100 MB synthetic chunk:

| Path | Best time | Scope |
| --- | --- | --- |
| Native CPU detect | 2769.1 ms | pattern match + window aggregation/scoring |
| Native GPU detect | 81.5 ms | same staged input, same detector config |
| Reported speedup | 34.0x | best of three, kernel scope only |

This excludes ingestion, tokenisation, queueing, storage, commits and dashboard overhead. It is not an end-to-end platform speedup. `docs/benchmarks/cuda-memory-pipeline.json` records allocation, transfer and bounded-VRAM behaviour, including a 4.25 GiB stream deliberately larger than physical VRAM; it is a memory diagnostic, **not** a speedup claim. Small inputs, sparse patterns and host marshalling can erase the advantage.

See `docs/evaluation/benchmark-report.md` for scope rules and the explicit `Not run` list.

---

## Interactive dashboard

```bash
make dashboard          # http://127.0.0.1:8501
make dashboard-css      # rebuild committed Tailwind bundle
```

The dashboard is a read-only presentation layer: it calls FastAPI read endpoints and reads committed benchmark/evaluation JSON. It never writes to DynamoDB, SQS or S3, and it never loads the raw dataset or all alerts into the browser.

Click interactions built in:

- **Section buttons** across the top, synchronized with the sidebar radio.
- **Detector sandbox**: edit log lines, patterns, window size, baseline length and z-threshold, then click **Run detector**. It executes the same CPU oracle used by the tests and shows matches, feature cells and alerts.
- **CPU vs GPU**: checkbox expands the CUDA staging diagnostic table.
- **AWS topology**: radio switches the displayed bucket, queue and table contracts.
- **Benchmarks**: four evidence views — kernel parity, CPU baselines, CUDA staging and detection quality — with raw file paths beside each.
- **Runs & alerts**: bounded pagination and status/rule/severity filters for live API data.

Visual direction and accessibility decisions are in `docs/decisions/0013-dashboard-design.md`.

---

## Apache Spark batch stage

Spark is an **optional batch executor**, not a replacement for S6 workers. It is implemented on `feat/spark-processing`, created from the final `main`/`v1.0-evaluated-release` point. Older CUDA branches and `v0.5-cuda-kernels` were deliberately not used because Spark depends on the final chunk manifest, job identity, artifact identity and observability contracts.

```bash
scripts/sentinel.sh install --spark
scripts/sentinel.sh spark --run-id run_abc123

# Or directly:
PYTHONPATH=src python -m sentinel.spark.cli plan --run-id run_abc123
PYTHONPATH=src python -m sentinel.spark.cli run --run-id run_abc123 \
  --master 'local[*]' --limit 200 --committer worker-fenced
```

Design points:

- `pyspark>=3.5,<4` is isolated in `requirements-spark.txt`; the package imports without it.
- The driver pages `SentinelJobs` by `run_id-index` and selects bounded `PENDING`/`QUEUED` CPU jobs. It never scans the whole table.
- Each task builds its own boto3 S3 client, fetches the chunk, verifies size/SHA-256 and runs the CPU detector. No Hadoop S3A connector is required, so local mode works with moto.
- `attempt_id` is derived from job ID, source hash and detector config hash — no wall clock. The result object matches the worker's attempt-scoped layout.
- Default `logging` committer is advisory. `worker-fenced` claims jobs with `WorkerStore.claim_job`, commits with the fencing token, writes alerts, finalises the run and deletes the already-dispatched SQS messages for committed jobs. A failed batch returns claims to `RETRYABLE_FAILED`.
- Spark has no per-task heartbeat and no DLQ. Pause normal workers during a batch; GPU-routed jobs are not selected.

Final verification used an injected Spark session and moto-backed boto3 tasks; a real `SparkSession` was not started because pyspark is not installed in this environment. Details: `docs/spark.md`, ADR-0014.

---

## API surface

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/runs` | Create or replay an idempotent run; requires `Idempotency-Key` |
| `GET /v1/runs/{run_id}` | Run state and aggregate progress |
| `GET /v1/runs/{run_id}/jobs` | Cursor-paginated job pages |
| `GET /v1/runs/{run_id}/alerts` | Cursor-paginated alert samples |
| `GET /healthz` | Liveness |
| `GET /readyz` | Dependency readiness |
| `GET /metrics` | Prometheus exposition (excluded from OpenAPI to preserve the committed contract) |

The committed schema is `docs/api/openapi.json`; regenerate with `make api-openapi` only when the public contract intentionally changes.

---

## Evaluation and evidence

`python -m sentinel.evaluation.cli evaluate --labels ... --detections ... --out ...` joins labels and alerts by `(window_id, service_id, ip, status, rule_id)`, keeps natural and synthetic strata separate, and reports undefined rates as `N/A` instead of zero. The small committed sample is synthetic: TP=4, FP=4, FN=13, precision=0.500, recall=0.235, F1=0.320, FPR=`N/A`; four `event_rate_burst` scenarios are explicitly unresolved because their pattern is not in the rules fixture.

Generated reports live in `docs/evaluation/`. The demo and chaos procedures are in `docs/demo-runbook.md`.

---

## Branch and release map

| Branch / tag | Content |
| --- | --- |
| `main` + `v0.5-cuda-kernels` | S1–S5 baseline, CUDA kernels and bounded staging evidence |
| `feat/worker-consumers`, `feat/job-state-and-leases`, `feat/outbox-reconciliation`, `test/failure-recovery` → `v0.6-reliable-distributed-workers` | S6 worker reliability |
| `feat/artifact-pipeline`, `feat/observability`, `feat/dashboard`, `ci/quality-gates` → `v0.7-observability-artifacts` | S7 artifacts, metrics, dashboard, CI |
| `feat/benchmark-harness`, `docs/benchmark-report`, `docs/demo-runbook` → `v1.0-evaluated-release` | S8 evaluation and final release |
| `feat/spark-processing` | Optional Spark batch stage from the final release point |

Decision records are in `docs/decisions/`. The private execution plan is intentionally gitignored.

---

## Security and operational boundaries

- Keep moto, worker metrics and internal service endpoints on the private LXD bridge.
- Bind development-facing host ports to loopback unless an explicit network security model says otherwise.
- Use dummy local credentials from `.env.example`; never commit `.env`, datasets or PID/log state.
- Queue messages carry object references and metadata only, never raw log text.
- Validate schema versions, manifests, checksums and artifact digests before execution.
- Do not promote run IDs, job IDs or source addresses into Prometheus labels.
- moto is an emulator: passing tests here is not proof of real AWS behaviour, quotas or IAM semantics.
- Synthetic anomalies support controlled experiments; they do not establish real-world detection quality.

---

## Repository layout

```text
src/sentinel/
  api/           control API, store, contracts, metrics route
  producer/      bounded chunker, manifests, outbox dispatch
  detector/      CPU oracle, semantics, native ABI wrappers
  worker/        leases, fencing, result commits, DLQ, reconciliation, CLI
  artifacts/     versioned artifact publish/verify
  metrics/       bounded metrics registry and exporter
  evaluation/    detection-quality metrics and report generation
  dashboard/     Streamlit/Tailwind read-only console
  spark/         optional batch executor and S6-fenced committer
tests/           cpu, cuda, api, producer, worker, artifacts, metrics,
                 dashboard, evaluation, spark, unit, fault_injection
docs/            decisions, benchmarks, evaluation, runbooks
lxd/ terraform/ monitoring/ scripts/
```

---

## Known limitations

- Single physical host and one GPU; no host-level failover or GPU hardware partitioning.
- moto is not a substitute for a real AWS integration and IAM review.
- CUDA speedup is workload-dependent and measured here only for one synthetic 100 MB chunk at kernel scope.
- One shared GPU plus unrecorded thermal/clock variation limits benchmark repeatability.
- Spark local mode has no cluster scheduler, autoscaling or cross-host shuffle contract.
- The detector detects only configured patterns and statistically unusual behaviour in the supplied data; it is not a general security product.
