# ADR-0007 — Streaming chunker, manifests, and deterministic jobs

* Status: accepted
* Date: 2026-11-23
* Deciders: project owner, agent

## Context

Sprint 3-B turns an ingest message into the immutable objects and job records
that every later stage depends on. The source may be multiple gigabytes while
the producer container has a 512 MiB cap, so the implementation cannot read
the object into memory. Chunk boundaries, line ids, checksums, and job ids
must be stable across retries, restarts, and duplicate SQS deliveries.

The run API (ADR-0006) already fixed the ingest message and the id derivation
in `src/sentinel/ids.py`. This ADR documents the producer side of that
contract.

## Decision

### Source encoding and record semantics

* Supported encodings are the ones accepted by `DatasetRef`: `utf-8`,
  `utf-8-sig`, and `latin-1`.
* Decoding is strict. Malformed bytes reject the run with
  `invalid_encoding`; there is no replacement-character fallback and no
  truncation.
* Records end at `LF`. A `CR` immediately before the `LF` is preserved in the
  immutable chunk bytes and removed for line-content validation, so LF, CRLF,
  and mixed files are accepted.
* A final record without a trailing newline is a valid record. A trailing
  newline does not create an extra empty record; blank records inside the file
  do count.
* `line_id` is `L + sha256(source_sha256|line_number)[:16]` (the existing
  `ids.line_id_for`), scoped to the authoritative source checksum.

### Bounded streaming and boundaries

* Source objects are read with `GetObject(...).Body.read(block_size)`, with a
  1 MiB block size. Only the current chunk, the current record, and one block
  are held in memory.
* `chunk_target_bytes` (default 16 MiB) is a soft upper bound. A chunk is cut
  *before* the record that would cross the target, so normal chunks stay at or
  below the target and a record is never split.
* A single record larger than the target but within
  `detector.max_line_bytes` is routed through as a one-record chunk. A record
  larger than `max_line_bytes` is rejected with `line_too_long`. The reject
  policy is the only oversize policy in S3-B
  (`SENTINEL_CHUNK_OVERSIZE_POLICY=reject`); quarantine is deliberately left
  for a later schema change because it alters line-number continuity.
* A source with no records (empty object) is rejected with `empty_dataset`.
  The API normally prevents this at submission time.

### Checksums, ids, and object layout

* The producer computes SHA-256 over the raw source bytes as it streams and
  compares it with the declared `dataset.sha256` when present. A mismatch
  rejects the run.
* Chunk and line ids use the shipped derivations:
  `chunk_id = chk- + sha256(run_id|index|chunk_sha256)[:16]` and
  `job_id = job- + sha256(run_id|chunk_id|detector_hash|artifact_id)[:20]`.
* Object keys are content-addressed and immutable:

  ```text
  chunks/<run-id>/<chunk-id>/data.bin
  chunks/<run-id>/<chunk-id>/manifest.json
  chunks/<run-id>/progress.json
  ```

* Chunk and manifest puts use `IfNoneMatch='*'`. Re-running for the same run
  reuses the existing object; the producer verifies its size and recorded
  SHA-256 metadata before trusting it and fails loudly on any conflict.
* Every chunk manifest carries `schema_version`, run/chunk identity, source
  identity and checksum, chunk checksum/size, record count and inclusive line
  range, first/last line ids, byte range, parser version, detector-config hash,
  timestamp policy, and an optional label reference. Manifests contain no
  wall-clock field, so repeated runs produce byte-identical documents.
* The artifact identity is the versioned placeholder `cpu-ref-v1` until Sprint
  7 pins builds. `backend_policy=auto` resolves to the configured
  `SENTINEL_AUTO_BACKEND` (default `gpu`); GPU jobs go to `jobs-gpu` and CPU
  jobs to `jobs-cpu`, never a shared queue.

### Progress and restart behaviour

* After each uploaded chunk the producer writes `progress.json` with the
  chunk metadata so far. After manifests and job records are durable it
  rewrites the checkpoint as `status=complete`, including the source SHA-256.
* A restart with an incomplete checkpoint re-reads the source deterministically
  and reuses already-uploaded chunks by their content-addressed keys. It does
  not create new logical chunk or job ids.
* A restart with a complete checkpoint whose source ETag and size still match
  rebuilds/repairs job records without reading the source at all. A changed
  ETag discards the checkpoint and forces a fresh scan.
* Job records are created with `attribute_not_exists(job_id)` and carry a
  `published` marker. A replay publishes only jobs that are not marked
  published; duplicate SQS delivery remains possible (at-least-once) and is
  safe because workers commit idempotently. The durable outbox remains a
  Sprint 6 concern.
* Terminal input failures mark the run `FAILED` with a machine-readable
  `error` object and delete the ingest message. Transient infrastructure
  failures leave the message for visibility-timeout redelivery and the DLQ
  policy.

### Queue message contract

Job messages contain references and metadata only: run/job/chunk ids, chunk and
manifest bucket/key/checksum, line range, detector-config hash, backend, and
artifact placeholder. Raw log bytes never enter SQS.

### Metrics

The producer emits one structured `producer_metrics` event per run with source
bytes read, chunk bytes written, chunks created/reused, lines accepted/rejected,
jobs created/existing/published, elapsed time, and throughput. Sprint 7 maps
these events onto Prometheus counters.

## Consequences

* A chunk may exceed the byte target only when it contains a single oversized
  record, which is the documented line-safety trade-off.
* In-progress checkpoints are advisory: restart re-reads the source but reuses
  uploaded objects. Complete checkpoints avoid the second read.
* The manifest schema is additive. Changing record semantics, the timestamp
  policy, or parser behaviour requires a new `parser_version` and, if the
  contract changes, a manifest schema version bump.
* `progress.json` is mutable metadata under the immutable chunk prefix; it is
  never referenced by a manifest or job.
* Executing the producer requires the `raw-chunks` bucket and both job queues
  to exist; readiness still checks only API dependencies.

## Revisit when

* Sprint 6 adds the outbox and worker leases; job publication may move to a
  pending-dispatch record.
* Sprint 4 fixes timestamp parsing and the alert schema; `timestamp_policy`
  and `parser_version` will advance together.
* Quarantine of malformed/oversized records is needed; it requires a documented
  line-id gap policy.
