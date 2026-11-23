"""Capability-specific SQS workers with fenced leases and idempotent commits.

Importing this package has no side effects: no clients are created, no threads
are started and no services are contacted until a :class:`Worker` is built.
"""

from __future__ import annotations

from .config import WorkerSettings
from .errors import (
    ArtifactError,
    ChecksumError,
    GpuUnavailableError,
    LeaseError,
    LeaseLostError,
    MessageError,
    PermanentError,
    StateTransitionError,
    StaleTokenError,
    TransientError,
    WorkerError,
)
from .state import TERMINAL_STATES, assert_transition, can_transition
from .store import WorkerStore
from .worker import Worker

__all__ = [
    "ArtifactError",
    "ChecksumError",
    "GpuUnavailableError",
    "LeaseError",
    "LeaseLostError",
    "MessageError",
    "PermanentError",
    "StateTransitionError",
    "StaleTokenError",
    "TERMINAL_STATES",
    "TransientError",
    "Worker",
    "WorkerError",
    "WorkerSettings",
    "WorkerStore",
    "assert_transition",
    "can_transition",
]
