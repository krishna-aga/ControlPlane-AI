"""
Data models and custom exception definitions for ControlPlane.ai
Built using Pydantic v2 for robust schema validation and type safety.
"""

from typing import Dict, List, Optional, Literal
from pydantic import BaseModel, Field


class PolicyLockingError(Exception):
    """Raised when an inner policy layer attempts to loosen a locked field."""
    pass


class LatencyBudgetExceededError(Exception):
    """Raised when latency budget is insufficient for active safety checks."""
    pass


class PolicyConfig(BaseModel):
    """Schema representing a raw or partial policy YAML file."""

    policy_name: Optional[str] = None
    policy_version: Optional[str] = "v1.0.0"
    description: Optional[str] = ""

    # Latency & Execution Controls
    latency_budget_ms: Optional[int] = Field(default=None, ge=10, le=5000)
    t2_enabled: Optional[bool] = None
    pii_mode: Optional[Literal["redact-and-proceed", "block-and-explain", "warn-and-confirm"]] = None
    fail_mode: Optional[Literal["fail_open", "fail_closed"]] = None
    allow_downrouting: Optional[bool] = None
    caching_enabled: Optional[bool] = True

    # Sensitivity Thresholds (Lower numerical value = stricter detection)
    pii_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    grounding_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    toxicity_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # Risk Fusion Bands
    low_band: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    high_band: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    cache_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # Detector Weights for fusion engine
    detector_weights: Optional[Dict[str, float]] = None

    # Locked fields list
    locked_fields: List[str] = Field(default_factory=list)


class BundleConfig(BaseModel):
    """Schema representing a compiled, resolved, immutable JSON policy bundle."""

    policy_name: str
    policy_version: str
    policy_hash: str
    compiled_at: str
    description: str = ""

    # Operational settings
    latency_budget_ms: int = 200
    t2_enabled: bool = False
    pii_mode: Literal["redact-and-proceed", "block-and-explain", "warn-and-confirm"] = "redact-and-proceed"
    fail_mode: Literal["fail_open", "fail_closed"] = "fail_open"
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
    detector_weights: Dict[str, float] = Field(default_factory=lambda: {"t0": 0.4, "pii": 0.2, "grounding": 0.2, "toxicity": 0.2})
    locked_fields: List[str] = Field(default_factory=list)
