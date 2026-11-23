"""DynamoDB / S3 / SQS adapter for the control API.

The store keeps the conditional expressions explicit: run creation is guarded
by ``attribute_not_exists`` so two concurrent requests with the same
idempotency key can never create two logical runs.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import Settings

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()

RUN_INDEX = "run_id-index"


def _clean(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def to_item(value: dict[str, Any]) -> dict[str, Any]:
    return {k: _serializer.serialize(_clean(v)) for k, v in value.items()}


def from_item(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _from_ddb(_deserializer.deserialize(v)) for k, v in item.items()}


def _from_ddb(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_ddb(v) for v in value]
    return value


class SentinelStore:
    def __init__(self, settings: Settings):
        config = Config(
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=3,
            read_timeout=30,
            s3={"addressing_style": "path"},
        )
        kwargs = {
            "endpoint_url": settings.aws_endpoint_url,
            "region_name": settings.aws_region,
            "aws_access_key_id": settings.aws_access_key_id,
            "aws_secret_access_key": settings.aws_secret_access_key,
            "config": config,
        }
        self.settings = settings
        self.s3 = boto3.client("s3", **kwargs)
        self.sqs = boto3.client("sqs", **kwargs)
        self.dynamodb = boto3.client("dynamodb", **kwargs)
        self._ingest_url: str | None = None
        self._job_urls: dict[str, str] = {}

    # -- object storage -----------------------------------------------------
    def head_dataset(self, bucket: str, key: str) -> int:
        head = self.s3.head_object(Bucket=bucket, Key=key)
        return int(head["ContentLength"])

    def head_object(self, bucket: str, key: str) -> dict[str, Any] | None:
        try:
            return self.s3.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise

    def open_object(self, bucket: str, key: str) -> tuple[Any, int, str]:
        """Open a bounded streaming read. The caller closes the returned body."""
        response = self.s3.get_object(Bucket=bucket, Key=key)
        return response["Body"], int(response["ContentLength"]), str(
            response.get("ETag", "").strip('"')
        )

    def get_object_bytes(self, bucket: str, key: str) -> bytes:
        return self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    def put_immutable(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        metadata: dict[str, str] | None = None,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Write an object whose key is content-addressed.

        ``IfNoneMatch='*'`` turns the write into a create-if-absent operation.
        Re-running the producer for the same run therefore reuses the first
        object instead of replacing it; the caller verifies the existing size
        and metadata before trusting it.
        """
        try:
            self.s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                Metadata=metadata or {},
                ContentType=content_type,
                IfNoneMatch="*",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in {"PreconditionFailed", "ConditionalRequestConflict"}:
                raise

    def put_json_immutable(
        self, bucket: str, key: str, body: bytes, *, metadata: dict[str, str]
    ) -> None:
        self.put_immutable(
            bucket, key, body, metadata=metadata, content_type="application/json"
        )

    def put_json_overwrite(
        self, bucket: str, key: str, body: bytes, *, metadata: dict[str, str]
    ) -> None:
        """Write a mutable checkpoint/progress document."""
        self.s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            Metadata=metadata,
            ContentType="application/json",
        )

    # -- ingestion queue ----------------------------------------------------
    def ingest_queue_url(self) -> str:
        if self._ingest_url is None:
            self._ingest_url = self.settings.ingest_queue_url or (
                self.sqs.get_queue_url(
                    QueueName=self.settings.ingest_queue_name
                )["QueueUrl"]
            )
        return self._ingest_url

    def check_ingest_queue(self) -> None:
        """Resolve the queue and prove it exists (used by ``/readyz``)."""
        self.sqs.get_queue_attributes(
            QueueUrl=self.ingest_queue_url(), AttributeNames=["QueueArn"]
        )

    def send_ingest(self, message: dict[str, Any]) -> None:
        self._send(self.ingest_queue_url(), message)

    def jobs_queue_url(self, backend: str) -> str:
        if backend not in self._job_urls:
            if backend == "gpu":
                url = self.settings.jobs_gpu_queue_url or self.sqs.get_queue_url(
                    QueueName=self.settings.jobs_gpu_queue_name
                )["QueueUrl"]
            elif backend == "cpu":
                url = self.settings.jobs_cpu_queue_url or self.sqs.get_queue_url(
                    QueueName=self.settings.jobs_cpu_queue_name
                )["QueueUrl"]
            else:  # pragma: no cover - guarded by the caller
                raise ValueError(f"unknown backend: {backend}")
            self._job_urls[backend] = url
        return self._job_urls[backend]

    def send_job(self, backend: str, message: dict[str, Any]) -> None:
        self._send(self.jobs_queue_url(backend), message)

    def _send(self, queue_url: str, message: dict[str, Any]) -> None:
        import json

        self.sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message, sort_keys=True),
            MessageAttributes={
                "message_type": {
                    "DataType": "String",
                    "StringValue": message.get("message_type", "job"),
                }
            },
        )

    # -- runs ---------------------------------------------------------------
    def create_run(self, run: dict[str, Any]) -> bool:
        try:
            self.dynamodb.put_item(
                TableName=self.settings.runs_table,
                Item=to_item(run),
                ConditionExpression="attribute_not_exists(run_id)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        response = self.dynamodb.get_item(
            TableName=self.settings.runs_table,
            Key={"run_id": {"S": run_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return from_item(item) if item else None

    def set_run_state(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        parts: list[str] = []
        for index, (key, value) in enumerate(fields.items()):
            nk, vk = f"#f{index}", f":v{index}"
            names[nk] = key
            values[vk] = _clean(value)
            parts.append(f"{nk} = {vk}")
        self.dynamodb.update_item(
            TableName=self.settings.runs_table,
            Key={"run_id": {"S": run_id}},
            UpdateExpression="SET " + ", ".join(parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={
                k: _serializer.serialize(v) for k, v in values.items()
            },
            ConditionExpression="attribute_exists(run_id)",
        )

    # -- jobs ---------------------------------------------------------------
    def create_job(self, job: dict[str, Any]) -> bool:
        try:
            self.dynamodb.put_item(
                TableName=self.settings.jobs_table,
                Item=to_item(job),
                ConditionExpression="attribute_not_exists(job_id)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        response = self.dynamodb.get_item(
            TableName=self.settings.jobs_table,
            Key={"job_id": {"S": job_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return from_item(item) if item else None

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        parts: list[str] = []
        for index, (key, value) in enumerate(fields.items()):
            nk, vk = f"#f{index}", f":v{index}"
            names[nk] = key
            values[vk] = _clean(value)
            parts.append(f"{nk} = {vk}")
        self.dynamodb.update_item(
            TableName=self.settings.jobs_table,
            Key={"job_id": {"S": job_id}},
            UpdateExpression="SET " + ", ".join(parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={
                k: _serializer.serialize(v) for k, v in values.items()
            },
            ConditionExpression="attribute_exists(job_id)",
        )

    # -- pagination ---------------------------------------------------------
    def _query_run(
        self, table: str, run_id: str, limit: int, start_key: dict[str, Any] | None
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        kwargs: dict[str, Any] = {
            "TableName": table,
            "IndexName": RUN_INDEX,
            "KeyConditionExpression": "run_id = :r",
            "ExpressionAttributeValues": {":r": {"S": run_id}},
            "Limit": limit,
        }
        if start_key:
            # The public cursor carries plain (job_id, run_id) strings; the
            # DynamoDB wire form is reconstructed here.
            kwargs["ExclusiveStartKey"] = to_item(start_key)
        response = self.dynamodb.query(**kwargs)
        items = [from_item(item) for item in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        return items, from_item(last_key) if last_key else None

    def query_jobs(
        self, run_id: str, limit: int, start_key: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        return self._query_run(
            self.settings.jobs_table, run_id, limit, start_key
        )

    def query_alerts(
        self, run_id: str, limit: int, start_key: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        return self._query_run(
            self.settings.alerts_table, run_id, limit, start_key
        )

    # -- readiness ----------------------------------------------------------
    def readiness(self) -> tuple[bool, dict[str, str]]:
        checks: dict[str, str] = {}
        for label, table in (
            ("dynamodb.runs", self.settings.runs_table),
            ("dynamodb.jobs", self.settings.jobs_table),
            ("dynamodb.alerts", self.settings.alerts_table),
        ):
            try:
                self.dynamodb.describe_table(TableName=table)
                checks[label] = "ok"
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code == "ResourceNotFoundException":
                    checks[label] = "missing"
                else:
                    checks[label] = f"error: {code}"
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                checks[label] = f"error: {type(exc).__name__}"
        try:
            self.s3.head_bucket(Bucket=self.settings.incoming_bucket)
            checks["s3.incoming"] = "ok"
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            checks["s3.incoming"] = (
                "missing" if code in ("404", "NoSuchBucket", "NotFound") else "error"
            )
        except Exception as exc:  # noqa: BLE001
            checks["s3.incoming"] = f"error: {type(exc).__name__}"
        try:
            self.check_ingest_queue()
            checks["sqs.ingest"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["sqs.ingest"] = f"error: {type(exc).__name__}"
        return all(value == "ok" for value in checks.values()), checks
