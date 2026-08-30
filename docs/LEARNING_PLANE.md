# Learning Plane

**Status:** Implemented (demo scope). `learning_plane/` — a 5-page Streamlit app over
the Data Plane's audit ledger. Source spec:
[`docs/38c6eafa-…_Learning_Plane__the_part_that_gets_better_over_time.pdf`](./38c6eafa-525e-41c7-b2ba-005306f4532b_Learning_Plane__the_part_that_gets_better_over_time.pdf).

> **Calibration never decides a threshold — it shows the tradeoff so a human picks
> one with eyes open.** Every other page in this plane exists to feed that one
> decision with real evidence instead of guesses: what did reviewers overturn, what
> did the current policy miss, what would a different cutoff actually cost.

Nothing in this plane runs on the request path. It's entirely offline/async, reading
what `data_plane/gateway.py` already wrote to the ledger — see `docs/GATEWAY.md` for
the plane that produces the data this one reads.

---

## 1. Run it

```bash
streamlit run learning_plane/app.py
```

Seed/regenerate the demo data first if `learning_plane/data/` is empty or stale:

```bash
python -m learning_plane.seed_demo_ledger --per-persona 250      # 750-row demo ledger
python -m learning_plane.shadow_eval --n 200 --seed 7             # shadow eval sample
python -m learning_plane.calibration --n 600 --seed 11            # calibration labelled set
```

All three are regeneratable and version-independent of each other; the Streamlit
pages also generate them on first load if missing.

---

## 2. The five pages

```
  Ledger Explorer          Reviewer Queue           Shadow Eval
  (data_plane.ledger,      (FLAG rows, ranked        (mock T2 judge over
   real hash chain)         by uncertainty)           an ALLOW sample)
        │                        │                          │
        │                        ▼                          ▼
        │                 FP ground truth            est. FN rate
        │                        │                          │
        │                        └───────────┬──────────────┘
        │                                     ▼
        │                            Calibration (the
        │                            tradeoff curve)
        │                                     │
        │                            propose a candidate
        │                                     ▼
        └──────────────────────────  Closing the Loop
                                     (human approves,
                                      diagram + KPI row)
```

### Ledger Explorer (`app.py`)
The only page that isn't synthetic logic on top of synthetic data — it loads the
demo ledger into a real `data_plane.ledger.Ledger` and calls its actual `.verify()`.
"Tamper this row" flips a field **without** recomputing `row_hash`, so the chain
visibly breaks red from that row forward. Nothing here is staged.

### Reviewer Queue (`pages/1_Reviewer_Queue.py`)
Surfaces `FLAG` rows ranked by distance from `fused_risk` to the nearest
`effective_bands` edge — closest calls first, not highest volume. Approve/Reject
persists to `learning_plane/data/reviewer_labels.json` and is the plane's only
source of **false-positive** ground truth.

### Shadow Eval (`pages/2_Shadow_Eval.py`, `shadow_eval.py`)
Re-judges a random sample of `ALLOW` decisions with a mock T2 judge (T2 itself isn't
built — see `docs/TIER_0.md` / the Tier 1 gap) and reports the disagreement rate as
an **estimated false-negative rate**. Always shown with a "not ground truth" caveat,
since the judge is imperfect too.

**Not the same thing as shadow deploy** (control plane): shadow eval audits what the
*current* policy is missing; shadow deploy compares *two policy versions* against
each other. The spec calls this out explicitly as a near-guaranteed point of
confusion — see §5.

### Calibration (`pages/3_Calibration.py`, `calibration.py`)
Sweeps 37 candidate values for `low_band` — a **locked** field, identical across all
three bundle personas (`bundles/*_bundle.json` → `locked_fields`) — over a dedicated
synthetic labelled set, and plots FP rate / est. FN rate / escalation rate / cost per
1k at every point. `current_locked_threshold()` reads the live value from the bundles
rather than hardcoding it, and asserts all three agree.

"Propose this candidate" writes a hashed proposal to
`learning_plane/data/calibration_proposals.json`. It never writes to a bundle or
policy file — `low_band` stays locked; a real change needs an org-baseline recompile
outside this demo.

### Closing the Loop (`pages/4_Closing_the_Loop.py`)
The diagram the spec says the original architecture was missing, wired to real state
where state exists:

| Step | Backed by |
| :--- | :--- |
| ① Calibration proposes | real — reads `calibration_proposals.json` |
| ② Human approves | real — a button here sets `approved: true` on the latest proposal; this is the non-negotiable gate, never automatic |
| ③ Control plane mints new version | preview only — shows the proposal's hash as an identifier; no bundle is compiled or written |
| ④ Shadow deploy v_old vs v_new | not implemented — informational only |
| ⑤ Data plane enforces new version | not implemented — informational only |
| ⑥ New decisions flow back to the ledger | this *is* the Ledger Explorer page |

Below the diagram, the KPI row implements the spec's stakeholder metrics table:

| Metric | Source |
| :--- | :--- |
| False positive rate | Reviewer Queue labels |
| Estimated false negative rate | Shadow Eval |
| Escalation rate (% hitting T2) | ledger `fusion.t2_recommended` |
| p50 / p95 added latency | ledger `latency_ms` |
| Cost per 1k interactions | Calibration's cost model at the current locked threshold |
| Override rate per use case | Reviewer Queue labels, grouped by persona |
| Cost avoided (cache) | ledger cache hits × an illustrative avoided-cost constant |

---

## 3. What's real vs. illustrative

* **Real:** the hash chain and its verification (`data_plane.ledger`), the
  uncertainty ranking formula, the reviewer approve/reject persistence, the
  calibration sweep math, `current_locked_threshold()` reading live from the
  bundles, the proposal/approval read-modify-write round trip.
* **Synthetic but schema-faithful:** the 750-row demo ledger
  (`seed_demo_ledger.py`) — shaped exactly like `Gateway.finish()`'s real payload
  and chained with the same `_row_hash` function, so the ledger behavior is genuine
  even though the traffic is generated.
* **Mocked by design, labelled as such in the UI:** the T2 judge in Shadow Eval (T2
  isn't built), the two-population ground-truth generator behind the Calibration
  sweep, and the illustrative ₹ unit-cost constants in both `calibration.py` and the
  Closing the Loop page's cost-avoided figure.

## 4. Privacy rule

Same structural rule as the rest of the system: every row is types, spans, and
scores — never raw PII values, canary tokens, or prompt/response text. This holds
across all synthetic generators here as much as it holds in `data_plane/`.
