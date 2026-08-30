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

    # Sensitivity Thresholds
    # NOTE: pii_threshold and toxicity_threshold are RISK CEILINGS -> lower is stricter.
    #       grounding_threshold is a SIMILARITY FLOOR -> HIGHER is stricter. See P1 in
    #       docs/POLICY_LOCKING_AND_RISK_NORMALIZATION.md
    pii_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    grounding_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    toxicity_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # Risk Fusion Bands
    low_band: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    high_band: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    cache_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # Detector Weights for fusion engine
    detector_weights: Optional[Dict[str, float]] = None

    # Per-detector critical floors, on the NORMALIZED S scale where 0.5 == the
    # detection threshold. A detector at/above its critical value floors the fused
    # risk at high_band regardless of weights. Without this, no single T1 detector
    # can reach any action at all (see P4).
    detector_critical_thresholds: Optional[Dict[str, float]] = None

    # Tier 0 emits categorical severities, not threshold-normalized scores, so it is
    # scored by lookup rather than by the piecewise normalizer (see T0-2).
    t0_severity_scores: Optional[Dict[str, float]] = None
    t0_aggregation: Optional[Literal["max", "noisy_or"]] = None

    # Tier 0 secret detection (see t0_deterministic_checks.md)
    blocklist_terms: Optional[List[str]] = None
    secret_min_length: Optional[int] = Field(default=None, ge=1)
    secret_entropy_ratio_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    # Tier 1 detector configuration
    ner_label_confidence: Optional[Dict[str, float]] = None
    pii_aggregation: Optional[Literal["max", "noisy_or", "density"]] = None

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

    # Per-detector critical floors (normalized S scale; 0.5 == detection threshold)
    detector_critical_thresholds: Dict[str, float] = Field(
        default_factory=lambda: {"pii": 0.90, "grounding": 0.90, "toxicity": 0.95}
    )

    # Tier 0 categorical scoring
    t0_severity_scores: Dict[str, float] = Field(
        default_factory=lambda: {"hard": 1.0, "high": 0.75, "medium": 0.40, "low": 0.15}
    )
    t0_aggregation: Literal["max", "noisy_or"] = "max"

    # Tier 0 secret detection
    blocklist_terms: List[str] = Field(default_factory=list)
    secret_min_length: int = 24
    secret_entropy_ratio_threshold: float = 0.9

    # Tier 1 detector configuration
    ner_label_confidence: Dict[str, float] = Field(
        default_factory=lambda: {"PERSON": 0.85, "GPE": 0.75, "ORG": 0.70}
    )
    pii_aggregation: Literal["max", "noisy_or", "density"] = "max"

    locked_fields: List[str] = Field(default_factory=list)
