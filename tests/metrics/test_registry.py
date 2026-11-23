"""Counters, gauges, histograms, label bounds and text rendering."""

from __future__ import annotations

from threading import Thread

import pytest

from sentinel.metrics.registry import (
    PROMETHEUS_CONTENT_TYPE,
    InvalidLabelError,
    MetricsRegistry,
    http_error_class,
    http_result_value,
)

LABELS = {
    "service": "gpu-worker",
    "backend": "gpu",
    "result": "success",
    "error_class": "none",
}


def test_counter_gauge_histogram_render() -> None:
    registry = MetricsRegistry(prefix="test")
    counter = registry.counter("jobs_total", "Completed jobs.", labels=tuple(LABELS))
    gauge = registry.gauge("queue_depth", "Pending items.", labels=("service", "backend", "result"))
    histogram = registry.histogram(
        "job_duration_seconds",
        "Job wall time.",
        labels=tuple(LABELS),
        buckets=(0.1, 1.0),
    )

    counter.inc(**LABELS)
    counter.inc(2, labels=LABELS)
    gauge.set(3, service="producer", backend="cpu", result="pending")
    histogram.observe(0.05, **LABELS)
    histogram.observe(2.0, **LABELS)

    text = registry.render()
    assert PROMETHEUS_CONTENT_TYPE.startswith("text/plain")
    assert "# HELP test_jobs_total Completed jobs." in text
    assert "# TYPE test_jobs_total counter" in text
    assert (
        'test_jobs_total{service="gpu-worker",backend="gpu",result="success",'
        'error_class="none"} 3' in text
    )
    assert (
        'test_queue_depth{service="producer",backend="cpu",result="pending"} 3'
        in text
    )
    assert "# TYPE test_job_duration_seconds histogram" in text
    assert (
        'test_job_duration_seconds_bucket{service="gpu-worker",backend="gpu",'
        'result="success",error_class="none",le="0.1"} 1' in text
    )
    assert (
        'test_job_duration_seconds_bucket{service="gpu-worker",backend="gpu",'
        'result="success",error_class="none",le="+Inf"} 2' in text
    )
    assert 'test_job_duration_seconds_sum{service="gpu-worker",backend="gpu",' in text
    assert (
        'test_job_duration_seconds_count{service="gpu-worker",backend="gpu",'
        'result="success",error_class="none"} 2' in text
    )


def test_labels_are_allowlisted_and_bounded() -> None:
    registry = MetricsRegistry(prefix="safe")
    counter = registry.counter("events_total", "Events.", labels=("service", "result"))

    with pytest.raises(InvalidLabelError):
        registry.counter("bad_total", "Bad.", labels=("run_id",))
    with pytest.raises(InvalidLabelError):
        counter.inc(service="worker-1234", result="success")
    with pytest.raises(InvalidLabelError):
        counter.inc(service="api", result="/v1/runs/run_abc123")
    with pytest.raises(InvalidLabelError):
        counter.inc(service="api", result="success", run_id="run_abc123")
    with pytest.raises(InvalidLabelError):
        counter.inc(service="api")

    counter.inc(service="api", result="success")
    text = registry.render()
    assert "run_id" not in text
    assert 'safe_events_total{service="api",result="success"} 1' in text


def test_histogram_buckets_are_fixed_and_bounded() -> None:
    registry = MetricsRegistry()
    with pytest.raises(ValueError, match="buckets"):
        registry.histogram(
            "too_many_total_seconds",
            "Too many buckets.",
            buckets=tuple(float(i) for i in range(32)),
        )

    histogram = registry.histogram(
        "latency_seconds", "Latency.", labels=("service",), buckets=(0.5,)
    )
    histogram.observe(0.25, service="api")
    with pytest.raises(ValueError):
        histogram.observe(10.0, service="api", le="99")  # unknown label
    text = registry.render()
    assert 'le="0.5"' in text
    assert 'le="+Inf"' in text
    assert 'le="99"' not in text


def test_registry_updates_are_thread_safe() -> None:
    registry = MetricsRegistry(prefix="threads")
    counter = registry.counter("ops_total", "Operations.", labels=("service",))

    def _worker() -> None:
        for _ in range(200):
            counter.inc(service="api")

    threads = [Thread(target=_worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert 'threads_ops_total{service="api"} 800' in registry.render()
    registry.reset()
    assert 'threads_ops_total{service="api"}' not in registry.render()


def test_http_helpers_use_route_templates_and_fixed_classes() -> None:
    assert (
        http_result_value("/v1/runs/{run_id}", "GET", 404)
        == "v1_runs_id:GET:4xx"
    )
    # Raw paths are never echoed into a label, even for unmatched routes.
    assert http_result_value("/v1/runs/run_abc123", "GET", 200) == "unmatched:GET:2xx"
    assert http_error_class(200) == "none"
    assert http_error_class(422) == "validation"
    assert http_error_class(503) == "dependency"
    assert http_error_class(500) == "internal"
