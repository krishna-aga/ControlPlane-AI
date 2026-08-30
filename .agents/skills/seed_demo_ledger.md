# Skill: Seed Demo Ledger (`seed_demo_ledger`)

## Trigger / Objective
Generate a synthetic but schema-accurate audit ledger for the Learning Plane demo UI, since real gateway traffic is too sparse to screen-record from a cold start. Rows are shaped exactly like `Gateway.finish()`'s payload and hash-chained with the same `data_plane.ledger._row_hash` the real ledger uses, so `Ledger.verify()` and the tamper demo are genuine.

## Input & Output Contracts
* **Inputs:** `--per-persona` (int, default 250), `--days` (int, default 14, spread window), `--seed` (int, default 42), `--out` (path, default `learning_plane/data/demo_ledger.json`).
* **Outputs:** JSON array of hash-chained ledger rows across the three real bundle personas (`bundles/*_bundle.json`), action mix weighted per persona's actual posture (fail_mode, t2_enabled, caching_enabled).

## Execution CLI Commands
```bash
python -m learning_plane.seed_demo_ledger --per-persona 250 --out learning_plane/data/demo_ledger.json
streamlit run learning_plane/app.py
```

## Validation Checks
1. Script asserts `ledger.verify() == (True, None)` before writing — a self-check that the chain it just built is genuinely valid.
2. No raw PII values, canary tokens, or prompt/response text anywhere in generated rows — same structural privacy rule as `InputGateResult.ledger_row()` / `T0Result.ledger_row()` / `FusionResult.ledger_row()`.
3. `learning_plane/app.py` Ledger Explorer: Verify Chain must report VALID on a fresh seed, and Tamper Row must make it report CORRUPTED at the tampered index without recomputing `row_hash`.
