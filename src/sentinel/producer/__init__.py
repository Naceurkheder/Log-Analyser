"""Streaming producer: S3 source -> immutable chunks -> deterministic jobs."""

from .chunker import (
    ChunkingError,
    ChunkRecord,
    ChunkingResult,
    SourceSpec,
    chunk_stream,
    line_ids,
)
from .producer import Producer, ProducerMetrics, TerminalIngestError
from .runner import IngestRunner

__all__ = [
    "ChunkRecord",
    "ChunkingError",
    "ChunkingResult",
    "IngestRunner",
    "Producer",
    "ProducerMetrics",
    "SourceSpec",
    "TerminalIngestError",
    "chunk_stream",
    "line_ids",
]
