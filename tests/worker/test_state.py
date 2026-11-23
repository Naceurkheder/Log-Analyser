"""Every permitted and forbidden job transition is defined in one place."""

from __future__ import annotations

import pytest

from sentinel.worker.errors import StateTransitionError
from sentinel.worker.state import (
    CLAIMABLE_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    assert_transition,
    can_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("PENDING", "QUEUED"),
        ("PENDING", "RUNNING"),
        ("QUEUED", "RUNNING"),
        ("RUNNING", "SUCCEEDED"),
        ("RUNNING", "RETRYABLE_FAILED"),
        ("RETRYABLE_FAILED", "QUEUED"),
        ("RETRYABLE_FAILED", "RUNNING"),
        ("RUNNING", "FAILED_PERMANENT"),
        ("RETRYABLE_FAILED", "FAILED_PERMANENT"),
        ("RUNNING", "DEAD_LETTERED"),
        ("RETRYABLE_FAILED", "DEAD_LETTERED"),
    ],
)
def test_legal_transitions(current: str, target: str) -> None:
    assert can_transition(current, target)
    assert_transition(current, target, job_id="job-test")


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("PENDING", "SUCCEEDED"),
        ("QUEUED", "SUCCEEDED"),
        ("PENDING", "RETRYABLE_FAILED"),
        ("SUCCEEDED", "QUEUED"),
        ("FAILED_PERMANENT", "RUNNING"),
        ("DEAD_LETTERED", "QUEUED"),
        ("RUNNING", "QUEUED"),
        ("RETRYABLE_FAILED", "SUCCEEDED"),
        ("UNKNOWN", "RUNNING"),
    ],
)
def test_illegal_transitions_raise_typed_error(current: str, target: str) -> None:
    assert not can_transition(current, target)
    with pytest.raises(StateTransitionError) as exc:
        assert_transition(current, target, job_id="job-test")
    assert exc.value.code == "illegal_state_transition"
    assert exc.value.details["job_id"] == "job-test"


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()
    assert CLAIMABLE_STATES == {"PENDING", "QUEUED", "RETRYABLE_FAILED"}
