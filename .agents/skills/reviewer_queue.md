# Skill: Reviewer Queue (`reviewer_queue`)

## Trigger / Objective
Surface the audit ledger's `FLAG` rows to a human reviewer, ranked by uncertainty
(distance from `fused_risk` to the nearest `effective_bands` edge) rather than by
volume — the spec's explicit design point is that the closest calls are the most
informative to review first. Approve/reject decisions are the ledger's only source
of false-positive ground truth, and feed the calibration sweep's FP-rate input.

## Input & Output Contracts
* **Inputs:** the shared demo ledger (`learning_plane/data/demo_ledger.json`, loaded
  once into `st.session_state.ledger_rows` via `learning_plane/common.py`).
* **Outputs:** `learning_plane/data/reviewer_labels.json` — a dict keyed by
  `request_id`, each entry `{label: approve|reject, action, fused_risk,
  dominant_detector, reviewed_at}`. No raw PII values, canary tokens, or
  prompt/response text — same structural privacy rule as the ledger itself.

## Execution CLI Commands
```bash
streamlit run learning_plane/app.py
# then open "Reviewer Queue" in the sidebar (learning_plane/pages/1_Reviewer_Queue.py)
```

## Validation Checks
1. Pending queue only ever contains `FLAG` rows whose `request_id` has no entry yet
   in `reviewer_labels.json`; a fresh Approve/Reject removes it from the pending list
   without re-running the seed script.
2. Queue ordering is by `common.uncertainty()` ascending — verified against a direct
   sort of the seeded ledger (closest-to-band-edge row first, e.g. distance ≈0.011
   ahead of a distance ≈0.198 row in the 750-row demo seed).
3. `common.save_reviewer_label` / `load_reviewer_labels` / `reset_reviewer_labels`
   round-trip correctly (write two labels, read them back, reset clears the file) —
   checked directly against `learning_plane/data/reviewer_labels.json`, not just
   through the UI.
