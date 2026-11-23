"""Load detector settings from a standalone JSON file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .semantics import DetectorSettings


def settings_from_file(path: str | Path) -> DetectorSettings:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(document, list):
        document = {"rules": document}
    if "detector" in document and isinstance(document["detector"], dict):
        document = document["detector"]
    return DetectorSettings.from_dict(document)
