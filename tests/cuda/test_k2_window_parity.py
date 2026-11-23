"""Randomized CPU/GPU parity for the windowed K2 scorer (Sprint 5 / S5-C).

The Python reference from Sprint 4 is the oracle. Feature aggregation counts
must match exactly; floating-point mean/std/score use the documented
tolerance ``abs=1e-6, rel=1e-6``. Native error paths (zero counts, uint32
overflow) are covered separately.
"""

from __future__ import annotations

import hashlib
import random

import pytest

from sentinel.detector.native import DEFAULT_GPU_LIB_PATH
from sentinel.detector.pipeline import detect
from sentinel.detector.semantics import Rule, DetectorSettings
from sentinel.detector.window_native import FeatureRow, detect_window, load_gpu_library

pytestmark = pytest.mark.skipif(
    not DEFAULT_GPU_LIB_PATH.is_file(),
    reason="build/libsentinel.so missing (run make lib)",
)

RULES = (
    Rule("r0", "ERROR", "high", 0),
    Rule("r1", "WARN", "medium", 1),
    Rule("r2", "Failed", "critical", 2),
)
RULE_INDEX = {"r0": 0, "r1": 1, "r2": 2}
SERVICES = ("sshd", "nginx", "app-server", "kernel", "auth-daemon")


@pytest.fixture(scope="module")
def gpu_lib():
    lib = load_gpu_library()
    assert lib.sentinel_abi_version() == 2
    return lib


def _settings(baseline: int = 4, z: float = 1.5) -> DetectorSettings:
    return DetectorSettings(rules=RULES, baseline_windows=baseline, z_threshold=z)


def _line(minute: int, service: str, ip: str, status: int, message: str) -> bytes:
    return (
        f"Jan  5 00:{minute % 60:02d}:00 lab {service}[1]: {message} "
        f'"GET /api HTTP/1.1" {status} {ip}\n'
    ).encode()


def _dataset(seed: int, keys: int, minutes: int = 8) -> bytes:
    rng = random.Random(seed)
    lines: list[bytes] = []
    for key in range(keys):
        service = SERVICES[key % len(SERVICES)]
        ip = f"10.{(key >> 8) & 255}.{key & 255}.1"
        base = rng.choice((1, 2, 3))
        for minute in range(minutes):
            # A spike in the last window produces alerts for several series.
            count = base + (12 if minute == minutes - 1 else minute % 3)
            message = rng.choice(("ERROR", "WARN", "Failed", "ERROR WARN"))
            for _ in range(count):
                lines.append(_line(minute, service, ip, 200 + (key % 4), message))
    rng.shuffle(lines)
    return b"".join(lines)


def _rows(cpu_result, settings: DetectorSettings) -> list[FeatureRow]:
    rows: list[FeatureRow] = []
    for feature in cpu_result.features:
        service, ip, status = feature.key
        rule = RULE_INDEX[feature.rule_id]
        # Emit one row per event so the GPU aggregation path is exercised even
        # though the Python oracle already provides aggregated counts.
        for _ in range(feature.count):
            rows.append((service, ip, status, feature.window_id, rule, 1))
    return rows


def _run(data: bytes, settings: DetectorSettings, lib):
    cpu = detect(data, settings=settings, source_sha256=hashlib.sha256(data).hexdigest())
    gpu = detect_window(
        _rows(cpu, settings), baseline_windows=settings.baseline_windows,
        z_threshold=settings.z_threshold, max_rule_index=2, lib=lib,
    )
    return cpu, gpu


def _expected_features(cpu_result) -> tuple[FeatureRow, ...]:
    return tuple(sorted(
        (feature.key[0], feature.key[1], feature.key[2], feature.window_id,
         RULE_INDEX[feature.rule_id], feature.count)
        for feature in cpu_result.features
    ))


def _actual_features(gpu_result) -> tuple[FeatureRow, ...]:
    return tuple(sorted(
        (row[0], row[1], row[2], row[3], row[4], row[5])
        for row in gpu_result.features
    ))


def _alert_identity(window: int, service: int, ip: int, status: int, rule: int):
    return (window, service, ip, status, rule)


def _assert_alerts_match(cpu_result, gpu_result) -> None:
    expected = {
        _alert_identity(a.window_id, a.service_id, a.ip, a.status,
                        RULE_INDEX[a.rule_id]): (a.count, a.mean, a.std, a.score)
        for a in cpu_result.alerts
    }
    actual = {
        _alert_identity(a.window, a.service, a.ip, a.status, a.rule):
            (a.count, a.mean, a.std, a.score)
        for a in gpu_result.alerts
    }
    assert set(actual) == set(expected)
    for key, e in expected.items():
        a = actual[key]
        assert a[0] == e[0]
        assert a[1] == pytest.approx(e[1], abs=1e-6, rel=1e-6)
        assert a[2] == pytest.approx(e[2], abs=1e-6, rel=1e-6)
        assert a[3] == pytest.approx(e[3], abs=1e-6, rel=1e-6)


@pytest.mark.parametrize("seed,keys", [(1, 3), (7, 8), (23, 16)])
def test_randomized_window_aggregation_and_scores(seed: int, keys: int, gpu_lib) -> None:
    settings = _settings()
    cpu, gpu = _run(_dataset(seed, keys), settings, gpu_lib)
    assert len(cpu.features) > 0
    assert cpu.metrics["alerts"] > 0, "fixture should produce non-vacuous alerts"
    assert _actual_features(gpu) == _expected_features(cpu)
    _assert_alerts_match(cpu, gpu)
    assert gpu.timing["peak_device_bytes"] > 0


def test_zero_variance_history_suppresses_alerts(gpu_lib) -> None:
    lines = [
        _line(minute, "sshd", "10.0.0.1", 200, "ERROR")
        for minute in range(6) for _ in range(2)
    ]
    cpu, gpu = _run(b"".join(lines), _settings(z=0.5), gpu_lib)
    assert cpu.metrics["alerts"] == 0
    assert gpu.alert_count == 0
    assert _actual_features(gpu) == _expected_features(cpu)


def test_warmup_requires_two_history_points(gpu_lib) -> None:
    lines = [_line(0, "nginx", "10.0.0.2", 200, "WARN")]
    lines += [_line(1, "nginx", "10.0.0.2", 200, "WARN") for _ in range(20)]
    cpu, gpu = _run(b"".join(lines), _settings(baseline=8), gpu_lib)
    assert cpu.metrics["alerts"] == 0  # window 1 has only one prior window
    assert gpu.alert_count == 0


def test_high_and_low_key_contention(gpu_lib) -> None:
    for keys in (1, 64):
        cpu, gpu = _run(_dataset(99, keys), _settings(), gpu_lib)
        assert _actual_features(gpu) == _expected_features(cpu)
        _assert_alerts_match(cpu, gpu)


def test_large_counts_aggregate_exactly(gpu_lib) -> None:
    # Direct feature rows avoid generating billions of log lines while still
    # exercising uint32 aggregation near the documented limit.
    big = 4_000_000_000
    rows: list[FeatureRow] = [
        (1, 0x0A000001, 0, 10, 0, big),
        (1, 0x0A000001, 0, 10, 0, 200_000_000),
        (1, 0x0A000001, 0, 11, 0, 5),
    ]
    gpu = detect_window(rows, baseline_windows=2, z_threshold=0.5,
                        max_rule_index=2, lib=gpu_lib)
    aggregated = {(r[3], r[4]): r for r in gpu.features}
    assert len(aggregated) == 2
    assert aggregated[(10, 0)][5] == big + 200_000_000
    assert aggregated[(11, 0)][5] == 5


def test_uint32_overflow_and_validation_are_rejected(gpu_lib) -> None:
    overflowing: list[FeatureRow] = [
        (1, 0x0A000001, 0, 10, 0, 2**32 - 1),
        (1, 0x0A000001, 0, 10, 0, 1),
    ]
    with pytest.raises(RuntimeError, match="INVALID"):
        detect_window(overflowing, baseline_windows=2, z_threshold=1.0,
                      max_rule_index=2, lib=gpu_lib)
    with pytest.raises(ValueError, match="counts"):
        detect_window([(1, 1, 0, 1, 0, 0)], baseline_windows=2,
                      z_threshold=1.0, lib=gpu_lib)
    with pytest.raises(ValueError, match="baseline_windows"):
        detect_window([], baseline_windows=0, z_threshold=1.0, lib=gpu_lib)
