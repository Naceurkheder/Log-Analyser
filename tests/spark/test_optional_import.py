"""The Spark package and plan path must import without the optional dependency."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"


def test_core_job_and_cli_import_without_pyspark() -> None:
    script = (
        "import sys; "
        "import sentinel.spark.core, sentinel.spark.job, sentinel.spark.cli; "
        "assert 'pyspark' not in sys.modules; "
        "print('ok')"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
