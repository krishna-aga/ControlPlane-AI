"""
Data models and custom exception definitions for ControlPlane.ai
Built using standard library dataclasses for zero-dependency portability.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Literal, Any


class PolicyLockingError(Exception):
    """Raised when an inner policy layer attempts to loosen a locked field."""
    pass


class LatencyBudgetExceededError(Exception):
    """Raised when latency budget is insufficient for active safety checks."""
    pass


@dataclass
class PolicyConfig:
    """Schema representing a raw or partial policy YAML file."""

    policy_name: Optional[str] = None
    policy_version: Optional[str] = "v1.0.0"
    description: Optional[str] = ""

    # Latency & Execution Controls
    latency_budget_ms: Optional[int] = None
    t2_enabled: Optional[bool] = None
    pii_mode: Optional[str] = None  # "redact-and-proceed", "block-and-explain", "warn-and-confirm"
    fail_mode: Optional[str] = None  # "fail_open", "fail_closed"
    allow_downrouting: Optional[bool] = None
    caching_enabled: Optional[bool] = True

    # Sensitivity Thresholds (Lower numerical value = stricter detection)
    pii_threshold: Optional[float] = None
    grounding_threshold: Optional[float] = None
    toxicity_threshold: Optional[float] = None

    # Risk Fusion Bands
    low_band: Optional[float] = None
    high_band: Optional[float] = None
    cache_threshold: Optional[float] = None

    # Detector Weights for fusion engine
    detector_weights: Optional[Dict[str, float]] = None

    # Locked fields list
    locked_fields: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert dataclass instance to dictionary excluding None values."""
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class BundleConfig:
    """Schema representing a compiled, resolved, immutable JSON policy bundle."""

    policy_name: str
    policy_version: str
    policy_hash: str
    compiled_at: str
    description: str = ""

    # Operational settings
    latency_budget_ms: int = 200
    t2_enabled: bool = False
    pii_mode: str = "redact-and-proceed"
    fail_mode: str = "fail_open"
    allow_downrouting: bool = True
    caching_enabled: bool = True

    # Active thresholds
    pii_threshold: float = 0.8
    grounding_threshold: float = 0.6
    toxicity_threshold: float = 0.7

    # Risk Fusion Bands
    low_band: float = 0.3
    high_band: float = 0.7
    cache_threshold: float = 0.90

    # Fusion weights & locked fields record
    detector_weights: Dict[str, float] = field(default_factory=lambda: {"t0": 0.4, "pii": 0.2, "grounding": 0.2, "toxicity": 0.2})
    locked_fields: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)
