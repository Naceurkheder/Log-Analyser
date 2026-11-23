"""Operator CLI: upload a dataset, then submit a run request.

Usage:
    PYTHONPATH=src python -m sentinel.api.cli upload data/bench.log \
        --key datasets/bench.log
    PYTHONPATH=src python -m sentinel.api.cli submit --request run.json
    PYTHONPATH=src python -m sentinel.api.cli serve --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

import boto3
from pydantic import ValidationError

from .config import Settings
from .contract import canonical_json
from .schemas import RunRequest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _s3_client(endpoint_url: str | None, region: str) -> Any:
    kwargs: dict[str, Any] = {"region_name": region}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.client("s3", **kwargs)


def _upload(
    path: Path,
    *,
    key: str,
    bucket: str | None = None,
    endpoint_url: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """Upload a dataset and return the authoritative reference fields."""
    settings = Settings.from_env()
    bucket = bucket or settings.incoming_bucket
    digest = _sha256_file(path)
    _s3_client(endpoint_url or settings.aws_endpoint_url, region or settings.aws_region).upload_file(
        str(path), bucket, key
    )
    return {
        "bucket": bucket,
        "key": key,
        "size_bytes": path.stat().st_size,
        "sha256": digest,
    }


def _cmd_upload(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 2
    if path.stat().st_size == 0:
        print("error: refusing to upload an empty dataset", file=sys.stderr)
        return 2
    report = _upload(
        path,
        key=args.key,
        bucket=args.bucket,
        endpoint_url=args.endpoint_url,
        region=args.region,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> tuple[int, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"error": {"message": raw.decode("utf-8", "replace")}}


def _get_json(url: str) -> tuple[int, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"error": {"message": raw.decode("utf-8", "replace")}}


def _render(args: argparse.Namespace, status: int, body: Any) -> int:
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0 if 200 <= status < 300 else 1


def _cmd_status(args: argparse.Namespace) -> int:
    base = (args.api_url or Settings.from_env().api_url).rstrip("/")
    return _render(args, *_get_json(f"{base}/v1/runs/{args.run_id}"))


def _cmd_page(args: argparse.Namespace, collection: str) -> int:
    base = (args.api_url or Settings.from_env().api_url).rstrip("/")
    query = f"?limit={args.limit}"
    if args.cursor:
        query += f"&cursor={args.cursor}"
    return _render(
        args, *_get_json(f"{base}/v1/runs/{args.run_id}/{collection}{query}")
    )


def _cmd_jobs(args: argparse.Namespace) -> int:
    return _cmd_page(args, "jobs")


def _cmd_alerts(args: argparse.Namespace) -> int:
    return _cmd_page(args, "alerts")


def _cmd_submit_file(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 2
    if path.stat().st_size == 0:
        print("error: refusing to upload an empty dataset", file=sys.stderr)
        return 2
    key = args.key or f"datasets/{path.name}-{_sha256_file(path)[:12]}"
    dataset = _upload(
        path, key=key, endpoint_url=args.endpoint_url, region=args.region
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "dataset": dataset,
        "detector": {
            "rules": [
                {
                    "rule_id": "cli-pattern-0",
                    "pattern": args.pattern,
                    "severity": "medium",
                }
            ]
        },
        "backend_policy": args.backend,
    }
    try:
        RunRequest.model_validate(payload)
    except ValidationError as exc:  # pragma: no cover - defensive
        print(f"error: invalid run request: {exc}", file=sys.stderr)
        return 2
    idempotency_key = args.idempotency_key or hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()[:32]
    base = (args.api_url or Settings.from_env().api_url).rstrip("/")
    return _render(
        args,
        *_post_json(
            f"{base}/v1/runs",
            payload,
            {
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
        ),
    )


def _cmd_submit(args: argparse.Namespace) -> int:
    request_path = Path(args.request)
    if not request_path.is_file():
        print(f"error: no such file: {request_path}", file=sys.stderr)
        return 2

    payload = json.loads(request_path.read_text(encoding="utf-8"))
    # Validate locally so obvious mistakes never reach the service.
    try:
        RunRequest.model_validate(payload)
    except ValidationError as exc:
        print(f"error: invalid run request: {exc}", file=sys.stderr)
        return 2

    idempotency_key = args.idempotency_key or hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()[:32]
    url = (args.api_url or Settings.from_env().api_url).rstrip("/") + "/v1/runs"

    status, response = _post_json(
        url,
        payload,
        {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        },
    )
    print(json.dumps(response, indent=2, sort_keys=True))
    return 0 if 200 <= status < 300 else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(
        "sentinel.api.main:app",
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


def _cmd_openapi(args: argparse.Namespace) -> int:
    from .main import create_app

    document = create_app().openapi()
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel-api", description="Sentinel-GPU control API tooling"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    upload = sub.add_parser("upload", help="upload a dataset to the incoming bucket")
    upload.add_argument("file", help="local file to upload")
    upload.add_argument("--key", required=True, help="destination object key")
    upload.add_argument("--bucket", default=None, help="destination bucket")
    upload.add_argument("--endpoint-url", default=None, help="AWS endpoint override")
    upload.add_argument("--region", default=None, help="AWS region override")
    upload.set_defaults(func=_cmd_upload)

    submit = sub.add_parser("submit", help="submit a run request JSON document")
    submit.add_argument("--request", required=True, help="RunRequest JSON file")
    submit.add_argument("--api-url", default=None, help="API base URL")
    submit.add_argument(
        "--idempotency-key",
        default=None,
        help="override the key (defaults to a hash of the request body)",
    )
    submit.set_defaults(func=_cmd_submit)

    submit_file = sub.add_parser(
        "submit-file", help="upload a dataset and submit a run in one step"
    )
    submit_file.add_argument("file", help="local log file to upload")
    submit_file.add_argument("--key", default=None, help="destination object key")
    submit_file.add_argument("--pattern", default="ERROR", help="initial detector rule")
    submit_file.add_argument(
        "--backend", choices=("gpu", "cpu", "auto"), default="auto"
    )
    submit_file.add_argument("--api-url", default=None)
    submit_file.add_argument("--endpoint-url", default=None)
    submit_file.add_argument("--region", default=None)
    submit_file.add_argument("--idempotency-key", default=None)
    submit_file.set_defaults(func=_cmd_submit_file)

    status = sub.add_parser("status", help="render one run status document")
    status.add_argument("run_id")
    status.add_argument("--api-url", default=None)
    status.set_defaults(func=_cmd_status)

    for name, function, help_text in (
        ("jobs", _cmd_jobs, "render a page of jobs"),
        ("alerts", _cmd_alerts, "render a page of alerts"),
    ):
        page = sub.add_parser(name, help=help_text)
        page.add_argument("run_id")
        page.add_argument("--limit", type=int, default=50)
        page.add_argument("--cursor", default=None)
        page.add_argument("--api-url", default=None)
        page.set_defaults(func=function)

    serve = sub.add_parser("serve", help="run the API with uvicorn")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=_cmd_serve)

    openapi = sub.add_parser("openapi", help="print or export the OpenAPI document")
    openapi.add_argument("--out", default=None, help="write to a file instead of stdout")
    openapi.set_defaults(func=_cmd_openapi)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
