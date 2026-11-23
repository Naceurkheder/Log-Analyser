# Sentinel detector algorithm semantics (cpu-reference-v1)

This document is the normative specification for the Sentinel-GPU detection
stages. It is written **before** the CPU implementation so the CPU path can act
as the correctness oracle for the CUDA port (Sprint 5). Any change to a rule
here requires a new `algorithm_version` and new golden outputs.

* Version: `cpu-reference-v1`
* Status: accepted (Sprint 4-A)
* Related: ADR-0007 (chunk/line IDs), ADR-0008 (ground truth),
  `include/sentinel/sentinel.h`

## 1. Input records and byte normalization

1. The detector input is the raw bytes of one immutable chunk (`data.bin`).
   It is a pure function of `(input bytes, detector config, run/chunk identity)`.
2. Records are delimited by `LF` (`0x0A`). The bytes between two LFs form one
   record. A trailing `CR` (`0x0D`) immediately before the LF is removed
   (CRLF → LF normalization). No other whitespace is trimmed.
3. A final record without a terminating LF is accepted as a record. A trailing
   LF does **not** create an extra empty record. A bare LF creates an empty
   record (line 0 bytes long) which is matched and counted like any other.
4. Record numbering is 1-based and continues across chunk boundaries via the
   chunk manifest's `line_number_start`. The stable line ID is
   `L + sha256(source_sha256 + '|' + line_no)[:16]` (ADR-0007).
5. Supported source encodings: `utf-8`, `utf-8-sig` (BOM removed at byte 0 of
   the source only) and `latin-1`. Decoding is strict: malformed UTF-8 is a
   terminal error (the record is rejected, never replaced with U+FFFD).
   Pattern matching itself works on the normalized **bytes**, not on decoded
   text, so byte semantics do not depend on the encoding label.
6. A normalized record longer than `max_line_bytes` (default 65536) is a
   terminal configuration/input error. There is no silent truncation; the
   producer's `oversize_policy` must already have rejected such a record.
7. Empty datasets produce zero matches, zero events, zero alerts and zero
   statistics. They are a valid result, not an error.

## 2. Rules and matching

1. A rule is `(rule_id, pattern, severity)` in configuration order. The
   zero-based position is its **rule index**.
2. `rule_id` is unique (validated by the API schema). Patterns are non-empty
   UTF-8 strings of at most 256 bytes containing no newline. Duplicate
   *patterns* with different `rule_id` values are allowed and are distinct
   rules; shared prefixes are also allowed.
3. Matching is a naive byte substring search: rule *p* matches record *r* iff
   `pattern_bytes[p] in r`. There is no regex, globbing, case folding, word
   boundary or tokenization. Only the first occurrence per rule is recorded.
4. Matches are emitted in ascending `(line_no, rule_index)` order. One line may
   create multiple matches; each match creates its own event and, if anomalous,
   its own alert candidate.
5. Rule order is configuration order, never a set/dict iteration order. The
   detector config hash is the SHA-256 of the canonical JSON of the whole
   detector config (`sentinel.ids.config_hash`) and is copied into chunk
   manifests, jobs and baseline records.
6. The native ABI (`libsentinel_cpu.so`, `libsentinel.so`) has smaller fixed
   limits: at most 32 patterns of at most 63 bytes, addressed by bit index.
   The Python binding validates these limits and fails with a clear error
   rather than silently dropping rules. Aho–Corasick is deliberately deferred.

## 3. Timestamps

1. Timestamp formats are tried in this order:
   1. RFC 3164 syslog: `Jan  5 03:12:00` (day may be space-padded). The year is
      taken from the detector setting `default_year` (default **2026**), never
      from the machine clock, so results are reproducible.
   2. nginx/clf: `05/Jan/2026:03:12:00` (usually inside `[...]`).
   3. ISO-8601: `YYYY-MM-DDTHH:MM:SS[.fraction](Z|±HH:MM)`.
2. Naive timestamps are interpreted as **UTC**. Offset-aware timestamps are
   normalized to UTC. All window math uses integer epoch seconds (fractional
   seconds are truncated toward zero after UTC normalization).
3. A record with a missing or invalid timestamp still contributes K1 pattern
   matches, but contributes **no windowed event**. It is counted separately as
   `missing_timestamps` / `invalid_timestamps` in the result metrics. It never
   raises an alert.
4. Events are sorted by `(epoch_seconds, line_no)` before windowing. Therefore
   file order, including late/out-of-order timestamps, cannot change the
   logical result. A late event is placed in the window derived from its own
   timestamp; it may contribute to that window's baseline within the same
   chunk. Cross-chunk baselines are out of scope for this version.

## 4. Windows and aggregation keys

1. `window_seconds` (1..3600, default 60) defines fixed, globally aligned
   windows: `window_id = floor(epoch_seconds / window_seconds)`.
2. A window covers the half-open interval
   `[window_id * W, (window_id + 1) * W)`. An event exactly at the start
   belongs to that window; an event exactly at the end belongs to the next one.
3. The aggregation key is the integer tuple
   `(service_id, ip, status)`:
   * `service_id`: first matching service tag in the record, checked in order
     `sshd[`, `nginx[`, `app-server[`, `kernel[`, `auth-daemon[`; 0 = unknown.
   * `ip`: first dotted-quad IPv4 address in the record, converted to host
     order; 0 = absent (which is indistinguishable from `0.0.0.0`).
   * `status`: first HTTP status code following a quoted request, e.g.
     `"GET / HTTP/1.1" 404`; 0 = absent.
4. Keys are compared as complete integer tuples. No hash alone identifies a
   cell: if the CUDA implementation uses hashing, it must retain the tuple for
   equality checks (collision resolution). The canonical string form used in
   output is `service_id:ip:status`.
5. Features are integer counts of `(key, window_id, rule_id)` — one event per
   pattern match, incremented by one. There is no sampling or weighting.

## 5. Baseline, scoring and warm-up

1. For each `(key, rule_id)` series, windows are ordered by `window_id`. For a
   target window, the baseline consists of the immediately preceding
   `baseline_windows` window counts **only** (default 10). The current window
   is never included in its own baseline. Missing intermediate windows count
   as 0, so a gap is represented explicitly rather than skipped.
2. Warm-up: if fewer than `MIN_HISTORY = 2` prior window counts exist, the
   target window emits no alert. This prevents a single observation from
   producing a z-score.
3. `mean = sum(history) / len(history)`;
   `variance = sum(x²)/n - mean²` clamped at 0 to avoid negative rounding;
   `std = sqrt(variance)`. Integer sums use 64-bit unsigned arithmetic;
   counts are capped at `UINT32_MAX` (4,294,967,295) and overflow is an
   explicit error, never a wrap.
4. If `std == 0` (zero variance), the score is defined as `z = 0`, not NaN or
   infinity, and no alert is emitted. A constant high-volume key is therefore
   not flagged by this method.
5. Otherwise `z = (count - mean) / std` computed in IEEE-754 **double** precision.
   An alert is emitted when `z >= z_threshold` (`>=`, not `>`; this matches the
   native K2 comparison). The default threshold is 3.0.
6. The same rules apply to the native K1/K2 compatibility path described in
   §7, except that K2 uses its documented whole-input histogram instead of a
   per-key window history.

## 6. Alerts, IDs and output ordering

1. One alert candidate per anomalous `(key, window_id, rule_id)` aggregate:
   ```json
   {
     "alert_id": "hash",
     "algorithm_version": "cpu-reference-v1",
     "run_id": "...", "chunk_id": "...", "line_id_first": "L...",
     "line_ids": ["L..."], "window_id": 123, "window_seconds": 60,
     "service_id": 2, "ip": 0, "status": 404,
     "rule_id": "http-404", "severity": "high",
     "count": 42, "mean": 1.5, "std": 0.7, "score": 57.142857,
     "created_at": "..."
   }
   ```
2. `alert_id` is
   `alert_id_for(run_id, chunk_id, ip, window_id, service_id, rule_id)`
   (`src/sentinel/ids.py`): a SHA-256-based deterministic ID. It does not
   include wall-clock time. Re-running the same input/config/identity yields
   identical IDs. `status` is included in alert metadata but not in the ID
   because the legacy helper's ID space is `(ip, bucket, service, rule)`;
   distinct statuses with identical other fields would collide, so the CPU
   reference appends a short canonical-status suffix when `status != 0`.
3. `score` is rounded to 6 decimal places for output stability.
4. Alerts are sorted by `(window_id, service_id, ip, status, rule_id)`;
   matches by `(line_no, rule_index)`; contributing `line_ids` are sorted.
5. Output JSON uses canonical key ordering. No maps with random iteration
   order are serialized directly.
6. Detector settings (`rules`, `window_seconds`, `baseline_windows`,
   `z_threshold`, `max_line_bytes`), the detector config hash, the algorithm
   version and the semantics document path are recorded in the run/chunk
   manifest, job records and saved CPU baseline (see `docs/decisions/0008`
   for label isolation). Labels are never an input to detection.

## 7. Native K1/K2 compatibility semantics (ABI parity path)

The exported C ABI has its own, simpler semantics used by the native parity
tests and benchmarks; it is not the windowed scorer from §5.

* `sentinel_tokenize` splits on LF, strips a preceding CR, accepts a final
  unterminated record, extracts the first IPv4 (host order) and the first
  `hh:mm:ss` minute-of-day bucket `hh*60+mm`, and detects the first service
  tag. Invalid time/time values yield bucket 0.
* K1 sets bit `p` of `line_flags[li]` when pattern `p` occurs in record `li`.
* K2 counts lines per `(ip, bucket, service)` cell in an open-addressing table
  (2²¹ slots, linear probing). `cells` is the number of non-empty cells;
  `mean` and `std` are the global count statistics over those cells.
* Flagging requires `cells >= 32`, `std > 0` and `(count - mean)/std >=
  z_threshold`. Zero variance yields no anomalies. Records are returned as
  `[ip, bucket, service, count]` quadruples sorted by
  `(bucket, service, ip)`.
* `sentinel_results` exposes `cells`, `mean` and `std` in addition to flags
  and anomalies so Python can verify the native statistics stage directly.

## 8. Reproducibility contract

* No wall-clock time, locale, environment variable or hash-map iteration may
  affect matches, events, scores, alert IDs or ordering.
* Floating-point math that participates in a threshold decision is `double`
  on both CPU and GPU; the only float in the ABI is the incoming threshold.
* Identical input/config/identity must produce byte-identical logical output
  on repeated runs. Timing and throughput are recorded separately and never
  mixed into logical results.
