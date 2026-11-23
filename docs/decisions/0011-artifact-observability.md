# ADR-0011 — Immutable artifact identity and bounded observability

* Status: accepted (Sprint 7 / S7)
* Date: 2026-11-23
* Context: ADR-0009 freezes native ABI v2 and documents two build outputs
  (`libsentinel_cpu.so`, `libsentinel.so`). Sprints 1–6 produce artifacts,
  runs, chunks and jobs, but nothing yet pins the *exact native bytes* used by
  a worker or exposes fleet health without leaking run/job/source identifiers
  into metric labels. A metric label containing a run id is a cardinality
  bomb on a 16 GB laptop fleet; an unverified `.so` is a silent ABI hazard.

## Decision

### 1. Artifact identity

The identity of a native library is the SHA-256 of its bytes. The publish
side (`sentinel.artifacts.publish`) computes the digest and writes an
immutable `ArtifactManifest` next to the copied library:

```
artifacts/<sha256[:12]>/libsentinel.so
artifacts/<sha256[:12]>/libsentinel.so.json
```

The same relative keys are used in the `artifacts` S3 bucket when `--bucket`
(and optionally `--endpoint-url`) is given. A publisher **never overwrites**
an existing local file or object key: a repeated publish verifies that the
existing bytes have the expected digest and reports them as `existing`. A
different byte string never reuses the content-addressed directory.

The frozen `ArtifactManifest` carries: `build_id`, `git_commit`, `abi_version`
(default `2`), `compiler_version`, `cuda_version`, `target_arch`,
`build_flags`, `test_results`, `sha256`, `size_bytes`, `created_at`, and
`artifact_name`. Lists are stored as tuples; JSON is canonical (sorted keys,
no whitespace), so the manifest itself is reproducible apart from its
creation timestamp. `sha256[:12]` is the short `artifact_id` that may be
logged or embedded in job ids.

Example:

```sh
PYTHONPATH=src python -m sentinel.artifacts.publish \
  --library build/libsentinel.so --out-dir artifacts \
  --bucket artifacts --endpoint-url http://10.20.0.10:4566 \
  --test-result pytest=passed --test-result cuda-parity=passed

PYTHONPATH=src python -m sentinel.artifacts.publish \
  --verify artifacts/<sha256[:12]>/libsentinel.so \
  --expected-sha256 <64-hex>
```

`--verify` (also exposed through `scripts/publish_artifact.py`) checks the
digest, the size, the manifest ABI version and, when the file is a loadable
ELF library, `sentinel_abi_version()`. It exits `0` with a JSON result on
success and `1` with `{"ok": false, "error": ...}` on any mismatch.

### 2. Worker verification contract

A worker must verify **the exact bytes it will load** before calling any
native symbol:

1. Download/copy `artifacts/<sha[:12]>/<name>` and its `.json` sidecar.
2. Compute SHA-256 of the library and require equality with `manifest.sha256`.
3. Require `manifest.abi_version == 2` *and* call `sentinel_abi_version()`
   via `ctypes` when the library can be loaded on that host. A disagreement
   between the manifest and the native library is a fatal error, never a
   fallback.
4. On mismatch, fail the job with a bounded `error_class` (`cuda`, `oom`,
   `io` or `unknown`) and do not retry with a different artifact.

`sentinel.artifacts.verify.verify_artifact` implements this contract on the
publish side and may be reused by workers; a worker-local minimal check must
enforce the same two invariants (digest equality, ABI v2). The build id is
diagnostic only and is never parsed.

### 3. Metric taxonomy

Metrics are rendered without third-party libraries as Prometheus text
(`text/plain; version=0.0.4; charset=utf-8`). `MetricsRegistry` is thread-safe,
uses fixed histogram buckets (no runtime bucket synthesis) and permits only
these label names/values:

| Label | Allowed values |
|---|---|
| `service` | `api`, `producer`, `gpu-worker`, `cpu-worker` |
| `backend` | `gpu`, `cpu`, `auto`, `none` |
| `result` | fixed outcomes (`ok`, `ready`, `not_ready`, `success`, `failure`, `retry`, `queued`, `rejected`, `dropped`, `pending`, `dlq`, `rss`, `limit`, `peak`, `used`) plus a closed set of `<route>:<METHOD>:<2xx\|3xx\|4xx\|5xx\|other>` values |
| `error_class` | `none`, `validation`, `not_found`, `conflict`, `http_error`, `dependency`, `internal`, `cuda`, `oom`, `io`, `unknown` |

**Never** add `run_id`, `job_id`, `chunk_id`, `source`, `key`, `path`,
`rule_id`, `idempotency_key` or a raw request path as a label. HTTP route
templates and status classes are encoded in the bounded `result` value using
a fixed route table, so `GET /v1/runs/{run_id}` is safe while
`/v1/runs/run_abc123` never becomes a series.

API metrics implemented in S7:

* `sentinel_http_requests_total{service,result,error_class}` — one increment
  per HTTP response, including the route template/status class.
* `sentinel_api_readiness{service,result,error_class}` — gauge set to `1` on a
  successful `/readyz` and `0` on failure.

Worker metric names are fixed here so the S6 workers can expose compatible
names without editing S7 files:

| Metric | Type | Labels / intent |
|---|---|---|
| `sentinel_worker_jobs_total` | counter | `service,backend,result,error_class`; completed/failed/retried jobs |
| `sentinel_worker_job_duration_seconds` | histogram | same labels; fixed buckets from the registry |
| `sentinel_worker_heartbeat_timestamp_seconds` | gauge | `service,backend,result="ok"`; Unix timestamp of last progress |
| `sentinel_worker_queue_depth` | gauge | `service,backend,result="pending"\|"dlq"` |
| `sentinel_queue_oldest_pending_seconds` | gauge | `service="producer",backend,result="pending"` |
| `sentinel_worker_dlq_total` | counter | `service,backend,result="dlq",error_class` |
| `sentinel_worker_cuda_errors_total` | counter | `service="gpu-worker",backend="gpu",result="failure",error_class="cuda"\|"oom"` |
| `sentinel_worker_outbox_backlog` | gauge | `service="producer",backend,result="pending"` |
| `sentinel_worker_memory_bytes` | gauge | `service,backend,result="rss"\|"limit"` |
| `sentinel_worker_gpu_memory_bytes` | gauge | `service="gpu-worker",backend="gpu",result="used"\|"peak"\|"limit"` |

`monitoring/alerts.yml` and `monitoring/grafana/dashboard.json` use only these
names. Prometheus scrapes every 15 seconds with
`--storage.tsdb.retention.time=12h`; targets are `api:8000`,
`producer:8081`, `gpu-worker-1:8082` and `cpu-worker-1:8083`.

### 4. What CI does and does not test

`.github/workflows/ci.yml` runs on every push and pull request with Python
3.11. It installs runtime + dev requirements, runs
`pytest tests/api tests/producer tests/unit tests/cpu tests/artifacts tests/metrics`,
builds the CPU-only CMake target, checks `terraform fmt/validate`, runs
`scripts/check_repo_hygiene.sh` and uploads a JUnit XML report. Hygiene uses
`git ls-files` and fails on tracked `.env` files, Terraform state/plans,
generated `data/`/`results/` files, compiled binaries and private plan names;
ignored local build outputs are allowed.

CI **does not** run CUDA tests or benchmarks. Those need CUDA 12.4, a
compatible host compiler and an `sm_86` GPU, and are dispatched manually via
`.github/workflows/gpu-benchmarks.yml` on a self-hosted runner labelled
`cuda`. Saved evidence in `docs/benchmarks/` is never regenerated by CPU CI.

## Consequences

* A published artifact can be reproduced from its manifest fields, and a
  stale or corrupted library is rejected before any native call.
* Metric series count is bounded even under hostile run ids or arbitrary
  URLs; dashboards can be written once without a label whitelist.
* The API's OpenAPI document is unchanged: `/metrics` is intentionally not in
  the schema, so the committed `docs/api/openapi.json` contract test still
  passes.
* Adding a new metric label value requires updating the allowlist in
  `sentinel.metrics.registry` and this ADR in the same change.
