"""Prometheus exposition and bounded HTTP instrumentation for the control API.

The route is deliberately excluded from OpenAPI: `/healthz`, `/readyz` and the
committed OpenAPI contract stay byte-identical for existing clients, while
Prometheus still gets a machine-readable endpoint.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, Response

from .registry import (
    PROMETHEUS_CONTENT_TYPE,
    MetricsRegistry,
    http_error_class,
    http_result_value,
)

log = logging.getLogger("sentinel.metrics.api")

api_metrics = MetricsRegistry(prefix="sentinel")

HTTP_REQUESTS = api_metrics.counter(
    "http_requests_total",
    "HTTP requests by fixed route template, status class and error class.",
    labels=("service", "result", "error_class"),
)
READINESS = api_metrics.gauge(
    "api_readiness",
    "1 when the last /readyz check succeeded, 0 when it failed.",
    labels=("service", "result", "error_class"),
)


def _route_template(request: Request) -> str | None:
    route = request.scope.get("route")
    return getattr(route, "path", None)


def add_metrics_route(
    app: FastAPI, registry: MetricsRegistry | None = None
) -> None:
    """Attach ``GET /metrics`` and bounded request/readiness instrumentation.

    Calling this twice on the same app is a no-op.  The registries supplied by
    tests are accepted so samples can be asserted in isolation.
    """
    if getattr(app.state, "_sentinel_metrics_installed", False):
        return
    active = registry or api_metrics
    app.state.metrics = active
    app.state._sentinel_metrics_installed = True

    requests = active.counter(
        "http_requests_total",
        "HTTP requests by fixed route template, status class and error class.",
        labels=("service", "result", "error_class"),
    )
    readiness = active.gauge(
        "api_readiness",
        "1 when the last /readyz check succeeded, 0 when it failed.",
        labels=("service", "result", "error_class"),
    )

    @app.get(
        "/metrics",
        include_in_schema=False,
        name="prometheus_metrics",
        summary="Prometheus exposition (not part of the versioned contract)",
    )
    def metrics() -> Response:
        return Response(content=active.render(), media_type=PROMETHEUS_CONTENT_TYPE)

    @app.middleware("http")
    async def _record_metrics(
        request: Request, call_next: Any
    ) -> Response:
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            error_class = http_error_class(status)
            result = http_result_value(
                _route_template(request), request.method, status
            )
            requests.inc(
                service="api", result=result, error_class=error_class
            )
            if request.url.path == "/readyz":
                ready = status < 400
                readiness.set(
                    1.0 if ready else 0.0,
                    service="api",
                    result="ready" if ready else "not_ready",
                    error_class=error_class,
                )
