"""ctypes binding for the GPU windowed K2 stage (Sprint 5 / S5-C).

The Python Sprint 4 scorer remains the correctness oracle. This binding only
transfers already-validated feature rows to ``sentinel_detect_window_gpu`` and
copies the aggregated features / scored alert candidates back. The native
function owns all of its output allocations, released by
``sentinel_free_window_results``.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass

from .native import (
    SentinelTiming,
    assert_abi,
    load_gpu_library,
)

MAX_FEATURES = 1 << 20
UINT32_MAX = 2**32 - 1
ERROR_NAMES = {
    0: "OK",
    -1: "INVALID",
    -2: "OOM",
    -3: "CUDA",
    -4: "UNSUPPORTED",
}

FeatureRow = tuple[int, int, int, int, int, int]  # service, ip, status, window, rule, count


class FeatureInput(ctypes.Structure):
    _fields_ = [
        ("count", ctypes.c_uint32),
        ("service", ctypes.POINTER(ctypes.c_uint32)),
        ("ip", ctypes.POINTER(ctypes.c_uint32)),
        ("status", ctypes.POINTER(ctypes.c_uint32)),
        ("window_id", ctypes.POINTER(ctypes.c_int64)),
        ("rule_index", ctypes.POINTER(ctypes.c_uint32)),
        ("event_count", ctypes.POINTER(ctypes.c_uint32)),
    ]


class WindowConfig(ctypes.Structure):
    _fields_ = [
        ("baseline_windows", ctypes.c_uint32),
        ("z_threshold", ctypes.c_float),
        ("max_rule_index", ctypes.c_uint32),
    ]


class WindowResults(ctypes.Structure):
    _fields_ = [
        ("feature_count", ctypes.c_uint32),
        ("service", ctypes.POINTER(ctypes.c_uint32)),
        ("ip", ctypes.POINTER(ctypes.c_uint32)),
        ("status", ctypes.POINTER(ctypes.c_uint32)),
        ("window_id", ctypes.POINTER(ctypes.c_int64)),
        ("rule_index", ctypes.POINTER(ctypes.c_uint32)),
        ("count", ctypes.POINTER(ctypes.c_uint32)),
        ("alert_count", ctypes.c_uint32),
        ("alert_service", ctypes.POINTER(ctypes.c_uint32)),
        ("alert_ip", ctypes.POINTER(ctypes.c_uint32)),
        ("alert_status", ctypes.POINTER(ctypes.c_uint32)),
        ("alert_window", ctypes.POINTER(ctypes.c_int64)),
        ("alert_rule", ctypes.POINTER(ctypes.c_uint32)),
        ("alert_count_out", ctypes.POINTER(ctypes.c_uint32)),
        ("alert_mean", ctypes.POINTER(ctypes.c_double)),
        ("alert_std", ctypes.POINTER(ctypes.c_double)),
        ("alert_score", ctypes.POINTER(ctypes.c_double)),
        ("timing", SentinelTiming),
    ]


@dataclass(frozen=True)
class WindowAlert:
    service: int
    ip: int
    status: int
    window: int
    rule: int
    count: int
    mean: float
    std: float
    score: float


@dataclass(frozen=True)
class WindowResult:
    features: tuple[FeatureRow, ...]
    alerts: tuple[WindowAlert, ...]
    timing: dict[str, int | float]

    @property
    def feature_count(self) -> int:
        return len(self.features)

    @property
    def alert_count(self) -> int:
        return len(self.alerts)


def _configure(lib: ctypes.CDLL) -> None:
    lib.sentinel_detect_window_gpu.argtypes = [
        ctypes.POINTER(FeatureInput), ctypes.POINTER(WindowConfig),
        ctypes.POINTER(WindowResults),
    ]
    lib.sentinel_detect_window_gpu.restype = ctypes.c_int
    lib.sentinel_free_window_results.argtypes = [ctypes.POINTER(WindowResults)]
    lib.sentinel_free_window_results.restype = None


def detect_window(
    rows: list[FeatureRow] | tuple[FeatureRow, ...],
    *,
    baseline_windows: int,
    z_threshold: float,
    max_rule_index: int = 255,
    lib: ctypes.CDLL | None = None,
    check_abi: bool = True,
) -> WindowResult:
    """Aggregate and score feature rows on the GPU window kernels."""
    if lib is None:
        lib = load_gpu_library()
    _configure(lib)
    if check_abi:
        assert_abi(lib)
    if not 1 <= baseline_windows <= 1440:
        raise ValueError("baseline_windows must be in [1, 1440]")
    if not 0.0 < z_threshold <= 1000.0:
        raise ValueError("z_threshold must be in (0, 1000]")
    if not 0 <= max_rule_index <= 255:
        raise ValueError("max_rule_index must be in [0, 255]")
    if len(rows) > MAX_FEATURES:
        raise ValueError(f"at most {MAX_FEATURES} feature rows are supported")

    n = len(rows)
    services = (ctypes.c_uint32 * n)()
    ips = (ctypes.c_uint32 * n)()
    statuses = (ctypes.c_uint32 * n)()
    windows = (ctypes.c_int64 * n)()
    rules = (ctypes.c_uint32 * n)()
    counts = (ctypes.c_uint32 * n)()
    for i, row in enumerate(rows):
        service, ip, status, window, rule, count = row
        if not (0 <= count <= UINT32_MAX) or count == 0:
            raise ValueError("feature counts must be in [1, UINT32_MAX]")
        if not 0 <= rule <= max_rule_index:
            raise ValueError("rule_index exceeds max_rule_index")
        services[i], ips[i], statuses[i] = service, ip, status
        windows[i], rules[i], counts[i] = window, rule, count

    feature_input = FeatureInput(
        n, services, ips, statuses, windows, rules, counts
    )
    config = WindowConfig(baseline_windows, ctypes.c_float(z_threshold).value,
                          max_rule_index)
    results = WindowResults()
    rc = lib.sentinel_detect_window_gpu(
        ctypes.byref(feature_input), ctypes.byref(config), ctypes.byref(results)
    )
    try:
        if rc != 0:
            raise RuntimeError(
                f"sentinel_detect_window_gpu failed: {ERROR_NAMES.get(rc, rc)}"
            )
        features = tuple(
            (
                int(results.service[i]), int(results.ip[i]),
                int(results.status[i]), int(results.window_id[i]),
                int(results.rule_index[i]), int(results.count[i]),
            )
            for i in range(results.feature_count)
        )
        alerts = tuple(
            WindowAlert(
                service=int(results.alert_service[i]),
                ip=int(results.alert_ip[i]),
                status=int(results.alert_status[i]),
                window=int(results.alert_window[i]),
                rule=int(results.alert_rule[i]),
                count=int(results.alert_count_out[i]),
                mean=float(results.alert_mean[i]),
                std=float(results.alert_std[i]),
                score=float(results.alert_score[i]),
            )
            for i in range(results.alert_count)
        )
        timing = {
            "peak_device_bytes": int(results.timing.peak_device_bytes),
            "alloc_ms": float(results.timing.alloc_ms),
            "h2d_ms": float(results.timing.h2d_ms),
            "kernel_ms": float(results.timing.kernel_ms),
            "d2h_ms": float(results.timing.d2h_ms),
            "total_ms": float(results.timing.total_ms),
        }
        return WindowResult(features, alerts, timing)
    finally:
        lib.sentinel_free_window_results(ctypes.byref(results))
