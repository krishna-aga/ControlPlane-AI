# Policy Locking & Risk Normalization — Defect Register and Fix Specification

**Status:** Specified, not implemented. No code changes have been made.
**Scope:** `control_plane/resolver.py`, `control_plane/models.py`, `policies/org_baseline.yaml`, and the Data Plane risk-normalization contract consumed by the fusion engine.

This document captures a design review of the policy locking model and the detector score normalization formula. Every claim below was verified against the actual repository code and policy files; verification output is reproduced in §4.

---

## 0. Defect Summary

| # | Defect | Severity | Status |
| :--- | :--- | :--- | :--- |
| **P1** | `grounding_threshold` locking direction is **inverted** | **Live bug in shipped code** | Open |
| **P2** | `toxicity_threshold`, `low_band`, `high_band` are not locked | Policy hole | Open |
| **P3** | Locked enums are **immutable** rather than tightenable | Latent bug | Open |
| **P4** | `detector_weights` unlocked, and not lockable by the current comparison | Policy hole | Open |
| **P5** | No validator for band ordering or weight-sum integrity | Missing validation | Open |
| **N1** | `min(1, P/T)` **saturates**, destroying severity ordering | Design defect | Open |
| **N2** | Critical thresholds have no coherent scale across detectors | Design defect | Open |
| **N3** | spaCy emits no confidence; NER sub-score collapses to binary | Design gap | Open |

P1–P5 are Control Plane. N1–N3 are the Data Plane normalization contract. They interact: N1's fix is what makes N2's fix possible, and P4's fix depends on N2.

---

# Part A — Control Plane Locking

## P1. `grounding_threshold` locking direction is inverted — **LIVE BUG**

### Symptom

`grounding_threshold` is grouped with `pii_threshold` and `toxicity_threshold` in the lower-is-stricter branch at [`resolver.py:82`](file:///home/krishna/Projects/ControlPlane/control_plane/resolver.py), and it is locked in `org_baseline.yaml`. Executed against the real resolver:

```text
RAISE to 0.9 (demands more grounding = STRICTER)  -> REJECTED (PolicyLockingError)
LOWER to 0.1 (demands almost none = LOOSER)       -> ACCEPTED
```

The lock is backwards today. A tenant can gut grounding enforcement, and is blocked from tightening it.

### Root cause

`pii_threshold` and `toxicity_threshold` are **risk ceilings** — a detection above the value is a problem, so *lower catches more* → lower is stricter.

`grounding_threshold` is a **similarity floor** — output must be at least this similar to source. Demanding *more* similarity is stricter. Confirmed from the scoring formula itself:

```text
S_grounding = 1 - min(1, C_sim / T_ground)
  T_ground=0.6, sim=0.50 -> S = 0.167
  T_ground=0.8, sim=0.50 -> S = 0.375     # higher T -> higher risk -> STRICTER
```

### Fix

Move `grounding_threshold` out of the lower-is-stricter set and into the higher-is-stricter set alongside `latency_budget_ms`.

> ⚠️ This changes enforcement behavior for any existing bundle. It is a correctness fix, not a preference — but it must be called out in release notes, because configurations previously accepted will now be rejected and vice-versa.

---

## P2. Missing locked fields

### Symptom

`locked_fields` in [`org_baseline.yaml`](file:///home/krishna/Projects/ControlPlane/policies/org_baseline.yaml) contains only `pii_threshold` and `grounding_threshold`. Therefore:

* `toxicity_threshold` → a tenant may set `1.0` and disable toxicity enforcement entirely.
* `high_band` → a tenant may set `0.99` so effectively nothing ever reaches `BLOCK`.
* `low_band` → a tenant may raise it so nothing ever leaves `ALLOW`.

### Fix

```yaml
locked_fields:
  - pii_threshold
  - grounding_threshold      # direction corrected per P1
  - toxicity_threshold       # NEW
  - low_band                 # NEW
  - high_band                # NEW
  - detector_critical_thresholds   # NEW, see P4
  - detector_weight_floors         # NEW, see P4
```

**Direction:** `toxicity_threshold`, `low_band`, and `high_band` are all lower-is-stricter (lowering `low_band` pushes more traffic out of `ALLOW`; lowering `high_band` pushes more into `BLOCK`), so they work with the existing comparison unchanged.

**Verified safe:** none of the three personas override any newly locked field, so this change breaks no existing configuration (§4C).

---

## P3. Locked enums are immutable, not tightenable

### Symptom

There is no enum branch in `_validate_locked_field_strictness()`. Locked enums fall through to the generic fallback at [`resolver.py:106`](file:///home/krishna/Projects/ControlPlane/control_plane/resolver.py), which rejects **any** divergence. A tenant tightening `pii_mode` from `redact-and-proceed` to `block-and-explain` receives a `PolicyLockingError` for being *more* strict.

Currently latent — neither enum is locked yet — but it activates the moment anyone locks one.

### Fix

Declare the ordering explicitly:

```python
ENUM_STRICTNESS = {
    "pii_mode":  ["warn-and-confirm", "redact-and-proceed", "block-and-explain"],
    "fail_mode": ["fail_open", "fail_closed"],
}
# tightening = moving right. Raise only when the index DECREASES.
```

---

## P4. `detector_weights` — unlocked, and unlockable by the current comparison

### Symptom

`detector_weights` is not locked. A tenant may ship:

```yaml
detector_weights: {t0: 1.0, pii: 0.0, grounding: 0.0, toxicity: 0.0}
```

zeroing out the entire T1 tier while still compiling cleanly.

### Why locking the map is the wrong instrument

Weights must sum to `1.0`, so *"every weight ≥ baseline"* is **arithmetically impossible** — raising one always lowers another. Locking the map exactly forbids all legitimate rebalancing (a non-RAG tenant genuinely should not carry grounding weight).

### The deeper problem: weights should not be safety-critical at all

Even a "protected" weight cannot make a T1 detector act alone. Measured against the current defaults:

```text
toxicity at P=0.99, no other detector firing:
  RAG (all 4 applicable)  -> R = 0.197 -> ALLOW
  non-RAG (renormalized)  -> R = 0.246 -> ALLOW
```

**A 99%-confidence toxic response is `ALLOW`ed under every persona.** Weight floors would be protecting a number that still cannot trigger action.

### Fix — two mechanisms

**(a) Per-detector critical floors.** If a detector's normalized score reaches its critical value, the fused risk is floored at `high_band` regardless of weights:

```python
if S[det] >= critical_S[det]:
    R_fused = max(R_fused, bundle.high_band)
```

Band comparison must be `R_fused >= high_band` (not `>`), so a floor landing exactly on the boundary triggers the severe action.

This makes safety **independent of weight arithmetic** — zeroing `toxicity`'s weight no longer disables toxicity.

**(b) Weight floors** to keep the graded middle honest:

```yaml
detector_critical_thresholds:   # NORMALIZED S scale — see N2
  pii: 0.90
  grounding: 0.90
  toxicity: 0.95

detector_weight_floors:
  pii: 0.15
  grounding: 0.15
  toxicity: 0.15
```

**(c) Map-aware locking**, since neither map works with scalar comparison:

```python
MAP_LOWER_IS_STRICTER  = {"detector_critical_thresholds"}   # lower fires sooner
MAP_HIGHER_IS_STRICTER = {"detector_weight_floors"}         # higher forces more weight

def _validate_locked_map(field_name, base_map, new_map):
    lower_stricter = field_name in MAP_LOWER_IS_STRICTER
    for key, base_v in base_map.items():
        if key not in new_map:
            raise PolicyLockingError(
                f"Field '{field_name}' is LOCKED; key '{key}' cannot be removed."
            )
        new_v = new_map[key]
        if (new_v > base_v) if lower_stricter else (new_v < base_v):
            raise PolicyLockingError(
                f"Field '{field_name}[{key}]' is LOCKED (base: {base_v}). "
                f"Attempted looser value ({new_v})."
            )
```

> **The removal guard is the load-bearing line.** Without it, a tenant evades every per-key check by simply omitting the `toxicity` key from the map.

---

## P5. Missing validators

Two configurations compile today that should not:

```python
def _validate_band_ordering(p):
    if p["low_band"] > p["high_band"]:
        raise ValueError(
            f"low_band ({p['low_band']}) exceeds high_band ({p['high_band']})."
        )

def _validate_weight_integrity(p):
    w = p["detector_weights"]
    floors = p.get("detector_weight_floors", {})
    total = sum(w.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"detector_weights must sum to 1.0 (got {total}).")
    for det, floor in floors.items():
        if w.get(det, 0.0) < floor:
            raise PolicyLockingError(
                f"detector_weights['{det}'] = {w.get(det, 0.0)} "
                f"is below locked floor {floor}."
            )

def _validate_critical_coherence(p):
    for det, crit in p.get("detector_critical_thresholds", {}).items():
        if not (0.5 <= crit <= 1.0):
            raise ValueError(
                f"detector_critical_thresholds['{det}'] = {crit} out of range; "
                f"must be in [0.5, 1.0] on the normalized S scale (0.5 == detection threshold)."
            )
```

* `low_band: 0.8, high_band: 0.3` currently compiles into inverted, meaningless bands.
* `detector_weights: {t0: 5.0, pii: 5.0}` compiles and produces fused scores above `1.0`, silently breaking every band comparison downstream.

---

## Structural change: replace the `if/elif` chain with a direction registry

`_validate_locked_field_strictness()` already has five branches and needs four more. Critically, **every field it does not know about silently falls into "immutable"** — which is precisely how P3 arose. Replace with a declarative registry:

```python
LOWER_IS_STRICTER = {
    "pii_threshold", "toxicity_threshold",
    "low_band", "high_band",
    "secret_entropy_ratio_threshold", "secret_min_length",
}
HIGHER_IS_STRICTER = {
    "latency_budget_ms",
    "grounding_threshold",          # P1 correction
}
BOOL_TRUE_IS_STRICTER  = {"t2_enabled", "nli_grounding_enabled"}
BOOL_FALSE_IS_STRICTER = {"allow_downrouting", "caching_enabled"}
MAP_LOWER_IS_STRICTER  = {"detector_critical_thresholds"}
MAP_HIGHER_IS_STRICTER = {"detector_weight_floors"}
ENUM_STRICTNESS = {
    "pii_mode":  ["warn-and-confirm", "redact-and-proceed", "block-and-explain"],
    "fail_mode": ["fail_open", "fail_closed"],
}
```

Keep the generic fallback as the final `else`, but make it **deliberate**: an unregistered locked field is immutable and fails closed. That is the correct default — it simply must not be where enums and maps land by accident.

---

# Part B — Risk Normalization

## N1. `min(1, P/T)` saturates

### Symptom

Above the threshold, all information is discarded. With `T = 0.7`:

```text
P = 0.70 -> S = 1.000
P = 0.99 -> S = 1.000
```

The system cannot distinguish "barely over the line" from "appallingly toxic."

### Why this costs more than reviewer-queue ranking

It makes the **Learning Plane's calibration sweep impossible.** A sweep asks *"what would have happened at `toxicity_threshold = 0.6`?"* — which requires the raw detector output. If the ledger stores only the capped value, every request above threshold is recorded as `1.0` and the counterfactual cannot be reconstructed without re-running every detector over historical traffic. See [`run_shadow_eval.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/run_shadow_eval.md).

### Fix (two parts, both required)

**(a) Persist raw scores in the ledger** alongside normalized ones. One float per detector per row, and the difference between a calibration sweep being a query and being a re-run.

**(b) Replace the cap with a piecewise-linear map:**

$$S = \begin{cases} 0.5 \cdot \dfrac{P}{T} & P < T \\[8pt] 0.5 + 0.5 \cdot \dfrac{P - T}{1 - T} & P \ge T \end{cases}$$

```python
def normalize(P: float, T: float) -> float:
    if T <= 0.0:  return 1.0 if P > 0 else 0.0     # degenerate guard
    if T >= 1.0:  return 0.5 * P                    # degenerate guard
    return 0.5 * (P / T) if P < T else 0.5 + 0.5 * (P - T) / (1.0 - T)
```

Continuous, monotonic, range `[0,1]`, and **`S = 0.5` is exactly the detection threshold for every detector** — the property N2 depends on.

### Grounding requires an explicit direction flip

Raw grounding is **risk-inverted**: cosine similarity means *higher = safer*, unlike `P(toxic)` and PII confidence. Define ungroundedness and normalize that:

```python
U = 1.0 - similarity
S_grounding = normalize(U, 1.0 - bundle.grounding_threshold)
```

Verified at `grounding_threshold = 0.6`:

```text
sim=0.95 -> S=0.0625      sim=0.60 -> S=0.5000 (at threshold)
sim=0.80 -> S=0.2500      sim=0.30 -> S=0.7500
                          sim=0.10 -> S=0.9167
```

### Known regression (accepted)

Piecewise halves a detector's contribution at the threshold point (`S = 1.0 → 0.5`). Measured impact: **no action changes**, because T1 detectors were already below `low_band` in both schemes (§4B). The regression is real and inconsequential; P4's critical floors are what actually restore T1's ability to act.

---

## N2. Critical thresholds have no coherent scale

### Symptom

Under `min(1, P/T)`, a critical threshold of `0.80` compared against `S` fires at raw `P = 0.80 × T`. Verified:

```text
[old scheme, critical_S = 0.80]
toxicity  threshold=0.7 -> raw P=0.560   *** fires BELOW the detection threshold ***
pii       threshold=0.8 -> raw P=0.640   *** fires BELOW the detection threshold ***
```

A field named "critical" — meaning *more* severe than normal — triggers *before* normal detection does. Semantics fully inverted.

### Rejected fix: define critical thresholds on the raw scale

This was the first proposal and **it is wrong.** Raw scales have no common direction:

| Detector | Raw signal | Direction |
| :--- | :--- | :--- |
| toxicity | `P(toxic)` | higher = worse |
| PII | confidence | higher = worse |
| grounding (cosine) | similarity | **higher = safer** |
| grounding (NLI) | `P(contradict)` | higher = worse |

A raw-scale `grounding: 0.90` would fire on the **best**-grounded outputs. Worse, the direction flips depending on `nli_grounding_enabled`. There is no single raw convention to anchor to.

### Accepted fix: critical thresholds on the normalized S scale

Piecewise normalization makes this coherent, because `S = 0.5` is the detection threshold for **every** detector regardless of raw direction. Verified:

```text
toxicity  threshold=0.7  critical_S=0.80 -> raw P=0.880   (above threshold ✓)
toxicity  threshold=0.7  critical_S=0.90 -> raw P=0.940   (above threshold ✓)
pii       threshold=0.8  critical_S=0.90 -> raw P=0.960   (above threshold ✓)
```

The validator becomes uniform and trivially checkable: `0.5 <= critical_S <= 1.0` (see `_validate_critical_coherence` in P5). Anything below `0.5` is, by construction, below the detection threshold — incoherent, and now rejected at compile time.

---

## N3. spaCy emits no confidence; NER sub-score collapses to binary

### Symptom

`S_PII = confidence / pii_threshold` has no numerator to read. spaCy's `doc.ents` entities carry no `.score` attribute — the model reports *"John Smith is a PERSON"* and stops.

### Fix (a): bundle-driven label confidence table

```yaml
ner_label_confidence:      # calibratable by the Learning Plane
  PERSON: 0.85
  GPE:    0.75
  ORG:    0.70
```

Satisfies the no-hardcoded-constants rule and becomes a calibration target once real traffic exists. The alternative — an HF token-classification pipeline with genuine per-entity scores — costs ~35 ms and consumes most of the T1 budget alone.

### Fix (b): the collapse problem, solved by N1

Under `min(1, c/T)` the table produces a near-binary sub-score. Verified:

```text
pii_threshold=0.8 (customer_support)
  old min(1,c/T): PERSON 1.000  GPE 0.938  ORG 0.875   spread=0.125
  new piecewise : PERSON 0.625  GPE 0.469  ORG 0.437   spread=0.188

pii_threshold=0.5 (decision_support)
  old min(1,c/T): PERSON 1.000  GPE 1.000  ORG 1.000   spread=0.000   <-- fully binary
  new piecewise : PERSON 0.850  GPE 0.750  ORG 0.700   spread=0.150
```

Under `decision_support` the old formula collapses **all three labels to exactly 1.0** — one incidental first name scores identically to a dumped customer list. Piecewise restores label discrimination at no extra cost.

### Fix (c): aggregation across entities remains open

Label discrimination is restored, but **volume insensitivity is not**: one `PERSON` still scores the same as fifty. Requires an explicit aggregation rule:

```yaml
pii_aggregation: density   # max | noisy_or | density
```

* `max` — current implicit behavior; volume-blind.
* `noisy_or` — $S = 1 - \prod_j (1 - c_j)$; saturates by the second entity at `c = 0.85`.
* `density` — entities per 100 tokens; the most honest gradation.

**Regardless of the choice, persist entity count and per-label breakdown in the ledger.** Those counts are what let the reviewer queue rank by severity and let the calibration sweep tune `ner_label_confidence` itself — the values above are estimates until traffic makes them measurements.

---

# 4. Verification Record

All figures below were produced by executing against the actual repository code and policy files.

### A. Piecewise normalization — mathematically sound

Across `T ∈ {0.5, 0.6, 0.7, 0.8, 0.95}`, sampling `P` at 1001 points:

* Continuity at `T`: discontinuity ≤ `1.05e-08` (float noise only)
* Monotonic: **true** for every `T`
* Range `[0,1]`: **true** for every `T`
* `S(0) = 0.000`, `S(T) = 0.500`, `S(1) = 1.000` for every `T`

Saturation eliminated at `T = 0.7`:

```text
P=0.70  old=1.000  new=0.500
P=0.80  old=1.000  new=0.667
P=0.90  old=1.000  new=0.833
P=0.99  old=1.000  new=0.983
```

### B. T1 powerlessness — confirmed, and total

```text
toxicity at P=0.99, no other detector firing:
  RAG (all 4)  old S=1.0   -> R=0.200 -> ALLOW
  RAG (all 4)  new S=0.983 -> R=0.197 -> ALLOW
  non-RAG      old S=1.0   -> R=0.250 -> ALLOW
  non-RAG      new S=0.983 -> R=0.246 -> ALLOW
```

No action differs between schemes. Critical floors (P4) are **mandatory**, not an optimization.

### C. Locking regression — nothing breaks

```text
customer_support.yaml   overrides-of-newly-locked: NONE -> safe
decision_support.yaml   overrides-of-newly-locked: NONE -> safe
internal_copilot.yaml   overrides-of-newly-locked: NONE -> safe
```

### D. New validators against current configs

```text
detector_weights sum = 1.0                    -> PASS
low_band 0.3 <= high_band 0.7                 -> PASS
weights vs floors {pii/grounding/toxicity:0.15} -> PASS
critical_S in [0.5, 1.0]                      -> PASS
```

### E. Existing test suite

The 5 tests in [`tests/test_control_plane.py`](file:///home/krishna/Projects/ControlPlane/tests/test_control_plane.py) cover control-plane locking, latency, and hashing only. Normalization is Data Plane code that does not yet exist, so no existing test is affected by Part B.

**However:** bundles gain new fields, so all three `policy_hash` values change. The hashes recorded in [`TECHNICAL_DOCUMENTATION_AND_SUMMARY.md`](file:///home/krishna/Projects/ControlPlane/docs/TECHNICAL_DOCUMENTATION_AND_SUMMARY.md) §2C go stale and must be regenerated.

---

# 5. Schema Changes Summary

### New `PolicyConfig` / `BundleConfig` fields

| Field | Type | Default | Locked | Direction |
| :--- | :--- | :--- | :--- | :--- |
| `detector_critical_thresholds` | `dict[str, float]` | `{pii: 0.90, grounding: 0.90, toxicity: 0.95}` | yes | map, lower stricter |
| `detector_weight_floors` | `dict[str, float]` | `{pii: 0.15, grounding: 0.15, toxicity: 0.15}` | yes | map, higher stricter |
| `ner_label_confidence` | `dict[str, float]` | `{PERSON: 0.85, GPE: 0.75, ORG: 0.70}` | no | — |
| `pii_aggregation` | `str` | `"density"` | no | enum |
| `nli_grounding_enabled` | `bool` | `false` | no | true stricter |

### Changed behavior

* `grounding_threshold` moves to **higher-is-stricter** (P1).
* `locked_fields` gains five entries (P2, P4).
* Detector normalization changes from `min(1, P/T)` to piecewise (N1).
* Grounding normalizes ungroundedness `U = 1 − similarity` (N1).
* Ledger rows gain `raw_score` per detector, plus NER entity counts (N1, N3).

---

# 6. Implementation Checklist

1. `resolver.py`: replace the `if/elif` chain with the direction registry; move `grounding_threshold` to `HIGHER_IS_STRICTER`.
2. `resolver.py`: add `_validate_locked_map()` including the key-removal guard.
3. `resolver.py`: add `_validate_band_ordering()`, `_validate_weight_integrity()`, `_validate_critical_coherence()`; call all three from `resolve_policy()`.
4. `models.py`: add the five new fields from §5 to both `PolicyConfig` and `BundleConfig`.
5. `org_baseline.yaml`: add the new maps and extend `locked_fields`.
6. Data Plane: implement `normalize(P, T)` with degenerate guards; use it for every detector; flip grounding via `U = 1 − similarity`.
7. Fusion: apply critical floors with `R_fused = max(R_fused, high_band)`; ensure band comparison uses `>=`.
8. Ledger: persist `raw_score` per detector and NER entity counts.
9. Recompile all three bundles; update the `policy_hash` values in `TECHNICAL_DOCUMENTATION_AND_SUMMARY.md`.
10. Extend `tests/test_control_plane.py`: grounding direction (both ways), enum tightening, map key-removal evasion, weight-sum, band-ordering, critical-coherence.

---

# 7. Open Items

1. **NLI has no threshold field.** `grounding_threshold` is a *similarity* floor and is meaningless against `P(contradict)`. Enabling NLI requires a separate `nli_risk_threshold`. No value proposed — needs a decision.
2. **`pii_aggregation` default** — `density` is proposed as most honest, but `max` is the conservative choice. Undecided.
3. **Critical floor target level.** Currently floors at `high_band`. An alternative is a per-detector target action, letting some detectors floor at `low_band` (warn) rather than severe. Undecided.
4. **P1 is a behavior change to a locked field.** Existing deployed bundles would flip enforcement direction on recompile. Needs a migration note if any bundle has shipped.
