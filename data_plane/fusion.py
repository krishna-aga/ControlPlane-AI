"""
Risk fusion and the graded action ladder.

Implements the Part B contract from docs/POLICY_LOCKING_AND_RISK_NORMALIZATION.md:
piecewise normalization (N1), the normalized-S critical scale (N2), and per-detector
critical floors (P4a). Every constant is read from the bundle.

Three properties worth stating up front, because each one exists to fix a specific
defect the register found:

  * `min(1, P/T)` saturated: P=0.70 and P=0.99 both scored 1.0 against T=0.70, so
    "barely over the line" and "appalling" were indistinguishable and the Learning
    Plane's calibration sweep was impossible. Replaced by a piecewise map where
    S = 0.5 is exactly the detection threshold for every detector (N1).

  * Fused risk is the MAXIMUM normalized score, not a weighted average. Averaging
    orthogonal risks is a category error - a clean toxicity score is not evidence that a
    blocklist hit is acceptable - and it made the result depend on how MANY detectors
    ran. Measured under the old scheme, a 99%-confidence toxic response scored 0.983
    when toxicity was the only applicable detector and 0.369 when three clean detectors
    ran alongside it: a swing of 0.614 on identical output, in which more safety
    checking made the response look safer. Taking the worst axis is both consistent and
    the right question - "how risky is this output" is the worst thing found, not the
    average across the things checked. This is P4's conclusion ("weights should not be
    safety-critical at all") carried to its end: `detector_weights` is removed.

  * T0 is categorical, not threshold-normalized. It has no threshold, so the piecewise
    map cannot apply to it; it is scored by severity lookup and must not be assumed to
    share T1's 0.5-midpoint semantics (T0-2).
"""

from typing import Dict, List, Optional, Tuple

from data_plane.models import DetectorSignals, FusionResult, severity_rank

# Detectors scored by the piecewise normalizer. T0 is deliberately absent: it is
# categorical and scored by lookup instead.
_T1_DETECTORS = ("pii", "grounding", "toxicity")


def normalize(raw: float, threshold: float) -> float:
    """
    Piecewise-linear map from a raw detector score onto the normalized S scale.

        S = 0.5 * (P / T)                     for P < T
        S = 0.5 + 0.5 * (P - T) / (1 - T)     for P >= T

    Continuous, monotonic, range [0,1], and **S = 0.5 is exactly the detection
    threshold for every detector** regardless of its raw scale. That single property is
    what makes critical thresholds comparable across detectors (N2) - without it a
    "critical" value of 0.80 fires at raw P = 0.56, i.e. *below* normal detection.
    """
    if threshold <= 0.0:
        return 1.0 if raw > 0.0 else 0.0
    if threshold >= 1.0:
        return 0.5 * raw
    if raw < threshold:
        return 0.5 * (raw / threshold)
    return 0.5 + 0.5 * (raw - threshold) / (1.0 - threshold)


def score_t0(severities: List[str], bundle: Dict) -> float:
    """
    Score Tier 0 findings by severity lookup.

    T0 has no threshold, so the piecewise map does not apply. This returns a
    CATEGORICAL severity score that does not carry T1's 0.5-midpoint meaning; fusion
    must not assume a shared interpretation (T0-2).
    """
    if not severities:
        return 0.0

    table = bundle.get("t0_severity_scores", {})
    scores = [float(table.get(s, 0.0)) for s in severities]

    if bundle.get("t0_aggregation", "max") == "noisy_or":
        product = 1.0
        for s in scores:
            product *= (1.0 - s)
        return 1.0 - product
    return max(scores)


def _normalize_signals(signals: DetectorSignals, bundle: Dict) -> Tuple[Dict[str, float], Dict[str, Optional[float]]]:
    """Map each applicable detector onto the S scale. Absent detectors are omitted."""
    normalized: Dict[str, float] = {}
    raw: Dict[str, Optional[float]] = {}

    if signals.t0_severities:
        normalized["t0"] = score_t0(signals.t0_severities, bundle)
        raw["t0"] = None                      # categorical; severities live in the T0 row

    if signals.pii_confidence is not None:
        raw["pii"] = signals.pii_confidence
        normalized["pii"] = normalize(signals.pii_confidence, float(bundle["pii_threshold"]))

    if signals.grounding_similarity is not None:
        # Grounding is risk-INVERTED: cosine similarity means higher = safer, unlike
        # P(toxic) and PII confidence. Normalize ungroundedness so the direction matches
        # every other detector on the S scale (N1).
        raw["grounding"] = signals.grounding_similarity
        ungrounded = 1.0 - signals.grounding_similarity
        normalized["grounding"] = normalize(
            ungrounded, 1.0 - float(bundle["grounding_threshold"])
        )

    if signals.toxicity_probability is not None:
        raw["toxicity"] = signals.toxicity_probability
        normalized["toxicity"] = normalize(
            signals.toxicity_probability, float(bundle["toxicity_threshold"])
        )

    return normalized, raw


def _dominant(normalized: Dict[str, float]) -> Optional[str]:
    """The detector carrying the highest normalized score - it selects the REMEDY."""
    return max(normalized, key=lambda k: normalized[k]) if normalized else None


def fuse(signals: DetectorSignals, bundle: Dict) -> FusionResult:
    """
    Fuse detector signals into a graded action.

    Order: normalize -> weight -> apply critical floors -> tighten bands on flagged
    input -> map to the ladder.
    """
    normalized, raw = _normalize_signals(signals, bundle)

    # The worst axis IS the risk. A detector that ran and found nothing is evidence
    # about a DIFFERENT axis, so it must not pull the score down; a detector that did
    # not run at all is simply absent from the max. Both cases fall out for free, which
    # is why this needs no applicability bookkeeping.
    fused = max(normalized.values()) if normalized else 0.0

    low_band = float(bundle["low_band"])
    high_band = float(bundle["high_band"])

    # --- input risk tightens the output cascade -------------------------------------
    # This is what `injection_action: flag` actually does. The prompt was forwarded
    # unmodified; the consequence lands here, as a contraction of the bands, so the same
    # model output resolves to a more severe action than it would have otherwise.
    tightened = False
    if signals.input_flagged and signals.injection_risk > 0.0:
        factor = 1.0 - (signals.injection_risk * float(bundle["input_risk_tightening"]))
        factor = max(factor, 0.0)
        low_band *= factor
        high_band *= factor
        tightened = True

    # --- critical floors -------------------------------------------------------------
    # A detector at or above its critical value floors fused risk at high_band. Under
    # max-aggregation a strong signal already survives on its own, so floors now serve a
    # narrower purpose: they escalate a detector that is severe on its OWN scale to the
    # severe action even when its normalized score sits below high_band.
    critical = bundle.get("detector_critical_thresholds", {})
    fired: List[str] = []
    for detector in _T1_DETECTORS:
        if detector in normalized and detector in critical:
            if normalized[detector] >= float(critical[detector]):
                fired.append(detector)

    # T0's floor is CATEGORICAL, compared by severity RANK rather than against the
    # normalized S scale - T0 has no threshold and so carries none of the 0.5-midpoint
    # semantics that govern T1 (T0-2). It survives the move to max-aggregation because a
    # `high` severity scores 0.75, which is below high_band; the floor is what carries a
    # leaked credential the rest of the way to BLOCK.
    floor_severity = bundle.get("t0_floor_severity", "high")
    if signals.t0_severities and floor_severity:
        threshold_rank = severity_rank(floor_severity)
        if threshold_rank >= 0 and any(
            severity_rank(s) >= threshold_rank for s in signals.t0_severities
        ):
            fired.append("t0")

    if fired:
        fused = max(fused, high_band)

    fused = min(max(fused, 0.0), 1.0)

    dominant = _dominant(normalized)
    action, reason = _decide_action(
        fused, low_band, high_band, normalized, dominant, fired, signals, bundle
    )

    # T2 is worth its ~600ms only inside the graded middle, where the answer is
    # genuinely uncertain. Comparisons are inclusive on both ends.
    t2_recommended = bool(bundle.get("t2_enabled")) and low_band <= fused <= high_band

    return FusionResult(
        action=action,
        fused_risk=fused,
        reason=reason,
        normalized=normalized,
        raw=raw,
        critical_fired=fired,
        dominant_detector=dominant,
        effective_low_band=low_band,
        effective_high_band=high_band,
        bands_tightened=tightened,
        t2_recommended=t2_recommended,
    )


def _decide_action(
    fused: float,
    low_band: float,
    high_band: float,
    normalized: Dict[str, float],
    dominant: Optional[str],
    critical_fired: List[str],
    signals: DetectorSignals,
    bundle: Dict,
) -> Tuple[str, str]:
    """
    Map fused risk to the graded ladder.

    A scalar alone cannot choose between REDACT, REGENERATE and FLAG - they are
    qualitatively different remedies, not degrees of one. So the band selects the
    SEVERITY and the dominant detector selects the REMEDY:

        >= high_band                     -> BLOCK
        [low_band, high_band)            -> REDACT     if the risk is maskable (T0/PII:
                                                          findings carry spans)
                                            REGENERATE if it is grounding (masking cannot
                                                          fix an ungrounded answer)
                                            FLAG       otherwise (toxicity: neither
                                                          maskable nor reliably fixed by
                                                          a retry)
        < low_band                       -> ALLOW

    The band comparison is `>=`, not `>`, so a critical floor landing exactly on
    high_band triggers the severe action rather than falling through (P4a).
    """
    if fused >= high_band:
        if critical_fired:
            return "BLOCK", (
                f"Critical floor fired on {', '.join(critical_fired)} "
                f"(S >= critical); fused risk {fused:.3f} >= high_band {high_band:.3f}."
            )
        return "BLOCK", f"Fused risk {fused:.3f} >= high_band {high_band:.3f}."

    if fused >= low_band:
        if dominant in ("t0", "pii"):
            return "REDACT", (
                f"Fused risk {fused:.3f} in graded band "
                f"[{low_band:.3f}, {high_band:.3f}); dominant signal '{dominant}' "
                f"carries character spans, so the finding is maskable."
            )
        if dominant == "grounding":
            return "REGENERATE", (
                f"Fused risk {fused:.3f} in graded band "
                f"[{low_band:.3f}, {high_band:.3f}); dominant signal 'grounding' - "
                f"masking cannot ground an unsupported answer, so retry instead."
            )
        return "FLAG", (
            f"Fused risk {fused:.3f} in graded band "
            f"[{low_band:.3f}, {high_band:.3f}); dominant signal "
            f"'{dominant}' is neither maskable nor reliably fixed by regeneration."
        )

    if signals.input_flagged:
        return "ALLOW", (
            f"Fused risk {fused:.3f} < low_band {low_band:.3f} even with bands "
            f"tightened by the flagged input."
        )
    return "ALLOW", f"Fused risk {fused:.3f} < low_band {low_band:.3f}."
