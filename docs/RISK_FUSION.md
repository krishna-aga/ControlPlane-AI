# Risk Fusion & the Graded Action Ladder

**Status:** Implemented. `data_plane/fusion.py`. Fused risk is the **maximum**
normalized detector score; `detector_weights` was removed (§5). 142 tests passing.

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
  R = max(S_d)          ◄── the worst axis IS the risk
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

**Safety is independent of weight arithmetic** — now structurally, since there is no
arithmetic left to game. Under max aggregation a severe detector already survives on its
own, so the floors serve a narrower purpose: escalating a detector that is severe on its
*own* scale to the severe action even when its normalized score sits below `high_band`.
That is what carries a `high` T0 finding (0.75) the rest of the way to BLOCK.

The band comparison is `>=`, not `>`: a floor sets risk to exactly `high_band`, and a
strict `>` would drop it into the graded middle.

---

## 5. Max aggregation — why there are no weights

Fused risk is the **maximum** normalized detector score. There is no `detector_weights`
field; it was removed.

### The defect it fixes

A weighted average makes the result depend on **how many detectors happened to run**,
because each detector's share is `1 / (number applicable)`. Measured under the old
scheme on identical output:

| scenario | old score | old action |
| :--- | ---: | :--- |
| toxicity 0.99, only detector applicable | 0.983 | BLOCK |
| toxicity 0.99, three clean detectors alongside | **0.369** | not blocked |
| blocklist `medium`, only detector applicable | 0.400 | REDACT |
| blocklist `medium`, three clean detectors alongside | **0.192** | ALLOW |

A swing of **0.614** on the most severe signal in the system — and in the wrong
direction: *running more safety checks made the response look safer.* Only the critical
floors kept the toxicity case from producing a wrong action, which meant the floors were
carrying the system while the average was unreliable in the only range it governed.

### Why averaging was the wrong operation

The detectors measure **orthogonal** risks. A clean toxicity score is not evidence that a
blocklist hit is acceptable — they are different axes. Averaging them treats "no toxicity
found" as partial evidence that an unrelated finding is fine.

"How risky is this output?" is answered by **the worst thing found**, not by the average
across the things checked. A car with failed brakes is not 25% defective because the
lights, tyres and horn are fine.

### Measured after

```
toxicity 0.99        alone=0.983/BLOCK    +1 clean=0.983/BLOCK    +3 clean=0.983/BLOCK
blocklist (medium)   alone=0.400/REDACT   +1 clean=0.400/REDACT   +3 clean=0.400/REDACT
```

### Two properties that now fall out for free

* **An inapplicable detector is simply absent from the max.** A non-RAG request has no
  grounding signal and needs no applicability bookkeeping — the renormalization that used
  to handle this is gone along with the problem it created.
* **A clean detector cannot lower the score.** Monotonic by construction.

### What replaced the weights

An org that cares more about one axis had a vague lever; it now has two precise ones,
both locked:

* **`pii_threshold` / `grounding_threshold` / `toxicity_threshold`** — how sensitive that
  detector is, i.e. when it starts counting.
* **`detector_critical_thresholds`** — how severe that detector must be to escalate on
  its own.

This is P4's own conclusion — *"weights should not be safety-critical at all"* — carried
to its end. Removing the field also closed a hole: `_validate_weight_integrity` checked
only that the values summed to 1.0, so `{t0: 2.0, pii: -1.0}` and `{nonsense: 1.0}` both
compiled.

### Consequence: T0 severities now matter directly

Under averaging, a `medium` T0 finding was multiplied down to 0.16 and usually vanished.
Under max it stands at its table value of **0.40**, above `low_band`, so a blocklist hit
now consistently reaches `REDACT`. That resolves the register's open item #7 — but it
means `t0_severity_scores` is now read directly rather than diluted, and those four
numbers deserve a deliberate review rather than remaining the spec's estimate.

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
* **`t0_severity_scores` is read directly under max aggregation** and is likewise
  unlocked, so the same evasion applies: setting every severity to 0.0 silences Tier 0's
  contribution to fusion entirely. Both maps need the same fix.
