"""Record splitting and normalization (docs/algorithm-semantics.md §1)."""

from __future__ import annotations

from dataclasses import dataclass

from ..ids import line_id_for
from .semantics import OversizeRecordError


@dataclass(frozen=True)
class Record:
    line_no: int
    raw: bytes          # normalized bytes passed to the matcher
    text: str           # decoded text used for timestamps/keys only
    line_id: str


def parse_records(
    data: bytes,
    *,
    source_sha256: str,
    encoding: str = "utf-8",
    max_line_bytes: int = 65536,
    line_number_start: int = 1,
) -> list[Record]:
    """Split a chunk into normalized, stable-ID records.

    LF is the delimiter; a preceding CR is removed; a final unterminated record
    is kept. Malformed UTF-8 is a terminal error, never replaced.
    """
    try:
        decoded = data.decode("utf-8-sig" if encoding == "utf-8-sig" else encoding)
    except UnicodeDecodeError as exc:
        raise ValueError(f"malformed {encoding} source bytes at {exc.start}") from exc

    raw_parts = data.split(b"\n")
    text_parts = decoded.split("\n")
    if raw_parts and raw_parts[-1] == b"":
        raw_parts.pop()
        text_parts.pop()  # a trailing LF does not create an empty record

    if encoding == "utf-8-sig" and raw_parts and raw_parts[0].startswith(b"\xef\xbb\xbf"):
        # utf-8-sig decoding removed the BOM from text_parts[0]; apply the same
        # normalization to the bytes used by the matcher.
        raw_parts[0] = raw_parts[0][3:]

    records: list[Record] = []
    for offset, (raw_part, text_part) in enumerate(zip(raw_parts, text_parts)):
        if raw_part.endswith(b"\r"):
            raw = raw_part[:-1]
            text = text_part[:-1]
        else:
            raw = raw_part
            text = text_part
        if len(raw) > max_line_bytes:
            raise OversizeRecordError(
                f"line {line_number_start + offset} is {len(raw)} bytes; "
                f"limit is {max_line_bytes}"
            )
        line_no = line_number_start + offset
        records.append(
            Record(line_no=line_no, raw=raw, text=text,
                   line_id=line_id_for(source_sha256, line_no))
        )
    return records
