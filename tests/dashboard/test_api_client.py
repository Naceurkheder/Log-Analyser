from __future__ import annotations

import httpx

from sentinel.dashboard.api_client import SentinelApiClient


def test_read_endpoints_and_query_bounds():
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "version": "1.0"})
        if request.url.path == "/v1/runs/run_1/jobs":
            return httpx.Response(200, json={"items": [], "count": 0, "next_cursor": None})
        if request.url.path == "/v1/runs/run_1/alerts":
            return httpx.Response(200, json={"items": [], "count": 0, "next_cursor": None})
        if request.url.path == "/v1/runs/run_1":
            return httpx.Response(200, json={"run_id": "run_1", "status": "RUNNING"})
        return httpx.Response(404, json={"error": "missing"})

    client = SentinelApiClient("http://api.test/", transport=httpx.MockTransport(handler))
    assert client.health().data["status"] == "ok"
    assert client.get_run("run_1").data["run_id"] == "run_1"
    assert client.list_jobs("run_1", limit=500, cursor="abc").ok
    assert client.list_alerts("run_1", limit=10).ok

    job_params = seen[2].params
    assert job_params["limit"] == "200"
    assert job_params["cursor"] == "abc"
    assert seen[3].params["limit"] == "10"


def test_degraded_states_are_explicit():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/broken-json":
            return httpx.Response(200, text="not json")
        return httpx.Response(503, text="down")

    client = SentinelApiClient("http://api.test", transport=httpx.MockTransport(handler))
    run = client.get_run("run_x")
    assert not run.ok
    assert "503" in (run.error or "")

    assert not client._get("/broken-json").ok

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    unreachable = SentinelApiClient("http://api.test", transport=httpx.MockTransport(boom))
    assert "cannot reach" in (unreachable.health().error or "")
