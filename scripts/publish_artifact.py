#!/usr/bin/env python3
"""Shell-friendly entry point for the S7 artifact publisher.

Usage::

    python scripts/publish_artifact.py --library build/libsentinel.so \
        --out-dir artifacts --bucket artifacts \
        --test-result pytest=passed

The implementation lives in ``sentinel.artifacts.publish`` so tests and
workers can import the same functions without going through the CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sentinel.artifacts.publish import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
