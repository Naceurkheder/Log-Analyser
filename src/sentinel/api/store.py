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

    # -- object storage -----------------------------------------------------
    def head_dataset(self, bucket: str, key: str) -> int:
        head = self.s3.head_object(Bucket=bucket, Key=key)
        return int(head["ContentLength"])

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
        import json

        self.sqs.send_message(
            QueueUrl=self.ingest_queue_url(),
            MessageBody=json.dumps(message, sort_keys=True),
            MessageAttributes={
                "message_type": {
                    "DataType": "String",
                    "StringValue": message.get("message_type", "ingest"),
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
