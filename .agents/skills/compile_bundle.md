# Skill: Compile Policy Bundle (`compile_bundle`)

## Trigger / Objective
Compile 2-tier policy configuration YAML files (`org_baseline.yaml` -> `use_case.yaml`) into a single, resolved, immutable JSON policy bundle (`bundle.json`). Enforce strict field-level locking rules and attach a cryptographic `policy_hash` (SHA-256).

## Input & Output Contracts
* **Inputs:**
  * `base_policy_path` (str): Path to baseline policy YAML (`policies/org_baseline.yaml`).
  * `policy_path` (str): Path to target use-case policy YAML (`policies/customer_support.yaml`, etc.).
  * `pin_version` (optional str): Optional version override.
* **Outputs:**
  * `bundle.json` containing: `policy_name`, `policy_version`, `policy_hash`, `compiled_at`, `latency_budget_ms`, `t2_enabled`, `pii_mode`, `fail_mode`, `allow_downrouting`, `caching_enabled`, `pii_threshold`, `grounding_threshold`, `toxicity_threshold`, `low_band`, `high_band`, `cache_threshold`, `locked_fields`, `detector_weights`.

## Execution CLI Commands
```bash
python -m control_plane.compiler \
  --base policies/org_baseline.yaml \
  --policy policies/customer_support.yaml \
  --out bundles/customer_support_bundle.json
```

## Validation Checks
1. **Strict Field Locking (Strict Exception Mode):** Ensure inner policy layers cannot loosen any field listed in `locked_fields` of parent layers. Attempting to loosen a locked sensitivity threshold (e.g. setting `pii_threshold: 0.9` when baseline locks `0.8`) raises a `PolicyLockingError` compilation exception. Setting a stricter value (e.g. `0.5`) is allowed.
2. **Latency Budget Verification:** Sum expected detector latencies ($\text{T0} + \text{T1} + \text{T2}$) and raise `LatencyBudgetExceededError` if requested budget is less than the required latency (e.g. $645\text{ ms}$ required when `t2_enabled: true`).
3. **Hash Integrity & Reproducibility:** Verify `sha256(canonical_json_without_hash)` matches `policy_hash`.
