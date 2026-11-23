"""Structured JSON logging and per-request correlation ids."""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

request_id_var: ContextVar[str] = ContextVar("sentinel_request_id", default="-")
log = logging.getLogger("sentinel.api")


def get_request_id() -> str:
    return request_id_var.get()


class JsonFormatter(logging.Formatter):
    """One JSON object per line; ``event_fields`` are merged at top level."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "event_fields", {}) or {})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    if any(getattr(h, "_sentinel_json", False) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler._sentinel_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id, log one structured line, never log headers/body."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            log.info(
                "http_request",
                extra={
                    "event_fields": {
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "status": status,
                        "duration_ms": duration_ms,
                    }
                },
            )
            request_id_var.reset(token)


def install_request_logging(app: FastAPI, level: str = "INFO") -> None:
    configure_logging(level)
    app.add_middleware(RequestContextMiddleware)
