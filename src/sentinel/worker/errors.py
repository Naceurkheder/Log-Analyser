"""Typed worker failures used for retry/DLQ classification."""

from __future__ import annotations

from typing import Any


class WorkerError(Exception):
    """Base class carrying a stable machine-readable error code."""

    code = "worker_error"
    transient = False

    def __init__(
        self, message: str, *, code: str | None = None, details: Any | None = None
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "transient": self.transient,
            "details": self.details,
        }


class TransientError(WorkerError):
    """S3/DDB/network failure: bounded retry with backoff."""

    code = "transient_failure"
    transient = True


class PermanentError(WorkerError):
    """Bad input or configuration: quarantine instead of retrying forever."""

    code = "permanent_failure"


class MessageError(PermanentError):
    """Malformed, unsupported or capability-mismatched queue message."""

    code = "invalid_message"


class DetectorConfigError(PermanentError):
    """Detector configuration does not match the pinned job configuration."""

    code = "invalid_detector_config"


class ChecksumError(PermanentError):
    """Chunk, manifest or result object digest/size mismatch."""

    code = "checksum_mismatch"


class ArtifactError(PermanentError):
    """Artifact is missing, has the wrong digest or an unsupported ABI."""

    code = "artifact_unavailable"


class GpuUnavailableError(ArtifactError):
    """GPU execution cannot be performed; never fall back to the CPU oracle."""

    code = "gpu_unavailable"


class StateTransitionError(PermanentError):
    """Illegal job state transition (a programming or corruption error)."""

    code = "illegal_state_transition"


class LeaseError(TransientError):
    """Lease ownership could not be established or renewed."""

    code = "lease_error"


class ClaimError(LeaseError):
    """Conditional claim lost to another worker or a terminal state."""

    code = "claim_conflict"


class StaleTokenError(LeaseError):
    """A conditional write was attempted with an obsolete fencing token."""

    code = "stale_fencing_token"


class LeaseLostError(LeaseError):
    """The active lease was lost; the attempt must not commit."""

    code = "lease_lost"


class JobNotFoundError(PermanentError):
    """The queue message refers to a job record that does not exist."""

    code = "job_not_found"


class PoisonMessageError(PermanentError):
    """The message was replayed too often or exhausted its receive budget."""

    code = "poison_message"
