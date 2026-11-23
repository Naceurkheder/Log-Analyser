# Sentinel-GPU failure/recovery demo runbook (Sprint 8)

> **Interface status.** Commands backed by the current tree are marked
> **available**: dataset generation, API/producer CLI, Makefile targets and the
> S8 evaluation/benchmark commands. Commands marked **[S6 intended]** are the
> worker interface defined in `SENTINEL-GPU-PERSONAL-EXECUTION-PLAN.md`; they are
> provided by Sprint 6 and may not exist in this tree until S6 is integrated.
> Verify them with `python -m sentinel.worker.cli --help` on the demo host.
> Do not present an intended command as already demonstrated.

All commands run from the repository root. The demo is **one physical host**:
LXD containers, moto (S3/SQS/DynamoDB emulation), queues and one optional GPU
passthrough device. It demonstrates distributed *failure handling*, not
multi-machine distribution.

## 0. Prerequisites

```bash
cp .env.example .env          # edit only if the lab profile differs
make preflight                # [S1 intended] host/RAM/GPU sanity check
make up PROFILE=16g           # [available] start the bounded fleet
make status                   # [available] container state and caps
make infra                    # [available] moto + Terraform apply
make verify                   # [available] buckets, queues, DLQs, redrive
```

`make down` stops containers but preserves LocalStack/Terraform state.
`make destroy` is destructive and is **not** part of the demo flow.

## 1. Fixed small dataset (labels stay off the worker path)

```bash
mkdir -p data/demo
PYTHONPATH=src .venv/bin/python scripts/gen_dataset.py \
  --lines 2000 --seed 11 --burst-every 60 \
  --out data/demo/demo.log \
  --labels-out data/demo/demo.labels.json
sha256sum data/demo/demo.log data/demo/demo.labels.json
```

Use the same seed/burst values for every rehearsal. The label manifest is kept
on the operator host and is **never** included in an upload, ingest message,
job or detector configuration. The detector only receives the dataset object
reference.

## 2. Normal API → producer → worker → result flow

Start the control API (local, loopback by default):

```bash
make api                      # [available] uvicorn on 127.0.0.1:8000
```

In another terminal, submit and record the run ID:

```bash
PYTHONPATH=src .venv/bin/python -m sentinel.api.cli submit-file \
  data/demo/demo.log \
  --key demo/demo.log \
  --pattern "Failed password" \
  --backend cpu \
  --api-url http://127.0.0.1:8000 | tee /tmp/sentinel-demo-submit.json
export RUN_ID=$(PYTHONPATH=src .venv/bin/python -c \
  'import json; print(json.load(open("/tmp/sentinel-demo-submit.json"))["run_id"])')
```

Run the producer once, then start a CPU worker:

```bash
make produce                  # [available] ingest -> chunks/manifests/jobs
PYTHONPATH=src .venv/bin/python -m sentinel.worker.cli serve --backend cpu
                              # [S6 intended] long-poll jobs-cpu
```

For a GPU rehearsal, use `--backend gpu` instead; do not mix backends in one
run. Check the result through the read-only API:

```bash
PYTHONPATH=src .venv/bin/python -m sentinel.api.cli status "$RUN_ID" --api-url http://127.0.0.1:8000
PYTHONPATH=src .venv/bin/python -m sentinel.api.cli jobs   "$RUN_ID" --api-url http://127.0.0.1:8000
PYTHONPATH=src .venv/bin/python -m sentinel.api.cli alerts "$RUN_ID" --api-url http://127.0.0.1:8000 --limit 50
```

Expected state sequence: `PENDING → QUEUED → RUNNING → SUCCEEDED`; failed
transient attempts go through `RETRYABLE_FAILED → QUEUED`, permanent ones to
`FAILED_PERMANENT` or `DEAD_LETTERED`.

## 3. State, queue depth and heartbeat inspection

```bash
# Queue depth (repeat before and after the producer/worker steps).
aws --endpoint-url "$AWS_ENDPOINT_URL" --region "$AWS_REGION" \
    sqs get-queue-attributes \
    --queue-url "$AWS_ENDPOINT_URL/000000000000/jobs-cpu" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible

# Authoritative job state (replace with the job_id printed by `jobs`).
aws --endpoint-url "$AWS_ENDPOINT_URL" --region "$AWS_REGION" \
    dynamodb get-item --table-name "$SENTINEL_JOBS_TABLE" \
    --key "{\"job_id\":{\"S\":\"$JOB_ID\"}}"

# Worker health and metrics endpoints [S6/S7 intended].
curl -fsS http://<cpu-worker-ip>:<health-port>/healthz
curl -fsS http://<cpu-worker-ip>:<metrics-port>/metrics | grep -E 'heartbeat|lease|visibility'
```

Inspect for: growing `ApproximateNumberOfMessages` (backlog), non-visible
messages (in-flight), `fencing_token`, `lease_expires_at`, `attempt_count` and
heartbeat age. A stale heartbeat with a non-empty queue is the signal for the
recovery steps below.

## 4. Worker-kill recovery and visibility/redelivery

1. Start a run with the fixed dataset and wait until a job is `RUNNING`.
2. Kill exactly one demo worker while the job is active:

   ```bash
   pkill -f 'sentinel.worker.cli serve --backend cpu'   # controlled demo kill
   ```

3. Watch the message become visible again after the visibility timeout and a
   new attempt claim the job (`attempt_count` and `fencing_token` increase).
   Do **not** shorten the production visibility timeout for the demo; if the
   wait is too long, record the configured value and continue.

   ```bash
   aws --endpoint-url "$AWS_ENDPOINT_URL" sqs get-queue-attributes \
       --queue-url "$AWS_ENDPOINT_URL/000000000000/jobs-cpu" \
       --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
   PYTHONPATH=src .venv/bin/python -m sentinel.worker.cli reconcile --once
                               # [S6 intended] republish stranded dispatches
   ```

4. Restart the worker and verify recovery:

   ```bash
   PYTHONPATH=src .venv/bin/python -m sentinel.worker.cli serve --backend cpu
   PYTHONPATH=src .venv/bin/python -m sentinel.api.cli status "$RUN_ID"
   PYTHONPATH=src .venv/bin/python -m sentinel.api.cli alerts "$RUN_ID"
   ```

   **One logical committed result** means one `SUCCEEDED` job record and one
   set of deterministic alert IDs for a run, even if `attempt_count > 1`.
   Attempt-specific S3 objects may exist; only the fenced commit becomes the
   logical result. Compare alert IDs before and after a redelivery to prove
   duplicate execution is harmless.

## 5. DLQ inspect and explicit replay

Inject a poison message by sending a message with an unsupported schema version
to `jobs-cpu-dlq`'s source queue, or corrupt a demo chunk checksum. After
`maxReceiveCount` (initial design value: five), inspect and replay:

```bash
PYTHONPATH=src .venv/bin/python -m sentinel.worker.cli dlq-inspect --queue jobs-cpu-dlq
                              # [S6 intended] message/job IDs, receive count,
                              # last error, object references
PYTHONPATH=src .venv/bin/python -m sentinel.worker.cli dlq-replay \
  --queue jobs-cpu-dlq --reason "demo replay after poison-message fix"
                              # [S6 intended] reason is mandatory; job identity
                              # is preserved and replay metadata is recorded
```

Verify the replayed job does not enter an automatic replay loop and that the
final logical result is still one committed result.

## 6. Safe reset sequence

```bash
make down                     # stops containers; preserves state
make up PROFILE=16g           # restart cleanly without destroying lab data
# Only if the demo must be wiped, purge demo queues explicitly:
aws --endpoint-url "$AWS_ENDPOINT_URL" sqs purge-queue \
    --queue-url "$AWS_ENDPOINT_URL/000000000000/jobs-cpu"
# Delete only demo-prefixed objects; never wipe the whole bucket for a reset.
aws --endpoint-url "$AWS_ENDPOINT_URL" s3 rm s3://incoming/demo --recursive
aws --endpoint-url "$AWS_ENDPOINT_URL" s3 rm s3://raw-chunks/demo --recursive
```

Do not run `make destroy` as a reset. If Terraform state or LocalStack state
must be discarded, treat it as a new lab build and re-run `make infra` +
`make verify`.

## 7. Fallback evidence if GPU passthrough fails

Record the failure visibly (driver, device presence, `nvidia-smi` output) and
continue with the CPU path; never relabel a CPU run as GPU:

```bash
make test-cpu                 # [available] CPU correctness suite
make cpu-cli                  # [available] writes results/cpu-reference-sample.json
PYTHONPATH=src .venv/bin/python -m pytest tests/evaluation -q   # [available]
nvidia-smi -L                 # capture GPU absence for the demo log
PYTHONPATH=src .venv/bin/python -m sentinel.worker.cli serve --backend cpu
                              # [S6 intended] explicit CPU-only fallback
```

Pre-recorded evidence to show instead of a live GPU run:

* `docs/benchmarks/cpu-baseline.json` and `docs/benchmarks/cuda-memory-pipeline.json`
* `docs/evaluation/sample-evaluation.json` and `docs/evaluation/benchmark-report.md`
* `results/cpu-reference-sample.json` produced by `make cpu-cli`

State on screen: "CPU fallback demonstration; GPU passthrough unavailable; no
live GPU result claimed."

## 8. What is distributed — and why GPU speedup varies

**Distributed here:** separate LXD containers for API/infra/workers, S3 object
storage, SQS job queues with DLQs, DynamoDB state, and independent worker
processes with leases/fencing. All of them run on **one physical host** and one
emulated AWS endpoint. This is enough to demonstrate crash recovery,
redelivery and duplicate-safe commits; it is not a multi-host scalability
result.

**Workload-dependent GPU speedup:** the saved 34.0x applies only to the
kernel-scope 100 MB staged benchmark. Small inputs are dominated by launch and
transfer overhead; sparse patterns and narrow windows can underuse the device;
host staging, H2D/D2H traffic and Python marshalling can dominate end-to-end;
and one shared GPU plus possible thermal throttling changes repeat timings.
The staging/VRAM JSON in `docs/benchmarks/` is a memory diagnostic, not a
speedup result. See `docs/evaluation/benchmark-report.md` for scope rules and
`docs/decisions/0012-evaluation.md` for the full metric/scope policy.
