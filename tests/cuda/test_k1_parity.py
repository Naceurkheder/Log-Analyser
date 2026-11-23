"""Randomized CPU/GPU equivalence for the native K1/K2 ABI (Sprint 5 / S5-A).

The GPU library is a drop-in replacement for ``libsentinel_cpu.so``: same
struct layouts, same tokenizer, same anomaly ordering. These tests exercise
that contract over randomized inputs, including a chunk boundary larger than
the default 16 MiB staging buffer.

Run after ``make lib lib-cpu`` (or ``make test-cuda``).
"""

from __future__ import annotations

import random

import pytest

from sentinel.detector.native import (
    DEFAULT_GPU_LIB_PATH,
    DEFAULT_LIB_PATH,
    assert_abi,
    detect_native,
    load_gpu_library,
    load_library,
)

pytestmark = pytest.mark.skipif(
    not (DEFAULT_LIB_PATH.is_file() and DEFAULT_GPU_LIB_PATH.is_file()),
    reason="native libraries missing (run make lib lib-cpu)",
)

PATTERNS = ["Failed password", "ERROR", " 404 ", "SEGFAULT", "retry"]
MESSAGES = (
    "ERROR db connection timeout",
    "Failed password from",
    "GET /missing HTTP/1.1 404",
    "kernel: SEGFAULT at 0x0",
    "retry scheduled",
    "health check ok",
)
SERVICES = ("sshd", "nginx", "app-server", "kernel", "auth-daemon")


@pytest.fixture(scope="module")
def cpu_lib():
    lib = load_library()
    assert_abi(lib)
    return lib


@pytest.fixture(scope="module")
def gpu_lib():
    lib = load_gpu_library()
    assert_abi(lib)
    return lib


def _line(index: int, rng: random.Random) -> bytes:
    bucket = index % 1440
    hour, minute = divmod(bucket, 60)
    ip = f"10.{(index >> 16) & 255}.{(index >> 8) & 255}.{index & 255 or 1}"
    service = SERVICES[index % len(SERVICES)]
    message = MESSAGES[rng.randrange(len(MESSAGES))]
    suffix = f" {ip}" if message.endswith("from") else ""
    return (
        f"Jan  5 {hour:02d}:{minute:02d}:00 lab {service}[1]: "
        f"{message}{suffix}\n"
    ).encode()


def _dataset(seed: int, lines: int) -> bytes:
    rng = random.Random(seed)
    return b"".join(_line(i, rng) for i in range(lines))


def _assert_parity(data: bytes, cpu_lib, gpu_lib, z: float = 3.0) -> tuple:
    cpu = detect_native(data, PATTERNS, z, lib=cpu_lib, backend="cpu")
    gpu = detect_native(data, PATTERNS, z, lib=gpu_lib, backend="gpu")
    assert gpu.line_count == cpu.line_count
    assert list(gpu.line_flags) == list(cpu.line_flags)
    assert gpu.cells == cpu.cells
    assert gpu.mean == pytest.approx(cpu.mean, abs=1e-12)
    assert gpu.std == pytest.approx(cpu.std, abs=1e-12)
    assert list(gpu.anomalies) == list(cpu.anomalies)
    return cpu, gpu


@pytest.mark.parametrize("lines,seed", [(1, 1), (17, 7), (257, 11), (4096, 23)])
def test_randomized_k1_and_k2_parity(lines: int, seed: int, cpu_lib, gpu_lib) -> None:
    _assert_parity(_dataset(seed, lines), cpu_lib, gpu_lib)


def test_empty_input_is_identical(cpu_lib, gpu_lib) -> None:
    cpu, gpu = _assert_parity(b"", cpu_lib, gpu_lib)
    assert cpu.line_flags == () and gpu.anomalies == ()


def test_staging_boundary_crosses_16mib(cpu_lib, gpu_lib) -> None:
    # ~70 bytes/line => ~17 MiB, which forces two line-aligned staging chunks
    # through the GPU path while the CPU path processes one contiguous buffer.
    data = _dataset(4242, 320_000)
    assert len(data) > (16 << 20)
    _assert_parity(data, cpu_lib, gpu_lib, z=2.5)


def test_stage_size_environment_is_bounded(
    cpu_lib, gpu_lib, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _dataset(99, 20_000)
    monkeypatch.setenv("SENTINEL_GPU_STAGE_BYTES", "1")
    small, small_gpu = _assert_parity(data, cpu_lib, gpu_lib)
    monkeypatch.delenv("SENTINEL_GPU_STAGE_BYTES")
    _, default_gpu = _assert_parity(data, cpu_lib, gpu_lib)
    assert small.timing["peak_device_bytes"] == 0
    assert 0 < small_gpu.timing["peak_device_bytes"] < default_gpu.timing["peak_device_bytes"]
    for phase in ("alloc_ms", "h2d_ms", "kernel_ms", "d2h_ms", "total_ms"):
        assert small_gpu.timing[phase] >= 0.0
        assert small.timing[phase] == 0.0


def test_too_large_stage_env_is_clamped(cpu_lib, gpu_lib, monkeypatch) -> None:
    data = _dataset(123, 4_000)
    monkeypatch.setenv("SENTINEL_GPU_STAGE_BYTES", str(1 << 40))
    _, clamped = _assert_parity(data, cpu_lib, gpu_lib)
    monkeypatch.delenv("SENTINEL_GPU_STAGE_BYTES")
    _, default = _assert_parity(data, cpu_lib, gpu_lib)
    assert clamped.timing["peak_device_bytes"] == default.timing["peak_device_bytes"]
