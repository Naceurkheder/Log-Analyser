"""ctypes binding for the native CPU reference (libsentinel_cpu.so).

The binding is intentionally thin: it tokenizes bytes with the shared native
tokenizer, calls ``sentinel_detect_cpu`` and copies the results into plain
Python values before freeing all native allocations. It has no web/queue/storage
dependencies. The .so must be built with ``make lib-cpu`` first.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LIB_PATH = REPO_ROOT / "build" / "libsentinel_cpu.so"
MAX_PATTERNS = 32
MAX_PATTERN_LEN = 63


class NativeLibraryUnavailable(RuntimeError):
    """Raised when the CPU shared library is missing or cannot be loaded."""


class SentinelConfig(ctypes.Structure):
    _fields_ = [
        ("patterns", ctypes.c_char_p * MAX_PATTERNS),
        ("pattern_count", ctypes.c_uint32),
        ("z_threshold", ctypes.c_float),
    ]


class SentinelInput(ctypes.Structure):
    _fields_ = [
        ("data", ctypes.POINTER(ctypes.c_uint8)),
        ("data_len", ctypes.c_size_t),
        ("line_count", ctypes.c_uint32),
        ("line_offsets", ctypes.POINTER(ctypes.c_uint32)),
        ("line_ip", ctypes.POINTER(ctypes.c_uint32)),
        ("line_bucket", ctypes.POINTER(ctypes.c_uint16)),
        ("line_service", ctypes.POINTER(ctypes.c_uint8)),
    ]


class SentinelResults(ctypes.Structure):
    _fields_ = [
        ("line_flags", ctypes.POINTER(ctypes.c_uint32)),
        ("anomaly_count", ctypes.c_uint32),
        ("anomaly_records", ctypes.POINTER(ctypes.c_uint32)),
        ("cells", ctypes.c_uint64),
        ("mean", ctypes.c_double),
        ("std", ctypes.c_double),
    ]


@dataclass(frozen=True)
class NativeResult:
    line_count: int
    line_flags: tuple[int, ...]
    anomalies: tuple[tuple[int, int, int, int], ...]  # ip, bucket, service, count
    cells: int
    mean: float
    std: float

    def to_dict(self) -> dict[str, object]:
        return {
            "line_count": self.line_count,
            "line_flags": list(self.line_flags),
            "anomalies": [list(a) for a in self.anomalies],
            "cells": self.cells,
            "mean": self.mean,
            "std": self.std,
        }


def library_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    env = os.environ.get("SENTINEL_CPU_LIB")
    return Path(env) if env else DEFAULT_LIB_PATH


def load_library(path: str | Path | None = None) -> ctypes.CDLL:
    target = library_path(path)
    if not target.is_file():
        raise NativeLibraryUnavailable(
            f"native CPU library not found at {target}; run 'make lib-cpu'"
        )
    lib = ctypes.CDLL(str(target))
    lib.sentinel_tokenize.argtypes = [
        ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32)),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint32)),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint16)),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    lib.sentinel_tokenize.restype = ctypes.c_int
    lib.sentinel_detect_cpu.argtypes = [
        ctypes.POINTER(SentinelInput), ctypes.POINTER(SentinelConfig),
        ctypes.POINTER(SentinelResults),
    ]
    lib.sentinel_detect_cpu.restype = ctypes.c_int
    lib.sentinel_free_results.argtypes = [ctypes.POINTER(SentinelResults)]
    lib.sentinel_free_results.restype = None
    return lib


@dataclass
class _Tokenized:
    data_buffer: ctypes.Array
    offsets: ctypes.Array
    ips: ctypes.Array
    buckets: ctypes.Array
    services: ctypes.Array
    count: int

    def as_input(self) -> SentinelInput:
        return SentinelInput(
            data=ctypes.cast(self.data_buffer, ctypes.POINTER(ctypes.c_uint8)),
            data_len=len(self.data_buffer), line_count=self.count,
            line_offsets=self.offsets, line_ip=self.ips,
            line_bucket=self.buckets, line_service=self.services,
        )

    def free(self, libc: ctypes.CDLL) -> None:
        for pointer in (self.offsets, self.ips, self.buckets, self.services):
            libc.free(ctypes.cast(pointer, ctypes.c_void_p))


def tokenize_native(data: bytes, lib: ctypes.CDLL | None = None) -> _Tokenized:
    lib = lib or load_library()
    libc = ctypes.CDLL(None)
    buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
    offsets = ctypes.POINTER(ctypes.c_uint32)()
    ips = ctypes.POINTER(ctypes.c_uint32)()
    buckets = ctypes.POINTER(ctypes.c_uint16)()
    services = ctypes.POINTER(ctypes.c_uint8)()
    count = ctypes.c_uint32(0)
    rc = lib.sentinel_tokenize(
        buffer, len(data), ctypes.byref(offsets), ctypes.byref(ips),
        ctypes.byref(buckets), ctypes.byref(services), ctypes.byref(count),
    )
    if rc != 0:
        raise RuntimeError(f"sentinel_tokenize failed with code {rc}")
    return _Tokenized(buffer, offsets, ips, buckets, services, count.value)


def detect_native(
    data: bytes,
    patterns: list[str] | tuple[str, ...],
    z_threshold: float = 4.0,
    *,
    lib: ctypes.CDLL | None = None,
) -> NativeResult:
    """Run the documented native K1/K2 ABI over raw bytes."""
    lib = lib or load_library()
    if len(patterns) > MAX_PATTERNS:
        raise ValueError(f"native ABI supports at most {MAX_PATTERNS} patterns")
    encoded = [pattern.encode("utf-8") for pattern in patterns]
    for pattern in encoded:
        if not 1 <= len(pattern) <= MAX_PATTERN_LEN:
            raise ValueError(
                f"native patterns must be 1..{MAX_PATTERN_LEN} bytes"
            )
    config = SentinelConfig()
    for index, pattern in enumerate(encoded):
        config.patterns[index] = pattern
    config.pattern_count = len(encoded)
    config.z_threshold = ctypes.c_float(z_threshold).value

    tokenized = tokenize_native(data, lib)
    native_input = tokenized.as_input()
    results = SentinelResults()
    rc = lib.sentinel_detect_cpu(
        ctypes.byref(native_input), ctypes.byref(config), ctypes.byref(results)
    )
    if rc != 0:
        lib.sentinel_free_results(ctypes.byref(results))
        tokenized.free(ctypes.CDLL(None))
        raise RuntimeError(f"sentinel_detect_cpu failed with code {rc}")

    try:
        n = tokenized.count
        flags = tuple(results.line_flags[i] for i in range(n))
        anomalies = tuple(
            (
                results.anomaly_records[4 * i + 0],
                results.anomaly_records[4 * i + 1],
                results.anomaly_records[4 * i + 2],
                results.anomaly_records[4 * i + 3],
            )
            for i in range(results.anomaly_count)
        )
        return NativeResult(
            line_count=n, line_flags=flags, anomalies=anomalies,
            cells=int(results.cells), mean=float(results.mean), std=float(results.std),
        )
    finally:
        lib.sentinel_free_results(ctypes.byref(results))
        tokenized.free(ctypes.CDLL(None))
