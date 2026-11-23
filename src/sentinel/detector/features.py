"""Key extraction and event/feature structures (docs/algorithm-semantics.md §4)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .semantics import SERVICE_TAGS

_SERVICE_RE = re.compile(r"(sshd|nginx|app-server|kernel|auth-daemon)\[")
_IPV4_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
_STATUS_RE = re.compile(r'"[^"\n]*"\s+([1-5][0-9]{2})\b')
_SERVICE_IDS = dict(SERVICE_TAGS)


def extract_service(text: str) -> int:
    match = _SERVICE_RE.search(text)
    return _SERVICE_IDS[match.group(1)] if match else 0


def extract_ip(text: str) -> int:
    """First valid IPv4 address as a host-order uint32 (0 when absent)."""
    for match in _IPV4_RE.finditer(text):
        octets = [int(part) for part in match.group(1).split(".")]
        if all(0 <= part <= 255 for part in octets):
            return (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
    return 0


def extract_status(text: str) -> int:
    match = _STATUS_RE.search(text)
    return int(match.group(1)) if match else 0


@dataclass(frozen=True)
class Match:
    line_no: int
    line_id: str
    rule_id: str
    rule_index: int
    severity: str
    pattern: str

    def to_dict(self) -> dict[str, object]:
        return {
            "line_no": self.line_no,
            "line_id": self.line_id,
            "rule_id": self.rule_id,
            "rule_index": self.rule_index,
            "severity": self.severity,
            "pattern": self.pattern,
        }


@dataclass(frozen=True)
class Event:
    line_no: int
    line_id: str
    rule_id: str
    rule_index: int
    severity: str
    epoch: int
    window_id: int
    service_id: int
    ip: int
    status: int

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.service_id, self.ip, self.status)

    def to_dict(self) -> dict[str, object]:
        return {
            "line_no": self.line_no, "line_id": self.line_id,
            "rule_id": self.rule_id, "rule_index": self.rule_index,
            "severity": self.severity, "epoch": self.epoch,
            "window_id": self.window_id, "key": list(self.key),
        }


@dataclass(frozen=True)
class Feature:
    key: tuple[int, int, int]
    window_id: int
    rule_id: str
    count: int
    line_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "key": list(self.key),
            "key_string": f"{self.key[0]}:{self.key[1]}:{self.key[2]}",
            "window_id": self.window_id,
            "rule_id": self.rule_id,
            "count": self.count,
            "line_ids": list(self.line_ids),
        }
