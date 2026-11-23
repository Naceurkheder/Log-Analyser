"""Environment-driven configuration for the optional Spark stage.

Only the base runtime dependencies are required to import this module; pyspark
is imported lazily by :mod:`sentinel.spark.job`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

from ..api.config import Settings
from .core import S3Connection


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or not raw.strip() else int(raw)


@dataclass(frozen=True)
class SparkSettings:
    """Immutable snapshot shared by the driver and its injected task clients."""

    api: Settings
    connection: S3Connection
    master: str = "local[*]"
    app_name: str = "sentinel-spark"
    shuffle_partitions: int = 8
    max_parallelism: int = 8
    page_size: int = 100
    max_pages: int = 10
    limit: int = 200
    results_bucket: str = "results"
    committer: str = "logging"

    @classmethod
    def from_env(cls) -> "SparkSettings":
        api = Settings.from_env()
        settings = cls(
            api=api,
            connection=S3Connection(
                endpoint_url=api.aws_endpoint_url,
                region=api.aws_region,
                access_key_id=api.aws_access_key_id,
                secret_access_key=api.aws_secret_access_key,
            ),
            master=os.environ.get("SENTINEL_SPARK_MASTER") or "local[*]",
            app_name=os.environ.get("SENTINEL_SPARK_APP_NAME") or "sentinel-spark",
            shuffle_partitions=_env_int("SENTINEL_SPARK_SHUFFLE_PARTITIONS", 8),
            max_parallelism=_env_int("SENTINEL_SPARK_MAX_PARALLELISM", 8),
            page_size=_env_int("SENTINEL_SPARK_PAGE_SIZE", 100),
            max_pages=_env_int("SENTINEL_SPARK_MAX_PAGES", 10),
            limit=_env_int("SENTINEL_SPARK_LIMIT", 200),
            results_bucket=(
                os.environ.get("SENTINEL_RESULTS_BUCKET") or "results"
            ),
            committer=os.environ.get("SENTINEL_SPARK_COMMITTER") or "logging",
        )
        settings.validate()
        return settings

    def overrides(
        self,
        *,
        master: str | None = None,
        app_name: str | None = None,
        shuffle_partitions: int | None = None,
        max_parallelism: int | None = None,
        page_size: int | None = None,
        max_pages: int | None = None,
        limit: int | None = None,
        results_bucket: str | None = None,
        endpoint_url: str | None = None,
        committer: str | None = None,
    ) -> "SparkSettings":
        """Return a new snapshot with CLI/explicit overrides applied."""
        api = self.api
        connection = self.connection
        if endpoint_url is not None:
            endpoint = endpoint_url or None
            api = replace(api, aws_endpoint_url=endpoint)
            connection = replace(connection, endpoint_url=endpoint)
        values: dict[str, Any] = {}
        for name, value in (
            ("master", master),
            ("app_name", app_name),
            ("shuffle_partitions", shuffle_partitions),
            ("max_parallelism", max_parallelism),
            ("page_size", page_size),
            ("max_pages", max_pages),
            ("limit", limit),
            ("results_bucket", results_bucket),
            ("committer", committer),
        ):
            if value is not None:
                values[name] = value
        updated = replace(self, api=api, connection=connection, **values)
        updated.validate()
        return updated

    def validate(self) -> None:
        if not self.master:
            raise ValueError("Spark master must not be empty")
        if not self.app_name:
            raise ValueError("Spark app name must not be empty")
        if self.shuffle_partitions < 1:
            raise ValueError("shuffle partitions must be positive")
        if self.max_parallelism < 1:
            raise ValueError("max parallelism must be positive")
        if not 1 <= self.page_size <= 1000:
            raise ValueError("page size must be in [1, 1000]")
        if self.max_pages < 1:
            raise ValueError("max pages must be positive")
        if self.limit < 1:
            raise ValueError("limit must be positive")
        if not self.results_bucket:
            raise ValueError("results bucket must not be empty")
        if self.committer not in {"logging", "worker-fenced"}:
            raise ValueError(
                "unknown result committer; expected 'logging' or 'worker-fenced'"
            )
        if self.connection.max_attempts < 1:
            raise ValueError("S3 retry count must be positive")
