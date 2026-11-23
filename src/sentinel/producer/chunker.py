"""Bounded streaming chunker for source log objects.

The chunker reads a source object in fixed-size blocks and never holds more
than one chunk (plus one block) in memory.  It splits only on record
boundaries: a chunk always contains whole records, and a record that is larger
than the configured byte target becomes its own chunk instead of being
truncated.

Contract (see ``docs/decisions/0007-streaming-chunker.md``):

* records are separated by ``LF``; a ``CR`` immediately before ``LF`` is kept
  in the chunk bytes and removed for line-content validation (CRLF/mixed
  endings are accepted);
* a final record without a trailing newline is a valid record;
* an empty source produces zero chunks and is rejected by the producer;
* source encoding is one of ``utf-8``, ``utf-8-sig`` and ``latin-1``;
  malformed bytes reject the run (no silent replacement);
* a record longer than ``max_line_bytes`` rejects the run;
* a record longer than ``target_bytes`` (but within ``max_line_bytes``) is
  routed through as a single-record chunk.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import BinaryIO, Callable

from ..ids import chunk_id_for, line_id_for, sha256_hex

DEFAULT_BLOCK_BYTES = 1024 * 1024

SUPPORTED_ENCODINGS = ("utf-8", "utf-8-sig", "latin-1")
SUPPORTED_OVERSIZE_POLICIES = ("reject",)


class ChunkingError(Exception):
    """Terminal validation failure: the source cannot be chunked as-is."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        line_number: int | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.line_number = line_number
        self.details = details or {}
        if line_number is not None:
            self.details.setdefault("line_number", line_number)


@dataclass(frozen=True)
class SourceSpec:
    bucket: str
    key: str
    encoding: str = "utf-8"
    size_bytes: int | None = None
    sha256: str | None = None
    etag: str = ""


@dataclass(frozen=True)
class ChunkRecord:
    index: int
    chunk_id: str
    sha256: str
    size_bytes: int
    line_count: int
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int


@dataclass
class ChunkingResult:
    source_sha256: str
    source_bytes: int
    chunks: list[ChunkRecord] = field(default_factory=list)


ChunkCallback = Callable[[ChunkRecord, bytes], None]


def _decode_content(content: bytes, encoding: str, first_line: bool) -> None:
    codec = encoding
    if encoding == "utf-8-sig" and not first_line:
        # A BOM is only meaningful at the start of the source.
        codec = "utf-8"
    try:
        content.decode(codec)
    except UnicodeDecodeError as exc:
        raise ChunkingError(
            "invalid_encoding",
            f"record is not valid {codec}",
            details={"encoding": codec, "byte_offset": exc.start},
        ) from exc


def chunk_stream(
    reader: BinaryIO,
    *,
    run_id: str,
    source: SourceSpec,
    target_bytes: int,
    max_line_bytes: int,
    oversize_policy: str = "reject",
    block_size: int = DEFAULT_BLOCK_BYTES,
    on_chunk: ChunkCallback | None = None,
) -> ChunkingResult:
    """Split ``reader`` into line-safe chunks and return their metadata.

    ``on_chunk`` is called as soon as a chunk is complete, so the producer can
    upload it while the rest of the source is still being read.  Manifests are
    written by the caller after the full source hash is known.
    """
    if target_bytes < 1:
        raise ValueError("target_bytes must be positive")
    if max_line_bytes < 1:
        raise ValueError("max_line_bytes must be positive")
    if oversize_policy not in SUPPORTED_OVERSIZE_POLICIES:
        raise ValueError(
            f"unsupported oversize policy: {oversize_policy!r}; "
            f"expected one of {SUPPORTED_OVERSIZE_POLICIES}"
        )
    if source.encoding not in SUPPORTED_ENCODINGS:
        raise ValueError(f"unsupported encoding: {source.encoding!r}")

    digest = hashlib.sha256()
    bytes_read = 0
    line_no = 0
    consumed = 0
    index = 0

    line_buf = bytearray()
    line_start_offset = 0

    chunk = bytearray()
    chunk_first_line: int | None = None
    chunk_byte_start = 0

    chunks: list[ChunkRecord] = []

    def emit(end_offset: int) -> None:
        nonlocal index, chunk, chunk_first_line, chunk_byte_start
        if not chunk or chunk_first_line is None:  # pragma: no cover
            return
        data = bytes(chunk)
        digest_chunk = sha256_hex(data)
        chunk_id = chunk_id_for(run_id, index, digest_chunk)
        record = ChunkRecord(
            index=index,
            chunk_id=chunk_id,
            sha256=digest_chunk,
            size_bytes=len(data),
            line_count=line_no - chunk_first_line + 1,
            line_start=chunk_first_line,
            line_end=line_no,
            byte_start=chunk_byte_start,
            byte_end=end_offset,
        )
        if on_chunk is not None:
            on_chunk(record, data)
        chunks.append(record)
        index += 1
        chunk = bytearray()
        chunk_first_line = None

    def add_record(raw: bytes, content: bytes, raw_start: int) -> None:
        nonlocal line_no, chunk_first_line, chunk_byte_start, consumed
        if len(content) > max_line_bytes:
            raise ChunkingError(
                "line_too_long",
                "record exceeds the configured maximum line size",
                line_number=line_no + 1,
                details={
                    "line_bytes": len(content),
                    "max_line_bytes": max_line_bytes,
                },
            )
        _decode_content(content, source.encoding, first_line=line_no == 0)

        # A chunk is cut *before* the record that would cross the target, so
        # normal chunks do not exceed the target.  A single oversized record
        # still becomes one whole chunk.
        if chunk and len(chunk) + len(raw) > target_bytes:
            emit(raw_start)
        if not chunk:
            chunk_first_line = line_no + 1
            chunk_byte_start = raw_start
        chunk.extend(raw)
        line_no += 1
        consumed = raw_start + len(raw)

    while True:
        block = reader.read(block_size)
        if not block:
            break
        block_start = bytes_read
        bytes_read += len(block)
        digest.update(block)

        pos = 0
        while pos < len(block):
            newline = block.find(b"\n", pos)
            if newline < 0:
                segment = block[pos:]
                if not line_buf:
                    line_start_offset = block_start + pos
                line_buf.extend(segment)
                # Keep the early bound one byte loose: a CR before LF is not
                # part of the record content.
                if len(line_buf) > max_line_bytes + 1:
                    raise ChunkingError(
                        "line_too_long",
                        "record exceeds the configured maximum line size",
                        line_number=line_no + 1,
                        details={
                            "max_line_bytes": max_line_bytes,
                            "observed_at_least": len(line_buf),
                        },
                    )
                break

            segment = block[pos:newline]
            if not line_buf:
                line_start_offset = block_start + pos
            content = bytes(line_buf) + segment
            if content.endswith(b"\r"):
                content = content[:-1]
            raw = bytes(line_buf) + segment + b"\n"
            add_record(raw, content, line_start_offset)
            line_buf = bytearray()
            pos = newline + 1

    if line_buf:
        # Final record without a trailing newline.
        content = bytes(line_buf)
        add_record(content, content, line_start_offset)
        line_buf = bytearray()

    if chunk:
        emit(consumed)

    if bytes_read != consumed:
        # Defensive: the byte accounting above must cover every source byte.
        raise ChunkingError(
            "chunk_byte_accounting",
            "chunk boundaries do not cover the whole source",
            details={"source_bytes": bytes_read, "accounted_bytes": consumed},
        )

    source_sha = digest.hexdigest()
    if source.size_bytes is not None and source.size_bytes != bytes_read:
        raise ChunkingError(
            "source_size_mismatch",
            "source object size changed while chunking",
            details={
                "expected_bytes": source.size_bytes,
                "actual_bytes": bytes_read,
            },
        )
    if source.sha256 is not None and source.sha256 != source_sha:
        raise ChunkingError(
            "source_sha256_mismatch",
            "source object does not match the declared checksum",
            details={"declared": source.sha256, "actual": source_sha},
        )

    return ChunkingResult(
        source_sha256=source_sha, source_bytes=bytes_read, chunks=chunks
    )


def line_ids(source_sha256: str, record: ChunkRecord) -> tuple[str, str]:
    return (
        line_id_for(source_sha256, record.line_start),
        line_id_for(source_sha256, record.line_end),
    )
