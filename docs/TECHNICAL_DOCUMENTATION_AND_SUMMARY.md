# Technical Documentation & Handover Summary: ControlPlane.ai

## 1. Project & Architectural Overview

* **Competition Track:** Accenture Innovation Challenge 2026 (Round 2, Problem Track 1: **ControlPlane.ai**)
* **Core Purpose:** A real-time, multi-tier Responsible AI safety and governance gateway that intercepts Generative AI requests and responses, decoupling policy configuration (YAML) from runtime enforcement code.
* **Core Architecture:** 3-Plane System Design:
  1. **Control Plane (Completed):** Human speed. Parses layered YAML policies, enforces field-level locking rules, validates latency budgets, computes deterministic SHA-256 policy hashes, and compiles immutable `bundle.json` files.
  2. **Data Plane (Next Up):** Millisecond latency. Gateway pipeline wrapping upstream LLMs (BYOK) with a tiered checking cascade ($T0 \rightarrow T1 \rightarrow \text{optional } T2$), semantic caching, stream monitoring, risk fusion, and telemetry logging.
  3. **Learning Plane (Planned):** Offline/Asynchronous. Cryptographic hash-chained audit ledger, offline shadow evaluation (false-negative estimation), calibration sweep tradeoff curves, and reviewer queue.

---

## 2. Completed Phase: Control Plane Implementation

### A. Policy Hierarchy (`policies/`)
* **`policies/org_baseline.yaml`**: Enterprise baseline rules. Locks `pii_threshold: 0.8` and `grounding_threshold: 0.6` in `locked_fields`.
* **`policies/customer_support.yaml`**: Profile A (High throughput bot). Latency budget $200\text{ ms}$, `t2_enabled: false`, `pii_mode: redact-and-proceed`, `fail_mode: fail_open`.
* **`policies/decision_support.yaml`**: Profile B (Regulated tool). Latency budget $800\text{ ms}$, `t2_enabled: true`, `pii_mode: block-and-explain`, `fail_mode: fail_closed`, strict `pii_threshold: 0.5`.
* **`policies/internal_copilot.yaml`**: Profile C (Employee assistant). Latency budget $400\text{ ms}$, `pii_mode: warn-and-confirm`, `fail_mode: fail_open`.

### B. Control Plane Package (`control_plane/`)
* **`control_plane/models.py`**: Built using **Pydantic v2** (`BaseModel`, `Field`). Defines `PolicyConfig`, `BundleConfig`, and custom exceptions (`PolicyLockingError`, `LatencyBudgetExceededError`).
* **`control_plane/resolver.py`**: 2-Tier policy inheritance engine. Implements **Strict Exception Field Locking** (attempting to loosen a locked field raises `PolicyLockingError`; setting a stricter value is allowed). Validates cumulative detector latency ($T0 \approx 5\text{ms}$, $T1 \approx 40\text{ms}$, $T2 \approx 600\text{ms}$) against `latency_budget_ms`.
* **`control_plane/compiler.py`**: CLI & programmatic API (`compile_bundle()`). Computes deterministic SHA-256 `policy_hash` over canonical JSON parameters and outputs compiled bundles to `bundles/<use_case>_bundle.json`.

### C. Compiled Bundles (`bundles/`)
Generated, immutable JSON bundles ready for Data Plane loading:
* `bundles/customer_support_bundle.json` (`policy_hash: 86aab9a168bbb66f26768340f6a6bbb1d1530c6e444deba23bce84ecc61bb67f`)
* `bundles/decision_support_bundle.json` (`policy_hash: 641fe1612a860997445fc8773ae5113fbe2c9374bb23d1d31e585e3a430da716`)
* `bundles/internal_copilot_bundle.json` (`policy_hash: f1123299b9d788c81b76feb08c2ddd67fa89f610678fd694da189cd08e62b76e`)

> Hashes changed twice: first when the bundles gained `detector_critical_thresholds`,
> `t0_severity_scores`, and the T0/T1 detector configuration fields
> (see `docs/POLICY_LOCKING_AND_RISK_NORMALIZATION.md`), and again when the
> `policy_hash` reproducibility defect was fixed (see below). The values above are the
> first ones that are actually reproducible — recompiling now returns the same hash.

> **`policy_hash` reproducibility (fixed).** `locked_fields` was built with
> `list(set(...))`. Python randomizes string hashing per process, so the list order
> changed on every run, and `json.dumps(sort_keys=True)` sorts dict keys but **not**
> list elements — so identical policy inputs produced a different SHA-256 on every
> compilation. The three hashes previously recorded here could never be reproduced.
> `resolver.py` now uses `sorted(set(...))`, making `locked_fields` canonical and the
> hash stable across processes. This is the guarantee that load-by-`policy_hash`,
> the semantic cache key, and ledger policy attestation all depend on.

### D. Automated Test Suite (`tests/test_control_plane.py`)
Run via `.venv/bin/python3 -m unittest discover -s tests`:
* `test_compile_all_personas`: Compiles all 3 personas cleanly.
* `test_strict_locking_exception`: Verifies `PolicyLockingError` when attempting to loosen a locked field (`pii_threshold: 0.9` vs baseline `0.8`).
* `test_stricter_override_allowed`: Verifies that a stricter override (`pii_threshold: 0.5`) is allowed.
* `test_latency_budget_exceeded`: Verifies `LatencyBudgetExceededError` when $200\text{ ms}$ budget cannot fit enabled T2 judge ($645\text{ ms}$ required).
* `test_hash_reproducibility`: Asserts identical parameters produce identical SHA-256 hashes.
* `TestLockingDirection` (5 tests): Regression cover for **P1** — `grounding_threshold` is a similarity *floor*, so raising it is stricter; the lock was previously inverted. Also covers newly-locked `toxicity_threshold` and enum tightening.
* `TestStructuralValidators` (4 tests): Rejects inverted risk bands, `detector_weights` not summing to $1.0$, and critical thresholds below the detection midpoint.
* `TestHashDeterminism` (3 tests): Regression cover for the `policy_hash` reproducibility
  defect. Compiles each persona in **subprocesses under five different `PYTHONHASHSEED`
  values** and asserts a single hash, asserts `locked_fields` is canonically ordered, and
  asserts each committed bundle's stored `policy_hash` still recompiles to itself
  (`compile_bundle.md` Validation Check 3). `test_hash_reproducibility` could not catch
  this — it hashes two hand-written dicts inside one interpreter, and the defect only
  appears across processes.
* **Test Status:** 17/17 tests passing.

---

## 3. Environment & Agent Rules

### Python & IDE Setup
* **Virtual Environment (`.venv`)**: Located at `.venv/`. Dependencies: `pyyaml>=6.0`, `pydantic>=2.0`.
* **IDE Configuration**: `.vscode/settings.json` and `pyrightconfig.json` configure Pyright / VS Code language server to use `.venv/bin/python` to avoid IDE linting errors.

### Mandatory Agent Operational Rules
1. **Single Source of Truth (SSOT):** Always consult `context.md` and `.agents/skills/*.md` before modifying code.
2. **Explicit Change Transparency:** Whenever an agent makes structural implementation changes, library/framework shifts (e.g. switching libraries), or refactoring decisions, the agent MUST explicitly and **boldly** state the exact change in their response to the user.
3. **Stateless Compliance:** Ensure no hardcoded policy constants exist in Data Plane code; all thresholds must be loaded dynamically from `bundle.json`.
4. **Privacy First:** Never store raw sensitive PII or unredaction maps in audit logs—log entity types, character spans, and confidence scores only.

---

## 4. Handover Blueprint for Next Phase: Data Plane Implementation

The incoming agent will build the **Data Plane** layer in `data_plane/gateway.py` with entry point `process_request(prompt, context_docs, bundle_path, session_id)`:

1. **Gateway Loader:** Load & cache `bundle.json` by `policy_hash`.
2. **Input Gate:** Prompt injection check + PII scanner (`redact-and-proceed` / `block-and-explain` / `warn-and-confirm`).
3. **Semantic Cache:** Keyed by `(tenant_id, scope, policy_hash, embedding)`. Return in $\sim 8\text{ ms}$ for non-volatile queries with zero PII detections.
4. **Complexity Router:** Down-route simple tasks if permitted by bundle (`allow_downrouting: true`).
5. **Model Call & Stream Monitor:** Wrap upstream LLM (BYOK); token runaway and repetition loop cut-off active.
6. **Tiered Output Cascade:**
   * **T0 (Deterministic, ~5ms, ₹0):** Luhn/Verhoeff checksums, canary tokens, blocklists.
   * **T1 (Parallel Heuristics, ~40ms, ₹0.002):** Async NER PII, Sentence-Transformers grounding check, toxicity classifier.
   * **T2 (LLM-as-a-Judge, ~600ms, ₹0.15):** Triggered only if `t2_enabled == True` AND fused risk is inside $[low\_band, high\_band]$.
7. **Risk Fusion & Graded Action Engine:** Map fused risk score to ladder: `ALLOW` | `REDACT` | `REGENERATE` | `FLAG` | `BLOCK`.
8. **Session State Tracker:** Track multi-turn flags and rework counters.
9. **Ledger Emission:** Log interaction row to hash-chained audit ledger.

---

## 5. Quick CLI Commands Cheatsheet for New Agent

```bash
# Activate virtual environment
source .venv/bin/activate

# Run Control Plane unit tests
.venv/bin/python3 -m unittest discover -s tests -p "test_*.py"

# Recompile a policy bundle
.venv/bin/python3 -m control_plane.compiler \
  --base policies/org_baseline.yaml \
  --policy policies/customer_support.yaml \
  --out bundles/customer_support_bundle.json
```
