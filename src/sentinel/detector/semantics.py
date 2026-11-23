"""Normative constants and configuration for cpu-reference-v1.

This module deliberately depends only on the Python standard library and
``sentinel.ids``. It must stay independent of FastAPI, SQS and DynamoDB so the
CPU reference can run as a standalone oracle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..ids import config_hash

ALGORITHM_VERSION = "cpu-reference-v1"
SEMANTICS_DOC = "docs/algorithm-semantics.md"
MIN_HISTORY = 2
UINT32_MAX = 2**32 - 1
DEFAULT_YEAR = 2026
DEFAULT_WINDOW_SECONDS = 60
DEFAULT_BASELINE_WINDOWS = 10
DEFAULT_Z_THRESHOLD = 3.0
DEFAULT_MAX_LINE_BYTES = 65536
VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
SERVICE_TAGS: tuple[tuple[str, int], ...] = (
    ("sshd", 1),
    ("nginx", 2),
    ("app-server", 3),
    ("kernel", 4),
    ("auth-daemon", 5),
)


class DetectorConfigError(ValueError):
    """Raised when detector settings violate the documented contract."""


class OversizeRecordError(ValueError):
    """Raised when a normalized record exceeds ``max_line_bytes``."""


@dataclass(frozen=True)
class Rule:
    rule_id: str
    pattern: str
    severity: str = "medium"
    index: int = 0

    @property
    def pattern_bytes(self) -> bytes:
        return self.pattern.encode("utf-8")


@dataclass(frozen=True)
class DetectorSettings:
    rules: tuple[Rule, ...]
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    baseline_windows: int = DEFAULT_BASELINE_WINDOWS
    z_threshold: float = DEFAULT_Z_THRESHOLD
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
    default_year: int = DEFAULT_YEAR
    encoding: str = "utf-8"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DetectorSettings":
        raw_rules = data.get("rules")
        if not isinstance(raw_rules, (list, tuple)) or not raw_rules:
            raise DetectorConfigError("detector config requires a non-empty rules list")
        rules: list[Rule] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_rules):
            if not isinstance(item, Mapping):
                raise DetectorConfigError(f"rule {index} must be an object")
            rule_id = str(item.get("rule_id", ""))
            pattern = str(item.get("pattern", ""))
            severity = str(item.get("severity", "medium"))
            if not rule_id or len(rule_id) > 64 or not rule_id[0].isalnum():
                raise DetectorConfigError(f"invalid rule_id at index {index}")
            if rule_id in seen:
                raise DetectorConfigError(f"duplicate rule_id: {rule_id}")
            seen.add(rule_id)
            if not pattern or "\n" in pattern or "\r" in pattern:
                raise DetectorConfigError(f"invalid pattern for {rule_id}")
            if len(pattern.encode("utf-8")) > 256:
                raise DetectorConfigError(f"pattern too long for {rule_id}")
            if severity not in VALID_SEVERITIES:
                raise DetectorConfigError(f"invalid severity for {rule_id}")
            rules.append(Rule(rule_id, pattern, severity, index))

        window_seconds = int(data.get("window_seconds", DEFAULT_WINDOW_SECONDS))
        baseline_windows = int(data.get("baseline_windows", DEFAULT_BASELINE_WINDOWS))
        z_threshold = float(data.get("z_threshold", DEFAULT_Z_THRESHOLD))
        max_line_bytes = int(data.get("max_line_bytes", DEFAULT_MAX_LINE_BYTES))
        default_year = int(data.get("default_year", DEFAULT_YEAR))
        encoding = str(data.get("encoding", "utf-8"))
        if not 1 <= window_seconds <= 3600:
            raise DetectorConfigError("window_seconds must be in [1, 3600]")
        if not 1 <= baseline_windows <= 1440:
            raise DetectorConfigError("baseline_windows must be in [1, 1440]")
        if not 0 < z_threshold <= 1000:
            raise DetectorConfigError("z_threshold must be in (0, 1000]")
        if not 256 <= max_line_bytes <= 1_048_576:
            raise DetectorConfigError("max_line_bytes must be in [256, 1048576]")
        if not 1970 <= default_year <= 9999:
            raise DetectorConfigError("default_year must be in [1970, 9999]")
        if encoding not in {"utf-8", "utf-8-sig", "latin-1"}:
            raise DetectorConfigError(f"unsupported source encoding: {encoding}")
        return cls(
            rules=tuple(rules), window_seconds=window_seconds,
            baseline_windows=baseline_windows, z_threshold=z_threshold,
            max_line_bytes=max_line_bytes, default_year=default_year,
            encoding=encoding,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules": [
                {"rule_id": r.rule_id, "pattern": r.pattern, "severity": r.severity}
                for r in self.rules
            ],
            "window_seconds": self.window_seconds,
            "baseline_windows": self.baseline_windows,
            "z_threshold": self.z_threshold,
            "max_line_bytes": self.max_line_bytes,
            "default_year": self.default_year,
            "encoding": self.encoding,
        }

    @property
    def config_hash(self) -> str:
        # Match the API's detector_config_hash exactly: reference-only extras
        # (default_year, encoding) are recorded separately, not hashed here.
        return config_hash({
            "rules": self.to_dict()["rules"],
            "window_seconds": self.window_seconds,
            "baseline_windows": self.baseline_windows,
            "z_threshold": self.z_threshold,
            "max_line_bytes": self.max_line_bytes,
        })
