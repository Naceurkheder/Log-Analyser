"""Deterministic timestamp parsing (docs/algorithm-semantics.md §3)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

_MONTHS = {name: index for index, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1
)}

_SYSLOG = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\b"
)
_CLF = re.compile(
    r"\b(\d{1,2})/([A-Z][a-z]{2})/(\d{4}):(\d{2}):(\d{2}):(\d{2})\b"
)
_ISO = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\b"
)
# Any token that looks like a time but fails strict parsing is "invalid",
# not "missing". This keeps bad timestamps observable in result metrics.
_CANDIDATE = re.compile(
    r"\b(?:\d{1,2}:[0-9]{2}:[0-9]{2}|\d{4}-\d{2}-\d{2}T|"
    + "|".join(_MONTHS) + r"\s+\d{1,2}\s+\d{2}:)"
)

TimestampStatus = Literal["ok", "missing", "invalid"]


def _epoch(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())


def _parse_syslog(match: re.Match[str], default_year: int) -> int | None:
    month = _MONTHS[match.group(1)]
    day, hour, minute, second = map(int, match.groups()[1:])
    try:
        return _epoch(datetime(default_year, month, day, hour, minute, second,
                               tzinfo=timezone.utc))
    except ValueError:
        return None


def _parse_clf(match: re.Match[str]) -> int | None:
    day, month_name, year, hour, minute, second = match.groups()
    month = _MONTHS.get(month_name)
    if month is None:
        return None
    try:
        return _epoch(datetime(int(year), month, int(day), int(hour),
                               int(minute), int(second), tzinfo=timezone.utc))
    except ValueError:
        return None


def _parse_iso(match: re.Match[str]) -> int | None:
    value = match.group(1).replace("z", "Z")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _epoch(parsed)


def classify_timestamp(
    text: str, default_year: int
) -> tuple[TimestampStatus, int | None]:
    """Return ``(status, epoch_seconds)``; epoch is None unless status == ok."""
    for pattern, parser in (
        (_SYSLOG, lambda m: _parse_syslog(m, default_year)),
        (_CLF, _parse_clf),
        (_ISO, _parse_iso),
    ):
        match = pattern.search(text)
        if match:
            epoch = parser(match)
            return ("ok", epoch) if epoch is not None else ("invalid", None)
    return ("invalid", None) if _CANDIDATE.search(text) else ("missing", None)


def window_id_for(epoch_seconds: int, window_seconds: int) -> int:
    """Globally aligned, half-open window ID (Python floor division)."""
    return epoch_seconds // window_seconds


def window_start_iso(window_id: int, window_seconds: int) -> str:
    return datetime.fromtimestamp(
        window_id * window_seconds, timezone.utc
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
