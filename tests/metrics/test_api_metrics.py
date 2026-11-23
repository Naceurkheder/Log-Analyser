"""GET /metrics on the real API and isolated instrumentation behaviour."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from sentinel.api.main import create_app
from sentinel.metrics.api import add_metrics_route, api_metrics
from sentinel.metrics.registry import PROMETHEUS_CONTENT_TYPE, MetricsRegistry

FORBIDDEN_LABELS = ("run_id=", "job_id=", "chunk_id=", "source=", "key=")


def test_metrics_route_in_real_app_has_content_type_and_bounded_labels() -> None:
    api_metrics.reset()
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
        client.get("/metrics")  # counted after the first render
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == PROMETHEUS_CONTENT_TYPE
    body = response.text
    assert "# HELP sentinel_http_requests_total" in body
    assert "# TYPE sentinel_http_requests_total counter" in body
    assert "# TYPE sentinel_api_readiness gauge" in body
    assert 'service="api"' in body
    assert 'result="healthz:GET:2xx"' in body
    assert 'result="metrics:GET:2xx"' in body
    for label in FORBIDDEN_LABELS:
        assert label not in body


def test_metrics_route_is_not_part_of_openapi_contract() -> None:
    app = create_app()
    assert "/metrics" not in app.openapi()["paths"]
    assert any(getattr(route, "path", None) == "/metrics" for route in app.routes)


def test_readiness_gauge_tracks_success_and_failure() -> None:
    cases = (
        ("readyok", 200, "ready", "none", 1),
        ("readyfail", 503, "not_ready", "dependency", 0),
    )
    for prefix, status, result, error_class, value in cases:
        registry = MetricsRegistry(prefix=prefix)
        app = FastAPI()

        def _readyz(status_code: int = status) -> object:
            if status_code >= 400:
                return JSONResponse(
                    status_code=status_code, content={"status": "not_ready"}
                )
            return {"status": "ready"}

        app.add_api_route("/readyz", _readyz, methods=["GET"])
        add_metrics_route(app, registry)

        with TestClient(app, raise_server_exceptions=False) as client:
            client.get("/readyz")
            metrics = client.get("/metrics")

        expected = (
            f'{prefix}_api_readiness{{service="api",result="{result}",'
            f'error_class="{error_class}"}} {value}'
        )
        assert expected in metrics.text


def test_add_metrics_route_is_idempotent() -> None:
    app = FastAPI()
    registry = MetricsRegistry(prefix="once")
    add_metrics_route(app, registry)
    add_metrics_route(app, registry)

    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.text.count("# TYPE once_http_requests_total counter") == 1


def test_registry_rejects_unbounded_labels_in_api_metrics() -> None:
    registry = MetricsRegistry(prefix="api_safe")
    add_metrics_route(FastAPI(), registry)
    with pytest.raises(ValueError):
        registry.counter("run_total", "Bad.", labels=("run_id",))
