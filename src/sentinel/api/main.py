"""FastAPI application: the platform's control boundary.

The API validates a small control request, records an idempotent run, and
publishes an ingest message. It never reads or streams log contents.
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, Header, Query, Request, Response

from .config import Settings
from .contract import (
    decode_cursor,
    derive_run_id,
    detector_config_hash,
    encode_cursor,
    request_fingerprint,
    utcnow,
    validate_idempotency_key,
)
from .errors import ApiError, install_error_handlers
from .log import install_request_logging
from .metrics import add_metrics_route, api_metrics
from .schemas import (
    AlertPage,
    AlertSummary,
    DatasetRef,
    ErrorResponse,
    JobPage,
    JobSummary,
    Links,
    LivenessResponse,
    Progress,
    ReadinessResponse,
    RunCreatedResponse,
    RunRequest,
    RunStatusResponse,
)
from .store import SentinelStore

API_VERSION = "0.3.0"
log = logging.getLogger("sentinel.api")

API_DESCRIPTION = """
Control API for the Sentinel-GPU log-analysis pipeline.

* Submit a run with `POST /v1/runs`; the body carries **object references and
  detector configuration only**, never raw log lines.
* `Idempotency-Key` is mandatory. Repeating a key with the same payload returns
  the original run; reusing it with a different payload is rejected with `409`.
* Jobs and alerts are served as cursor-paginated pages (DynamoDB
  `run_id-index`), so responses stay bounded.
""".strip()

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Invalid request or cursor"},
    404: {"model": ErrorResponse, "description": "Unknown run"},
    409: {"model": ErrorResponse, "description": "Idempotency key reuse"},
    422: {"model": ErrorResponse, "description": "Schema or reference validation"},
    503: {"model": ErrorResponse, "description": "Dependency unavailable"},
}


def _links(run_id: str) -> Links:
    return Links(
        run=f"/v1/runs/{run_id}",
        jobs=f"/v1/runs/{run_id}/jobs",
        alerts=f"/v1/runs/{run_id}/alerts",
    )


def _get_store(request: Request) -> SentinelStore:
    return request.app.state.store


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings


def _require_run(store: SentinelStore, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise ApiError(404, "run_not_found", f"run '{run_id}' does not exist")
    return run


def _resolve_dataset(
    settings: Settings, store: SentinelStore, ref: DatasetRef
) -> dict[str, Any]:
    """Validate the object reference and fill in the authoritative size."""
    dataset = ref.model_dump(mode="json")
    if ref.bucket != settings.incoming_bucket:
        raise ApiError(
            422,
            "dataset_bucket_not_allowed",
            f"datasets must be stored in the '{settings.incoming_bucket}' bucket",
            details={"bucket": ref.bucket},
        )

    if not settings.verify_dataset_object:
        return dataset

    try:
        size = store.head_dataset(ref.bucket, ref.key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            raise ApiError(
                422,
                "dataset_not_found",
                f"dataset s3://{ref.bucket}/{ref.key} does not exist",
            ) from exc
        raise ApiError(
            503, "dependency_unavailable", "object storage is unavailable"
        ) from exc

    if size == 0:
        raise ApiError(422, "empty_dataset", "the dataset object is empty")
    if ref.size_bytes is not None and ref.size_bytes != size:
        raise ApiError(
            422,
            "dataset_size_mismatch",
            "declared dataset size does not match the stored object",
            details={"declared_bytes": ref.size_bytes, "actual_bytes": size},
        )
    if size > settings.max_dataset_bytes:
        raise ApiError(
            422,
            "dataset_too_large",
            "dataset exceeds the configured size limit",
            details={"size_bytes": size, "max_bytes": settings.max_dataset_bytes},
        )

    dataset["size_bytes"] = size
    return dataset


def _ingest_message(
    run: dict[str, Any], request_hash: str, config_hash: str
) -> dict[str, Any]:
    """Queue payload: references and metadata only, no raw log content."""
    return {
        "schema_version": run["schema_version"],
        "message_type": "ingest",
        "run_id": run["run_id"],
        "request_hash": request_hash,
        "detector_config_hash": config_hash,
        "backend_policy": run["backend_policy"],
        "dataset": run["dataset"],
        "detector": run["detector"],
    }


def _queue_run(store: SentinelStore, run: dict[str, Any]) -> dict[str, Any]:
    """Publish the ingest message; the store resolves URL-or-name itself."""
    if run.get("ingest_published"):
        return run

    try:
        store.send_ingest(
            _ingest_message(run, run["request_hash"], run["detector_config_hash"])
        )
    except ClientError as exc:
        log.warning(
            "ingest_publish_failed",
            extra={"event_fields": {"run_id": run["run_id"], "error": type(exc).__name__}},
        )
        try:
            store.set_run_state(
                run["run_id"],
                status="INGEST_FAILED",
                ingest_published=False,
                updated_at=utcnow(),
            )
        except ClientError:
            pass
        raise ApiError(
            503,
            "ingest_publish_failed",
            "run was recorded but could not be queued; retry with the same "
            "Idempotency-Key",
            details={"run_id": run["run_id"]},
        ) from exc

    updated_at = utcnow()
    store.set_run_state(
        run["run_id"],
        status="QUEUED",
        ingest_published=True,
        updated_at=updated_at,
    )
    queued = dict(run)
    queued.update(status="QUEUED", ingest_published=True, updated_at=updated_at)
    return queued


def _created_response(run: dict[str, Any], *, created: bool) -> RunCreatedResponse:
    return RunCreatedResponse(
        run_id=run["run_id"],
        schema_version=run["schema_version"],
        status=run["status"],
        created=created,
        created_at=run["created_at"],
        links=_links(run["run_id"]),
    )


def _register_routes(app: FastAPI) -> None:
    @app.post(
        "/v1/runs",
        response_model=RunCreatedResponse,
        status_code=201,
        tags=["runs"],
        summary="Create or retrieve an idempotent run",
        description=(
            "Validate the dataset reference and detector configuration, then "
            "record the run. The response is `201` for a new run and `200` when "
            "the same Idempotency-Key and payload are replayed."
        ),
        responses={201: {"description": "Run created"}, **ERROR_RESPONSES},
    )
    def create_run(
        payload: RunRequest,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            description="Stable client key (8-128 chars) for retry-safe creation.",
        ),
        store: SentinelStore = Depends(_get_store),
        settings: Settings = Depends(_get_settings),
    ) -> RunCreatedResponse:
        key = validate_idempotency_key(idempotency_key)
        fingerprint = request_fingerprint(payload)
        run_id = derive_run_id(key)

        existing = store.get_run(run_id)
        if existing is not None:
            return _replay(existing, fingerprint, store, response)

        dataset = _resolve_dataset(settings, store, payload.dataset)
        now = utcnow()
        run: dict[str, Any] = {
            "run_id": run_id,
            "schema_version": payload.schema_version,
            "idempotency_key": key,
            "request_hash": fingerprint,
            "status": "PENDING",
            "backend_policy": payload.backend_policy,
            "created_at": now,
            "updated_at": now,
            "dataset": dataset,
            "detector": payload.detector.model_dump(mode="json"),
            "detector_config_hash": detector_config_hash(payload.detector),
            "jobs_total": 0,
            "jobs_completed": 0,
            "jobs_failed": 0,
            "alerts_total": 0,
            "ingest_published": False,
        }

        if not store.create_run(run):
            # Lost a race with a concurrent request for the same key.
            existing = store.get_run(run_id)
            if existing is None:  # pragma: no cover - emulator inconsistency
                raise ApiError(
                    503,
                    "run_creation_race",
                    "run creation raced; retry with the same Idempotency-Key",
                )
            return _replay(existing, fingerprint, store, response)

        run = _queue_run(store, run)
        response.headers["Location"] = f"/v1/runs/{run_id}"
        return _created_response(run, created=True)

    @app.get(
        "/v1/runs/{run_id}",
        response_model=RunStatusResponse,
        tags=["runs"],
        summary="Run status and aggregate progress",
        responses=ERROR_RESPONSES,
    )
    def get_run(
        run_id: str,
        store: SentinelStore = Depends(_get_store),
    ) -> RunStatusResponse:
        run = _require_run(store, run_id)
        return RunStatusResponse(
            run_id=run["run_id"],
            schema_version=run["schema_version"],
            status=run["status"],
            backend_policy=run["backend_policy"],
            created_at=run["created_at"],
            updated_at=run["updated_at"],
            dataset=DatasetRef.model_validate(run["dataset"]),
            detector=run["detector"],
            detector_config_hash=run["detector_config_hash"],
            progress=Progress(
                jobs_total=run.get("jobs_total", 0),
                jobs_completed=run.get("jobs_completed", 0),
                jobs_failed=run.get("jobs_failed", 0),
                alerts_total=run.get("alerts_total", 0),
            ),
            links=_links(run["run_id"]),
        )

    @app.get(
        "/v1/runs/{run_id}/jobs",
        response_model=JobPage,
        tags=["jobs"],
        summary="Paginated job status for a run",
        responses=ERROR_RESPONSES,
    )
    def list_jobs(
        run_id: str,
        store: SentinelStore = Depends(_get_store),
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, description="Opaque page cursor"),
    ) -> JobPage:
        _require_run(store, run_id)
        start_key = (
            decode_cursor(cursor, ("job_id", "run_id")) if cursor else None
        )
        items, last_key = store.query_jobs(run_id, limit=limit, start_key=start_key)
        return JobPage(
            items=[JobSummary.model_validate(item) for item in items],
            count=len(items),
            next_cursor=encode_cursor(last_key) if last_key else None,
        )

    @app.get(
        "/v1/runs/{run_id}/alerts",
        response_model=AlertPage,
        tags=["alerts"],
        summary="Paginated alerts for a run",
        responses=ERROR_RESPONSES,
    )
    def list_alerts(
        run_id: str,
        store: SentinelStore = Depends(_get_store),
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, description="Opaque page cursor"),
    ) -> AlertPage:
        _require_run(store, run_id)
        start_key = (
            decode_cursor(cursor, ("alert_id", "run_id")) if cursor else None
        )
        items, last_key = store.query_alerts(
            run_id, limit=limit, start_key=start_key
        )
        return AlertPage(
            items=[AlertSummary.model_validate(item) for item in items],
            count=len(items),
            next_cursor=encode_cursor(last_key) if last_key else None,
        )

    @app.get(
        "/healthz",
        response_model=LivenessResponse,
        tags=["health"],
        summary="Process liveness",
    )
    def healthz() -> LivenessResponse:
        return LivenessResponse(version=API_VERSION)

    @app.get(
        "/readyz",
        response_model=ReadinessResponse,
        tags=["health"],
        summary="Dependency readiness",
        responses={503: {"model": ReadinessResponse, "description": "Not ready"}},
    )
    def readyz(
        response: Response,
        store: SentinelStore = Depends(_get_store),
    ) -> ReadinessResponse:
        ready, checks = store.readiness()
        if not ready:
            response.status_code = 503
        return ReadinessResponse(
            status="ready" if ready else "not_ready", checks=checks
        )


def _replay(
    existing: dict[str, Any],
    fingerprint: str,
    store: SentinelStore,
    response: Response,
) -> RunCreatedResponse:
    if existing.get("request_hash") != fingerprint:
        raise ApiError(
            409,
            "idempotency_key_reuse",
            "this Idempotency-Key was already used with a different payload",
            details={"run_id": existing.get("run_id")},
        )
    # A previous attempt may have recorded the run but failed to publish it.
    existing = _queue_run(store, existing)
    response.status_code = 200
    return _created_response(existing, created=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="Sentinel-GPU API",
        version=API_VERSION,
        description=API_DESCRIPTION,
        openapi_tags=[
            {"name": "runs", "description": "Run creation, idempotency, status"},
            {"name": "jobs", "description": "Paginated job projections"},
            {"name": "alerts", "description": "Paginated alert projections"},
            {"name": "health", "description": "Liveness and readiness"},
        ],
    )
    app.state.settings = settings
    app.state.store = SentinelStore(settings)
    install_request_logging(app, settings.log_level)
    install_error_handlers(app)
    add_metrics_route(app, api_metrics)
    _register_routes(app)
    return app


app = create_app()
