"""API-facing re-exports for the S7 Prometheus route.

The implementation lives in :mod:`sentinel.metrics.api`; keeping this small
shim next to the other API modules lets ``main.py`` import it consistently
with ``.log`` and ``.errors``.
"""

from __future__ import annotations

from ..metrics.api import (
    HTTP_REQUESTS,
    READINESS,
    add_metrics_route,
    api_metrics,
)

__all__ = [
    "HTTP_REQUESTS",
    "READINESS",
    "add_metrics_route",
    "api_metrics",
]
