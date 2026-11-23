#!/usr/bin/env python3
"""Bounded CUDA memory-pipeline measurement (Sprint 5 / S5-D).

Streams a synthetic log file in line-aligned chunks through the native GPU
library, records the ABI phase timings, samples VRAM with ``nvidia-smi`` and
sweeps the clamped staging size (2/4/8/16 MiB). A second run can stream a file
larger than the physical 4 GB VRAM to prove that neither host nor device ever
holds the whole dataset.

No CPU/GPU speedup is inferred here: phase numbers are enqueue/wall-clock
measurements and VRAM is sampled externally. See docs/native-abi.md §6.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from sentinel.detector.native import assert_abi, detect_native, load_gpu_library

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SMALL = REPO_ROOT / "data" / "bench.log"
DEFAULT_LARGE = REPO_ROOT / "data" / "bench-stream-4g.log"
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "benchmarks" / "cuda-memory-pipeline.json"
SCHEMA_VERSION = 1
STAGE_SIZES_MIB = (2, 4, 8, 16)
MAX_STAGE_BYTES = 16 << 20


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _gpu_info() -> dict[str, Any]:
    try:
        out = subprocess.check_output([
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ], text=True, timeout=10).strip().splitlines()[0]
        name, total_mib, driver = [part.strip() for part in out.split(",")]
        return {"name": name, "total_vram_mib": int(float(total_mib)),
                "driver": driver}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        return {"name": "unavailable", "error": str(exc)}


class VramSampler:
    """Poll nvidia-smi on a background thread while the pipeline runs."""

    def __init__(self, interval: float = 0.2) -> None:
        self.interval = interval
        self.samples: list[int] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _used_mib(self) -> int | None:
        try:
            out = subprocess.check_output([
                "nvidia-smi", "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ], text=True, timeout=5).strip().splitlines()[0]
            return int(float(out.strip()))
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None

    def start(self) -> None:
        initial = self._used_mib()
        if initial is not None:
            self.samples.append(initial)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            value = self._used_mib()
            if value is not None:
                self.samples.append(value)

    def stop(self) -> dict[str, int]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        final = self._used_mib()
        if final is not None:
            self.samples.append(final)
        if not self.samples:
            return {"baseline_mib": 0, "peak_mib": 0, "samples": 0}
        return {
            "baseline_mib": min(self.samples),
            "peak_mib": max(self.samples),
            "samples": len(self.samples),
        }


def _generate_large(path: Path, template: Path, gib: float) -> None:
    target = int(gib * (1 << 30))
    block = template.read_bytes()
    if not block:
        raise RuntimeError(f"template {template} is empty")
    written = 0
    with path.open("wb") as handle:
        while written < target:
            handle.write(block)
            written += len(block)
    with path.open("rb+") as handle:
        handle.seek(target)
        tail = handle.read(4096)
        trim = tail.rfind(b"\n") + 1
        handle.truncate(target - len(tail) + trim)


def _detect_chunk(chunk: bytes, patterns: list[str], z: float, lib,
                  stage_mib: int, stats: dict[str, int]) -> Any:
    """One chunk through the full native pipeline; retry OOM once, smaller."""
    os.environ["SENTINEL_GPU_STAGE_BYTES"] = str(stage_mib << 20)
    try:
        return detect_native(chunk, patterns, z, lib=lib, backend="gpu")
    except RuntimeError as exc:
        if "OOM" not in str(exc):
            raise
        stats["oom_retries"] += 1
        os.environ["SENTINEL_GPU_STAGE_BYTES"] = str(1 << 20)
        return detect_native(chunk, patterns, z, lib=lib, backend="gpu")


def _stream(path: Path, patterns: list[str], z: float, lib,
            chunk_bytes: int, stage_mib: int, stats: dict[str, int]) -> dict[str, Any]:
    phases = ["alloc_ms", "h2d_ms", "kernel_ms", "d2h_ms", "total_ms"]
    totals = {phase: 0.0 for phase in phases}
    peak_device_bytes = 0
    carry = b""
    chunks = 0
    markers = 0
    started = time.perf_counter()

    with path.open("rb") as handle:
        while True:
            raw = handle.read(chunk_bytes)
            if not raw:
                break
            data = carry + raw
            if len(raw) == chunk_bytes:
                boundary = data.rfind(b"\n") + 1
                if boundary == 0:
                    raise RuntimeError("chunk contains no line boundary")
                chunk, carry = data[:boundary], data[boundary:]
            else:
                chunk, carry = data, b""
            if not chunk:
                continue
            result = _detect_chunk(chunk, patterns, z, lib, stage_mib, stats)
            chunks += 1
            markers += sum(1 for flags in result.line_flags if flags)
            for phase in phases:
                totals[phase] += result.timing[phase]
            peak_device_bytes = max(peak_device_bytes,
                                    result.timing["peak_device_bytes"])
    if carry:
        result = _detect_chunk(carry, patterns, z, lib, stage_mib, stats)
        chunks += 1
        markers += sum(1 for flags in result.line_flags if flags)
        for phase in phases:
            totals[phase] += result.timing[phase]
        peak_device_bytes = max(peak_device_bytes,
                                result.timing["peak_device_bytes"])

    wall_ms = (time.perf_counter() - started) * 1000.0
    size_mib = path.stat().st_size / (1 << 20)
    return {
        "input_name": path.name,
        "input_mib": round(size_mib, 2),
        "host_chunk_mib": round(chunk_bytes / (1 << 20), 2),
        "stage_mib": stage_mib,
        "chunks": chunks,
        "pattern_marker_lines": markers,
        "phase_totals_ms": {key: round(value, 3) for key, value in totals.items()},
        "wall_ms": round(wall_ms, 3),
        "throughput_mb_s": round(size_mib / (wall_ms / 1000.0), 2),
        "peak_reported_device_bytes": peak_device_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_SMALL)
    parser.add_argument("--large-input", type=Path, default=DEFAULT_LARGE)
    parser.add_argument("--large-gib", type=float, default=4.25,
                        help="generate a file larger than 4 GiB VRAM")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--patterns", nargs="*",
                        default=["ERROR", "Failed password", " 404 "])
    parser.add_argument("--z-threshold", type=float, default=3.0)
    parser.add_argument("--skip-large", action="store_true",
                        help="only run the micro-batch sweep (quick check)")
    args = parser.parse_args()

    lib = load_gpu_library()
    assert_abi(lib)
    info = _gpu_info()
    stats = {"oom_retries": 0}
    os.environ.pop("SENTINEL_GPU_STAGE_BYTES", None)

    sweep: list[dict[str, Any]] = []
    for mib in STAGE_SIZES_MIB:
        sampler = VramSampler()
        sampler.start()
        row = _stream(args.input, args.patterns, args.z_threshold, lib,
                      mib << 20, mib, stats)
        row["vram"] = sampler.stop()
        sweep.append(row)
        print(f"sweep {mib:2d} MiB: {row['throughput_mb_s']:>8.2f} MB/s, "
              f"VRAM peak {row['vram']['peak_mib']} MiB")

    large: dict[str, Any] | None = None
    if not args.skip_large:
        if not args.large_input.is_file():
            print(f"generating {args.large_gib} GiB stream file...")
            _generate_large(args.large_input, args.input, args.large_gib)
        os.environ["SENTINEL_GPU_STAGE_BYTES"] = str(MAX_STAGE_BYTES)
        sampler = VramSampler()
        sampler.start()
        large = _stream(args.large_input, args.patterns, args.z_threshold, lib,
                        MAX_STAGE_BYTES, 16, stats)
        large["vram"] = sampler.stop()
        large["input_sha256"] = _sha256(args.large_input)
        print(f"large stream: {large['input_mib']} MiB in "
              f"{large['wall_ms']:.0f} ms, VRAM peak {large['vram']['peak_mib']} MiB")

    document = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": "cpu-reference-v1",
        "semantics_doc": "docs/algorithm-semantics.md",
        "abi_doc": "docs/native-abi.md",
        "host_profile": "16g (measured on the development laptop)",
        "gpu": info,
        "input": {"name": args.input.name, "sha256": _sha256(args.input)},
        "config": {"patterns": args.patterns, "z_threshold": args.z_threshold,
                   "stage_sizes_mib": list(STAGE_SIZES_MIB)},
        "oom_retries": stats["oom_retries"],
        "micro_batch_sweep": sweep,
        "large_stream": large,
        "notes": [
            "Phase timings are CPU-side enqueue/allocation measurements from sentinel_timing; total_ms includes native synchronization, while wall_ms also includes Python file reading, ctypes marshalling and line-flag counting.",
            "VRAM is sampled externally with nvidia-smi; peak_reported_device_bytes is the library's allocation estimate.",
            "Each chunk is line-aligned; neither host nor device holds the whole dataset. K2 statistics are per chunk, not global across the stream.",
            "No CPU/GPU speedup claim is made by this benchmark.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
