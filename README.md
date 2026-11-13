# Sentinel-GPU

**A distributed, GPU-accelerated pipeline for log anomaly detection.**

Sentinel-GPU processes large system-log datasets as independent jobs across a heterogeneous worker pool. A CUDA implementation accelerates selected detection stages, while a CPU implementation provides a deterministic reference for correctness and performance comparison. The platform combines asynchronous job execution, object storage, infrastructure as code, container isolation, and operational observability.

> **Project status:** Active development. This README describes the target architecture and intended execution model. A component should be considered implemented only when its code, configuration, and corresponding tests are present in the repository.

## Overview

The pipeline accepts a dataset, divides it into bounded chunks, routes each chunk to a worker with the required execution capability, and persists detection results for evaluation and inspection.

Core engineering objectives:

- **GPU computing:** hand-written CUDA kernels for parallel pattern matching and sliding-window anomaly statistics.
- **Distributed execution:** asynchronous jobs, capability-aware queues, worker leases, heartbeats, retries, and dead-letter queues.
- **Correctness:** a CPU reference implementation with deterministic output semantics for validating CUDA results.
- **Infrastructure as code:** Terraform-managed S3, SQS, and DynamoDB resources through LocalStack for local development.
- **Isolation:** independently operated services connected over a private LXD network.
- **Observability:** metrics and dashboards for queue health, processing latency, throughput, worker status, and evaluation results.
- **Reproducibility:** versioned detector artifacts, immutable input chunks, checksums, explicit configurations, and repeatable benchmarks.

## Architecture

![Sentinel-GPU architecture](docs/assets/architecture.png)

The deployment is organized into three logical planes.

### Control and build plane

| Component | Responsibility |
|---|---|
| Terraform / LXD provisioning | Creates the local network, service instances, profiles, and emulated cloud resources. |
| CUDA build pipeline | Compiles CUDA sources, runs unit and parity tests, and creates versioned artifacts. |
| Artifact registry | Stores immutable worker artifacts with version and checksum metadata. |

Build and provisioning tasks are separate from job execution. Workers consume a pinned artifact; they do not compile CUDA code at runtime.

### Data plane

| Component | Responsibility |
|---|---|
| FastAPI gateway | Validates run requests, enforces idempotency, and exposes run and result APIs. |
| Ingestion queue | Decouples request acceptance from dataset processing. |
| Producer / chunker | Streams input data, splits it at record boundaries, creates manifests, and routes jobs by backend capability. |
| Object storage | Stores source datasets, immutable chunks, build artifacts, and result objects. |
| Capability queues | Separate GPU and CPU work so a worker does not repeatedly receive a job it cannot execute. |
| GPU worker | Downloads the pinned artifact and chunk, executes CUDA kernels, and commits results. |
| CPU worker | Runs the equivalent CPU path for baseline measurements, fallback policies, and correctness comparison. |
| Metadata store | Persists run state, job state, attempt metadata, and alert records. |

The local deployment uses LocalStack to provide S3-, SQS-, and DynamoDB-compatible APIs. Application components communicate through service endpoints and APIs rather than shared process memory or a shared application filesystem.

### Observability plane

| Component | Responsibility |
|---|---|
| Prometheus | Scrapes service and worker metrics and evaluates alert rules. |
| Grafana | Displays operational dashboards and service health. |
| Web dashboard | Presents run status, alert samples, and benchmark summaries through the API. It is a read-only presentation layer, not a source of truth. |

### Deployment model

The reference environment runs on one Linux host using LXD containers and a private bridge network. It is a **distributed application topology on a single physical host**, not a multi-host cluster. The separation provides independent process namespaces, network identities, resource controls, and restart boundaries, but it does not provide host-level fault tolerance.

The design targets one physical CUDA-capable GPU. The default execution model is one GPU worker with bounded job concurrency. Multiple containers attached to the same device do not create independent GPUs or independent VRAM isolation; concurrent GPU execution must be treated as a measured scheduling experiment, not as hardware partitioning.

## Processing lifecycle

1. A client submits a run request referencing an input object and a detector configuration.
2. The API validates the request and creates or returns the run associated with the supplied idempotency key.
3. An ingestion message is published so the API does not have to stream or chunk a large dataset synchronously.
4. The producer reads the object incrementally, splits it at record boundaries, and writes immutable chunk objects and manifests.
5. The producer creates job records and durable publish intents. An outbox dispatcher publishes pending jobs to the appropriate execution queue and reconciles unfinished publications.
6. A worker receives a job, validates its manifest and checksum, retrieves the pinned detector artifact, and processes the chunk.
7. The worker writes attempt-scoped output objects, conditionally commits the logical result and alert metadata, and acknowledges the queue message only after the durable commit succeeds.
8. The API exposes run state and paginated results. The evaluation tooling calculates detection and performance metrics from the run manifest and persisted outputs.

### Job state model

A job follows an explicit state machine:

```text
PENDING -> QUEUED -> RUNNING -> SUCCEEDED
                    |    |
                    |    +------> RETRYABLE_FAILED -> QUEUED
                    |
                    +-----------> FAILED
                    |
                    +-----------> DEAD_LETTERED
```

The exact transition rules belong in the job repository/state-transition layer. State changes must be conditional so a delayed or stale worker cannot overwrite a newer attempt.

### Delivery and idempotency guarantees

SQS-style queues provide **at-least-once delivery**. A message can be delivered more than once, including after a worker has completed computation but before its acknowledgement is recorded. Sentinel-GPU therefore does not claim exactly-once execution.

The processing contract is instead:

- Derive a stable logical `job_id` from the run, chunk, detector configuration, and pinned artifact identity.
- Assign a unique `attempt_id` and monotonically increasing attempt/fencing value to each execution attempt.
- Use conditional writes for state transitions and result commits.
- Use deterministic logical result and alert identifiers to suppress duplicate records.
- Store intermediate outputs under attempt-scoped object keys; publish the winning attempt through a conditional metadata commit.
- Acknowledge a queue message only after the result commit is durable.
- Use a transactional-outbox-style record and reconciliation loop to recover from the gap between persisting a job and publishing its queue message.

A worker heartbeat extends the queue visibility timeout while the job is active. If the worker stops renewing its lease, the message becomes eligible for redelivery. A stale worker must not be allowed to commit after a newer attempt has taken ownership.

## Queue topology

Separate queues enforce worker capability at the infrastructure boundary.

| Queue | Consumer | Purpose | Dead-letter queue |
|---|---|---|---|
| `ingest-queue` | Producer | Dataset ingestion and chunk orchestration | `ingest-dlq` |
| `jobs-gpu` | GPU worker | CUDA-required jobs | `jobs-gpu-dlq` |
| `jobs-cpu` | CPU worker | CPU baseline and CPU-only jobs | `jobs-cpu-dlq` |

The initial design uses standard queues and does not depend on message ordering. A future ordering requirement must define FIFO message groups and ordering semantics explicitly.

The backend policy is recorded for each run/job:

- `gpu`: execute on the GPU path. Do not silently substitute CPU execution; wait or fail according to the configured run policy if GPU execution is unavailable.
- `cpu`: execute on the CPU path, primarily for controlled baselines and CPU-only operation.
- `auto`: select an available supported backend and persist the actual backend used for every job. Any fallback after a GPU execution failure must be explicit and visible in run metadata.

## CUDA processing design

The CUDA implementation is divided into independently testable stages. The CPU implementation defines the expected semantics for each stage.

### K1 — Parallel pattern matching

The matcher scans log records for configured signatures such as error codes, authentication failures, and selected request patterns.

A suitable input representation is a flattened byte buffer with a record-offset array. This avoids creating one device allocation per log line and allows threads to locate record boundaries. The initial implementation can use per-record substring matching; an Aho–Corasick DFA is a possible extension when the pattern set justifies its additional state-management complexity.

Each record produces a compact match result containing the matched signature identifiers and relevant record metadata. Pattern configuration and character encoding rules must be versioned so CPU and GPU paths apply identical semantics.

### K2 — Sliding-window statistics

The statistics stage aggregates event counts over configured windows and entity keys, such as source address, service, or event category. A subsequent stage calculates statistical features and applies the configured anomaly threshold.

The implementation must define:

- Window width, step, and event-time boundary semantics.
- Entity-key normalization and collision handling.
- Minimum sample count and warm-up behavior.
- Zero-variance handling and finite-value checks.
- Late, missing, malformed, or out-of-order timestamps.
- Threshold configuration and versioning.

A direct global `atomicAdd` histogram is a useful baseline, but heavily skewed keys can cause contention. Compare it with block-local aggregation or partial histograms followed by a reduction before selecting the final implementation.

### CPU/GPU parity

The CPU path is the correctness oracle, not merely a slower worker implementation. Tests should compare:

- Matched record identifiers and signature IDs.
- Per-window aggregate counts.
- Anomaly decisions and their supporting features.
- Edge cases such as empty input, malformed records, repeated keys, boundary timestamps, and zero-variance windows.

Integer outputs should match exactly. Floating-point outputs should use a documented tolerance and identical threshold-boundary rules. A performance result is valid only after correctness checks pass for the same input and detector configuration.

### Bounded device memory

The dataset must never be loaded entirely into device memory. The worker reads bounded chunks, stages bounded micro-batches, and reuses device buffers. Pinned host buffers and asynchronous copies may be used where profiling shows a benefit; they must remain bounded because pinned memory consumes host resources. Buffer size, launch geometry, and stream count are benchmark parameters, not assumptions.

## Data and storage contracts

### Object storage layout

Object keys should be immutable and namespaced by run and chunk identity. A representative layout is:

```text
incoming/<dataset-id>/source.log
chunks/<run-id>/<chunk-id>/data.bin
chunks/<run-id>/<chunk-id>/manifest.json
artifacts/<artifact-version>/libsentinel.so
artifacts/<artifact-version>/manifest.json
results/<run-id>/<job-id>/<attempt-id>/output.jsonl
results/<run-id>/<job-id>/<attempt-id>/manifest.json
```

The exact bucket names and key prefixes are configuration, not embedded application constants. Manifests should include at least the object key, byte size, record count, checksum, schema version, and relevant detector/artifact identifiers.

### Job message contract

Queue messages should carry references and execution metadata, not raw log content. A representative message is:

```json
{
  "schema_version": 1,
  "run_id": "run_...",
  "job_id": "job_...",
  "chunk": {
    "bucket": "raw-chunks",
    "key": "chunks/run_.../chunk_000042/data.bin",
    "sha256": "..."
  },
  "detector": {
    "config_version": "detector-v1",
    "config_digest": "..."
  },
  "execution": {
    "backend": "gpu",
    "artifact_version": "v1",
    "artifact_digest": "..."
  }
}
```

Workers must validate the schema version, required fields, object existence, checksum, and artifact compatibility before executing a job. Unknown schema versions should fail explicitly and be routed to the dead-letter workflow after the configured receive policy.

### Metadata model

The metadata store should represent runs, jobs, attempts, and alerts separately or through well-defined item types with explicit partition/sort-key conventions. The model must support:

- Conditional creation of a run for an idempotency key.
- Querying jobs by run and status without scanning the entire table.
- Conditional job transitions and attempt ownership.
- Stable alert identity and deduplication.
- Paginated result retrieval.
- Retention and cleanup of completed-run metadata.

The key schema and access patterns should be documented alongside the Terraform table definitions before implementation. Avoid table scans in API request paths and avoid unbounded alert payloads in metadata items.

## Failure handling

| Failure case | Expected behavior |
|---|---|
| Producer stops during chunking | Re-read from a durable checkpoint or restart deterministically; do not publish incomplete manifests. |
| Crash after job record creation but before queue publish | Outbox dispatcher finds and republishes the pending intent. |
| Duplicate queue delivery | Idempotent job/result keys prevent duplicate logical output. |
| Worker exits during processing | Visibility lease expires; another delivery can claim a new attempt. |
| Old worker resumes after lease expiry | Fencing/conditional commit rejects the stale attempt. |
| Invalid input or unsupported schema | Record a terminal validation error and route the message through the DLQ policy. |
| Missing or corrupt chunk | Checksum validation fails before kernel execution; record the object reference and failure reason. |
| Missing or incompatible CUDA artifact | Fail visibly; do not run an unpinned or locally compiled substitute. |
| CUDA out-of-memory | Release transient buffers, reduce batch size where safe, and retry under a bounded policy. |
| GPU becomes unavailable | Keep GPU-only work pending or apply an explicitly configured fallback policy. |
| LocalStack becomes unavailable | Fail health checks, apply bounded retries, and preserve durable work for recovery. |
| Result object written but metadata commit fails | Keep the object attempt-scoped; retry the commit or clean up the unreferenced object later. |
| Poison message repeatedly fails | Move it to the relevant DLQ and retain diagnostics for inspection. |
| Dashboard or monitoring service stops | Processing continues; observability is not on the critical execution path. |

Retries must be bounded and classified. Transient infrastructure failures may be retried; deterministic validation failures should not be retried indefinitely. DLQ redrive must be an explicit operational action with a way to inspect the original payload and failure metadata.

## API surface

The API is the control interface for the pipeline. The following is the intended REST surface; endpoint availability and request schemas must match the implementation as it evolves.

| Endpoint | Purpose |
|---|---|
| `POST /v1/runs` | Create or retrieve an idempotent run. |
| `GET /v1/runs/{run_id}` | Return run status and aggregate progress. |
| `GET /v1/runs/{run_id}/jobs` | Return paginated job status and attempt summaries. |
| `GET /v1/runs/{run_id}/alerts` | Return paginated alert records. |
| `GET /healthz` | Process liveness check. |
| `GET /readyz` | Dependency/readiness check. |
| `GET /metrics` | Prometheus metrics endpoint. |

Submission should be asynchronous: the API validates and records the run, then returns a run identifier. It should not hold the HTTP request open while a dataset is chunked or processed.

## Observability

Metrics should describe operational behavior without using run IDs, job IDs, source addresses, or other high-cardinality values as Prometheus labels.

Recommended metrics include:

| Metric | What it measures |
|---|---|
| `sentinel_jobs_submitted_total` | Jobs accepted by backend and outcome. |
| `sentinel_jobs_completed_total` | Successfully committed jobs by worker/backend. |
| `sentinel_job_duration_seconds` | End-to-end job processing duration. |
| `sentinel_queue_messages_visible` | Visible queue depth by queue. |
| `sentinel_worker_heartbeat_age_seconds` | Age of the last successful worker heartbeat. |
| `sentinel_worker_active_jobs` | Currently active jobs per worker. |
| `sentinel_chunk_processing_bytes_total` | Bytes processed by backend. |
| `sentinel_cuda_kernel_duration_seconds` | Kernel execution time by kernel/version. |
| `sentinel_dlq_messages` | Messages waiting for investigation. |
| `sentinel_result_commit_failures_total` | Failed durable result commits. |

Track logs and traces separately from metrics. Include correlation fields such as `run_id`, `job_id`, and `attempt_id` in structured logs, but do not promote them to metric labels. Monitoring outages must not stop the data plane.

## Evaluation and benchmarking

Evaluation must distinguish algorithm performance from distributed-pipeline overhead.

### Detection quality

Use labeled data and report:

- True positives, false positives, true negatives, and false negatives.
- Precision, recall, F1 score, and false-positive rate.
- Per-signature or per-anomaly-class results where labels support them.
- Results for both original and synthetically modified datasets, clearly separated.

Synthetic attack injection should use a fixed seed and a versioned injection manifest. The manifest must identify inserted records and their ground-truth labels. Do not mix injected labels with original dataset labels without preserving provenance.

### Performance

Compare the following execution modes on the same data and detector configuration:

1. Sequential CPU reference.
2. Parallel CPU worker.
3. GPU kernel-only execution, excluding host/device transfer.
4. GPU processing including transfer and staging.
5. End-to-end distributed execution, including queueing and persistence.

Record:

- Wall-clock duration and throughput (bytes/s and records/s).
- Kernel time and host/device transfer time.
- Queue wait time and job processing latency.
- Median and tail latency across repeated runs.
- Worker count, backend, chunk size, micro-batch size, and artifact version.
- GPU model/driver/runtime, CPU, operating system, and software versions.

Perform warm-up runs and repeated measurements. Report the distribution, not only the best run. Measure small and large inputs because kernel launch and transfer overhead can make a GPU slower for small workloads. Do not claim a fixed acceleration factor in advance; report measured kernel-only and end-to-end speedup separately.

### Scalability experiments

Vary one dimension at a time:

- Dataset size.
- Chunk size and micro-batch size.
- Number of CPU workers.
- GPU job concurrency.
- Pattern-set size and event-key skew.
- Queue backlog and retry rate.

The single-host LXD topology demonstrates service-level concurrency and recovery. It does not, by itself, establish horizontal scalability across independent physical hosts.

## Security and operational boundaries

- Keep LocalStack, worker metrics, and internal service endpoints on the private network.
- Bind development-facing host ports to loopback unless an explicit network security model says otherwise.
- Use non-production credentials for local emulation and keep secrets out of source control.
- Validate all API inputs, object references, manifest fields, and queue payloads.
- Treat uploaded logs as untrusted data; constrain parsing, record size, encoding, and decompression behavior.
- Sanitize datasets before publishing examples or benchmark fixtures; logs can contain credentials, tokens, personal data, or internal infrastructure details.
- Do not include raw log contents or secrets in metric labels or routine application logs.
- Pin build artifacts by version and digest; reject unknown or mismatched artifacts.
- For a deployment against real AWS, replace local credentials and endpoints with least-privilege IAM roles/policies, private networking, appropriate encryption, and production secret management.

LocalStack is an AWS API emulator for development and testing. Passing tests against LocalStack is not proof that all AWS service behavior, quotas, IAM evaluation, or operational characteristics are identical in a real AWS account.

## Technology stack

| Area | Technology |
|---|---|
| Service isolation | LXD |
| Infrastructure as code | Terraform |
| Local AWS-compatible services | LocalStack (S3, SQS, DynamoDB) |
| API | Python, FastAPI |
| GPU compute | CUDA, C++ |
| CPU reference | C++ implementation used as the correctness baseline |
| Worker orchestration | Python services with a native detector interface |
| Metrics | Prometheus |
| Dashboards | Grafana and Streamlit |
| Dataset/evaluation tooling | Python |

The exact compiler, CUDA toolkit, driver, Python, and service versions should be pinned in the relevant build and deployment files. The host NVIDIA driver and the container CUDA runtime must be compatible; passing GPU device nodes into a container does not remove the host-driver dependency.

## Repository layout

The following is the intended source layout. Directories should be added as their corresponding components are implemented.

```text
sentinel-gpu/
├── README.md
├── Makefile
├── .env.example
├── terraform/
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   └── modules/
├── lxd/
│   ├── profiles/
│   └── scripts/
├── src/
│   ├── api/
│   ├── producer/
│   ├── worker/
│   ├── cuda/
│   ├── common/
│   ├── dashboard/
│   └── eval/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── fault_injection/
│   └── fixtures/
├── scripts/
│   ├── download_dataset.sh
│   └── generate_ground_truth.py
├── monitoring/
│   ├── prometheus.yml
│   └── grafana/
└── docs/
    ├── assets/
    │   ├── architecture.png
    │   └── architecture.svg
    ├── decisions/
    └── benchmarks/
```

## Development roadmap

1. **Infrastructure foundation:** establish the LXD network and service lifecycle; provision the LocalStack buckets, queues, dead-letter queues, and tables with Terraform.
2. **Detection correctness:** implement the CPU reference, define input/output contracts, and add deterministic tests for matching and window statistics.
3. **CUDA implementation:** implement and validate the kernels, verify CPU/GPU parity, and measure transfer and kernel costs independently.
4. **Distributed job execution:** implement the API, producer, outbox, capability queues, worker leases, idempotent commits, retries, and DLQ inspection.
5. **Build and artifact lifecycle:** compile, test, version, publish, retrieve, and verify immutable CUDA artifacts.
6. **Operations and evaluation:** add metrics, dashboards, failure-injection tests, reproducible datasets, and benchmark reports.

## Known limitations

- The reference deployment is single-host; host failure stops the entire local environment.
- LocalStack behavior is not a substitute for integration testing against real AWS before a cloud deployment.
- GPU execution is not guaranteed to outperform CPU execution for every dataset size or detector configuration.
- One physical GPU does not provide hardware-isolated GPU instances merely because multiple processes or containers can access it.
- Rule matching and statistical thresholds detect only the patterns and behaviors represented by their configuration and training/evaluation data; they are not a general-purpose security detector.
- Synthetic anomalies support controlled experiments but do not establish real-world detection quality on their own.

## References

- [CUDA C++ Programming Guide](https://docs.nvidia.com/cuda/cuda-c-programming-guide/)
- [NVIDIA CUDA Toolkit Documentation](https://docs.nvidia.com/cuda/)
- [LXD Documentation](https://documentation.ubuntu.com/lxd/)
- [Terraform Documentation](https://developer.hashicorp.com/terraform/docs)
- [LocalStack Documentation](https://docs.localstack.cloud/)
- [Amazon SQS Developer Guide](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html)
- [Amazon DynamoDB Developer Guide](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Introduction.html)

---

Sentinel-GPU is intended as an engineering and research project for studying GPU acceleration, distributed job processing, fault recovery, and reproducible performance evaluation.
