"""
Data Plane result models for ControlPlane.ai

Central design rule, enforced by the types here: the canonical (normalized) view of a
prompt is EVIDENCE, never PAYLOAD. `CanonicalView.text` is what detectors scan;
`InputGateResult.forward_prompt` is what goes upstream. They are different strings and
nothing copies one into the other.

The earlier spec conflated them, which meant the canonicalizer - which appends decoded
base64 inline - spliced decoded attack text into the prompt it then forwarded to the
model. See docs/INPUT_GATE.md
"""

from typing import Dict, List, Literal, Optional, Tuple
from pydantic import BaseModel, Field


class Evasion(BaseModel):
    """One obfuscation the canonicalizer had to undo to expose the underlying text."""

    kind: Literal["invisible_char", "homoglyph", "base64", "whitespace_padding"]
    detail: str                      # human-readable, safe to log
    count: int = 1
    offsets: List[int] = Field(default_factory=list)   # offsets into the RAW prompt


class CanonicalView(BaseModel):
    """
    Scan-only canonicalization of a prompt. NEVER forwarded upstream: NFKC folding,
    whitespace collapse and inline base64 expansion all corrupt user intent, and the
    base64 expansion in particular would hand the model a decoded payload it was never
    sent.
    """

    text: str
    evasions: List[Evasion] = Field(default_factory=list)

    @property
    def had_evasion(self) -> bool:
        return bool(self.evasions)


class InjectionFinding(BaseModel):
    """A prompt-injection pattern match, located in the CANONICAL view."""

    pattern_name: str
    severity: float                  # 0.0-1.0, per-pattern
    matched_text: str                # from the canonical view, for the evidence trail
    span: Tuple[int, int]            # offsets into CanonicalView.text, NOT the raw prompt


class PIIFinding(BaseModel):
    """
    A PII detection, located in the RAW prompt.

    Spans index the raw prompt deliberately. Detecting on the canonical view would put
    every span behind an NFKC fold and a whitespace collapse, so the placeholder map
    would no longer index the string actually being forwarded - the input-side twin of
    the T0-5 span-drift defect.
    """

    entity_type: str
    span: Tuple[int, int]            # offsets into the RAW prompt
    confidence: float
    placeholder: Optional[str] = None
    checksum_validated: bool = False


class InputGateResult(BaseModel):
    """
    Outcome of the Input Gate.

    `forward_prompt` is the raw prompt with PII placeholders substituted and nothing
    else changed. Under `injection_action: flag` it is byte-identical to the raw prompt
    apart from those substitutions - a flagged injection is NOT edited out, because
    deleting a matched span forwards the remainder of the attack.
    """

    action: Literal["ALLOW", "FLAG", "BLOCK"]
    blocked_reason: Optional[str] = None

    forward_prompt: str
    injection_risk: float = 0.0
    injection_findings: List[InjectionFinding] = Field(default_factory=list)
    evasions: List[Evasion] = Field(default_factory=list)
    canonical_text: str = ""         # evidence trail only; never sent upstream

    pii_findings: List[PIIFinding] = Field(default_factory=list)
    pii_mode: str = "redact-and-proceed"

    policy_hash: str = ""
    latency_ms: float = 0.0

    def ledger_row(self) -> Dict:
        """
        Privacy-safe projection for the audit ledger: entity types, spans, scores and
        pattern names only. Raw PII values, the placeholder map, and the raw prompt are
        all excluded by construction rather than by remembering to strip them.
        """
        return {
            "action": self.action,
            "injection_risk": round(self.injection_risk, 4),
            "injection_patterns": [f.pattern_name for f in self.injection_findings],
            "evasions": [{"kind": e.kind, "count": e.count} for e in self.evasions],
            "pii_findings": [
                {
                    "entity_type": f.entity_type,
                    "character_span": list(f.span),
                    "confidence_score": f.confidence,
                    "checksum_validated": f.checksum_validated,
                }
                for f in self.pii_findings
            ],
            "policy_hash": self.policy_hash,
            "latency_ms": round(self.latency_ms, 3),
        }


class DetectorSignals(BaseModel):
    """
    Raw detector outputs, before normalization.

    Raw values are carried explicitly because the ledger must persist them (N1a). A
    calibration sweep asks "what would have happened at toxicity_threshold = 0.6?" - if
    only normalized scores are stored, that counterfactual cannot be reconstructed
    without re-running every detector over historical traffic.

    Any field left None means the detector was NOT APPLICABLE for this request (no RAG
    context, detector disabled). Fusion renormalizes the remaining weights rather than
    scoring an absent detector as zero, which would silently dilute every other signal.
    """

    t0_severities: List[str] = Field(default_factory=list)   # "hard" | "high" | "medium" | "low"
    pii_confidence: Optional[float] = None                   # max entity confidence
    grounding_similarity: Optional[float] = None             # cosine; HIGHER is safer
    toxicity_probability: Optional[float] = None             # P(toxic); higher is worse

    injection_risk: float = 0.0                              # from the Input Gate
    input_flagged: bool = False


class FusionResult(BaseModel):
    """Fused risk, the action it maps to, and the arithmetic that produced both."""

    action: Literal["ALLOW", "REDACT", "REGENERATE", "FLAG", "BLOCK"]
    fused_risk: float
    reason: str

    normalized: Dict[str, float] = Field(default_factory=dict)   # S per detector
    raw: Dict[str, Optional[float]] = Field(default_factory=dict)
    weights_applied: Dict[str, float] = Field(default_factory=dict)
    contributions: Dict[str, float] = Field(default_factory=dict)

    critical_fired: List[str] = Field(default_factory=list)
    dominant_detector: Optional[str] = None

    effective_low_band: float = 0.0
    effective_high_band: float = 0.0
    bands_tightened: bool = False
    t2_recommended: bool = False

    def ledger_row(self) -> Dict:
        """Privacy-safe projection. Scores only - no output text, no entity values."""
        return {
            "action": self.action,
            "fused_risk": round(self.fused_risk, 4),
            "raw_scores": {k: v for k, v in self.raw.items() if v is not None},
            "normalized_scores": {k: round(v, 4) for k, v in self.normalized.items()},
            "weights_applied": {k: round(v, 4) for k, v in self.weights_applied.items()},
            "critical_fired": self.critical_fired,
            "dominant_detector": self.dominant_detector,
            "effective_bands": [
                round(self.effective_low_band, 4),
                round(self.effective_high_band, 4),
            ],
            "bands_tightened": self.bands_tightened,
            "t2_recommended": self.t2_recommended,
        }
