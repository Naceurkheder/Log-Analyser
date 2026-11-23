"""Single source of truth for job state transitions.

``PENDING -> QUEUED -> RUNNING -> SUCCEEDED``

``RUNNING -> RETRYABLE_FAILED -> QUEUED`` (bounded retry)

``RUNNING / RETRYABLE_FAILED -> FAILED_PERMANENT / DEAD_LETTERED``

Every write that changes job state goes through :func:`assert_transition`, so
an illegal transition fails before any conditional DynamoDB expression is
issued. DynamoDB conditions remain the authoritative concurrency guard.
"""

from __future__ import annotations

from .errors import StateTransitionError

PENDING = "PENDING"
QUEUED = "QUEUED"
RUNNING = "RUNNING"
RETRYABLE_FAILED = "RETRYABLE_FAILED"
SUCCEEDED = "SUCCEEDED"
FAILED_PERMANENT = "FAILED_PERMANENT"
DEAD_LETTERED = "DEAD_LETTERED"

TERMINAL_STATES = frozenset({SUCCEEDED, FAILED_PERMANENT, DEAD_LETTERED})
CLAIMABLE_STATES = frozenset({PENDING, QUEUED, RETRYABLE_FAILED})

TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({QUEUED, RUNNING, FAILED_PERMANENT, DEAD_LETTERED}),
    QUEUED: frozenset({RUNNING, FAILED_PERMANENT, DEAD_LETTERED}),
    RUNNING: frozenset(
        {SUCCEEDED, RETRYABLE_FAILED, FAILED_PERMANENT, DEAD_LETTERED}
    ),
    RETRYABLE_FAILED: frozenset(
        {QUEUED, RUNNING, FAILED_PERMANENT, DEAD_LETTERED}
    ),
    SUCCEEDED: frozenset(),
    FAILED_PERMANENT: frozenset(),
    DEAD_LETTERED: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def assert_transition(current: str, target: str, *, job_id: str = "") -> None:
    if not can_transition(current, target):
        raise StateTransitionError(
            f"illegal job transition {current!r} -> {target!r}",
            details={"job_id": job_id, "from": current, "to": target},
        )
