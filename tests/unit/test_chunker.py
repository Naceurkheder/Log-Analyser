"""Bounded chunker: record safety, line endings, encodings, determinism."""

from __future__ import annotations

import hashlib
import io
from typing import Any

import pytest

from sentinel.ids import chunk_id_for, line_id_for
from sentinel.producer import ChunkingError, SourceSpec, chunk_stream


def _run(
    data: bytes,
    *,
    target: int = 1024,
    max_line: int = 4096,
    encoding: str = "utf-8",
    declared_sha: str | None = None,
) -> tuple[Any, list[bytes]]:
    collected: list[bytes] = []

    def on_chunk(record: Any, payload: bytes) -> None:
        assert record.size_bytes == len(payload)
        assert record.sha256 == hashlib.sha256(payload).hexdigest()
        collected.append(payload)

    result = chunk_stream(
        io.BytesIO(data),
        run_id="run_test",
        source=SourceSpec(
            bucket="incoming",
            key="datasets/x.log",
            encoding=encoding,
            size_bytes=len(data),
            sha256=declared_sha,
        ),
        target_bytes=target,
        max_line_bytes=max_line,
        block_size=7,  # deliberately smaller than a record to exercise spanning
        on_chunk=on_chunk,
    )
    assert b"".join(collected) == data
    assert result.source_sha256 == hashlib.sha256(data).hexdigest()
    assert result.source_bytes == len(data)
    assert [c.index for c in result.chunks] == list(range(len(result.chunks)))
    if result.chunks:
        assert [c.byte_start for c in result.chunks] == [
            0,
            *[c.byte_end for c in result.chunks[:-1]],
        ]
        assert result.chunks[-1].byte_end == len(data)
    for record, payload in zip(result.chunks, collected):
        assert record.chunk_id == chunk_id_for("run_test", record.index, record.sha256)
        assert record.line_count == record.line_end - record.line_start + 1
        assert payload == data[record.byte_start : record.byte_end]
    return result, collected


@pytest.mark.parametrize(
    ("data", "lines"),
    [
        (b"a\nb\n", 2),
        (b"a\r\nb\r\n", 2),
        (b"a\r\nb\nc", 3),
        (b"a\nb", 2),
        (b"\n\n", 2),
    ],
)
def test_line_endings_and_final_record(data: bytes, lines: int) -> None:
    result, _ = _run(data)
    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.line_count == lines
    assert chunk.line_start == 1
    assert chunk.line_end == lines
    assert chunk.byte_start == 0
    assert chunk.byte_end == len(data)


class _SpyReader(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.read_sizes: list[int | None] = []

    def read(self, size: int | None = -1) -> bytes:  # type: ignore[override]
        self.read_sizes.append(size)
        return super().read(size)


def test_source_is_read_in_bounded_blocks() -> None:
    data = b"x" * 100_000 + b"\n" + b"y\n" * 1000
    reader = _SpyReader(data)
    collected: list[bytes] = []
    chunk_stream(
        reader,
        run_id="run_test",
        source=SourceSpec(bucket="incoming", key="k", size_bytes=len(data)),
        target_bytes=8192,
        max_line_bytes=200_000,
        block_size=4096,
        on_chunk=lambda _record, payload: collected.append(payload),
    )
    # The producer never asks S3 for the whole object: every read is bounded.
    assert reader.read_sizes
    assert all(size is not None and 0 < size <= 4096 for size in reader.read_sizes)
    assert b"".join(collected) == data


def test_empty_source_has_no_chunks() -> None:
    result, collected = _run(b"")
    assert result.chunks == []
    assert collected == []


def test_boundaries_never_split_a_record() -> None:
    lines = [f"record-{i:03d}-payload\n".encode() for i in range(40)]
    data = b"".join(lines)
    result, collected = _run(data, target=100)

    assert len(result.chunks) > 1
    seen = 0
    for record, payload in zip(result.chunks, collected):
        # Every chunk is a concatenation of whole source lines.
        assert payload.endswith(b"\n")
        assert data.startswith(payload, record.byte_start)
        assert b"record-" in payload
        seen += record.line_count
    assert seen == len(lines)
    # Normal chunks stay within the target (the target is a soft upper bound
    # only for a single oversized record).
    assert all(c.size_bytes <= 100 for c in result.chunks)


def test_oversized_record_becomes_own_chunk() -> None:
    big = b"X" * 250 + b"\n"
    data = b"small\n" + big + b"tail\n"
    result, collected = _run(data, target=100, max_line=4096)

    assert len(result.chunks) == 3
    assert collected[1] == big
    assert result.chunks[1].line_count == 1
    assert result.chunks[1].line_start == 2


def test_record_over_max_line_rejects() -> None:
    with pytest.raises(ChunkingError) as excinfo:
        _run(b"ok\n" + b"X" * 500 + b"\n", max_line=256)
    assert excinfo.value.code == "line_too_long"
    assert excinfo.value.line_number == 2


def test_invalid_utf8_rejects() -> None:
    with pytest.raises(ChunkingError) as excinfo:
        _run(b"valid\n\xff\xfe broken\n")
    assert excinfo.value.code == "invalid_encoding"


def test_utf8_sig_and_latin1_accepted() -> None:
    bom = b"\xef\xbb\xbfhello\n"
    result, collected = _run(bom, encoding="utf-8-sig")
    assert collected == [bom]
    assert result.chunks[0].line_count == 1

    latin = b"caf\xe9\n"
    result, collected = _run(latin, encoding="latin-1")
    assert collected == [latin]


def test_source_checksum_mismatch_rejects() -> None:
    with pytest.raises(ChunkingError) as excinfo:
        _run(b"abc\n", declared_sha="0" * 64)
    assert excinfo.value.code == "source_sha256_mismatch"


def test_declared_checksum_match_accepts() -> None:
    data = b"abc\n"
    result, _ = _run(data, declared_sha=hashlib.sha256(data).hexdigest())
    assert result.source_sha256 == hashlib.sha256(data).hexdigest()


def test_line_ids_are_stable_and_source_scoped() -> None:
    data = b"a\nb\nc\n"
    result, _ = _run(data)
    chunk = result.chunks[0]
    assert line_id_for(result.source_sha256, 1) != line_id_for(result.source_sha256, 2)
    assert chunk.line_start == 1 and chunk.line_end == 3

    again, _ = _run(data)
    assert [c.chunk_id for c in result.chunks] == [c.chunk_id for c in again.chunks]


def test_unsupported_oversize_policy_is_a_configuration_error() -> None:
    with pytest.raises(ValueError):
        chunk_stream(
            io.BytesIO(b"a\n"),
            run_id="run_test",
            source=SourceSpec(bucket="incoming", key="k"),
            target_bytes=10,
            max_line_bytes=10,
            oversize_policy="quarantine",
        )
