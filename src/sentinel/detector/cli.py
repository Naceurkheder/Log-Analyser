"""Standalone CLI for the CPU reference detector.

Examples:
    python -m sentinel.detector.cli detect \
        --input tests/fixtures/sample.log --rules tests/fixtures/cpu/rules.json
    python -m sentinel.detector.cli baseline \
        --input data/bench.log --rules tests/fixtures/cpu/rules.json --threads 2

No web framework, queue or database is imported by this module.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .baseline import file_sha256, run_baseline
from .config import settings_from_file
from .native import NativeLibraryUnavailable, detect_native
from .pipeline import detect
from .semantics import ALGORITHM_VERSION


def _native_summary(data: bytes, settings: Any) -> dict[str, Any]:
    try:
        result = detect_native(
            data, [rule.pattern for rule in settings.rules],
            z_threshold=4.0,
        )
    except (NativeLibraryUnavailable, ValueError) as exc:
        return {"available": False, "reason": str(exc)}
    return {
        "available": True,
        "line_count": result.line_count,
        "flagged_lines": sum(1 for flags in result.line_flags if flags),
        "cells": result.cells,
        "mean": round(result.mean, 6),
        "std": round(result.std, 6),
        "anomalies": len(result.anomalies),
    }


def _cmd_detect(args: argparse.Namespace) -> int:
    settings = settings_from_file(args.rules)
    data = Path(args.input).read_bytes()
    source_sha256 = args.source_sha or file_sha256(data)
    result = detect(
        data, settings=settings, source_sha256=source_sha256,
        run_id=args.run_id, chunk_id=args.chunk_id,
        line_number_start=args.line_number_start,
    )
    document: dict[str, Any] = {
        "schema_version": 1,
        "algorithm_version": ALGORITHM_VERSION,
        "config_hash": settings.config_hash,
        "detector": settings.to_dict(),
        "input": {
            "name": Path(args.input).name,
            "bytes": len(data),
            "sha256": source_sha256,
        },
        "reference": result.to_dict(),
    }
    if args.native:
        document["native"] = _native_summary(data, settings)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.out == "-":
        sys.stdout.write(payload)
    else:
        Path(args.out).write_text(payload, encoding="utf-8")
    return 0


def _cmd_baseline(args: argparse.Namespace) -> int:
    settings = settings_from_file(args.rules)
    native_stats = _native_summary(Path(args.input).read_bytes(), settings) \
        if args.native else None
    document = run_baseline(
        args.input, settings, repeats=args.repeats, threads=args.threads,
        output_dir=args.out_dir, native_stats=native_stats,
    )
    print(json.dumps({
        "algorithm_version": document["algorithm_version"],
        "rows": document["rows"],
    }, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    detect_cmd = sub.add_parser("detect", help="run the CPU reference over a file")
    detect_cmd.add_argument("--input", required=True, help="raw chunk/dataset bytes")
    detect_cmd.add_argument("--rules", required=True, help="detector config JSON")
    detect_cmd.add_argument("--source-sha", default=None)
    detect_cmd.add_argument("--run-id", default="run")
    detect_cmd.add_argument("--chunk-id", default="chunk")
    detect_cmd.add_argument("--line-number-start", type=int, default=1)
    detect_cmd.add_argument("--out", default="-", help="output JSON path or '-'")
    detect_cmd.add_argument("--native", action="store_true",
                            help="also summarize the native K1/K2 result")
    detect_cmd.set_defaults(func=_cmd_detect)

    baseline_cmd = sub.add_parser("baseline", help="save CPU timing baselines")
    baseline_cmd.add_argument("--input", required=True)
    baseline_cmd.add_argument("--rules", required=True)
    baseline_cmd.add_argument("--repeats", type=int, default=3)
    baseline_cmd.add_argument("--threads", type=int, default=1)
    baseline_cmd.add_argument("--out-dir", default="results")
    baseline_cmd.add_argument("--native", action="store_true")
    baseline_cmd.set_defaults(func=_cmd_baseline)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
