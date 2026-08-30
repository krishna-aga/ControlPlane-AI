# Risk Fusion & the Graded Action Ladder

**Status:** Implemented. `data_plane/fusion.py`. 28 fusion tests, 69 total, passing.

Implements Part B of
[`POLICY_LOCKING_AND_RISK_NORMALIZATION.md`](POLICY_LOCKING_AND_RISK_NORMALIZATION.md) —
N1 (piecewise normalization), N2 (the normalized-S critical scale), P4a (critical
floors) — plus the T0-2 categorical-scoring contract and the action-ladder mapping,
which no prior document defined.

---

## 1. Pipeline

```
DetectorSignals (raw)
      │
      ├─ T0 severities ──► score_t0()      categorical lookup, NOT piecewise
      ├─ pii_confidence ─► normalize(P, pii_threshold)
      ├─ grounding_sim ──► normalize(1 - sim, 1 - grounding_threshold)   ← direction flip
      └─ toxicity_prob ──► normalize(P, toxicity_threshold)
      │
      ▼
  weights restricted to APPLICABLE detectors, renormalized to sum 1.0
      │
      ▼
  R = Σ S_d · w_d
      │
      ├─ critical floors:  if S_d >= critical_d  →  R = max(R, high_band)
      ├─ input tightening: if flagged            →  bands ×= (1 − risk · tightening)
      ▼
  action ladder
```

---

## 2. Piecewise normalization (N1)

$$S = \begin{cases} 0.5 \cdot \dfrac{P}{T} & P < T \\[6pt] 0.5 + 0.5 \cdot \dfrac{P - T}{1 - T} & P \ge T \end{cases}$$

Verified at `T = 0.7`:

| P | `min(1, P/T)` | piecewise |
| :--- | :--- | :--- |
| 0.70 | 1.000 | **0.500** |
| 0.80 | 1.000 | **0.667** |
| 0.90 | 1.000 | **0.833** |
| 0.99 | 1.000 | **0.983** |

The old map discarded everything above the threshold — "barely over the line" and
"appalling" were the same number, which made the Learning Plane's calibration sweep
impossible to run as a query.

**The property everything else depends on: `S = 0.5` is exactly the detection threshold
for every detector, whatever its raw scale.** That is what makes a single critical
threshold comparable across detectors (N2). Without it, a critical value of `0.80` fires
at raw `P = 0.56` — *below* normal detection, for a field meaning "more severe than
normal".

**Grounding flips direction.** Cosine similarity means higher = safer, unlike `P(toxic)`
and PII confidence. Fusion normalizes *ungroundedness*, `U = 1 − similarity`, against
`1 − grounding_threshold`. Verified at `grounding_threshold = 0.6`:

```
sim 0.95 → 0.0625      sim 0.60 → 0.5000  (exactly at threshold)
sim 0.80 → 0.2500      sim 0.30 → 0.7500      sim 0.10 → 0.9167
```

**Raw scores are persisted alongside normalized ones** (N1a). A calibration sweep asks
"what would have happened at `toxicity_threshold = 0.6`?"; that counterfactual is
unreconstructable from normalized scores alone.

---

## 3. T0 is categorical (T0-2)

T0 has no threshold, so the piecewise map cannot apply to it. It is scored by
`t0_severity_scores` lookup with `t0_aggregation` (`max` | `noisy_or`).

`S_t0` therefore does **not** carry T1's 0.5-midpoint meaning. Fusion must not assume a
shared interpretation, and critical floors are applied only to T1 detectors.

---

## 4. Critical floors (P4a) — the fix that matters most

The register measured the pre-fix behaviour: **toxicity at `P = 0.99` with nothing else
firing fused to 0.197 and resolved to `ALLOW` under every persona.** Weights alone cannot
make a detector act, because a weight of 0.2 caps that detector's contribution at 0.2.

```python
if S[detector] >= critical_S[detector]:
    R = max(R, high_band)
```

Measured now:

```
toxicity P=0.99 alone  →  S=0.983  critical fired  →  BLOCK
toxicity P=0.80 alone  →  S=0.667  no floor        →  FLAG
```

**Safety is independent of weight arithmetic.** Zeroing toxicity's weight entirely does
not disable toxicity — guarded by `test_critical_survives_a_zeroed_weight`. This is why
floors were chosen over the weight floors originally proposed in P4b.

The band comparison is `>=`, not `>`: a floor sets risk to exactly `high_band`, and a
strict `>` would drop it into the graded middle.

---

## 5. Weight renormalization

Weights are restricted to detectors that actually ran, then renormalized to sum 1.0. A
non-RAG request has no grounding signal; scoring it `0.0` would carry grounding's weight
as dead mass and drag fused risk down for reasons unrelated to safety.

---

## 6. Input risk tightens the output cascade

**This is what `injection_action: flag` does.** The prompt is forwarded unmodified; the
consequence lands here.

```
factor  = 1 − (injection_risk × input_risk_tightening)
bands  ×= factor
```

Measured — identical model output, identical detector scores, only the input differs:

```
clean input     fused=0.428  bands=[0.30, 0.70]  →  REDACT
FLAGGED input   fused=0.428  bands=[0.15, 0.35]  →  BLOCK
```

Feeding injection in as just another weighted detector would have diluted it through the
same weight arithmetic that P4 showed cannot escalate anything. Band contraction keeps
it structural.

---

## 7. The action ladder

No prior document mapped fused risk onto `ALLOW | REDACT | REGENERATE | FLAG | BLOCK`.
A scalar alone cannot: `REDACT`, `REGENERATE` and `FLAG` are qualitatively different
remedies, not degrees of one severity.

**The band selects severity; the dominant detector selects the remedy.**

| Fused risk | Dominant | Action | Why |
| :--- | :--- | :--- | :--- |
| `>= high_band` | any | `BLOCK` | — |
| `[low, high)` | `t0` / `pii` | `REDACT` | findings carry character spans, so the risk is maskable |
| `[low, high)` | `grounding` | `REGENERATE` | masking cannot ground an unsupported answer |
| `[low, high)` | `toxicity` | `FLAG` | neither maskable nor reliably fixed by a retry |
| `< low_band` | any | `ALLOW` | — |

`T2` is recommended only when `t2_enabled` **and** `low_band <= R <= high_band` — the
graded middle is the only place a ~600 ms judge changes an outcome.

---

## 8. Open

* **Masking spans must be applied before de-anonymization** (T0-5). `REDACT` is decided
  here but executed by the output stage, which does not exist yet.
* **`REGENERATE` has no retry budget.** Nothing bounds the loop if a regenerated answer
  is also ungrounded. Needs a `max_regenerations` bundle field.
* **`detector_critical_thresholds` is still not in `locked_fields`**, and map-aware
  locking (P4c) is unimplemented — so a tenant can raise every critical value to 1.0 and
  disable the floors this document calls load-bearing, or drop a key entirely and remove
  one detector's floor.
