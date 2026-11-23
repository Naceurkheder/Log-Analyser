# One-shot lifecycle script

`scripts/sentinel.sh` installs this repository's Python side, starts the whole
local stack, reports status, and closes everything again. It is intentionally a
single script so a fresh Linux machine can be brought up and torn down with the
same commands.

All runtime state lives in `.local/sentinel/` (ignored by git):

| Path | Purpose |
| --- | --- |
| `.local/sentinel/*.pid` | PID of each managed background process |
| `.local/sentinel/*.log` | stdout/stderr for API, producer, workers and dashboard |
| `.env` | Local dummy credentials and resource names, created from `.env.example` only when missing |

## Prerequisites on the target machine

The script checks these commands and fails with an explicit message:

- `python3` with `venv` support
- `terraform`
- `lxc` (the LXD fleet that hosts moto and the service containers)
- `curl` for the run/status health checks
- `docker` only when `--monitoring` is requested
- `nvidia-smi` only when `--with-gpu` is requested

The API and dashboard bind to `127.0.0.1` by default. Change them with
`--api-host` / `--dashboard-host` only if you understand the exposure.

## Commands

```bash
scripts/sentinel.sh install --spark          # venv, deps, .env, dataset, terraform init
scripts/sentinel.sh run --with-gpu --monitoring
scripts/sentinel.sh status
scripts/sentinel.sh spark --run-id run_abc123
scripts/sentinel.sh close --keep-fleet       # leave moto/containers running
scripts/sentinel.sh close                    # stop everything, including the fleet
scripts/sentinel.sh restart                  # close --keep-fleet, then run
```

`install` is idempotent enough for a fresh checkout: it reuses an existing
`.env`, recreates the virtualenv, installs pinned dependency ranges, writes
`data/bench.log` and `data/bench.labels.json`, and runs `terraform init`.

`run` starts the LXD fleet, applies Terraform, then launches in this order:

1. FastAPI control API on `127.0.0.1:8000`
2. Chunk producer (`--watch --wait 20`)
3. CPU worker with fenced leases
4. GPU worker only with `--with-gpu` and a visible `nvidia-smi`
5. Streamlit dashboard on `127.0.0.1:8501`
6. Prometheus/Grafana only with `--monitoring`

`close` terminates only PIDs recorded in `.local/sentinel/`, then stops the
monitoring compose stack and the LXD fleet unless `--keep-fleet` is given. It
never kills unrelated processes.

`spark` runs the optional batch stage for one run. By default it uses the
`worker-fenced` committer, which claims jobs with the S6 conditional store,
writes an immutable attempt object, writes alerts, commits with the fencing
token, and finalises the run. Use `--committer logging` for evidence-only
replay that makes no durable state change. Pause the normal workers before a
Spark batch so both executors do not race for the same jobs.

## Parameters

Every flag has an environment-variable equivalent. Flags win when both are
present.

| Flag | Env var | Default | Meaning |
| --- | --- | --- | --- |
| `--venv PATH` | `VENV` | `.venv` | Virtualenv directory |
| `--api-host HOST` | `API_HOST` | `127.0.0.1` | API bind host |
| `--api-port PORT` | `API_PORT` | `8000` | API port |
| `--dashboard-host HOST` | `DASH_HOST` | `127.0.0.1` | Dashboard bind host |
| `--dashboard-port PORT` | `DASH_PORT` | `8501` | Dashboard port |
| `--backend NAME` | `BACKEND` | `auto` | Producer backend policy (`auto`/`cpu`/`gpu`) |
| `--dataset-mb N` | `DATASET_MB` | `100` | Synthetic dataset size |
| `--spark` | — | off | Install optional `pyspark` |
| `--monitoring` | — | off | Start Prometheus/Grafana |
| `--with-gpu` | — | off | Start GPU worker when hardware is visible |
| `--keep-fleet` | — | off | Do not stop LXD during `close` |
| `--skip-dataset` | — | off | Do not regenerate the dataset on install |
| `--run-id ID` | `RUN_ID` | empty | Run for `spark` |
| `--committer NAME` | `SPARK_COMMITTER` | `worker-fenced` | Spark committer |
| — | `AWS_ENDPOINT_URL` | `http://10.20.0.10:4566` | moto/S3 endpoint |
| — | `SPARK_MASTER` | `local[*]` | Spark master URL |

Example with environment variables instead of flags:

```bash
API_PORT=8100 DASH_PORT=8600 DATASET_MB=250 \
  scripts/sentinel.sh install --spark
API_PORT=8100 DASH_PORT=8600 BACKEND=cpu \
  scripts/sentinel.sh run --monitoring
```

The same commands are available as Make targets:
`make install-all`, `make run-all`, `make status-all`, `make close-all`,
`make restart-all`, and `make spark-batch`. Pass extra flags with
`SENTINEL_ARGS="--with-gpu --monitoring"`.

## Troubleshooting

- `API did not become healthy`: inspect `.local/sentinel/api.log`; the usual
  cause is Terraform not having been applied or moto not reachable.
- A worker exits immediately: inspect its log for a missing artifact digest or
  a queue URL error; `--with-gpu` without `nvidia-smi` is skipped with a warning.
- `pyspark missing`: run `scripts/sentinel.sh install --spark`.
- Stale processes: run `scripts/sentinel.sh status`, then
  `scripts/sentinel.sh close`. Only recorded PIDs are signalled.
