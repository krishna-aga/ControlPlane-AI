# Skill: Run Shadow Evaluation (`run_shadow_eval`)

## Trigger / Objective
Offline false-negative rate estimation. Sample a fraction (e.g., ~2%) of historical `ALLOWED` traffic from the audit ledger and run counterfactual T2 (LLM-as-a-Judge) evaluations to measure undetected safety misses.

## Input & Output Contracts
* **Inputs:**
  * `ledger_path` (str): Path to audit ledger file/table.
  * `sample_rate` (float): Sampling ratio (default: `0.02`).
  * `judge_model` (str): Model identifier for T2 judge.
* **Outputs:**
  * `ShadowEvalResult`: Total sampled, total judge disagreements, estimated false negative rate (FNR), estimated miss cost impact.

## Execution CLI Commands
```bash
python -m learning_plane.shadow_eval \
  --ledger logs/audit_ledger.jsonl \
  --sample-rate 0.02 \
  --out reports/shadow_eval_summary.json
```

## Validation Checks
1. Sampling must be strictly random over `ALLOWED` requests, without selection bias.
2. Shadow eval must run entirely offline asynchronously without blocking live traffic or user responses.
