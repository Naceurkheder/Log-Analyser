"""Read-only client for the FastAPI control boundary.

The dashboard never writes to DynamoDB, SQS or S3. Every live value comes from
the versioned read endpoints exposed by the API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class ApiResponse:
    data: Any | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class SentinelApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    def _get(self, path: str, params: dict[str, Any] | None = None) -> ApiResponse:
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                response = client.get(f"{self.base_url}{path}", params=params)
        except httpx.HTTPError as exc:
            return ApiResponse(None, f"cannot reach {self.base_url}: {exc.__class__.__name__}")

        if response.status_code >= 400:
            return ApiResponse(None, f"API returned {response.status_code} for {path}")
        try:
            return ApiResponse(response.json(), None)
        except ValueError:
            return ApiResponse(None, f"API returned non-JSON for {path}")

    def health(self) -> ApiResponse:
        return self._get("/healthz")

    def readiness(self) -> ApiResponse:
        return self._get("/readyz")

    def get_run(self, run_id: str) -> ApiResponse:
        return self._get(f"/v1/runs/{run_id}")

    def list_jobs(
        self, run_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> ApiResponse:
        params: dict[str, Any] = {"limit": min(max(limit, 1), 200)}
        if cursor:
            params["cursor"] = cursor
        return self._get(f"/v1/runs/{run_id}/jobs", params)

    def list_alerts(
        self, run_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> ApiResponse:
        params: dict[str, Any] = {"limit": min(max(limit, 1), 200)}
        if cursor:
            params["cursor"] = cursor
        return self._get(f"/v1/runs/{run_id}/alerts", params)
