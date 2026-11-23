"""Randomized comparison against a deliberately simple, independent oracle."""

from __future__ import annotations

import random
import re
from datetime import datetime, timezone
from typing import Any

from sentinel.detector import DetectorSettings, detect

RULES = [
    {"rule_id": "auth-fail", "pattern": "Failed password", "severity": "high"},
    {"rule_id": "app-error", "pattern": "ERROR", "severity": "medium"},
    {"rule_id": "http-404", "pattern": " 404 ", "severity": "medium"},
]
ORACLE_TIME = re.compile(rb"Jan  5 03:(\d{2}):(\d{2})")
ORACLE_SERVICE = re.compile(
    rb"(sshd|nginx|app-server|kernel|auth-daemon)\["
)
ORACLE_IP = re.compile(rb"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
ORACLE_STATUS = re.compile(rb'"[^"\n]*"\s+([1-5][0-9]{2})\b')
SERVICE_IDS = {"sshd": 1, "nginx": 2, "app-server": 3, "kernel": 4, "auth-daemon": 5}


def _oracle(data: bytes, cfg: DetectorSettings) -> tuple[list[tuple[int, str]], list[dict[str, Any]]]:
    """Return (line_no, rule_id) matches and aggregate (key, window, rule, count, score)."""
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    matches: list[tuple[int, str]] = []
    counts: dict[tuple[tuple[int, int, int], int, str], int] = {}
    for line_no, raw in enumerate(parts, start=1):
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        for rule in cfg.rules:
            if rule.pattern_bytes in raw:
                matches.append((line_no, rule.rule_id))
                moment = ORACLE_TIME.search(raw)
                if not moment:
                    continue
                minute, second = map(int, moment.groups())
                epoch = int(datetime(2026, 1, 5, 3, minute, second,
                                     tzinfo=timezone.utc).timestamp())
                service_match = ORACLE_SERVICE.search(raw)
                service = SERVICE_IDS.get(
                    service_match.group(1).decode() if service_match else "", 0
                )
                ip_match = ORACLE_IP.search(raw)
                ip = 0
                if ip_match:
                    octets = [int(p) for p in ip_match.group(1).split(b".")]
                    if all(p <= 255 for p in octets):
                        ip = (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
                status_match = ORACLE_STATUS.search(raw)
                status = int(status_match.group(1)) if status_match else 0
                key = (service, ip, status)
                window = epoch // cfg.window_seconds
                identity = (key, window, rule.rule_id)
                counts[identity] = counts.get(identity, 0) + 1

    series: dict[tuple[tuple[int, int, int], str], dict[int, int]] = {}
    for (key, window, rule), count in counts.items():
        series.setdefault((key, rule), {})[window] = count
    scored: list[dict[str, Any]] = []
    for (key, rule), windows in series.items():
        start = min(windows)
        for target in sorted(windows):
            history = [
                windows.get(target - back, 0)
                for back in range(1, cfg.baseline_windows + 1)
                if target - back >= start
            ]
            if len(history) < 2:
                continue
            mean = sum(history) / len(history)
            var = max(0.0, sum(x * x for x in history) / len(history) - mean * mean)
            std = var ** 0.5
            if std <= 0:
                continue
            score = (windows[target] - mean) / std
            if score >= cfg.z_threshold:
                scored.append({
                    "key": key, "window": target, "rule": rule,
                    "count": windows[target], "score": round(score, 6),
                })
    scored.sort(key=lambda item: (
        item["window"], item["key"], item["rule"]
    ))
    return matches, scored


def _random_corpus(rng: random.Random) -> bytes:
    lines: list[bytes] = []
    for _ in range(rng.randint(8, 30)):
        minute = rng.randint(0, 5)
        second = rng.randint(0, 59)
        ip = f"10.0.{rng.randint(0, 2)}.{rng.randint(1, 9)}".encode()
        template = rng.choice(("sshd", "nginx", "app", "mixed", "quiet"))
        prefix = f"Jan  5 03:{minute:02d}:{second:02d} lab ".encode()
        if template == "sshd":
            line = prefix + b"sshd[1]: Failed password from " + ip + b"\n"
        elif template == "nginx":
            line = (prefix + b'nginx[1]: ' + ip + b' "GET / HTTP/1.1" 404 12'
                    + b" Failed password\n")
        elif template == "app":
            line = prefix + b"app-server[1]: ERROR E1203 retry\n"
        elif template == "mixed":
            line = prefix + b'nginx[1]: ' + ip + b' "GET / HTTP/1.1" 404 ERROR\n'
        else:
            line = prefix + b"kernel[1]: INFO ok\n"
        if rng.random() < 0.25:
            line = line[:-1] + b"\r\n"
        lines.append(line)
    return b"".join(lines)


def test_randomized_corpus_matches_simple_oracle() -> None:
    rng = random.Random(20261123)
    cfg = DetectorSettings.from_dict({
        "rules": RULES, "window_seconds": 60, "baseline_windows": 3,
        "z_threshold": 3.0,
    })
    for trial in range(60):
        data = _random_corpus(rng)
        expected_matches, expected_alerts = _oracle(data, cfg)
        result = detect(data, settings=cfg, source_sha256=f"sha-{trial}")
        actual_matches = [(m.line_no, m.rule_id) for m in result.matches]
        assert actual_matches == expected_matches, trial
        actual_alerts = [
            {
                "key": (a.service_id, a.ip, a.status),
                "window": a.window_id,
                "rule": a.rule_id,
                "count": a.count,
                "score": round(a.score, 6),
            }
            for a in result.alerts
        ]
        assert actual_alerts == expected_alerts, trial
