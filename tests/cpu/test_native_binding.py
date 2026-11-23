"""Native ABI binding correctness (K1 flags and K2 statistics)."""

from __future__ import annotations

import math
import random
import re

import pytest

from sentinel.detector.native import DEFAULT_LIB_PATH, detect_native
from sentinel.detector.records import parse_records

pytestmark = pytest.mark.skipif(
    not DEFAULT_LIB_PATH.is_file(),
    reason="build/libsentinel_cpu.so missing (run make lib-cpu)",
)

PATTERNS = ["Failed password", "Invalid user", "ERROR", " 404 ", "SEGFAULT"]
IPV4 = re.compile(rb"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
HMS = re.compile(rb"(\d{2}):(\d{2}):(\d{2})")
SERVICE_TAGS = (
    (b"sshd[", 1), (b"nginx[", 2), (b"app-server[", 3),
    (b"kernel[", 4), (b"auth-daemon[", 5),
)


def _records(data: bytes) -> list[bytes]:
    return [record.raw for record in parse_records(data, source_sha256="s")]


def _k1_oracle(data: bytes) -> list[int]:
    flags = []
    for record in _records(data):
        value = 0
        for index, pattern in enumerate(PATTERNS):
            if pattern.encode() in record:
                value |= 1 << index
        flags.append(value)
    return flags


def _cell(ip: int, bucket: int, service: int) -> int:
    return (((ip << 16) | ((bucket & 0x7FF) << 5) | (service & 0x1F))
            | (1 << 62)) & 0xFFFFFFFFFFFFFFFF


def _slot(cell: int) -> int:
    return ((cell * 0x9E3779B97F4A7C15) >> 32) & ((1 << 21) - 1)


def _k2_oracle(data: bytes, threshold: float = 4.0) -> dict[str, object]:
    counts: dict[tuple[int, int, int], int] = {}
    for record in _records(data):
        ip = 0
        for match in IPV4.finditer(record):
            octets = [int(part) for part in match.group(1).split(b".")]
            if all(part <= 255 for part in octets):
                ip = (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
                break
        moment = HMS.search(record)
        bucket = 0
        if moment:
            hour, minute, second = map(int, moment.groups())
            if hour <= 23 and minute <= 59 and second <= 59:
                bucket = hour * 60 + minute
        service = 0
        for tag, value in SERVICE_TAGS:
            if tag in record:
                service = value
                break
        key = (ip, bucket, service)
        counts[key] = counts.get(key, 0) + 1

    n = len(counts)
    total = sum(counts.values())
    sumsq = sum(value * value for value in counts.values())
    mean = total / n if n else 0.0
    var = (sumsq / n - mean * mean) if n else 0.0
    std = math.sqrt(var) if var > 0 else 0.0
    anomalies: list[tuple[int, int, int, int]] = []
    if n >= 32 and std > 0:
        for (ip, bucket, service), count in counts.items():
            if (count - mean) / std >= threshold:
                anomalies.append((ip, bucket, service, count))
        anomalies.sort(key=lambda item: (item[1], item[2], item[0]))
    return {"cells": n, "mean": mean, "std": std, "anomalies": anomalies}


def test_native_k1_matches_pure_byte_matcher(golden_bytes: bytes) -> None:
    result = detect_native(golden_bytes, PATTERNS)
    assert result.line_count == len(_records(golden_bytes))
    assert list(result.line_flags) == _k1_oracle(golden_bytes)
    assert sum(1 for flags in result.line_flags if flags) > 0


def test_native_k2_stats_match_independent_oracle(golden_bytes: bytes) -> None:
    for data in (golden_bytes, _many_cells()):
        actual = detect_native(data, PATTERNS)
        expected = _k2_oracle(data)
        assert actual.cells == expected["cells"]
        assert actual.mean == pytest.approx(expected["mean"], abs=1e-9)
        assert actual.std == pytest.approx(expected["std"], abs=1e-9)
        assert list(actual.anomalies) == expected["anomalies"]


def _many_cells(count: int = 500) -> bytes:
    lines: list[bytes] = []
    for index in range(count):
        bucket = index % 1400
        hour, minute = divmod(bucket, 60)
        octets = (10, (index >> 16) & 255, (index >> 8) & 255, index & 255 or 1)
        ip = ".".join(str(part) for part in octets).encode()
        lines.append(
            f"Jan  5 {hour:02d}:{minute:02d}:00 lab sshd[1]: "
            f"Failed password from {ip.decode()}\n".encode()
        )
    return b"".join(lines)


def _ip_string(value: int) -> str:
    return ".".join(str((value >> shift) & 255) for shift in (24, 16, 8, 0))


def test_native_open_addressing_resolves_hash_slot_collisions() -> None:
    # Find two distinct cells whose 2^21-slot hash buckets collide (birthday
    # search; int hashing is deterministic across Python runs).
    slots: dict[int, int] = {}
    pair: tuple[int, int] | None = None
    rng = random.Random(20261123)
    for _ in range(6000):
        ip = rng.randrange(1, 1 << 30)
        cell = _cell(ip, 0, 1)
        slot = _slot(cell)
        if slot in slots and slots[slot] != cell:
            pair = (slots[slot], cell)
            break
        slots[slot] = cell
    assert pair is not None, "failed to construct a hash-slot collision"
    first, second = pair
    mask = ~(1 << 62)
    ip_a, ip_b = (first & mask) >> 16, (second & mask) >> 16  # bucket=0, service=1
    assert ip_a != ip_b

    lines = [
        f"Jan  5 00:00:00 lab sshd[1]: Failed password from {_ip_string(ip_a)}\n",
        f"Jan  5 00:00:00 lab sshd[1]: Failed password from {_ip_string(ip_b)}\n",
    ]
    for filler in range(30):
        bucket = filler + 1
        hour, minute = divmod(bucket, 60)
        lines.append(
            f"Jan  5 {hour:02d}:{minute:02d}:00 lab sshd[1]: "
            f"Failed password from 10.1.{filler // 256}.{filler % 256 + 1}\n"
        )
    data = "".join(lines).encode()
    result = detect_native(data, PATTERNS)
    assert result.cells == 32  # both colliding keys survived linear probing
    assert list(result.line_flags) == _k1_oracle(data)
    expected = _k2_oracle(data)
    assert list(result.anomalies) == expected["anomalies"]


def test_native_empty_and_validation() -> None:
    empty = detect_native(b"", PATTERNS)
    assert empty.cells == 0 and empty.mean == 0.0 and empty.std == 0.0
    assert empty.line_flags == () and empty.anomalies == ()
    with pytest.raises(ValueError, match="63 bytes"):
        detect_native(b"x\n", ["x" * 64])
    with pytest.raises(ValueError, match="32 patterns"):
        detect_native(b"x\n", [f"p{i}" for i in range(33)])
