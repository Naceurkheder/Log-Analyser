"""Dependency-free Prometheus-compatible metrics (S7)."""

from __future__ import annotations

from .registry import (
    ALLOWED_LABELS,
    DEFAULT_BUCKETS,
    InvalidLabelError,
    MetricsRegistry,
    PROMETHEUS_CONTENT_TYPE,
    http_error_class,
    http_result_value,
)

__all__ = [
    "ALLOWED_LABELS",
    "DEFAULT_BUCKETS",
    "InvalidLabelError",
    "MetricsRegistry",
    "PROMETHEUS_CONTENT_TYPE",
    "http_error_class",
    "http_result_value",
]
