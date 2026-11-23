#!/usr/bin/env python3
"""Render the Sprint 8 benchmark report from saved JSON evidence.

Inputs are the committed benchmark JSON files plus an optional evaluation
report.  This script does **not** run benchmarks; every number it prints comes
from a saved file.  Hardware/end-to-end repetitions that were not run are
listed under ``Not run``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

CPU_BASELINE = Path("docs/benchmarks/cpu-baseline.json")
CUDA_MEMORY = Path("docs/benchmarks/cuda-memory-pipeline.json")
KERNEL_PARITY = Path("docs/benchmarks/kernel-parity.json")
EVALUATION_REPORT = Path("docs/evaluation/sample-evaluation.json")
OUTPUT = Path("docs/evaluation/benchmark-report.md")


def load_kernel_scope() -> dict[str, Any]:
    """Read the committed kernel-scope evidence; fail loudly if it is absent."""
    data = json.loads(KERNEL_PARITY.read_text(encoding="utf-8"))
    timings = data.get("timings_ms") or {}
    return {
        "cpu_ms": float(timings["cpu"]),
        "gpu_ms": float(timings["gpu"]),
        "speedup": float(timings["speedup"]),
        "scope": data.get("scope") or "kernel scope only",
        "scope_note": data.get("scope_note") or "",
        "repetitions": data.get("timing_policy") or data.get("repetitions") or "unspecified",
        "source": data.get("source") or "unspecified source",
        "evidence_tag": data.get("recorded_at") or "undated",
    }

REPRODUCTION = """\
# Regenerate the committed detection-quality evidence (inputs are small and committed).
mkdir -p /tmp/sentinel-eval
PYTHONPATH=src .venv/bin/python scripts/gen_dataset.py \\
  --lines 2000 --seed 11 --burst-every 60 \\
  --out /tmp/sentinel-eval/eval2000.log \\
  --labels-out docs/evaluation/sample-labels.json
PYTHONPATH=src .venv/bin/python - <<'PY'
import hashlib, json
from pathlib import Path
from sentinel.detector import detect
from sentinel.detector.config import settings_from_file
raw = Path('/tmp/sentinel-eval/eval2000.log').read_bytes()
sha = hashlib.sha256(raw).hexdigest()
settings = settings_from_file('tests/fixtures/cpu/rules.json')
result = detect(raw, settings=settings, source_sha256=sha,
                run_id='run-eval-sample', chunk_id='chunk-eval-sample')
pairs = sorted({(m.rule_id, m.pattern) for m in result.matches})
payload = {
    'projection': ('alerts + distinct match (rule_id, pattern) pairs from '
                   'sentinel.detector.pipeline.detect(...).to_dict()'),
    'algorithm_version': result.algorithm_version,
    'source_sha256': sha,
    'run_id': 'run-eval-sample',
    'chunk_id': 'chunk-eval-sample',
    'rules': settings.to_dict(),
    'matches': [{'rule_id': rid, 'pattern': pattern} for rid, pattern in pairs],
    'alerts': [a.to_dict() for a in result.alerts],
    'metrics': result.metrics,
}
Path('docs/evaluation/sample-detections.json').write_text(
    json.dumps(payload, indent=2, sort_keys=True) + '\\n', encoding='utf-8')
PY

# Rebuild both committed reports from the saved evidence.
PYTHONPATH=src .venv/bin/python -m sentinel.evaluation.cli evaluate \\
  --labels docs/evaluation/sample-labels.json \\
  --detections docs/evaluation/sample-detections.json \\
  --out docs/evaluation/sample-evaluation.json
PYTHONPATH=src .venv/bin/python scripts/benchmark_report.py \\
  --evaluation-report docs/evaluation/sample-evaluation.json \\
  --out docs/evaluation/benchmark-report.md
"""


def _load(path: Path) -> dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return document


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def _fmt(value: Any, digits: int = 3) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}"


def _metrics_table(blocks: list[tuple[str, Mapping[str, Any]]]) -> str:
    columns = (
        ("precision", "Precision"), ("recall", "Recall"), ("f1", "F1"),
        ("false_positive_rate", "FPR"), ("fp_per_1000_units", "FP/1k"),
        ("fp_per_10000_units", "FP/10k"),
    )
    headers = ["Stratum", "TP", "FP", "TN", "FN", *[name for _, name in columns], "Units"]
    rows = []
    for label, block in blocks:
        counts = block["counts"]
        metrics = block["metrics"]
        rows.append([
            label,
            *["N/A" if counts[key] is None else counts[key]
              for key in ("tp", "fp", "tn", "fn")],
            *[_fmt(metrics[key]) for key, _ in columns],
            counts["evaluation_units"],
        ])
    return _table(headers, rows)


def render_report(
    cpu_baseline: Mapping[str, Any],
    cuda_memory: Mapping[str, Any],
    evaluation: Mapping[str, Any] | None = None,
) -> str:
    """Build the complete markdown report string from saved evidence."""
    detector = cpu_baseline.get("detector", {})
    cpu_input = cpu_baseline.get("input", {})
    gpu = cuda_memory.get("gpu", {})
    rows = cpu_baseline.get("rows", [])
    oracle = next(
        (row for row in rows if row.get("mode") == "cpu-reference-single-thread"),
        rows[0] if rows else {},
    )
    kernel = load_kernel_scope()
    speedup = kernel["speedup"]

    out = [
        "# Sentinel-GPU benchmark and evaluation report",
        "",
        "This report is rendered from **saved evidence only**. No benchmark, "
        "repetition or end-to-end run was executed to produce it. Timing scopes "
        "are never mixed: kernel-only, full-pipeline CPU and staging/VRAM "
        "diagnostics are separate sections.",
        "",
        "## Environment and configuration metadata",
        "",
        _table(
            ["Item", "Value", "Evidence"],
            [
                ["Evidence date", kernel["evidence_tag"], "kernel-parity.json"],
                ["Host profile", cuda_memory.get("host_profile", "N/A"), "cuda-memory-pipeline.json"],
                ["GPU", gpu.get("name", "N/A"), "cuda-memory-pipeline.json"],
                ["GPU driver", gpu.get("driver", "N/A"), "cuda-memory-pipeline.json"],
                ["GPU VRAM (MiB)", gpu.get("total_vram_mib", "N/A"), "cuda-memory-pipeline.json"],
                ["Input", cpu_input.get("name", "N/A"), "cpu-baseline.json"],
                ["Input bytes", cpu_input.get("bytes", "N/A"), "cpu-baseline.json"],
                ["Input SHA-256", cpu_input.get("sha256", "N/A"), "cpu-baseline.json"],
                ["Algorithm version", cpu_baseline.get("algorithm_version", "N/A"), "cpu-baseline.json"],
                ["Detector config hash", detector.get("config_hash", "N/A"), "cpu-baseline.json"],
                ["Rules", ", ".join(detector.get("rules", [])), "cpu-baseline.json"],
                ["Window seconds", detector.get("window_seconds", "N/A"), "cpu-baseline.json"],
                ["Baseline windows", detector.get("baseline_windows", "N/A"), "cpu-baseline.json"],
                ["Z threshold", detector.get("z_threshold", "N/A"), "cpu-baseline.json"],
            ],
        ),
        "",
        "## Raw evidence",
        "",
        "- [CPU baseline JSON](../benchmarks/cpu-baseline.json)",
        "- [CPU baseline CSV](../benchmarks/cpu-baseline.csv)",
        "- [CUDA memory pipeline JSON](../benchmarks/cuda-memory-pipeline.json)",
        "- [Kernel parity JSON](../benchmarks/kernel-parity.json)",
        "- [Benchmark methodology README](../benchmarks/README.md)",
    ]
    if evaluation is not None:
        out.append("- [Detection-quality sample report](sample-evaluation.json)")

    out.extend([
        "",
        "## Kernel-only CPU vs GPU timing (scope: native detect over staged 100 MB input)",
        "",
        _table(
            ["Backend", "Time (ms)", "Throughput context", "Scope"],
            [
                ["CPU kernel path", f"{kernel['cpu_ms']:.1f}",
                 "same 100 MB input", kernel["scope"]],
                ["GPU kernel path", f"{kernel['gpu_ms']:.1f}",
                 "same 100 MB input", kernel["scope"]],
                ["Speedup", f"{speedup:.1f}x", "CPU ms / GPU ms", "kernel scope only"],
            ],
        ),
        "",
        f"Provenance: {str(kernel['source']).rstrip('.')}. Repetitions: "
        f"{kernel['repetitions']}. {kernel['scope_note']} "
        "`cpu-baseline.json` supplies the full-pipeline CPU reference and input "
        "identity; it contains no GPU kernel timing. This report does not run "
        "benchmarks; it only renders the committed kernel-parity JSON.",
        "",
        "## CPU full-pipeline baseline (different scope — not comparable to kernel-only)",
        "",
        _table(
            ["Mode", "Threads", "Best ms", "Mean ms", "MB/s", "Alerts", "Features"],
            [
                [
                    row.get("mode", "N/A"), row.get("threads", "N/A"),
                    row.get("best_ms", "N/A"), row.get("mean_ms", "N/A"),
                    row.get("throughput_mb_s", "N/A"), row.get("alerts", "N/A"),
                    row.get("features", "N/A"),
                ]
                for row in rows
            ],
        ),
        "",
        f"Oracle row: `{oracle.get('mode', 'N/A')}`, "
        f"{oracle.get('repeats', 'N/A')} repetitions. This includes Python "
        "parsing, feature extraction and alert generation; do not divide its "
        "timing by a kernel-only GPU timing and call the result a speedup.",
        "",
        "## Staging / VRAM diagnostic — NOT a speedup result",
        "",
        _table(
            ["Stage MiB", "Chunks", "Wall ms", "Native total ms", "Kernel ms",
             "Peak VRAM MiB", "MB/s"],
            [
                [
                    row.get("stage_mib", "N/A"), row.get("chunks", "N/A"),
                    row.get("wall_ms", "N/A"),
                    row.get("phase_totals_ms", {}).get("total_ms", "N/A"),
                    row.get("phase_totals_ms", {}).get("kernel_ms", "N/A"),
                    row.get("vram", {}).get("peak_mib", "N/A"),
                    row.get("throughput_mb_s", "N/A"),
                ]
                for row in cuda_memory.get("micro_batch_sweep", [])
            ],
        ),
        "",
        "Large stream (4.25 GiB, larger than the 4096 MiB device): "
        f"{cuda_memory.get('large_stream', {}).get('wall_ms', 'N/A')} ms wall, "
        f"{cuda_memory.get('large_stream', {}).get('phase_totals_ms', {}).get('total_ms', 'N/A')} ms "
        "native total, "
        f"{cuda_memory.get('large_stream', {}).get('vram', {}).get('peak_mib', 'N/A')} MiB peak VRAM. "
        "These numbers measure bounded staging, allocation and host/device "
        "transfer behaviour. They are **NOT** a CPU/GPU speedup result.",
    ])

    if evaluation is not None:
        strata = evaluation.get("strata", {})
        out.extend([
            "",
            "## Detection quality",
            "",
            f"Evaluation unit: `{', '.join(evaluation['evaluation_unit']['fields'])}`. "
            f"Window seconds: `{evaluation.get('window_seconds')}`. "
            "Natural and synthetic label strata are reported separately.",
            "",
            "### Natural labels",
            "",
            _metrics_table([("natural", strata["natural"])]),
            "",
            "### Synthetic labels",
            "",
            _metrics_table([("synthetic", strata["synthetic"])]),
            "",
            f"Unresolved items: {len(evaluation.get('unresolved', []))}. "
            f"Missing-item records: {len(evaluation.get('missing', []))}. "
            "Undefined rates are shown as `N/A`. For the positive-only generator "
            "manifest, TN/FPR are undefined and unmatched alerts are counted as "
            "FP relative to known injections.",
        ])

    out.extend([
        "",
        "## Limitations",
        "",
        "- All measurements come from **one physical host** with **one GPU**; "
        "container isolation does not create independent hardware.",
        "- Infrastructure uses the **moto emulator** for S3/SQS/DynamoDB, so no "
        "cloud-service latency or availability claim is made.",
        "- Synthetic labels are derived from injected bursts and carry "
        "**synthetic label bias**; unmatched alerts may be genuine traffic "
        "anomalies rather than detector errors.",
        "- The 2,000-line sample and the 100 MB bench are not representative of "
        "all log sources, services or incident types; **dataset representativeness** "
        "is limited.",
        "- **Thermal throttling** and clock variation were not recorded for the "
        "saved kernel figures; only the best of repeated trials is reported.",
        "- Kernel-scope timing excludes ingestion, tokenisation, queueing and "
        "storage. Staging/VRAM numbers are diagnostics, not speedups.",
        "",
        "## Not run (explicit)",
        "",
        "- **5-repetition workload matrix** — not run. Saved CPU evidence has "
        "3 repetitions; no 100 MiB/500 MiB/1 GiB/2 GiB × chunk-size × backend "
        "matrix was executed in S8.",
        "- **PR-AUC** — not run. The sample provides binary unit labels, not a "
        "threshold sweep with calibrated scores.",
        "- **Full end-to-end benchmark** — not run. API → queue → worker → "
        "storage timing was not measured; only saved detector and staging "
        "evidence is available.",
        "- **Pageable-memory baseline** — not run. No unified/pageable-memory "
        "comparison against explicit staging was measured.",
        "",
        "## Reproduction",
        "",
        "```bash",
        REPRODUCTION.rstrip(),
        "```",
    ])
    return "\n".join(out) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cpu-baseline", type=Path, default=CPU_BASELINE)
    parser.add_argument("--cuda-memory", type=Path, default=CUDA_MEMORY)
    parser.add_argument("--evaluation-report", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cpu_baseline = _load(args.cpu_baseline)
    cuda_memory = _load(args.cuda_memory)
    evaluation = _load(args.evaluation_report) if args.evaluation_report else None
    text = render_report(cpu_baseline, cuda_memory, evaluation)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
