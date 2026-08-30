# Project Context: ControlPlane.ai

## 1. Project Overview
* **Name / Track:** ControlPlane.ai (Accenture Innovation Challenge, Track 1: Responsible AI Checker)
* **Core Purpose:** A real-time, multi-tier safety and governance gateway that intercepts GenAI requests and responses, decoupling policy configuration (YAML) from runtime enforcement code.
* **Architecture:** Three-Plane Design:
  1. **Control Plane:** Human speed. Merges layered YAMLs, enforces field-level locking, validates structural coherence, hashes, and compiles immutable `bundle.json` files.
  2. **Data Plane:** Milliseconds. Gateway pipeline wrapping upstream LLMs (BYOK) with a tiered checking cascade ($T0 \rightarrow T1 \rightarrow \text{optional } T2$), semantic caching, stream monitoring, risk fusion, and telemetry logging.
  3. **Learning Plane:** Offline/Asynchronous. Cryptographic hash-chained audit ledger, offline shadow evaluation (false-negative estimation), calibration sweep tradeoff curves, and reviewer queue.

---

## 2. Agent Collaboration, `.agents` Workflow & Skill Management
* **Multi-Agent Persistence Model:** 
  * LLM sessions are stateless; repo-level documentation serves as the persistent memory and Single Source of Truth (SSOT).
  * Agent instructions and tool capabilities are indexed in the root `.agents` manifest file.
* **Skill Encapsulation (`.agents/skills/*.md`):**
  * Modular technical workflows are documented as discrete skill files (e.g., `.agents/skills/compile_bundle.md`, `.agents/skills/run_shadow_eval.md`, `.agents/skills/verify_ledger.md`).
  * When an agent discusses, tests, or finalizes a new capability, the agent must update `.agents` and create/update the corresponding `skill.md` file.
  * Every skill file must detail: **Trigger / Objective**, **Input/Output Contracts**, **Execution CLI Commands**, and **Validation Checks**.

---

## 3. Target Profiles & Bundle Personas
The system enforces different risk postures using identical gateway code driven entirely by loaded bundles. Latency differs as a *consequence* of `t2_enabled`, not as a configured budget:

* **Profile A: Customer Support Bot (`customer_support.yaml`)**
  * `t2_enabled`: `false`
  * `pii_mode`: `redact-and-proceed`
  * `fail_mode`: `fail_open` (with warning banner)
  * `cache_threshold`: $0.90$

* **Profile B: Regulated Decision Support (`decision_support.yaml`)**
  * `t2_enabled`: `true`
  * `pii_mode`: `block-and-explain`
  * `fail_mode`: `fail_closed`
  * `caching`: disabled

* **Profile C: Internal Copilot (`internal_copilot.yaml`)**
  * `pii_mode`: `warn-and-confirm`
  * `fail_mode`: `fail_open`

---

## 4. Data Plane Pipeline Execution Order
1. **Gateway Loader:** Resolves tenant scope, loads bundle by `policy_hash`.
2. **Input Gate:** Prompt injection check + in-flight PII scanner (`redact-and-proceed` / `block-and-explain`).
3. *(Semantic cache and complexity router are specified but out of scope for the prototype.)*
4. **Model Call (BYOK):** Calls upstream model. The response is **buffered, not streamed** — the tiered cascade cannot `BLOCK`, `REDACT` or `REGENERATE` text already delivered to the client.
5. **Tiered Output Checks:**
   * **T0 (Deterministic, ~5ms, ₹0):** Checksum ID validation (Luhn, Verhoeff), canary token scan, secret scan, blocklists.
   * **T1 (Parallel Heuristics, ~40ms, ₹0.002):** NER PII detector, embedding grounding check against source chunks, toxicity classifier.
   * **T2 (LLM-as-a-Judge, ~600ms, ₹0.15):** Triggered only if `t2_enabled: true` AND fused risk is inside $[low\_band, high\_band]$.
6. **Risk Fusion & Action Engine:** Graded ladder (`ALLOW`, `REDACT`, `REGENERATE`, `FLAG`, `BLOCK`). A flagged input contracts the bands via `input_risk_tightening`.
7. **Session State Tracker:** The tenant owns the conversation; the gateway owns the risk posture. Findings are deduped by message content hash, so one attack replayed across turns counts once.
8. **Ledger Emission:** Writes record to hash-chained audit ledger (`request_id`, `policy_hash`, `detector_scores`, `action`, `cost`, `latency_ms`).

---

## 5. Key Implementation Rules
* **No Hardcoded Constants:** All thresholds (`pii_threshold`, `grounding_threshold`, `low_band`, `high_band`) must be loaded dynamically from `bundle.json`.
* **Strict Privacy in Storage:** Never store raw sensitive PII or unredaction maps in the audit ledger—log entity types, character spans, and confidence scores only.
* **Locking Enforcement:** Downstream policy layers can make locked fields stricter, never looser.
* **No Latency Negotiation:** There is no `latency_budget_ms`. Latency is measured and reported per stage, never traded against safety; `t2_enabled` is the only depth knob. See `docs/NO_LATENCY_BUDGET.md`.
* **Fail Mode Compliance:** Detector *exceptions* and model-call failures fall back to the bundle's `fail_open` or `fail_closed` setting. `fail_mode` is set and locked in the baseline, so tenants may only tighten it.
* **Explicit Change Transparency:** Whenever an agent makes structural implementation changes, library/framework shifts (e.g., using `@dataclass` instead of `Pydantic`), or refactoring decisions, the agent MUST explicitly and **boldly** state the change in their response to the user.

---
