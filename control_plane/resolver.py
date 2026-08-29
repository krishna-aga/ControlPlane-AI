"""
Policy Resolver for ControlPlane.ai
Handles 2-tier policy inheritance, strict field-level locking, and latency budget verification.
"""

from typing import Dict, Any, List
import yaml

from control_plane.models import (
    PolicyConfig,
    PolicyLockingError,
    LatencyBudgetExceededError,
)

# Minimum latency estimates per safety tier (ms)
T0_ESTIMATED_MS = 5
T1_ESTIMATED_MS = 40
T2_ESTIMATED_MS = 600


def load_yaml_policy(file_path: str) -> PolicyConfig:
    """Load and parse a YAML policy file into a PolicyConfig model."""
    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return PolicyConfig(**data)


def resolve_policy(base_policy: PolicyConfig, child_policy: PolicyConfig) -> Dict[str, Any]:
    """
    Resolves child_policy against base_policy.
    Enforces strict field-level locking:
    - If a field is in base_policy.locked_fields, child can only make it stricter.
    - If child attempts to loosen a locked field, raises PolicyLockingError.
    - Validates cumulative detector latency against latency_budget_ms.
    """
    resolved: Dict[str, Any] = base_policy.model_dump(exclude_unset=True)
    child_dict: Dict[str, Any] = child_policy.model_dump(exclude_unset=True)

    locked_fields: List[str] = list(set(
        resolved.get("locked_fields", []) + child_dict.get("locked_fields", [])
    ))

    # Apply non-locked and locked overrides with strict validation
    for key, val in child_dict.items():
        if val is None:
            continue

        if key in locked_fields and key in resolved:
            base_val = resolved[key]
            _validate_locked_field_strictness(key, base_val, val)
        
        # Override field value
        resolved[key] = val

    resolved["locked_fields"] = locked_fields

    # Validate mandatory defaults
    _apply_mandatory_defaults(resolved)

    # Validate latency budget
    _validate_latency_budget(resolved)

    return resolved


def _validate_locked_field_strictness(field_name: str, base_val: Any, new_val: Any) -> None:
    """
    Strict Exception Validation:
    Thresholds (pii_threshold, grounding_threshold, toxicity_threshold):
      - Lower numerical value is STRICTER (catches more).
      - If new_val > base_val -> LOOSER -> raise PolicyLockingError!
    Latency Budget:
      - Higher numerical value is STRICTER (allocates more time for checks).
      - If new_val < base_val -> LOOSER -> raise PolicyLockingError!
    Booleans/Enums (allow_downrouting, fail_mode):
      - Cannot change locked value to a looser configuration.
    """
    if base_val == new_val:
        return

    # Numerical sensitivity thresholds (lower = stricter)
    if field_name in ["pii_threshold", "grounding_threshold", "toxicity_threshold"]:
        if new_val > base_val:
            raise PolicyLockingError(
                f"Field '{field_name}' is LOCKED by parent policy (base: {base_val}). "
                f"Attempted looser setting ({new_val}). Lower threshold is required for strictness."
            )
    
    # Latency budget (higher = stricter, allows more time)
    elif field_name == "latency_budget_ms":
        if new_val < base_val:
            raise PolicyLockingError(
                f"Field '{field_name}' is LOCKED by parent policy (base: {base_val}). "
                f"Attempted looser budget ({new_val} ms). Higher budget is required."
            )

    # Boolean down-routing permission (false = stricter)
    elif field_name == "allow_downrouting":
        if base_val is False and new_val is True:
            raise PolicyLockingError(
                f"Field '{field_name}' is LOCKED to False by parent policy. "
                f"Inner policy cannot enable downrouting."
            )

    # Generic fallback for locked fields: Any divergence to a looser setting is disallowed
    elif new_val != base_val:
        raise PolicyLockingError(
            f"Field '{field_name}' is LOCKED by parent policy ({base_val}). "
            f"Cannot override with '{new_val}'."
        )


def _apply_mandatory_defaults(policy_dict: Dict[str, Any]) -> None:
    """Ensure all required bundle fields have sensible fallbacks if unspecified."""
    defaults = {
        "policy_name": "resolved-policy",
        "policy_version": "v1.0.0",
        "description": "Resolved policy bundle",
        "latency_budget_ms": 200,
        "t2_enabled": False,
        "pii_mode": "redact-and-proceed",
        "fail_mode": "fail_open",
        "allow_downrouting": True,
        "caching_enabled": True,
        "pii_threshold": 0.8,
        "grounding_threshold": 0.6,
        "toxicity_threshold": 0.7,
        "low_band": 0.3,
        "high_band": 0.7,
        "cache_threshold": 0.90,
        "detector_weights": {"t0": 0.4, "pii": 0.2, "grounding": 0.2, "toxicity": 0.2},
        "locked_fields": [],
    }
    for k, v in defaults.items():
        if k not in policy_dict or policy_dict[k] is None:
            policy_dict[k] = v


def _validate_latency_budget(policy_dict: Dict[str, Any]) -> None:
    """Check if latency budget is sufficient to run configured safety tiers."""
    budget = policy_dict.get("latency_budget_ms", 200)
    t2_enabled = policy_dict.get("t2_enabled", False)

    required_latency = T0_ESTIMATED_MS + T1_ESTIMATED_MS
    if t2_enabled:
        required_latency += T2_ESTIMATED_MS

    if budget < required_latency:
        raise LatencyBudgetExceededError(
            f"Latency budget of {budget}ms is insufficient for requested configuration. "
            f"Required estimated latency is {required_latency}ms "
            f"(T0: {T0_ESTIMATED_MS}ms, T1: {T1_ESTIMATED_MS}ms, T2: {T2_ESTIMATED_MS if t2_enabled else 0}ms)."
        )
