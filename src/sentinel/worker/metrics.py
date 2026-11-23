"""Dependency-free Prometheus text endpoint and worker health check."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


@dataclass
class WorkerMetrics:
    """In-process counters and gauges for one worker process."""

    capability: str
    worker_id: str
    jobs_processed: int = 0
    jobs_retried: int = 0
    heartbeat_failures: int = 0
    lease_losses: int = 0
    dlq_sends: int = 0
    alerts_written: int = 0
    outbox_republished: int = 0
    run_completions: int = 0
    messages_ignored: int = 0
    active_state: str = "idle"
    accepting_work: bool = True
    lease_age_seconds: float = 0.0
    heartbeat_age_seconds: float = 0.0
    last_error: str = ""
    labels: dict[str, str] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = "ok" if self.accepting_work else "paused"
        return payload

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def render(self) -> str:
        labels = ",".join(
            [
                f'capability="{self._escape(self.capability)}"',
                f'worker_id="{self._escape(self.worker_id)}"',
            ]
        )
        counters = {
            "sentinel_worker_jobs_processed_total": self.jobs_processed,
            "sentinel_worker_retries_total": self.jobs_retried,
            "sentinel_worker_heartbeat_failures_total": self.heartbeat_failures,
            "sentinel_worker_lease_losses_total": self.lease_losses,
            "sentinel_worker_dlq_sends_total": self.dlq_sends,
            "sentinel_worker_alerts_written_total": self.alerts_written,
            "sentinel_worker_outbox_republished_total": self.outbox_republished,
            "sentinel_worker_run_completions_total": self.run_completions,
            "sentinel_worker_messages_ignored_total": self.messages_ignored,
        }
        gauges = {
            "sentinel_worker_lease_age_seconds": self.lease_age_seconds,
            "sentinel_worker_heartbeat_age_seconds": self.heartbeat_age_seconds,
        }
        lines = [
            "# HELP sentinel_worker_info Worker identity and capability.",
            "# TYPE sentinel_worker_info gauge",
            f"sentinel_worker_info{{{labels}}} 1",
            "# HELP sentinel_worker_active_state Current worker lifecycle state.",
            "# TYPE sentinel_worker_active_state gauge",
            (
                "sentinel_worker_active_state{"
                f'{labels},state="{self._escape(self.active_state)}"'
                "} 1"
            ),
            "# HELP sentinel_worker_accepting_work Whether new jobs are accepted.",
            "# TYPE sentinel_worker_accepting_work gauge",
            f"sentinel_worker_accepting_work{{{labels}}} {int(self.accepting_work)}",
        ]
        for name, value in counters.items():
            lines.extend(
                [
                    f"# HELP {name} Worker counter.",
                    f"# TYPE {name} counter",
                    f"{name}{{{labels}}} {value}",
                ]
            )
        for name, value in gauges.items():
            lines.extend(
                [
                    f"# HELP {name} Current lease/heartbeat age in seconds.",
                    f"# TYPE {name} gauge",
                    f"{name}{{{labels}}} {value:.3f}",
                ]
            )
        return "\n".join(lines) + "\n"


class MetricsServer:
    """Tiny built-in HTTP server; imported modules never start it implicitly."""

    def __init__(self, metrics: WorkerMetrics, host: str, port: int) -> None:
        self.metrics = metrics
        handler = _handler_for(metrics)

        class _Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = _Server((host, port), handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="sentinel-metrics",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


def _handler_for(metrics: WorkerMetrics) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = urlparse(self.path).path
            if path == "/healthz":
                body = json.dumps(
                    {
                        "status": "ok" if metrics.accepting_work else "paused",
                        "capability": metrics.capability,
                        "worker_id": metrics.worker_id,
                        "active_state": metrics.active_state,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                content_type = "application/json"
            elif path == "/metrics":
                body = metrics.render().encode("utf-8")
                content_type = "text/plain; version=0.0.4; charset=utf-8"
            else:
                body = b'{"status":"not_found"}\n'
                content_type = "application/json"
                self.send_response(404)
                self._finish(body, content_type)
                return
            self.send_response(200)
            self._finish(body, content_type)

        def _finish(self, body: bytes, content_type: str) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler
