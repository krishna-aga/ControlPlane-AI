# Skill: Closing the Loop (`closing_the_loop`)

## Trigger / Objective
Tie the other three Learning Plane pages together into the diagram the spec calls
out as what separates a learning plane from a logging plane (calibration proposes →
human approves → control plane mints new version → shadow deploy → enforce → back to
the ledger), plus the stakeholder KPI row (FP rate, est. FN rate, escalation rate,
p50/p95 latency, cost/1k, override rate, cost avoided). See `docs/LEARNING_PLANE.md`
§2 "Closing the Loop" and §3 for the full real-vs-illustrative breakdown.

## Input & Output Contracts
* **Inputs:** `learning_plane/data/calibration_proposals.json` (from Calibration),
  `reviewer_labels.json` (from Reviewer Queue), `shadow_eval_results.json` (from
  Shadow Eval, loaded via `shadow_eval.load_or_run()`), and the live demo ledger.
* **Outputs:** a button ("Approve latest proposal") sets `approved: true` /
  `approved_at` on the latest entry in `calibration_proposals.json` — this is the
  only write this page performs, and it is the spec's non-negotiable human gate,
  never automatic. No bundle or policy file is touched; steps ③–⑤ of the diagram are
  preview/informational only (see the table in `docs/LEARNING_PLANE.md`).

## Execution CLI Commands
```bash
streamlit run learning_plane/app.py
# then open "Closing the Loop" in the sidebar (learning_plane/pages/4_Closing_the_Loop.py)
```

## Validation Checks
1. Diagram step ① only shows ✅ if a proposal exists; step ② only offers the Approve
   button once step ① is reached, and only shows ✅ once `approved` is actually set —
   verified via a direct propose→approve round trip against the JSON file (not just a
   page load), since bare-mode execution never triggers a button click.
2. Step ③'s preview hash matches the proposal's own `proposal_hash` — no separate
   "mint" logic exists, since minting a real bundle hash is explicitly out of scope
   here (`low_band` stays locked).
3. KPI row degrades gracefully with zero reviewer labels (shows "no reviews yet"
   rather than crashing on an empty list) — the demo ledger starts with no reviews
   until someone visits the Reviewer Queue page.
4. Override-rate-by-persona breakdown handles reviewer labels saved before
   `policy_hash` was added to the schema (bucketed as "unknown (recorded before
   persona tracking)") rather than raising a `KeyError`.
