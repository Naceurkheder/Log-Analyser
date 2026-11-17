#!/usr/bin/env python3
"""Synthetic log dataset generator for Sentinel-GPU.

Replaces bulk LogHub downloads with a reproducible, locally generated corpus
shaped like real mixed syslog traffic. Injection points (failed-login bursts,
404 floods from a single IP, error-code spikes) are deterministic per seed,
which makes the same file reusable as labeled ground truth in Sprint 6.

Design constraints:
* streaming writer — memory use is bounded regardless of --size-mb
* fixed RNG seed -> byte-identical output for identical parameters
* one obvious line format per service, unambiguous for the K1 pattern set

Usage:
    python3 scripts/gen_dataset.py --size-mb 100 --out data/dataset.log
    python3 scripts/gen_dataset.py --lines 500 --out tests/fixtures/sample.log --stats
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

HOST = "lab-host"
MONTH = "Jan"
DAY = 5
YEAR = 2026

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


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

def generate(*, out, seed: int, target_bytes: int | None, target_lines: int | None,
             burst_every: int, stats: dict) -> None:
    rng = random.Random(seed)
    ts = 3 * 3600 + 12 * 60  # 03:12:00

    ip_pool = [f"10.{rng.randint(1, 60)}.{rng.randint(0, 254)}.{rng.randint(1, 254)}"
               for _ in range(120)]

    state = {"burst_idx": 0, "written": 0, "nlines": 0}
    buf: list[str] = []
    FLUSH_LINES = 8192

    def emit(line: str) -> None:
        """Append one line to the buffer, flushing when the buffer fills."""
        buf.append(line)
        state["nlines"] += 1
        if len(buf) >= FLUSH_LINES:
            flush()

    def flush() -> None:
        if not buf:
            return
        chunk = "".join(line + "\n" for line in buf)
        out.write(chunk)
        state["written"] += len(chunk)
        buf.clear()

    while True:
        if target_lines is not None and state["nlines"] >= target_lines:
            break
        if target_bytes is not None and state["written"] >= target_bytes:
            break

        if state["burst_idx"] >= burst_every:
            state["burst_idx"] = 0
            ip = f"203.0.113.{rng.randint(1, 254)}"
            pick = rng.random()
            if pick < 0.35:
                n = burst_failed_login(rng, ts, buf, ip)
                stats["failed_login"] += n
            elif pick < 0.6:
                n = burst_invalid_user(rng, ts, buf, ip)
                stats["invalid_user"] += n
            elif pick < 0.85:
                n = burst_404(rng, ts, buf, ip)
                stats["http_404"] += n
            else:
                n = burst_error_code(rng, ts, buf)
                stats["error_code"] += n
            state["nlines"] += n
            ts += n
            if target_lines is not None and state["nlines"] >= target_lines:
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--size-mb", type=float, help="target output size in MB")
    ap.add_argument("--lines", type=int, help="target number of lines")
    ap.add_argument("--out", default="-", help="output path ('-' = stdout)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--burst-every", type=int, default=400,
                    help="mean quiet period between injected bursts")
    ap.add_argument("--stats", action="store_true", help="print JSON stats to stderr")
    args = ap.parse_args()

    if not args.size_mb and not args.lines:
        ap.error("one of --size-mb or --lines is required")

    # Rough model: one synthetic line averages ~90 bytes.
    target_bytes = int(args.size_mb * 1_000_000) if args.size_mb else None
    target_lines = args.lines

    stats = {"seed": args.seed, "failed_login": 0, "invalid_user": 0,
             "http_404": 0, "error_code": 0, "segfault": 0}

    if args.out == "-":
        generate(out=sys.stdout, seed=args.seed, target_bytes=target_bytes,
                 target_lines=target_lines, burst_every=args.burst_every, stats=stats)
    else:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", buffering=1 << 20) as fh:
            generate(out=fh, seed=args.seed, target_bytes=target_bytes,
                     target_lines=target_lines, burst_every=args.burst_every, stats=stats)

    if args.stats:
        print(json.dumps(stats, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
