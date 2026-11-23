"""Built-in health/metrics HTTP server and Prometheus text format."""

from __future__ import annotations

import http.client
from typing import Any

from sentinel.worker.metrics import MetricsServer, WorkerMetrics


def test_prometheus_text_contains_required_metrics() -> None:
    metrics = WorkerMetrics(
        capability="gpu",
        worker_id="worker-1",
        jobs_processed=3,
        jobs_retried=1,
        heartbeat_failures=2,
        dlq_sends=1,
        active_state="running",
        accepting_work=True,
        lease_age_seconds=1.25,
    )
    body = metrics.render()
    assert 'sentinel_worker_info{capability="gpu"' in body
    assert "sentinel_worker_jobs_processed_total" in body
    assert "sentinel_worker_retries_total" in body
    assert "sentinel_worker_heartbeat_failures_total" in body
    assert "sentinel_worker_dlq_sends_total" in body
    assert "sentinel_worker_active_state" in body
    assert "sentinel_worker_lease_age_seconds" in body
    assert "# TYPE sentinel_worker_jobs_processed_total counter" in body


def test_http_server_exposes_healthz_and_metrics() -> None:
    metrics = WorkerMetrics(capability="cpu", worker_id="worker-http")
    server = MetricsServer(metrics, "127.0.0.1", 0)
    server.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        assert response.status == 200
        assert b'"status":"ok"' in response.read()
        connection.request("GET", "/metrics")
        response = connection.getresponse()
        assert response.status == 200
        assert b"sentinel_worker_info" in response.read()
        connection.request("GET", "/missing")
        assert connection.getresponse().status == 404
        connection.close()
    finally:
        server.close()


def test_paused_worker_reports_paused_health() -> None:
    metrics = WorkerMetrics(capability="cpu", worker_id="worker-1")
    metrics.accepting_work = False
    assert metrics.snapshot()["status"] == "paused"
    assert "sentinel_worker_accepting_work" in metrics.render()
