"""Ground-truth label manifest helpers (Sprint 3-C).

Labels live in their own JSON document.  Workers are given only the dataset
object reference, so injected-scenario metadata can never leak into detection
or output.  The helpers here resolve the manifest's deterministic line ranges
against the chunker's stable line IDs for evaluation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .ids import line_id_for

LABEL_SCHEMA_VERSION = 1


def load_manifest(path: str | Path) -> dict[str, Any]:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != LABEL_SCHEMA_VERSION:
        raise ValueError("unsupported ground-truth manifest schema_version")
    if not manifest.get("synthetic"):
        raise ValueError("manifest is not marked synthetic")
    return manifest


def scenario_line_ids(
    manifest: dict[str, Any], scenario: dict[str, Any]
) -> tuple[str, str]:
    """First and last stable line ID of a labelled scenario range."""
    source_sha = manifest["source"]["sha256"]
    span = scenario["line_range"]
    return (
        line_id_for(source_sha, span["start"]),
        line_id_for(source_sha, span["end"]),
    )


def labels_for_chunk(
    manifest: dict[str, Any], chunk: Any
) -> list[dict[str, Any]]:
    """Scenarios touching ``chunk`` (a ChunkRecord-like object).

    The returned line-ID range is clipped to the chunk, so callers can join
    detections to labels without reading the dataset again.
    """
    source_sha = manifest["source"]["sha256"]
    matches: list[dict[str, Any]] = []
    for scenario in manifest["scenarios"]:
        span = scenario["line_range"]
        first = max(span["start"], chunk.line_start)
        last = min(span["end"], chunk.line_end)
        if first > last:
            continue
        matches.append(
            {
                "scenario_id": scenario["scenario_id"],
                "kind": scenario["kind"],
                "expected_label": scenario["expected"]["label"],
                "evaluation_unit": scenario["expected"]["evaluation_unit"],
                "window_id": scenario["window"]["id"],
                "key": scenario["key"],
                "line_ids": (
                    line_id_for(source_sha, first),
                    line_id_for(source_sha, last),
                ),
                "line_range": {"start": first, "end": last},
            }
        )
    return matches


def iter_window_labels(manifest: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield one evaluation label per injected window."""
    for scenario in manifest["scenarios"]:
        yield {
            "window_id": scenario["window"]["id"],
            "window_seconds": scenario["window"]["seconds"],
            "key": scenario["key"],
            "scenario_id": scenario["scenario_id"],
            "label": scenario["expected"]["label"],
            "synthetic": True,
        }
