"""Load saved evidence and detect shipped capabilities without importing them."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_DIR = REPO_ROOT / "docs" / "benchmarks"
EVALUATION_DIR = REPO_ROOT / "docs" / "evaluation"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def benchmark_evidence() -> dict[str, Any]:
    """Return only values that exist in committed evidence files."""
    cpu = _load_json(BENCHMARK_DIR / "cpu-baseline.json") or {}
    cuda = _load_json(BENCHMARK_DIR / "cuda-memory-pipeline.json") or {}
    parity = _load_json(BENCHMARK_DIR / "kernel-parity.json") or {}
    evaluation = _load_json(EVALUATION_DIR / "sample-evaluation.json")
    return {
        "cpu": cpu,
        "cuda": cuda,
        "parity": parity,
        "evaluation": evaluation,
        "paths": {
            "cpu": "docs/benchmarks/cpu-baseline.json",
            "cuda": "docs/benchmarks/cuda-memory-pipeline.json",
            "parity": "docs/benchmarks/kernel-parity.json",
            "evaluation": "docs/evaluation/sample-evaluation.json",
            "report": "docs/evaluation/benchmark-report.md",
        },
    }


def capability_rows() -> list[dict[str, str]]:
    """Report code presence honestly; presence is not the same as a run."""
    checks = [
        ("S1–S2", "Infrastructure + ingestion contract", "terraform/queues.tf"),
        ("S3", "Run API + producer", "src/sentinel/api/main.py"),
        ("S4", "CPU reference detector", "src/sentinel/detector/pipeline.py"),
        ("S5", "CUDA kernels + ABI v2", "include/sentinel/sentinel.h"),
        ("S6", "Workers, leases, outbox, DLQ", "src/sentinel/worker"),
        ("S7", "Artifacts + metrics + dashboard", "src/sentinel/artifacts"),
        ("S8", "Evaluation + demo runbook", "src/sentinel/evaluation"),
    ]
    rows: list[dict[str, str]] = []
    for sprint, label, relative in checks:
        path = REPO_ROOT / relative
        present = path.exists()
        rows.append(
            {
                "sprint": sprint,
                "component": label,
                "status": "Shipped" if present else "Planned",
                "check": relative,
            }
        )
    return rows


def module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def not_run_items() -> list[str]:
    return [
        "five-repetition median/IQR workload matrix",
        "full end-to-end pipeline benchmark",
        "pageable-memory baseline",
        "PR-AUC (score outputs and labels do not yet support a meaningful curve)",
        "multi-GPU or multi-host scalability study",
    ]


def format_ms(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):,.1f} ms"
    return "—"
