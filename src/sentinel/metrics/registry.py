"""Small, thread-safe metrics registry with Prometheus text exposition.

There are no third-party dependencies and no background threads.  Every label
*name* and every label *value* comes from a closed allowlist, so neither a
request path nor a run/job/source id can turn into a time series.  HTTP route
templates and status classes are encoded in the bounded ``result`` value
instead of adding unbounded ``route``/``status`` label dimensions.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

SERVICE_VALUES = frozenset({"api", "producer", "gpu-worker", "cpu-worker"})
BACKEND_VALUES = frozenset({"gpu", "cpu", "auto", "none"})
ERROR_CLASS_VALUES = frozenset(
    {
        "none",
        "validation",
        "not_found",
        "conflict",
        "http_error",
        "dependency",
        "internal",
        "cuda",
        "oom",
        "io",
        "unknown",
    }
)

# Fixed route templates: the raw request path is never used as a label.
HTTP_ROUTE_TEMPLATES = {
    "/v1/runs": "v1_runs",
    "/v1/runs/{run_id}": "v1_runs_id",
    "/v1/runs/{run_id}/jobs": "v1_runs_id_jobs",
    "/v1/runs/{run_id}/alerts": "v1_runs_id_alerts",
    "/healthz": "healthz",
    "/readyz": "readyz",
    "/metrics": "metrics",
}
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_HTTP_STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx", "other")
HTTP_RESULT_VALUES = frozenset(
    f"{route}:{method}:{status}"
    for route in (*HTTP_ROUTE_TEMPLATES.values(), "unmatched")
    for method in _HTTP_METHODS
    for status in _HTTP_STATUS_CLASSES
)

RESULT_VALUES = frozenset(
    {
        "ok",
        "ready",
        "not_ready",
        "success",
        "failure",
        "retry",
        "queued",
        "rejected",
        "dropped",
        "pending",
        "dlq",
        "rss",
        "limit",
        "peak",
        "used",
    }
) | HTTP_RESULT_VALUES

ALLOWED_LABELS: dict[str, frozenset[str]] = {
    "service": SERVICE_VALUES,
    "backend": BACKEND_VALUES,
    "result": RESULT_VALUES,
    "error_class": ERROR_CLASS_VALUES,
}

DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    float("inf"),
)
MAX_BUCKETS = 16
_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_]*$")


class InvalidLabelError(ValueError):
    """Raised for an unknown label name or an unapproved label value."""


def http_result_value(route_template: str | None, method: str, status_code: int) -> str:
    """Map a route template/status to one fixed ``result`` label value."""
    route = HTTP_ROUTE_TEMPLATES.get(route_template or "", "unmatched")
    try:
        status_class = f"{int(status_code) // 100}xx"
    except (TypeError, ValueError):  # pragma: no cover - defensive
        status_class = "other"
    if status_class not in _HTTP_STATUS_CLASSES:
        status_class = "other"
    return f"{route}:{str(method).upper()}:{status_class}"


def http_error_class(status_code: int) -> str:
    """Map an HTTP status to a small, fixed error-class vocabulary."""
    code = int(status_code)
    if code < 400:
        return "none"
    if code in (400, 422):
        return "validation"
    if code == 404:
        return "not_found"
    if code == 409:
        return "conflict"
    if code == 503:
        return "dependency"
    if code < 500:
        return "http_error"
    return "internal"


def _escape_help(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(value: Any) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _format_value(value: float) -> str:
    number = float(value)
    if math.isinf(number):
        return "+Inf" if number > 0 else "-Inf"
    if number.is_integer():
        return str(int(number))
    return format(number, ".12g")


def _format_upper(value: float) -> str:
    return "+Inf" if math.isinf(value) and value > 0 else _format_value(value)


@dataclass(frozen=True, slots=True)
class _MetricDef:
    name: str
    help_text: str
    kind: str
    label_names: tuple[str, ...]
    buckets: tuple[float, ...] | None = None


@dataclass(slots=True)
class _HistogramSample:
    cumulative: list[int]
    total: float = 0.0
    count: int = 0

    def copy(self) -> "_HistogramSample":
        return _HistogramSample(list(self.cumulative), self.total, self.count)


def _validate_buckets(buckets: Sequence[float]) -> tuple[float, ...]:
    values = tuple(float(value) for value in buckets)
    if not values or len(values) > MAX_BUCKETS:
        raise ValueError(f"buckets must contain 1..{MAX_BUCKETS} values")
    if any(math.isnan(value) or value < 0 for value in values):
        raise ValueError("buckets must be non-negative finite values (plus +Inf)")
    if any(math.isinf(value) for value in values[:-1]):
        raise ValueError("+Inf is only allowed as the last bucket")
    if not math.isinf(values[-1]):
        values = (*values, float("inf"))
    finite = [value for value in values if math.isfinite(value)]
    if any(later <= earlier for earlier, later in zip(finite, finite[1:])):
        raise ValueError("buckets must be strictly increasing")
    return values


def _validate_label_names(labels: Sequence[str] | str) -> tuple[str, ...]:
    names = (labels,) if isinstance(labels, str) else tuple(labels)
    for name in names:
        if name not in ALLOWED_LABELS:
            raise InvalidLabelError(
                f"label {name!r} is not allowlisted; allowed: "
                f"{sorted(ALLOWED_LABELS)}"
            )
    if len(set(names)) != len(names):
        raise InvalidLabelError("duplicate label names")
    return names


class Counter:
    """Monotonic counter; use a name ending in ``_total`` by convention."""

    __slots__ = ("_registry", "_definition")

    def __init__(self, registry: "MetricsRegistry", definition: _MetricDef) -> None:
        self._registry = registry
        self._definition = definition

    @property
    def name(self) -> str:
        return self._definition.name

    def inc(
        self,
        amount: float = 1.0,
        *,
        labels: Mapping[str, Any] | None = None,
        **label_values: Any,
    ) -> None:
        self._registry._counter_inc(self._definition, amount, labels, label_values)


class Gauge:
    """A value that can go up or down."""

    __slots__ = ("_registry", "_definition")

    def __init__(self, registry: "MetricsRegistry", definition: _MetricDef) -> None:
        self._registry = registry
        self._definition = definition

    @property
    def name(self) -> str:
        return self._definition.name

    def set(
        self,
        value: float,
        *,
        labels: Mapping[str, Any] | None = None,
        **label_values: Any,
    ) -> None:
        self._registry._gauge_set(self._definition, value, labels, label_values)

    def inc(
        self,
        amount: float = 1.0,
        *,
        labels: Mapping[str, Any] | None = None,
        **label_values: Any,
    ) -> None:
        self._registry._gauge_add(self._definition, amount, labels, label_values)

    def dec(
        self,
        amount: float = 1.0,
        *,
        labels: Mapping[str, Any] | None = None,
        **label_values: Any,
    ) -> None:
        self._registry._gauge_add(self._definition, -amount, labels, label_values)


class Histogram:
    """Fixed-bucket histogram; bucket boundaries cannot change at runtime."""

    __slots__ = ("_registry", "_definition")

    def __init__(self, registry: "MetricsRegistry", definition: _MetricDef) -> None:
        self._registry = registry
        self._definition = definition

    @property
    def name(self) -> str:
        return self._definition.name

    def observe(
        self,
        value: float,
        *,
        labels: Mapping[str, Any] | None = None,
        **label_values: Any,
    ) -> None:
        self._registry._histogram_observe(
            self._definition, value, labels, label_values
        )


class MetricsRegistry:
    """Thread-safe collection of counters, gauges and bounded histograms."""

    def __init__(
        self, prefix: str = "sentinel", buckets: Sequence[float] = DEFAULT_BUCKETS
    ) -> None:
        self.prefix = prefix
        self.default_buckets = _validate_buckets(buckets)
        self._lock = threading.RLock()
        self._definitions: dict[str, _MetricDef] = {}
        self._counters: dict[str, dict[tuple[str, ...], float]] = {}
        self._gauges: dict[str, dict[tuple[str, ...], float]] = {}
        self._histograms: dict[str, dict[tuple[str, ...], _HistogramSample]] = {}

    def _full_name(self, name: str, kind: str) -> str:
        if not isinstance(name, str) or not name:
            raise ValueError("metric name must be a non-empty string")
        value = name
        if self.prefix and not value.startswith(f"{self.prefix}_"):
            value = f"{self.prefix}_{value}"
        if kind == "counter" and not value.endswith("_total"):
            value = f"{value}_total"
        if not _NAME_RE.fullmatch(value):
            raise ValueError(f"invalid Prometheus metric name: {value!r}")
        return value

    def _register(
        self,
        name: str,
        help_text: str,
        kind: str,
        labels: Sequence[str] | str,
        buckets: Sequence[float] | None = None,
    ) -> _MetricDef:
        definition = _MetricDef(
            name=self._full_name(name, kind),
            help_text=str(help_text),
            kind=kind,
            label_names=_validate_label_names(labels),
            buckets=_validate_buckets(buckets) if buckets is not None else None,
        )
        with self._lock:
            existing = self._definitions.get(definition.name)
            if existing is not None and existing != definition:
                raise ValueError(
                    f"metric {definition.name} is already registered with a "
                    "different definition"
                )
            if existing is None:
                self._definitions[definition.name] = definition
                self._counters.setdefault(definition.name, {})
                self._gauges.setdefault(definition.name, {})
                self._histograms.setdefault(definition.name, {})
        return definition

    def counter(
        self,
        name: str,
        help_text: str,
        labels: Sequence[str] | str = (),
    ) -> Counter:
        return Counter(self, self._register(name, help_text, "counter", labels))

    def gauge(
        self,
        name: str,
        help_text: str,
        labels: Sequence[str] | str = (),
    ) -> Gauge:
        return Gauge(self, self._register(name, help_text, "gauge", labels))

    def histogram(
        self,
        name: str,
        help_text: str,
        labels: Sequence[str] | str = (),
        buckets: Sequence[float] | None = None,
    ) -> Histogram:
        return Histogram(
            self,
            self._register(
                name,
                help_text,
                "histogram",
                labels,
                buckets or self.default_buckets,
            ),
        )

    def _label_key(
        self,
        definition: _MetricDef,
        labels: Mapping[str, Any] | None,
        label_values: Mapping[str, Any],
    ) -> tuple[str, ...]:
        supplied = dict(labels or {})
        supplied.update(label_values)
        expected = set(definition.label_names)
        unknown = set(supplied) - expected
        if unknown:
            raise InvalidLabelError(
                f"unexpected labels for {definition.name}: {sorted(unknown)}"
            )
        missing = expected - set(supplied)
        if missing:
            raise InvalidLabelError(
                f"missing labels for {definition.name}: {sorted(missing)}"
            )
        for key, value in supplied.items():
            rendered = str(value)
            allowed = ALLOWED_LABELS[key]
            if rendered not in allowed:
                raise InvalidLabelError(
                    f"label {key}={rendered!r} is not allowlisted for "
                    f"{definition.name}"
                )
        return tuple(str(supplied[name]) for name in definition.label_names)

    def _counter_inc(
        self,
        definition: _MetricDef,
        amount: float,
        labels: Mapping[str, Any] | None,
        label_values: Mapping[str, Any],
    ) -> None:
        value = float(amount)
        if value < 0 or not math.isfinite(value):
            raise ValueError("counter increments must be finite and non-negative")
        key = self._label_key(definition, labels, label_values)
        with self._lock:
            series = self._counters[definition.name]
            series[key] = series.get(key, 0.0) + value

    def _gauge_set(
        self,
        definition: _MetricDef,
        value: float,
        labels: Mapping[str, Any] | None,
        label_values: Mapping[str, Any],
    ) -> None:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("gauge values must be finite")
        key = self._label_key(definition, labels, label_values)
        with self._lock:
            self._gauges[definition.name][key] = number

    def _gauge_add(
        self,
        definition: _MetricDef,
        amount: float,
        labels: Mapping[str, Any] | None,
        label_values: Mapping[str, Any],
    ) -> None:
        value = float(amount)
        if not math.isfinite(value):
            raise ValueError("gauge changes must be finite")
        key = self._label_key(definition, labels, label_values)
        with self._lock:
            series = self._gauges[definition.name]
            series[key] = series.get(key, 0.0) + value

    def _histogram_observe(
        self,
        definition: _MetricDef,
        value: float,
        labels: Mapping[str, Any] | None,
        label_values: Mapping[str, Any],
    ) -> None:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("histogram observations must be finite and non-negative")
        key = self._label_key(definition, labels, label_values)
        buckets = definition.buckets or self.default_buckets
        with self._lock:
            series = self._histograms[definition.name]
            sample = series.get(key)
            if sample is None:
                sample = _HistogramSample([0] * len(buckets))
                series[key] = sample
            sample.total += number
            sample.count += 1
            for index, upper in enumerate(buckets):
                if number <= upper:
                    sample.cumulative[index] += 1

    def reset(self) -> None:
        """Clear all samples (not definitions); useful for isolated tests."""
        with self._lock:
            for series in self._counters.values():
                series.clear()
            for series in self._gauges.values():
                series.clear()
            for series in self._histograms.values():
                series.clear()

    @staticmethod
    def _labels(
        definition: _MetricDef,
        key: tuple[str, ...],
        extra: tuple[str, str] | None = None,
    ) -> str:
        pairs = [
            f'{name}="{_escape_label(value)}"'
            for name, value in zip(definition.label_names, key)
        ]
        if extra:
            pairs.append(f'{extra[0]}="{_escape_label(extra[1])}"')
        return "{" + ",".join(pairs) + "}" if pairs else ""

    def render(self) -> str:
        """Render all samples as Prometheus text (0.0.4 exposition format)."""
        with self._lock:
            definitions = sorted(self._definitions.values(), key=lambda item: item.name)
            counters = {name: dict(series) for name, series in self._counters.items()}
            gauges = {name: dict(series) for name, series in self._gauges.items()}
            histograms = {
                name: {key: sample.copy() for key, sample in series.items()}
                for name, series in self._histograms.items()
            }

        lines: list[str] = []
        for definition in definitions:
            name = definition.name
            lines.append(f"# HELP {name} {_escape_help(definition.help_text)}")
            lines.append(f"# TYPE {name} {definition.kind}")
            if definition.kind == "histogram":
                buckets = definition.buckets or self.default_buckets
                for key in sorted(histograms[name]):
                    sample = histograms[name][key]
                    labels = self._labels(definition, key)
                    for upper, cumulative in zip(buckets, sample.cumulative):
                        bucket_labels = self._labels(
                            definition, key, ("le", _format_upper(upper))
                        )
                        lines.append(
                            f"{name}_bucket{bucket_labels} {_format_value(cumulative)}"
                        )
                    lines.append(
                        f"{name}_sum{labels} {_format_value(sample.total)}"
                    )
                    lines.append(
                        f"{name}_count{labels} {_format_value(sample.count)}"
                    )
                continue

            series = counters[name] if definition.kind == "counter" else gauges[name]
            for key in sorted(series):
                labels = self._labels(definition, key)
                lines.append(f"{name}{labels} {_format_value(series[key])}")

        return "\n".join(lines) + ("\n" if lines else "")
