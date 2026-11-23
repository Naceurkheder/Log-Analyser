"""Command-line interface for Sprint 8 detection-quality evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .report import evaluate_files, render_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel.evaluation",
        description="Evaluate detector output against ground-truth labels.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    evaluate = subcommands.add_parser(
        "evaluate", help="join labels and detections, then write a report",
    )
    evaluate.add_argument("--labels", required=True, help="label manifest JSON")
    evaluate.add_argument("--detections", required=True, help="detector output JSON")
    evaluate.add_argument("--out", required=True, help="report output path")
    evaluate.add_argument(
        "--format", choices=("json", "markdown"), default="json",
        help="report format (default: json)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "evaluate":
        report = evaluate_files(args.labels, args.detections)
        if args.format == "markdown":
            text = render_markdown(report)
        else:
            text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
