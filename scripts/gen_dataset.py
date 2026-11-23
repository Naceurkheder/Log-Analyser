#!/usr/bin/env python3
"""Synthetic log dataset generator for Sentinel-GPU.

Replaces bulk LogHub downloads with a reproducible, locally generated corpus
shaped like real mixed syslog traffic. Injection points (failed-login bursts,
404 floods from a single IP, error-code spikes and single-key rate bursts) are
deterministic per seed.  With ``--labels-out`` the generator also writes a
*separate* ground-truth manifest (Sprint 3-C); the detector never sees it.

Design constraints:
* streaming writer — memory use is bounded regardless of --size-mb
* fixed RNG seed -> byte-identical output for identical parameters
* one obvious line format per service, unambiguous for the K1 pattern set
* labels are derived artifacts, never embedded in the dataset bytes

Usage:
    python3 scripts/gen_dataset.py --size-mb 100 --out data/dataset.log
    python3 scripts/gen_dataset.py --lines 500 --out tests/fixtures/sample.log \
        --labels-out tests/fixtures/sample.labels.json --stats
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

HOST = "lab-host"
MONTH = "Jan"
DAY = 5
YEAR = 2026

# Synthetic clock origin.  The generator uses a fixed calendar date so the
# corpus and its window labels are reproducible across machines/timezones.
BASE_EPOCH_SECONDS = int(datetime(YEAR, 1, DAY, tzinfo=timezone.utc).timestamp())
PARSER_VERSION = "line-v1"
GENERATOR_VERSION = "1.0"

SERVICES = ["sshd", "nginx", "auth-daemon", "kernel", "app-server"]

USERS = [
    "root", "admin", "deploy", "jenkins", "backup",
    "oracle", "postgres", "test", "git", "nagios",
]

ERROR_CODES = [
    "E1203", "E0421", "E3301", "E2109", "E0098", "E7745", "E5502", "E3001",
]

URI_PATHS = [
    "/index.html", "/login", "/wp-login.php", "/api/v1/status", "/robots.txt",
    "/admin/config", "/static/app.js", "/favicon.ico", "/.env", "/backup.sql",
]

INACTIVE_PATHS = [
    "/wp-login.php", "/admin/config", "/.env", "/backup.sql", "/setup.php",
]

# ---------------------------------------------------------------------------
# Line emitters — every emitter returns exactly one line (with trailing \n)
# ---------------------------------------------------------------------------

def _stamp(ts: int) -> str:
    hh = ts % 86400
    return f"{MONTH}  {DAY:2d} {hh // 3600:02d}:{(hh % 3600) // 60:02d}:{hh % 60:02d}"


def _utc(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _ip(rng: random.Random, pool: list[str]) -> str:
    # 95% normal pool, 5% "external" range so bursts are visually distinct
    if rng.random() < 0.05:
        return f"203.0.113.{rng.randint(1, 254)}"
    return rng.choice(pool)


def sshd_line(rng: random.Random, ts: int, ip: str, kind: str) -> str:
    pid = 2000 + rng.randint(0, 999)
    head = f"{_stamp(ts)} {HOST} sshd[{pid}]: "
    if kind == "fail":
        user = rng.choice(USERS)
        port = rng.randint(1024, 65535)
        return head + (
            f"Failed password for invalid user {user} from {ip} port {port} ssh2"
        )
    if kind == "invalid":
        user = rng.choice(USERS)
        return head + f"Invalid user {user} from {ip} port {rng.randint(1024, 65535)}"
    user = rng.choice(USERS)
    return head + f"Accepted publickey for {user} from {ip} port {rng.randint(1024, 65535)} ssh2"


def nginx_line(rng: random.Random, ts: int, ip: str, *, status: int) -> str:
    pid = 1900 + rng.randint(0, 99)
    path = rng.choice(INACTIVE_PATHS if status == 404 else URI_PATHS)
    verb = "POST" if "login" in path else "GET"
    size = rng.randint(80, 900) if status >= 400 else rng.randint(300, 40000)
    return (
        f"{_stamp(ts)} {HOST} nginx[{pid}]: {ip} - - "
        f"[{DAY:02d}/{MONTH}/{YEAR}:{_stamp(ts)[7:]}] "
        f'"{verb} {path} HTTP/1.1" {status} {size}'
    )


def app_error_line(rng: random.Random, ts: int) -> str:
    pid = 991 + rng.randint(0, 8)
    code = rng.choice(ERROR_CODES)
    what = rng.choice(
        [
            "db connection timeout, retry",
            "upstream read timeout",
            "cache backend flush failed",
            "queue consumer lag exceeded",
        ]
    )
    return (
        f"{_stamp(ts)} {HOST} app-server[{pid}]: "
        f"ERROR {code} {what} {rng.randint(1, 9)}"
    )


def kernel_line(rng: random.Random, ts: int) -> str:
    msg = rng.choice(
        [
            "INFO task block on rwsem",
            "INFO IRQ handled",
            "INFO TCP: request socket recv_locked",
            "INFO usb 2-1: new high-speed device",
        ]
    )
    return f"{_stamp(ts)} {HOST} kernel[{rng.randint(0, 3)}]: {msg}"


def segfault_line(rng: random.Random, ts: int) -> str:
    return (
        f"{_stamp(ts)} {HOST} app-server[9{rng.randint(10, 99)}]: SEGFAULT "
        f"at {rng.randint(0, 0xFFFF):04x}{rng.randint(0, 0xFFFFFF):06x} "
        f"ip {rng.randint(0, 0xFFFF):04x}{rng.randint(0, 0xFFFFFF):06x}"
    )


# ---------------------------------------------------------------------------
# Bursts (attack-like injections)
# ---------------------------------------------------------------------------

def burst_failed_login(rng: random.Random, ts: int, lines: list[str], ip: str) -> int:
    n = rng.randint(30, 80)
    for _ in range(n):
        lines.append(sshd_line(rng, ts, ip, "fail"))
    return n


def burst_invalid_user(rng: random.Random, ts: int, lines: list[str], ip: str) -> int:
    n = rng.randint(20, 50)
    for _ in range(n):
        lines.append(sshd_line(rng, ts, ip, "invalid"))
    return n


def burst_404(rng: random.Random, ts: int, lines: list[str], ip: str) -> int:
    n = rng.randint(50, 150)
    for _ in range(n):
        lines.append(nginx_line(rng, ts, ip, status=404))
    return n


def burst_error_code(rng: random.Random, ts: int, lines: list[str]) -> int:
    n = rng.randint(25, 70)
    for _ in range(n):
        lines.append(app_error_line(rng, ts))
    return n


def burst_event_rate(rng: random.Random, ts: int, lines: list[str], ip: str) -> int:
    """A single (service, ip) key sending normal-looking requests very fast."""
    n = rng.randint(60, 160)
    for _ in range(n):
        lines.append(nginx_line(rng, ts, ip, status=200))
    return n


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

def generate(*, out, seed: int, target_bytes: int | None, target_lines: int | None,
             burst_every: int, stats: dict, scenarios: list[dict] | None = None,
             window_seconds: int = 60) -> None:
    if window_seconds < 1:
        raise ValueError("window_seconds must be positive")
    rng = random.Random(seed)
    ts = 3 * 3600 + 12 * 60  # 03:12:00

    ip_pool = [f"10.{rng.randint(1, 60)}.{rng.randint(0, 254)}.{rng.randint(1, 254)}"
               for _ in range(120)]

    state = {"burst_idx": 0, "written": 0, "pending": 0, "nlines": 0}
    buf: list[str] = []
    FLUSH_LINES = 8192

    def emit(line: str) -> None:
        """Append one line to the buffer, honoring the byte budget."""
        nbytes = len(line) + 1
        if (target_bytes is not None
                and state["written"] + state["pending"] + nbytes > target_bytes):
            state["stop"] = True
            return
        buf.append(line)
        state["pending"] += nbytes
        state["nlines"] += 1
        if len(buf) >= FLUSH_LINES:
            flush()

    def flush() -> None:
        if not buf:
            return
        chunk = "".join(line + "\n" for line in buf)
        out.write(chunk)
        state["written"] += len(chunk)
        state["pending"] = 0
        buf.clear()

    def record_scenario(
        kind: str, start_line: int, count: int, start_ts: int,
        *, pattern: str, key: dict,
    ) -> None:
        if scenarios is None:
            return
        epoch = BASE_EPOCH_SECONDS + start_ts
        window_id = epoch // window_seconds
        window_start = window_id * window_seconds
        scenarios.append({
            "scenario_id": f"scn-{len(scenarios) + 1:04d}",
            "kind": kind,
            "synthetic": True,
            "seed": seed,
            "line_range": {"start": start_line, "end": start_line + count - 1},
            "line_count": count,
            "timestamp": {
                "start_offset_seconds": start_ts,
                "end_offset_seconds": start_ts + count - 1,
                "start_utc": _utc(epoch),
                "end_utc": _utc(BASE_EPOCH_SECONDS + start_ts + count - 1),
            },
            "window": {
                "seconds": window_seconds,
                "id": window_id,
                "start_utc": _utc(window_start),
                "end_utc": _utc(window_start + window_seconds),
            },
            "rule": {"pattern": pattern, "hint": kind},
            "key": key,
            "expected": {"label": "anomalous", "evaluation_unit": "window"},
        })

    while True:
        if state.get("stop"):
            break
        if target_lines is not None and state["nlines"] >= target_lines:
            break
        if (target_bytes is not None
                and state["written"] + state["pending"] >= target_bytes):
            break

        if state["burst_idx"] >= burst_every:
            state["burst_idx"] = 0
            ip = f"203.0.113.{rng.randint(1, 254)}"
            pick = rng.random()
            start_line = state["nlines"] + 1
            if pick < 0.28:
                n = burst_failed_login(rng, ts, buf, ip)
                kind, pattern = "failed_login_burst", "Failed password"
                key = {"service": "sshd", "ip": ip}
                stats["failed_login"] += n
            elif pick < 0.45:
                n = burst_invalid_user(rng, ts, buf, ip)
                kind, pattern = "invalid_user_burst", "Invalid user"
                key = {"service": "sshd", "ip": ip}
                stats["invalid_user"] += n
            elif pick < 0.65:
                n = burst_404(rng, ts, buf, ip)
                kind, pattern = "http_404_burst", " 404 "
                key = {"service": "nginx", "ip": ip, "status": 404}
                stats["http_404"] += n
            elif pick < 0.80:
                n = burst_error_code(rng, ts, buf)
                kind, pattern = "error_code_burst", "ERROR"
                key = {"service": "app-server"}
                stats["error_code"] += n
            else:
                n = burst_event_rate(rng, ts, buf, ip)
                kind, pattern = "event_rate_burst", "GET /api/v1/status"
                key = {"service": "nginx", "ip": ip, "status": 200}
                stats["event_rate"] += n
            record_scenario(kind, start_line, n, ts, pattern=pattern, key=key)
            state["nlines"] += n
            # account approximately for burst bytes so the byte budget stays tight
            state["pending"] += n * 90
            ts += n
            if state.get("stop") or (
                target_lines is not None and state["nlines"] >= target_lines
            ) or (
                target_bytes is not None
                and state["written"] + state["pending"] >= target_bytes
            ):
                break
            continue
        state["burst_idx"] += 1

        roll = rng.random()
        if roll < 0.25:
            emit(sshd_line(rng, ts, _ip(rng, ip_pool), "ok"))
        elif roll < 0.5:
            emit(nginx_line(rng, ts, _ip(rng, ip_pool), status=200))
        elif roll < 0.7:
            emit(kernel_line(rng, ts))
        elif roll < 0.9:
            emit(app_error_line(rng, ts))
        else:
            emit(sshd_line(rng, ts, _ip(rng, ip_pool), rng.choice(["fail", "invalid"])))
            stats["failed_login"] += 1
        if rng.random() < 0.0004:
            emit(segfault_line(rng, ts))
            stats["segfault"] += 1
        ts += 1

    flush()

    stats["bytes"] = state["written"]
    stats["lines"] = state["nlines"]


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def build_label_manifest(
    *, path: Path, seed: int, burst_every: int, window_seconds: int,
    stats: dict, scenarios: list[dict],
) -> dict:
    """Build a deterministic, detector-independent ground-truth document."""
    sha256, size = _file_digest(path)
    return {
        "schema_version": 1,
        "kind": "sentinel.ground-truth",
        "synthetic": True,
        "generator": {"name": "scripts/gen_dataset.py", "version": GENERATOR_VERSION},
        "seed": seed,
        "parser_version": PARSER_VERSION,
        "source": {
            "name": path.name,
            "sha256": sha256,
            "size_bytes": size,
            "line_count": stats["lines"],
            "encoding": "utf-8",
        },
        "label_unit": "window",
        "window_seconds": window_seconds,
        "line_id_policy": {
            "formula": "L + sha256(source.sha256 + '|' + line_no)[:16]",
            "line_number_base": 1,
        },
        "burst_every": burst_every,
        "scenarios": scenarios,
        "isolation": (
            "This manifest is stored outside the detector input. Do not pass it "
            "to workers or include it in run/ingest messages."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--size-mb", type=float, help="target output size in MB")
    ap.add_argument("--lines", type=int, help="target number of lines")
    ap.add_argument("--out", default="-", help="output path ('-' = stdout)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--burst-every", type=int, default=400,
                    help="mean quiet period between injected bursts")
    ap.add_argument("--window-seconds", type=int, default=60,
                    help="evaluation window for the label manifest")
    ap.add_argument("--labels-out", default=None,
                    help="write the separate ground-truth manifest here")
    ap.add_argument("--stats", action="store_true", help="print JSON stats to stderr")
    args = ap.parse_args()

    if not args.size_mb and not args.lines:
        ap.error("one of --size-mb or --lines is required")
    if args.labels_out and args.out == "-":
        ap.error("--labels-out requires a file --out (the source hash is needed)")
    if args.burst_every < 1:
        ap.error("--burst-every must be positive")

    # Rough model: one synthetic line averages ~90 bytes.
    target_bytes = int(args.size_mb * 1_000_000) if args.size_mb else None
    target_lines = args.lines

    stats = {"seed": args.seed, "failed_login": 0, "invalid_user": 0,
             "http_404": 0, "error_code": 0, "event_rate": 0, "segfault": 0}
    scenarios: list[dict] = []

    if args.out == "-":
        generate(out=sys.stdout, seed=args.seed, target_bytes=target_bytes,
                 target_lines=target_lines, burst_every=args.burst_every,
                 stats=stats, scenarios=scenarios if args.labels_out else None,
                 window_seconds=args.window_seconds)
    else:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", buffering=1 << 20) as fh:
            generate(out=fh, seed=args.seed, target_bytes=target_bytes,
                     target_lines=target_lines, burst_every=args.burst_every,
                     stats=stats, scenarios=scenarios if args.labels_out else None,
                     window_seconds=args.window_seconds)
        if args.labels_out:
            manifest = build_label_manifest(
                path=path, seed=args.seed, burst_every=args.burst_every,
                window_seconds=args.window_seconds, stats=stats,
                scenarios=scenarios,
            )
            labels_path = Path(args.labels_out)
            labels_path.parent.mkdir(parents=True, exist_ok=True)
            labels_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            stats["labels_path"] = str(labels_path)

    if args.stats:
        print(json.dumps(stats, indent=2, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
