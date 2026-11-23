"""Stable CPU baseline records (Sprint 4-D).

Separates the single-thread reference timing from optional multi-thread worker
throughput. Timings are never mixed into logical detector output.
"""

from __future__ import annotations

import csv
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .pipeline import detect
from .semantics import ALGORITHM_VERSION, SEMANTICS_DOC, DetectorSettings

BASELINE_SCHEMA_VERSION = 1
CSV_FIELDS = [
    "schema_version", "algorithm_version", "mode", "threads", "input_name",
    "input_bytes", "line_count", "config_hash", "rules_count", "repeats",
    "best_ms", "mean_ms", "throughput_mb_s", "matches", "features", "alerts",
    "semantics_doc",
]


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _line_aligned_blocks(data: bytes, parts: int) -> list[tuple[int, int]]:
    """Split at LF boundaries into ``parts`` non-empty byte ranges."""
    if parts < 2 or len(data) < parts:
        return [(0, len(data))]
    targets = [round(len(data) * i / parts) for i in range(1, parts)]
    boundaries = {0, len(data)}
    for target in targets:
        pos = data.find(b"\n", max(0, target - 64), min(len(data), target + 64))
        if pos < 0:
            pos = data.find(b"\n", target)
        if 0 < pos < len(data):
            boundaries.add(pos + 1)
    ordered = sorted(boundaries)
    return list(zip(ordered, ordered[1:]))


def _threaded_detect(data: bytes, settings: DetectorSettings,
                     source_sha256: str, threads: int) -> dict[str, int]:
    """Throughput-only parallel probe; per-block histories are independent."""
    blocks = _line_aligned_blocks(data, threads)
    starts: list[int] = []
    line_no = 1
    for start, end in blocks:
        starts.append(line_no)
        line_no += data[start:end].count(b"\n") + (
            1 if end > start and not data[end - 1:end] == b"\n" else 0
        )
    with ThreadPoolExecutor(max_workers=len(blocks)) as pool:
        futures = [
            pool.submit(
                detect, data[start:end], settings=settings,
                source_sha256=source_sha256, run_id="baseline",
                chunk_id=f"thread-{index}", line_number_start=starts[index],
            )
            for index, (start, end) in enumerate(blocks)
        ]
        results = [future.result() for future in futures]
    return {
        "line_count": sum(r.line_count for r in results),
        "matches": sum(r.metrics["matches"] for r in results),
        "features": sum(r.metrics["features"] for r in results),
        "alerts": sum(r.metrics["alerts"] for r in results),
    }


def run_baseline(
    input_path: str | Path,
    settings: DetectorSettings,
    *,
    repeats: int = 3,
    threads: int = 1,
    output_dir: str | Path = "results",
    native_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Measure and save baseline rows; return the JSON document."""
    if repeats < 1:
        raise ValueError("repeats must be positive")
    if threads < 1:
        raise ValueError("threads must be positive")
    path = Path(input_path)
    data = path.read_bytes()
    source_sha256 = file_sha256(data)
    modes = [("cpu-reference-single-thread", 1)]
    if threads > 1:
        modes.append(("cpu-worker-threaded", threads))

    rows: list[dict[str, Any]] = []
    for mode, worker_count in modes:
        times: list[float] = []
        metrics: dict[str, int] = {}
        for _ in range(repeats):
            started = time.perf_counter()
            if worker_count == 1:
                result = detect(
                    data, settings=settings, source_sha256=source_sha256,
                    run_id="baseline", chunk_id="single-thread",
                )
                metrics = {
                    "line_count": result.line_count,
                    "matches": result.metrics["matches"],
                    "features": result.metrics["features"],
                    "alerts": result.metrics["alerts"],
                }
            else:
                metrics = _threaded_detect(data, settings, source_sha256, worker_count)
            times.append((time.perf_counter() - started) * 1000.0)
        best = min(times)
        rows.append({
            "schema_version": BASELINE_SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "mode": mode,
            "threads": worker_count,
            "input_name": path.name,
            "input_bytes": len(data),
            "line_count": metrics["line_count"],
            "config_hash": settings.config_hash,
            "rules_count": len(settings.rules),
            "repeats": repeats,
            "best_ms": round(best, 3),
            "mean_ms": round(sum(times) / len(times), 3),
            "throughput_mb_s": round(len(data) / 1_000_000 / (best / 1000.0), 2),
            "matches": metrics["matches"],
            "features": metrics["features"],
            "alerts": metrics["alerts"],
            "semantics_doc": SEMANTICS_DOC,
        })

    document = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "semantics_doc": SEMANTICS_DOC,
        "input": {
            "name": path.name,
            "bytes": len(data),
            "sha256": source_sha256,
        },
        "detector": {
            "config_hash": settings.config_hash,
            "rules": [r.rule_id for r in settings.rules],
            "window_seconds": settings.window_seconds,
            "baseline_windows": settings.baseline_windows,
            "z_threshold": settings.z_threshold,
            "max_line_bytes": settings.max_line_bytes,
        },
        "native": native_stats,
        "rows": rows,
    }

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "cpu-baseline.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with (target / "cpu-baseline.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in CSV_FIELDS})
    return document
