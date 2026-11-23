"""SQS ingest-loop for the streaming producer."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..api.config import Settings
from ..api.store import SentinelStore
from .producer import Producer, TerminalIngestError

log = logging.getLogger("sentinel.producer")


class IngestRunner:
    def __init__(
        self,
        producer: Producer | None = None,
        settings: Settings | None = None,
        store: SentinelStore | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.store = store or SentinelStore(self.settings)
        self.producer = producer or Producer(self.settings, self.store)

    def process_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Process one received SQS message.

        Returns the producer result.  Raises ``TerminalIngestError`` for
        messages that cannot succeed by retrying; the caller deletes those so
        they do not churn until the DLQ threshold.
        """
        return self.producer.process(message)

    def poll(
        self, *, max_messages: int = 1, wait_seconds: int = 0
    ) -> list[dict[str, Any]]:
        response = self.store.sqs.receive_message(
            QueueUrl=self.store.ingest_queue_url(),
            MaxNumberOfMessages=max(1, min(max_messages, 10)),
            WaitTimeSeconds=max(0, min(wait_seconds, 20)),
            AttributeNames=["ApproximateReceiveCount"],
        )
        results: list[dict[str, Any]] = []
        for message in response.get("Messages", []):
            try:
                body = json.loads(message["Body"])
            except json.JSONDecodeError:
                log.error(
                    "producer_invalid_message_json",
                    extra={"event_fields": {"sqs_message_id": message.get("MessageId")}},
                )
                # Poison payload: delete so it cannot loop forever; the SQS
                # message id is retained in the log for diagnosis.
                self._delete(message)
                continue

            try:
                result = self.process_message(body)
            except TerminalIngestError as exc:
                log.error(
                    "producer_terminal_error",
                    extra={
                        "event_fields": {
                            "code": exc.code,
                            "run_id": exc.run_id,
                            "details": exc.details,
                            "sqs_message_id": message.get("MessageId"),
                        }
                    },
                )
                self._delete(message)
                results.append(
                    {
                        "run_id": exc.run_id,
                        "error": exc.code,
                        "terminal": True,
                    }
                )
                continue
            except Exception as exc:  # noqa: BLE001 - transient, leave for retry
                log.exception(
                    "producer_transient_error",
                    extra={
                        "event_fields": {
                            "error": type(exc).__name__,
                            "sqs_message_id": message.get("MessageId"),
                            "receive_count": message.get("Attributes", {}).get(
                                "ApproximateReceiveCount"
                            ),
                        }
                    },
                )
                results.append(
                    {
                        "run_id": body.get("run_id"),
                        "error": type(exc).__name__,
                        "terminal": False,
                    }
                )
                continue

            self._delete(message)
            results.append(result)
        return results

    def _delete(self, message: dict[str, Any]) -> None:
        self.store.sqs.delete_message(
            QueueUrl=self.store.ingest_queue_url(),
            ReceiptHandle=message["ReceiptHandle"],
        )

    def run(
        self,
        *,
        once: bool = True,
        max_messages: int = 1,
        wait_seconds: int = 0,
        idle_sleep_seconds: float = 1.0,
    ) -> list[dict[str, Any]]:
        """Poll the ingest queue; ``once=False`` keeps polling until stopped."""
        all_results: list[dict[str, Any]] = []
        while True:
            results = self.poll(max_messages=max_messages, wait_seconds=wait_seconds)
            all_results.extend(results)
            if once:
                return all_results
            if not results:
                time.sleep(idle_sleep_seconds)
