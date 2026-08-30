# Skill: Calibration (`calibration`)

## Trigger / Objective
Sweep candidate values for the escalation threshold (`low_band` — a LOCKED field,
identical across all 3 bundle personas, see `bundles/*_bundle.json`
`locked_fields`) and plot FP rate / est. FN rate / escalation rate / cost per 1k
requests at each point. Calibration never picks a threshold — it shows the tradeoff
so a human picks one with eyes open, per the spec's explicit framing of this as the
highest-value artifact in the Learning Plane.

## Input & Output Contracts
* **Inputs:** a dedicated synthetic labelled set (`learning_plane/data/
  calibration_labelled_set.json`, generated on first run if absent) — two overlapping
  populations (`actually_unsafe: bool` vs. `fused_risk`) so no threshold is perfect by
  construction. `current_locked_threshold()` reads `low_band` live from
  `bundles/*_bundle.json` rather than hardcoding it, and asserts all three bundles
  agree (they must, since it's locked).
* **Outputs:** nothing is written to any bundle or policy file — `low_band` is locked,
  so a candidate here is only ever a proposal. Clicking "Propose this candidate" in
  the UI appends a record (`from_threshold`, `to_threshold`, metrics at both points, a
  SHA-256 `proposal_hash`) to `learning_plane/data/calibration_proposals.json`.

## Execution CLI Commands
```bash
python -m learning_plane.calibration --n 600 --seed 11
streamlit run learning_plane/app.py
# then open "Calibration" in the sidebar (learning_plane/pages/3_Calibration.py)
```

## Validation Checks
1. `fp_rate` is monotonically non-increasing and `fn_rate` monotonically
   non-decreasing as the threshold rises — checked against a 37-point sweep
   (0.05–0.95): at 0.05, fp_rate=0.94/fn_rate=0.0; at 0.90, fp_rate=0.0/fn_rate=0.98.
2. `cost_per_1k` is U-shaped, not monotonic — its minimum in the current seed sits
   near threshold ≈0.25 ($19.88/1k) versus the current locked 0.30 ($27.79/1k), which
   is the actual proposal the demo should walk through.
3. `current_locked_threshold()` asserts all 3 bundles agree on `low_band` — this is a
   real invariant of the locking system, not a demo convenience.
4. The "Propose" write path (hash computation + JSON append) verified directly
   (bare-mode script execution never triggers a Streamlit button click), separate from
   the page's own clean bare-mode and live-server smoke tests.
