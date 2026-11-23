"""CPU reference detector — deterministic oracle for the CUDA port."""

from .features import Event, Feature, Match
from .native import NativeResult, detect_native, load_library
from .pipeline import Alert, DetectionResult, detect
from .semantics import ALGORITHM_VERSION, DetectorConfigError, DetectorSettings, Rule

__all__ = [
    "ALGORITHM_VERSION", "Alert", "DetectionResult", "DetectorConfigError",
    "DetectorSettings", "Event", "Feature", "Match", "NativeResult", "Rule",
    "detect", "detect_native", "load_library",
]
