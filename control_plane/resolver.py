"""
Policy Resolver for ControlPlane.ai
Handles 2-tier policy inheritance, strict field-level locking, and structural validation.
"""

from typing import Dict, Any, List
import yaml

from control_plane.models import PolicyConfig, PolicyLockingError

# There is deliberately no latency budget and no per-tier latency estimates here.
# The estimates that used to live at this point (T0=5, T1=40, T2=600) were unvalidated
# hardcoded guesses - measured T0 is 0.13ms - and the budget they fed created a
# safety-theatre configuration: a tenant could declare the minimum budget alongside
# fail_open, so any detector overrun shipped unchecked output while still compiling to
# a clean, hash-attested bundle.
#
# Latency is now measured and reported per stage, never negotiated. `t2_enabled` is the
# only depth knob. See docs/NO_LATENCY_BUDGET.md

# --- Field strictness registry ---------------------------------------------
# Declarative, so adding a field means adding it to a set rather than growing an
# if/elif chain. Anything NOT registered here falls through to the generic branch
# and is treated as immutable when locked (fail closed).

# Risk ceilings: a detection ABOVE the value is a problem, so lowering catches more.
LOWER_IS_STRICTER = {
    "pii_threshold",
    "toxicity_threshold",
    "low_band",
    "high_band",
    "secret_entropy_ratio_threshold",
    "secret_min_length",
    "injection_threshold",
}

# Floors: demanding a HIGHER value is stricter.
#   grounding_threshold is a minimum acceptable SIMILARITY, so raising it demands
#   more grounding. It was previously grouped with the risk ceilings above, which
#   inverted its lock: tenants could loosen it and were blocked from tightening it.
#   See P1 in docs/POLICY_LOCKING_AND_RISK_NORMALIZATION.md
HIGHER_IS_STRICTER = {
    "grounding_threshold",
    # A larger penalty adds more risk for a disguised prompt -> stricter.
    "injection_evasion_penalty",
    # More contraction of the output bands after a flagged input -> stricter.
    "input_risk_tightening",
}

# Booleans, by which value is the safer one.
BOOL_TRUE_IS_STRICTER = {"t2_enabled"}
BOOL_FALSE_IS_STRICTER = {"allow_downrouting", "caching_enabled"}

# Enums ordered least -> most strict. Locking one should permit tightening,
# not freeze it (see P3).
ENUM_STRICTNESS = {
    "pii_mode": ["warn-and-confirm", "redact-and-proceed", "block-and-explain"],
    "fail_mode": ["fail_open", "fail_closed"],
    # 'flag' does not edit the prompt - it raises the session risk posture and feeds
    # fusion, so the output cascade runs stricter. There is no 'sanitize' rung: lexical
    # removal of an injection forwards the remainder of the attack. See docs/INPUT_GATE.md
    "injection_action": ["allow", "flag", "block"],
}


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
    """
    resolved: Dict[str, Any] = base_policy.model_dump(exclude_unset=True)
    child_dict: Dict[str, Any] = child_policy.model_dump(exclude_unset=True)

    # sorted(), not list(set()): Python randomizes string hashing per process, so
    # list(set(...)) yields a different order on every run. locked_fields is part of
    # the canonical hash payload, and json.dumps(sort_keys=True) sorts dict keys but
    # NOT list elements - so the unordered variant made policy_hash non-reproducible
    # across processes, breaking bundle-load-by-hash and ledger attestation.
    locked_fields: List[str] = sorted(set(
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

    # Structural validation of the resolved policy
    _validate_band_ordering(resolved)
    _validate_weight_integrity(resolved)
    _validate_critical_coherence(resolved)

    return resolved


def _validate_locked_field_strictness(field_name: str, base_val: Any, new_val: Any) -> None:
    """
    Strict Exception Validation: a locked field may only be overridden with a
    STRICTER value. Direction is resolved from the registry at module top.
    Unregistered locked fields are treated as immutable (fail closed).
    """
    if base_val == new_val:
        return

    if field_name in LOWER_IS_STRICTER:
        if new_val > base_val:
            raise PolicyLockingError(
                f"Field '{field_name}' is LOCKED by parent policy (base: {base_val}). "
                f"Attempted looser setting ({new_val}). A lower value is required for strictness."
            )

    elif field_name in HIGHER_IS_STRICTER:
        if new_val < base_val:
            raise PolicyLockingError(
                f"Field '{field_name}' is LOCKED by parent policy (base: {base_val}). "
                f"Attempted looser setting ({new_val}). A higher value is required for strictness."
            )

    elif field_name in ENUM_STRICTNESS:
        order = ENUM_STRICTNESS[field_name]
        if new_val not in order:
            raise PolicyLockingError(
                f"Field '{field_name}' is LOCKED; '{new_val}' is not a recognized value."
            )
        if order.index(new_val) < order.index(base_val):
            raise PolicyLockingError(
                f"Field '{field_name}' is LOCKED by parent policy ('{base_val}'). "
                f"Attempted looser setting ('{new_val}')."
            )

    elif field_name in BOOL_TRUE_IS_STRICTER:
        if base_val is True and new_val is False:
            raise PolicyLockingError(
                f"Field '{field_name}' is LOCKED to True by parent policy. "
                f"Inner policy cannot disable it."
            )

    elif field_name in BOOL_FALSE_IS_STRICTER:
        if base_val is False and new_val is True:
            raise PolicyLockingError(
                f"Field '{field_name}' is LOCKED to False by parent policy. "
                f"Inner policy cannot enable it."
            )

    # Unregistered locked field: immutable. Fail closed rather than guess a direction.
    else:
        raise PolicyLockingError(
            f"Field '{field_name}' is LOCKED by parent policy ({base_val}) and has no "
            f"registered strictness direction. Cannot override with '{new_val}'."
        )


def _apply_mandatory_defaults(policy_dict: Dict[str, Any]) -> None:
    """Ensure all required bundle fields have sensible fallbacks if unspecified."""
    defaults = {
        "policy_name": "resolved-policy",
        "policy_version": "v1.0.0",
        "description": "Resolved policy bundle",
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
        "detector_critical_thresholds": {"pii": 0.90, "grounding": 0.90, "toxicity": 0.95},
        "t0_severity_scores": {"hard": 1.0, "high": 0.75, "medium": 0.40, "low": 0.15},
        "t0_aggregation": "max",
        "blocklist_terms": [],
        "secret_min_length": 24,
        "secret_entropy_ratio_threshold": 0.9,
        "ner_label_confidence": {"PERSON": 0.85, "GPE": 0.75, "ORG": 0.70},
        "pii_aggregation": "max",
        "injection_threshold": 0.7,
        "injection_action": "flag",
        "injection_evasion_penalty": 0.15,
        "input_risk_tightening": 0.5,
        "locked_fields": [],
    }
    for k, v in defaults.items():
        if k not in policy_dict or policy_dict[k] is None:
            policy_dict[k] = v


def _validate_band_ordering(policy_dict: Dict[str, Any]) -> None:
    """low_band must not exceed high_band, else the risk bands are inverted."""
    low, high = policy_dict["low_band"], policy_dict["high_band"]
    if low > high:
        raise ValueError(
            f"low_band ({low}) exceeds high_band ({high}); risk bands would be inverted."
        )


def _validate_weight_integrity(policy_dict: Dict[str, Any]) -> None:
    """Fusion weights must sum to 1.0, else fused risk can exceed the band scale."""
    weights = policy_dict.get("detector_weights", {})
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(
            f"detector_weights must sum to 1.0 (got {total}). "
            f"Fused risk would fall outside the [0,1] band scale."
        )


def _validate_critical_coherence(policy_dict: Dict[str, Any]) -> None:
    """
    Critical thresholds live on the NORMALIZED S scale, where 0.5 is the detection
    threshold for every detector. A value below 0.5 would fire before normal
    detection does - incoherent for a field meaning 'more severe than normal'.
    """
    for detector, critical in policy_dict.get("detector_critical_thresholds", {}).items():
        if not (0.5 <= critical <= 1.0):
            raise ValueError(
                f"detector_critical_thresholds['{detector}'] = {critical} is out of range. "
                f"Must be within [0.5, 1.0] on the normalized S scale "
                f"(0.5 == the detection threshold)."
            )

