# Skill: Execute Data Plane Pipeline (`execute_data_plane`)

## Trigger / Objective
Intercept an incoming GenAI prompt and context docs, evaluate real-time safety via a multi-tier cascade (T0 $\rightarrow$ T1 $\rightarrow$ optional T2), perform semantic caching and BYOK model calls, fuse detector scores into a graded action, and log telemetry to the immutable audit ledger.

## Input & Output Contracts
* **Inputs:**
  * `prompt` (str): Incoming user prompt.
  * `context_docs` (list[str]): Retrieved RAG chunks.
  * `bundle_path` (str): Path to loaded `bundle.json`.
  * `session_id` (str): Multi-turn session identifier.
* **Outputs:**
  * `GatewayResult` object containing:
    * `action` (str): `ALLOW` | `REDACT` | `REGENERATE` | `FLAG` | `BLOCK`
    * `response` (str): Processed LLM output text or blocked explanation message.
    * `fused_risk` (float): Combined risk score $[0.0, 1.0]$.
    * `telemetry` (dict): `request_id`, `latency_ms`, `tokens_used`, `cost_incurred`, `served_from`, `policy_hash`.

## Execution CLI Commands / Programmatic API
```python
from data_plane.gateway import process_request

result = await process_request(
    prompt="My account number is 4111-1111-1111-1111",
    context_docs=["Account policy rules..."],
    bundle_path="bundles/customer_support_bundle.json",
    session_id="sess_12345"
)
```

## Pipeline Execution Steps
1. **Load Bundle & Verify Hash:** Read `t2_enabled`, `pii_mode`, `fail_mode`, `injection_action`. There is no latency budget — see `docs/NO_LATENCY_BUDGET.md`.
2. **Input Gate:** Prompt injection check + PII redaction (`redact-and-proceed` / `block-and-explain`).
3. **Semantic Cache Check:** Check cache by key `(tenant_id, scope, policy_hash, prompt_embedding)`. Return $\sim 8\text{ ms}$ hit if valid.
4. **Upstream Model Call (BYOK):** Call model API; stream monitor cuts off runaway tokens or repetition loops.
5. **Tiered Output Cascade:**
   * **T0 (Deterministic, ~5ms):** Luhn/Verhoeff checksums, canary tokens, blocklists.
   * **T1 (Parallel Heuristics, ~40ms):** Async NER PII, Sentence-Transformers grounding check, toxicity classifier.
   * **T2 (LLM-as-a-Judge, ~600ms):** Run only if `t2_enabled == True` AND `low_band <= fused_risk <= high_band`.
6. **Risk Fusion Engine:** Compute weighted risk score and map to graded action ladder.
7. **Session State & Telemetry Emission:** Update session flag counters and write structured interaction row to audit ledger.

## Validation Checks
1. No raw PII stored in ledger output.
2. Detector *exceptions* and model-call failures trigger `bundle.fail_mode` (`fail_open` vs `fail_closed`). There are no timeouts.
3. Per-stage `latency_ms` is measured and recorded in every ledger row — reported, never negotiated.
