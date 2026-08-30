# Skill: Run Shadow Eval (`run_shadow_eval`)

## Trigger / Objective
Estimate the fast pipeline's false-negative rate offline: re-judge a random ~2%
sample of ALLOWED decisions with a mock T2 judge (T2 itself isn't built — see
`docs/TIER_1.md`) and report the disagreement rate. Sampling must be random, never
drawn from "interesting" traffic, or the estimate is meaningless. The result is a
noisy estimate, not ground truth — the UI must always show that caveat.

## Input & Output Contracts
* **Inputs:** none required for a fresh run (`learning_plane/shadow_eval.py` generates
  its own fixed ~200-row audit sample rather than deriving it from the small 750-row
  demo ledger, where 2% of ALLOW rows is under 10 — too thin to chart). CLI flags:
  `--n` (default 200), `--days` (14), `--seed` (7), `--judge-seed`, `--sample-out`,
  `--results-out`.
* **Outputs:** `learning_plane/data/shadow_sample.json` (the sampled ALLOW-shaped
  rows) and `learning_plane/data/shadow_eval_results.json` (`sample_size`,
  `flagged_count`, `estimated_fnr`, and each row's judge verdict). No raw PII values,
  canary tokens, or prompt/response text — same structural privacy rule as the ledger.

## Execution CLI Commands
```bash
python -m learning_plane.shadow_eval --n 200 --seed 7
streamlit run learning_plane/app.py
# then open "Shadow Eval" in the sidebar (learning_plane/pages/2_Shadow_Eval.py)
```

## Validation Checks
1. `estimated_fnr == flagged_count / sample_size`, always in [0, 1].
2. Judge catch probability is detector-specific and near-edge-biased by construction
   (`BASE_CATCH_PROB`, `NEAR_EDGE_BONUS`) — grounding checked as the top disagreement
   source across 5 different seeds (7, 99, 12345, 555, 1) before shipping, since the
   original weights left the ranking within sampling noise at n≈50/detector.
3. Streamlit page's "Re-run shadow eval" button draws a fresh random seed each click
   and updates all KPIs/chart/table together — verified via direct script execution
   (exit 0, no errors) and a live headless server smoke test.
