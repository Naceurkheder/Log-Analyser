#!/usr/bin/env python3
"""Thin wrapper for ``python -m sentinel.evaluation.cli evaluate``.

Kept for operator ergonomics; all behaviour lives in the evaluation package.
"""

from __future__ import annotations

import sys

from sentinel.evaluation.cli import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
