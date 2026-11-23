# Monitoring (S7)

Prometheus scrapes the LXD fleet every 15 seconds and keeps **12 hours** of
samples. Long-term storage is deliberately out of scope. The retention flag
is set in `docker-compose.yml`:

```
--storage.tsdb.retention.time=12h
```

Start the stack from the repository root:

```sh
docker compose -f monitoring/docker-compose.yml up -d
# Prometheus: http://127.0.0.1:9090
# Grafana:    http://127.0.0.1:3000  (admin/admin)
```

The Grafana datasource (`uid: prometheus`) and the *Sentinel-GPU fleet
overview* dashboard are provisioned from `monitoring/grafana/`.

## Scrape targets

| Job | Target | Service label |
|---|---|---|
| `sentinel-api` | `api:8000` | `api` |
| `sentinel-producer` | `producer:8081` | `producer` |
| `sentinel-gpu-worker` | `gpu-worker-1:8082` | `gpu-worker` |
| `sentinel-cpu-worker` | `cpu-worker-1:8083` | `cpu-worker` |

Every target exposes `GET /metrics` in Prometheus text format. The API route
is implemented with the dependency-free registry in `sentinel.metrics`; the
worker names are fixed by ADR-0011 so a worker can be scraped without changing
Prometheus or Grafana.

## Alerts

`alerts.yml` is mounted at `/etc/prometheus/alerts.yml` and loaded by the
`rule_files` entry in `prometheus.yml`.

| Alert | Condition | Likely cause |
|---|---|---|
| `StuckQueue` | oldest pending item > 15m for 5m | producer dispatch stalled |
| `StaleHeartbeat` | no heartbeat for > 120s | worker hung or stopped |
| `GrowingDLQ` | DLQ counter increases within 15m for 10m | repeated job failures |
| `RepeatedCUDAErrors` | >= 3 CUDA/OOM errors in 10m | device OOM or driver failure |
| `OutboxBacklog` | pending outbox > 100 for 10m | SQS publication stalled |
| `MemoryPressure` | RSS > 85% of cap or VRAM > 90% for 5m | bounded staging exceeded |

There is no Alertmanager in this local stack; inspect `prometheus/alerts` for
firing state. Do not add labels containing run ids, job ids, source keys or
raw paths — the metric taxonomy in ADR-0011 is intentionally bounded.
