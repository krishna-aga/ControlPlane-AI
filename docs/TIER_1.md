# Tier 1 — Grounding, Toxicity, and the Bias Gap

**Status:** Grounding and toxicity are **implemented** —
`data_plane/detectors/grounding.py` (sentence-transformers/all-MiniLM-L6-v2) and
`toxicity.py` (unitary/toxic-bert), real model weights, wired into
`data_plane/gateway.py` and covered by `tests/test_tier1_detectors.py` and
`tests/test_gateway.py::TestTier1Wiring`. The decision to vendor weights (§7) was
reversed; everything else decided in this document — bias declared not built,
`grounding_threshold` left uncalibrated, NLI not built — still holds exactly as
written. **T1-7 and T1-8 (§5) were fixed separately**, before the detectors existed:
they were live defects in code that already shipped, not part of the detector build.
**Scope:** `data_plane/detectors/grounding.py` and `toxicity.py`, `data_plane/models.py`,
`data_plane/gateway.py`, `control_plane/{models,resolver}.py`,
`policies/org_baseline.yaml`.

Two decisions were taken when this document was written, and they shape everything
below:

* **Bias is declared, not built** (§4). No bias fields are added to the schema.
* **`grounding_threshold` is not calibrated.** It stays at `0.6` — an estimate about a
  scale nobody has measured. §2B states exactly what that costs, deliberately, instead
  of quietly leaving it as a number that looks decided.

NER is already specified — `ner_label_confidence` and `pii_aggregation` are in the
bundle, N3 in the register covers label confidence, and
[`ORIGIN_AND_EXONERATION.md`](ORIGIN_AND_EXONERATION.md) covers what to do with its
findings. This document covers the other two axes and what they expose in the code that
already exists.

---

## 0. Summary

| # | Item | Kind | Disposition |
| :--- | :--- | :--- | :--- |
| **T1-1** | The bundle pins thresholds but not the **scale** they are measured on | Audit hole | **Fixed** — `grounding_model` / `toxicity_model`, locked immutable |
| **T1-2** | Whole-response scoring dilutes a single bad sentence | Design defect | **Fixed** — worst-sentence scoring, both detectors |
| **T1-3** | The grounding band geometry is probably miscalibrated | Calibration risk | **Still accepted, unmeasured** — §2B. Confirmed live: a genuinely correct answer scored 0.75 similarity and landed in REGENERATE, exactly the risk this row predicted |
| **T1-4** | Cosine measures topic, not truth | Accepted limitation | Still true of the real detector — document, as T0-8 does |
| **T1-5** | NLI cannot reuse `grounding_threshold` — direction flips | Latent P1 | Still not built — separate detector key, if ever built |
| **T1-6** | One `toxicity_probability` collapses six unequal labels | Design defect | **Fixed** — `toxicity_label_weights`, locked. Exposed a second live bug in the process: see T1-9 |
| **T1-7** | `DetectorSignals` cannot express *"the detector failed"* | **Fixed** | `detector_status`, routed through `fail_mode` (§5) |
| **T1-8** | Every critical floor is **inert** at the shipped bands | **Fixed (warns)** | `_validate_critical_coherence` now warns when a critical value is inert (§5) |
| **T1-9** | `unitary/toxic-bert` emits Kaggle label names, not Detoxify's | **Live bug, fixed** | `_LABEL_ALIASES` in `toxicity.py` — see §3E |

T1-7, T1-8 and T1-9 are defects found in code; T1-1, T1-2 and T1-6 are now implemented
per their original disposition. T1-3, T1-4 and T1-5 remain open exactly as designed.

---

## 1. The bundle pins the thresholds but not the scale (T1-1)

`grounding_threshold: 0.6` is a cosine similarity floor. Cosine similarity **from which
model?** `all-MiniLM-L6-v2` and `all-mpnet-base-v2` do not produce the same number for
the same sentence pair. Swap the model and `0.6` silently enforces something else while
`policy_hash` stays identical. The same holds for `toxicity_threshold: 0.7` across
`unitary/toxic-bert` and any replacement.

This is the argument [`TIER_0.md`](TIER_0.md) §2 already makes for keeping blocklist
terms inline in the bundle rather than in an external file:

> If the blocklist lived outside, `policy_hash` would no longer pin actual enforcement
> behaviour and the audit guarantee would develop a hole.

T1 opens that hole wider, because a model identity is the **more** load-bearing half of
a T1 threshold. A number without its scale is not a policy.

```yaml
grounding_model: sentence-transformers/all-MiniLM-L6-v2
toxicity_model:  unitary/toxic-bert
```

Locked and **immutable** — the resolver's generic fallback is correct here, because
there is no ordering on model identity and therefore no "stricter" direction to define.
Verified at load the way `policy_hash` already is (`INPUT_GATE.md` §6): if the loaded
model's identity does not match the bundle, refuse rather than enforce a threshold
against the wrong scale.

**Not added to the schema yet.** Nothing consumes them, and the bundles already carry
three unconsumed fields (`caching_enabled`, `cache_threshold`, `allow_downrouting` —
`GATEWAY.md` §7). Adding two more, plus a hash churn on all three bundles, buys nothing
until a detector reads them.

---

## 2. Grounding

### A. The unit is the claim, not the response (T1-2)

One cosine over the whole answer reintroduces the dilution defect a third time. A
400-word faithful summary containing one fabricated sentence still scores high overall —
the fabrication is averaged away by the correct text around it. That is **N4** and the
Tier 0 weight-dilution problem again, one level down.

Score **per sentence**, similarity = **max over retrieved chunks**, response score =
**the worst sentence**.

This is deliberately **not** a bundle knob, unlike `pii_aggregation` and
`t0_aggregation`. Taking the worst unit is the same argument that removed
`detector_weights` — *"how risky is this output" is answered by the worst thing found,
not the average across the things checked* — and that argument is a correctness
position, not a preference. Offering `mean` here would offer a tenant the defect back.

**Non-claim sentences have no ground truth.** *"Happy to help."* / *"Anything else?"*
cannot be grounded against any chunk and will score near-zero similarity, so under
worst-sentence they would fire on essentially every response — the over-flagging failure
the problem statement names directly (*"alert fatigue … pushes users to ignore or bypass
warnings"*). A claim filter is required before scoring, and a token-count floor
(`grounding_min_claim_tokens`) is the cheap version. It is a heuristic and will be
wrong sometimes; that residual error is part of why grounding's remedy is `REGENERATE`
rather than `BLOCK` — the cheapest thing to be wrong about.

### B. Where the bands actually sit — and the risk being accepted (T1-3)

Inverting `fusion.normalize()` at the shipped `grounding_threshold: 0.6`, `low_band:
0.3`, `high_band: 0.7`. **These figures are exact** — computed from the committed
bundle and the real function, identical across all three personas:

| best-chunk cosine | S | action, grounding dominant |
| ---: | ---: | :--- |
| > 0.760 | < 0.30 | `ALLOW` |
| 0.760 → 0.360 | 0.30 → 0.70 | **`REGENERATE`** |
| < 0.360 | > 0.70 | `BLOCK` |
| < 0.120 | ≥ 0.90 | critical value reached (but see T1-8) |

Two observations follow from the arithmetic alone:

**The `REGENERATE` window spans cosine 0.36–0.76.** That is an extremely wide band in
embedding terms.

**The critical value is reached only below cosine 0.12.** For a sentence-embedding model
that is near the floor of the output range — roughly "no lexical or topical relationship
whatsoever."

> **What is NOT established here.** Whether real grounded paraphrases actually land
> inside that `REGENERATE` window is an **estimate, not a measurement**. Typical
> guidance puts a faithful MiniLM paraphrase of its source chunk somewhere around
> 0.5–0.8, which would put a large share of *correct* answers into `REGENERATE` — but
> that figure is recalled, not sampled, and this project has already been burned once by
> exactly that move. **T0-10** was a threshold calibrated on hand-constructed strings and
> applied to a different alphabet, missing ~78% of real secrets. Treating a remembered
> cosine range as a measurement would be the same error with a different constant.
>
> Calibration was **deliberately deferred** — it requires vendoring a ~90 MB model, and
> the decision was taken not to. So `grounding_threshold: 0.6` remains an unvalidated
> constant, in the same category as the `T0_ESTIMATED_MS` guesses that
> [`NO_LATENCY_BUDGET.md`](NO_LATENCY_BUDGET.md) §2 deleted for violating the project's
> own no-hardcoded-constants rule. It is recorded as a known risk, not a settled value.

**The locking direction makes this the org's problem, not the tenant's.** If the window
does turn out to over-flag, the correction is to *lower* `grounding_threshold` — which
is the **looser** direction under P1, and locked. That is the right lock: a tenant must
not be able to gut grounding. But it means the baseline number has to be correct, and
this one has never been checked. `low_band` / `high_band` are shared across every
detector, so grounding's window cannot be moved independently of everyone else's.

**Before this detector is trusted in a demo, sample the cosine distribution on grounded
vs. fabricated pairs for the pinned model and set the threshold from it.** That is the
T0-10 method, and it is the only thing that converts this row from a guess into a
number.

### C. Cosine measures topic, not truth (T1-4)

```
"The refund window is 30 days"     vs  "The refund window is 90 days"      ≈ 0.95
"Customers are eligible"           vs  "Customers are not eligible"        ≈ 0.97
```

**The most dangerous hallucination class — a correct-looking claim with one wrong fact,
or a flipped negation — is the exact class cosine similarity cannot see.** Embedding
grounding catches whole-cloth invention on a topic absent from the corpus. It does not
catch a contradicted number, and a contradicted number is what actually reaches a
customer as a wrong refund policy.

Handled the way **T0-8** handles the canary limitation: stated, not engineered around.
Chasing it with embeddings is the wrong tool, not an unfinished feature.

It also lands somewhere useful. This gap **is** the argument for T2: an LLM judge handed
the chunk and the claim can answer the entailment question that cosine structurally
cannot. It is why `decision_support` sets `t2_enabled: true`, and it is the clearest
example in the system of the graded middle being where a ~600 ms judge changes an
outcome.

### D. NLI is a separate detector, not a mode (T1-5)

Open Item #1 in the register notes NLI needs its own threshold. It needs more than that.

`grounding_threshold` is a similarity **floor** — higher is stricter.
`P(contradiction)` is a risk **ceiling** — lower is stricter. One field cannot carry both
directions, and putting it in the wrong set is **P1**, the live bug this project already
shipped once.

The `nli_grounding_enabled` flag proposed in the register §5 makes it worse: the
*meaning* of the persisted `raw["grounding"]` would depend on a config flag, so a
calibration sweep could not tell which scale a historical row was measured on — breaking
the N1a guarantee that the raw scores exist for.

**Register it as its own detector key instead** — `grounding` (cosine) and `entailment`
(NLI) — each with its own threshold, critical value and ledger column. Max aggregation
makes adding a detector free, which is a real dividend of the fusion refactor: an
inapplicable detector is simply absent from the max. This also avoids one field doing two
incompatible jobs, which is [`NO_LATENCY_BUDGET.md`](NO_LATENCY_BUDGET.md) §3's whole
lesson.

`nli_grounding_enabled` was specified in the register §5 but **never added to
`models.py`**, so there is nothing to migrate.

### E. Empty retrieval is `None`, never `0.0`

A no-context request emitting `grounding_similarity = 0.0` scores `S = 1.0` and blocks
everything. It must be **absent** from the max.

Conversely — and this is the part worth saying out loud — **absent grounding is not
evidence of groundedness.** A fully hallucinated answer to a general-knowledge question
scores nothing on this axis, because no ground truth exists to score it against. That is
honest gateway behaviour rather than a hole, and it is the argument for `require_retrieval`
in [`GATEWAY.md`](GATEWAY.md) §9.

---

## 3. Toxicity

### A. One scalar collapses six unequal labels (T1-6)

Detoxify emits `toxicity`, `severe_toxicity`, `obscene`, `threat`, `insult`,
`identity_attack`. Collapsing them into a single `toxicity_probability` discards the
distinction that matters most: a **threat** and an **obscenity** are not the same
finding. That is **N3**'s NER label-collapse defect on a second detector.

Fixed in the pattern the bundle already uses for `ner_label_confidence`:

```yaml
toxicity_label_weights:
  identity_attack: 1.0
  threat:          1.0
  severe_toxicity: 1.0
  insult:          0.7
  obscene:         0.5
  toxicity:        0.6
```

Raw score is `max(P_label × weight_label)`, and normalization against
`toxicity_threshold` happens **once, afterwards**. That ordering is load-bearing: it
preserves the property that `S = 0.5` is exactly the detection threshold, which is what
makes `detector_critical_thresholds` comparable across detectors (N2).

Locked, `MAP_HIGHER_IS_STRICTER`, compared entry by entry with the key-removal guard —
the same treatment `t0_severity_scores` received in P4c, and for the same reason: under
max aggregation the table is read directly rather than diluted, so zeroing an entry
silences that label outright.

### B. The toxicity detector is itself a documented source of bias

Models trained on the Jigsaw corpus — Detoxify included — systematically over-score
African-American English, reclaimed slurs, and text that merely **mentions** identity
terms. *"I am a gay man"* scores high on the identity term alone.

**So a toxicity detector deployed at blocking severity disproportionately blocks content
from and about minority groups.** The detector meant to catch harm is a source of it.
This is not a hypothetical: it is the best-documented failure mode of the model class,
and any deployment that hard-blocks on its output inherits it.

The action ladder already gets the common case right. `toxicity` dominant inside the
graded band maps to **`FLAG`** — human review — on the stated grounds that it is
*"neither maskable nor reliably fixed by a retry."* That is the correct posture for a
classifier with a known demographic error profile: route it to a person.

The autonomous BLOCK path is the one to decide deliberately. Measured against the shipped
bundle:

```
toxicity_threshold = 0.7,  low_band = 0.3,  high_band = 0.7

raw P(toxic) < 0.420   ->  S < 0.30   ->  ALLOW
raw P(toxic)   0.420   ->  S = 0.30   ->  FLAG  (band floor)
raw P(toxic)   0.700   ->  S = 0.50   ->  FLAG  (detection threshold)
raw P(toxic)   0.820   ->  S = 0.70   ->  BLOCK (band ceiling)
raw P(toxic)   0.970   ->  S = 0.95   ->  critical value (inert - see T1-8)
```

So toxicity blocks alone from **raw P ≥ 0.82**, not at the critical value of 0.95 that
`detector_critical_thresholds` appears to set. Whether an autonomous block belongs to
this detector at all is a policy question that should be answered on purpose rather than
inherited from a default.

### C. Origin applies — but it does not exonerate

A model quoting the user's own abusive message back scores toxic. The shared
`resolve_origin()` from [`ORIGIN_AND_EXONERATION.md`](ORIGIN_AND_EXONERATION.md) should
run on toxicity findings too — but the naive move is to reuse the PII exoneration rule,
and here that is **wrong**.

For toxicity the risk is **delivery**, not origination. Echoing a slur back to a user is
bad output regardless of who typed it first. Origin is **informational** — valuable in
the ledger and for the reviewer queue, worthless as an exoneration.

That extends the existing taxonomy rather than contradicting it:

| risk type | example | origin-sensitive? |
| :--- | :--- | :--- |
| **fabrication** | invented person, org, place | **yes** — corpus origin means no fabrication |
| **exposure** | card, Aadhaar, secret | no — a real card is dangerous wherever it came from |
| **delivery** | slur, threat, insult | no — the harm is in sending it |

Only fabrication risk is origin-sensitive. The other two are not, for different reasons.
`ORIGIN_AND_EXONERATION.md` §3 draws the first two rows; this is the third.

### D. Worst sentence, same as grounding

One toxic sentence inside a long polite answer must not average away. Same argument as
§2A; not a knob, for the same reason.

### E. The live model doesn't speak the spec's label names (T1-9)

`unitary/toxic-bert` was pinned by name in §3A's example config before it was ever
loaded. Loading it and calling `pipeline("text-classification", model=..., top_k=None)`
returns:

```
toxic, severe_toxic, obscene, threat, insult, identity_hate
```

not the Detoxify-convention names this document (and `toxicity_label_weights`) used:
`toxicity`, `severe_toxicity`, `identity_attack`. Three of six disagree. Matched blind
- `weights.get(entry["label"], 0.0)` against the raw label - every lookup on those three
misses, and the detector silently returns `0.0` forever: not an error, not a crash, a
clean-looking zero that means "unchecked," indistinguishable in the ledger from
"checked and found nothing." This is the T0-10 failure shape again - a value computed
against the wrong alphabet - one detector class over.

Fixed with an explicit alias table (`_LABEL_ALIASES` in `data_plane/detectors/toxicity.py`)
mapping the model's raw label to the canonical name before the weight lookup, verified
by `tests/test_tier1_detectors.py::test_label_names_are_correctly_aliased` against the
real model (a mocked model would have passed with the bug still present, since the mock
would never emit the wrong names to begin with).

---

## 4. Bias — declared, not built

**Track 1 names bias, hallucination, and privacy.** Grounding addresses hallucination,
PII and NER address privacy, and **"toxicity" is not bias.**

> *"Candidates from that region tend to underperform."*

Perfectly polite, scores near zero on every toxicity label, and is the actual failure
mode an enterprise deploying a hiring copilot would care about. `identity_attack` catches
targeted harassment; it does not catch stereotyping, differential treatment, or skewed
recommendation.

**Disposition: the axis is named and left unbuilt.** No `bias_*` fields are added to the
schema, because unconsumed bundle fields are a wart this project already carries three of
(`GATEWAY.md` §7), and because *the simplest secure design is no mechanism*
([`NO_LATENCY_BUDGET.md`](NO_LATENCY_BUDGET.md) §3) — a bias field that nothing enforces
is exactly the safety theatre that document exists to argue against. A stub detector
would be worse than the gap: it would let the system claim coverage it does not have.

**Where it belongs when it is built: T2.** Detecting a stereotype requires reading the
claim in context and judging it, which is what an LLM judge does and what no cheap
classifier does. The alternative considered — a protected-attribute lexicon crossed with
generalizing predicates — has a false-positive rate high enough that it could only ever
feed `FLAG`, and it would collide directly with §3B: a lexicon-driven bias detector fires
on the same identity-term mentions that already make the toxicity classifier unreliable.

**The claim to make honestly:**

> Bias is the third risk in the track and the system does not detect it. Hallucination
> and privacy are covered by dedicated detectors; bias is delegated to T2, which only
> `decision_support` enables. Naming the gap is a stronger position than a stub that
> reports zero.

One thing the architecture *does* answer, and should be said alongside the gap. The
problem statement notes that *"bias, hallucination, and privacy risks often overlap — a
fabricated detail about a person can simultaneously be a hallucination and a privacy
concern."* **Max aggregation handles that overlap correctly by construction:** the same
output scores on several axes at once, the worst one selects the action, and nothing is
double-counted or diluted. The fusion design already answers the overlap complexity. What
is missing is only the third axis.

---

## 5. Two defects in shipped code that T1 exposed — both now fixed

### T1-7 — there is no way to say *"the detector failed"* — **fixed**

`DetectorSignals` documented `None` as meaning **not applicable**. There was no
representation for *the detector ran and threw* — under max aggregation the two cases
were indistinguishable: a crashed detector was simply absent from the max, which looked
exactly like a non-RAG request having no grounding signal. Worse, in the code that
actually shipped, nothing even caught the exception: a thrown `run_t0` crashed
`process_request` outright, which is neither `fail_open` nor `fail_closed` — just an
unhandled 500.

**Fix.** `DetectorSignals.detector_status: Dict[str, Literal["failed"]]` — absence still
means "ran clean" or "did not apply", exactly as before; only `"failed"` is ever written,
since that's the one bit `raw`/`normalized` cannot already express
(`data_plane/models.py`). `Gateway._run_t0()` wraps both call sites — the main
generation path and the cache-revalidation path — and turns an exception into
`detector_status={"t0": "failed"}` instead of letting it propagate. `fuse()`
(`data_plane/fusion.py`) routes a failed entry through `bundle["fail_mode"]`:
`fail_closed` forces `BLOCK` with the failed detector(s) named in the reason string;
`fail_open` still delivers the response but the failure is now recorded in
`FusionResult.detector_status`, which reaches the ledger via `ledger_row()` — "clean"
and "unchecked" are distinguishable there for the first time. A cache-revalidation
failure additionally forces an eviction and a `not cached` skip reason regardless of
`fail_mode`, since serving or storing a response T0 could not re-verify defeats the
point of the revalidation step. Covered by `tests/test_fusion.py::TestDetectorFailure`
and `tests/test_gateway.py::TestT0FailureFailMode`.

### T1-8 — every critical floor is inert at the shipped bands — **fixed (warns)**

`detector_critical_thresholds` is described in [`RISK_FUSION.md`](RISK_FUSION.md) §4 as
*"the fix that matters most."* It was, under weighted averaging, where a toxicity score
of 0.983 was multiplied down to 0.197 and resolved to `ALLOW`.

Under **max aggregation** the floor only changes an outcome when a detector's critical
value sits **below** `high_band` — otherwise a detector reaching its critical value has
already exceeded the band and blocks on its own arithmetic. Measured against the
committed bundle:

```
                critical_S      high_band     floor
  pii              0.90    >=     0.70        REDUNDANT
  grounding        0.90    >=     0.70        REDUNDANT
  toxicity         0.95    >=     0.70        REDUNDANT

  toxicity S=0.95, floors enabled   -> BLOCK
  toxicity S=0.95, floors disabled  -> BLOCK      (no difference)
```

Band tightening does not rescue it: a flagged input *shrinks* `high_band`, which widens
the gap. The floors are inert in every reachable configuration of the shipped bundles.

**This is the same conclusion already recorded for `t0_floor_severity`** — `high` scores
0.75, above `high_band` 0.70 — generalized to all four detectors. The mechanism is not
broken, and it should not be removed: it is **live as a tenant-tightenable lever**, since
`detector_critical_thresholds` is `MAP_LOWER_IS_STRICTER` and a tenant may lower a
critical value beneath the band. Verified:

```
  tenant tightens toxicity critical to 0.60, output scores S=0.65:
    with floor:    BLOCK   (fused floored to 0.700)
    without floor: FLAG    (fused 0.650)
```

It also still shapes the audit record — `critical_fired` feeds the BLOCK reason string
even when the action is unchanged.

**What this costs:** the shipped defaults `{pii: 0.90, grounding: 0.90, toxicity: 0.95}`
look like safeguards and are not. An org that wants a detector to escalate on its own
must set its critical value **below `high_band`**, and nothing in the schema or the
validator says so. `_validate_critical_coherence` checks `0.5 <= crit <= 1.0` — it would
accept every inert value in the current baseline without comment.

**Fix.** `_validate_critical_coherence` (`control_plane/resolver.py`) now warns —
`InertCriticalThresholdWarning`, never raises — whenever a resolved critical value is
`>= high_band`. It cannot be an error: the values are legal, and a tenant may later
tighten `high_band` underneath them, at which point the same entry stops being inert.
`compile_bundle` (`control_plane/compiler.py`) catches and prints every warning raised
during resolution, so `python -m control_plane.compiler` surfaces exactly the table
above at compile time instead of leaving the author to infer it — the P8 `extra="forbid"`
argument again: *for a policy authoring surface, the author believing they configured
something they did not is the worst failure mode.* The shipped baseline still ships all
three entries inert; compiling any of the three persona bundles now prints the warning
for `pii`, `grounding`, and `toxicity`, which is accurate — nothing about the underlying
tenant-lever mechanism changed, only its visibility. Covered by
`tests/test_control_plane.py::TestStructuralValidators::test_inert_critical_threshold_warns_not_rejects`.

---

## 6. Bundle fields — implemented

| field | default | direction | locked |
| :--- | :--- | :--- | :--- |
| `grounding_model` | `sentence-transformers/all-MiniLM-L6-v2` | immutable | yes |
| `toxicity_model` | `unitary/toxic-bert` | immutable | yes |
| `grounding_min_claim_tokens` | `6` | higher = fewer sentences scored = looser | no |
| `toxicity_label_weights` | §3A table | map, higher stricter | yes |
| `entailment_threshold` | — | lower stricter | only if NLI ships |

The first four are in `policies/org_baseline.yaml` and every compiled bundle now.
`entailment_threshold` stays undefined - NLI is still not built (§2D). `grounding_threshold`,
`toxicity_threshold` and `detector_critical_thresholds` predate this change and were
already locked.

There are deliberately **no** `grounding_aggregation` or `toxicity_aggregation` knobs
(§2A), and **no** `bias_*` fields (§4).

---

## 7. Deliberately not built

* **Both detectors are now built** (`data_plane/detectors/grounding.py`, `toxicity.py`),
  reversing the earlier decision not to vendor weights. `spaCy`'s NER remains out of
  scope here - NER is already specified separately (see the intro).
* **Calibrating `grounding_threshold`** (§2B) — still deliberately deferred. Having the
  model now doesn't retroactively calibrate the threshold; that requires deliberately
  sampling a real cosine distribution on grounded vs. fabricated pairs, which nothing in
  this change did. Recorded as an accepted risk rather than a settled number, and now
  confirmed live (§2B, T1-3 row).
* **Bias** (§4) — declared, delegated to T2.
* **NLI entailment** (§2D) — designed as a separate detector key, not built.
* **Parallelism.** The docs describe T1 as *"parallel heuristics, ~40 ms."* Both figures
  are unvalidated in exactly the way `T0_ESTIMATED_MS` was. Three CPU-bound detectors
  contend on the GIL except inside native inference, so the speedup is real but bounded
  and unmeasured. Per [`NO_LATENCY_BUDGET.md`](NO_LATENCY_BUDGET.md), the answer is to
  measure and report it, never to trade against it — and to stop repeating `~40 ms` until
  something has measured it.

## 8. Open

* **T1-7, T1-8 and T1-9 are fixed** (§5, §3E) — no longer open.
* **`grounding_threshold` calibration** (§2B, T1-3) — still genuinely open. Now
  confirmed live rather than merely predicted: sample the real cosine distribution on
  grounded vs. fabricated pairs for `all-MiniLM-L6-v2` before trusting this in
  production, exactly as originally scoped.
* **T1 detectors run sequentially, not in parallel**, in `Gateway.process_request()`.
  The "~40 ms parallel" figure in §7's parallelism note was never validated and this
  change didn't validate it either - grounding and toxicity are two more sequential
  calls today, not a measured concurrent budget.
* **`pii_aggregation` and `t0_aggregation` remain knobs** while grounding and toxicity
  are specified without one (§2A). That inconsistency is defensible — those two predate
  the max-aggregation argument — but it should be resolved in one direction eventually.
