# .agents Directory & Multi-Agent Skill Index

This directory maintains persistent memory, skill specifications, and agent operational guidelines for the **ControlPlane.ai** codebase.

## Directory Structure
```
.agents/
├── README.md                 # Agent manifest & skills index
└── skills/                   # Modular technical skill specifications
    ├── compile_bundle.md      # Control Plane policy compilation & locking
    ├── execute_data_plane.md  # Real-time Data Plane execution pipeline
    ├── t0_deterministic_checks.md  # Tier 0 canary / checksum / secret / blocklist checks
    ├── run_shadow_eval.md     # Learning Plane false-negative counterfactual estimation
    ├── verify_ledger.md       # Audit ledger cryptographic hash-chain verification
    ├── seed_demo_ledger.md    # Synthetic demo ledger for the Learning Plane Streamlit UI
    ├── reviewer_queue.md      # Uncertainty-ranked human review queue for FLAG rows
    ├── calibration.md         # Threshold sweep tradeoff curve + proposal record
    ├── closing_the_loop.md    # Loop diagram (propose→approve→…) + stakeholder KPI row
    └── prompt injection/      # Input Gate sub-skills
        ├── text_normalization.md
        ├── input_gate_heuristics.md
        └── ml_classifier.md
```

## Agent Operational Rules
1. **Single Source of Truth (SSOT):** Always consult `context.md` and `.agents/skills/*.md` before modifying or extending architecture.
2. **Skill Registration:** Whenever a new capability, workflow, or CLI command is introduced, update this index and create/update the corresponding `.agents/skills/<skill_name>.md`.
3. **Stateless Compliance:** Ensure no hardcoded policy constants exist in the Data Plane code; all settings must be dynamically read from `bundle.json`.
4. **Privacy First:** Ensure audit logs only record scores, entity types, and character spans—never raw PII values.
5. **Explicit Change Transparency:** Whenever an agent makes structural implementation changes, library/framework shifts (e.g., using `@dataclass` instead of `Pydantic`), or refactoring decisions, the agent MUST explicitly and **boldly** state the change in their response to the user.

## Skill Index
| Skill File | Description | Trigger / Objective |
| :--- | :--- | :--- |
| [`compile_bundle.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/compile_bundle.md) | Control Plane Bundle Compiler | Resolves layered YAML policies, enforces field-level locking, hashes SHA-256 bundle. |
| [`execute_data_plane.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/execute_data_plane.md) | Data Plane Gateway Pipeline | **Implemented** — see [`docs/GATEWAY.md`](file:///home/krishna/Projects/ControlPlane/docs/GATEWAY.md). Signature superseded; T1/T2, cache and router not built. |
| [`t0_deterministic_checks.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/t0_deterministic_checks.md) | Tier 0 Deterministic Output Checks | **Implemented** — see [`docs/TIER_0.md`](file:///home/krishna/Projects/ControlPlane/docs/TIER_0.md) for the two defects found during implementation (T0-9 unreachable dictionary guard, T0-10 entropy threshold recall). |
| [`prompt injection/text_normalization.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/prompt%20injection/text_normalization.md) | Input Text Normalization | Canonicalizes prompts: base64/hex decode, NFKC, invisible-char strip, leetspeak mapping. |
| [`prompt injection/input_gate_heuristics.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/prompt%20injection/input_gate_heuristics.md) | Input Gate Heuristic Scanner | **SUPERSEDED** by [`docs/INPUT_GATE.md`](file:///home/krishna/Projects/ControlPlane/docs/INPUT_GATE.md) — implemented in `data_plane/`. Retained for provenance. |
| [`prompt injection/ml_classifier.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/prompt%20injection/ml_classifier.md) | Input Gate ML Classifier | DeBERTa/MiniLM semantic injection scoring for prompts that evade static regex. |
| [`t1_detectors`](file:///home/krishna/Projects/ControlPlane/docs/TIER_1.md) | Tier 1 Grounding & Toxicity | **Implemented** — see [`docs/TIER_1.md`](file:///home/krishna/Projects/ControlPlane/docs/TIER_1.md). `data_plane/detectors/grounding.py` (real MiniLM) and `toxicity.py` (real toxic-bert), wired into `data_plane/gateway.py`. `grounding_threshold` remains deliberately uncalibrated (T1-3); bias remains declared, not built (§4). T1-7/T1-8/T1-9 fixed — see doc §5, §3E. |
| [`semantic_cache`](file:///home/krishna/Projects/ControlPlane/docs/SEMANTIC_CACHE.md) | Semantic Response Cache | **Implemented** — see [`docs/SEMANTIC_CACHE.md`](file:///home/krishna/Projects/ControlPlane/docs/SEMANTIC_CACHE.md). A hit skips the model call and the expensive tiers, never Tier 0. Closed a live policy hole: `cache_threshold` shipped unlocked and `internal_copilot` was loosening it. |
| [`run_shadow_eval.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/run_shadow_eval.md) | False-Negative Shadow Estimator | **Implemented** — see [`learning_plane/pages/2_Shadow_Eval.py`](file:///home/krishna/Projects/ControlPlane/learning_plane/pages/2_Shadow_Eval.py). Mock T2 judge (real T2 not built); calibration page not built. |
| [`verify_ledger.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/verify_ledger.md) | Ledger Integrity Verifier | Verifies cryptographic `prev_hash` chain integrity across audit log records. |
| [`seed_demo_ledger.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/seed_demo_ledger.md) | Learning Plane Demo Ledger Seeder | **Implemented** — see [`learning_plane/app.py`](file:///home/krishna/Projects/ControlPlane/learning_plane/app.py). |
| [`reviewer_queue.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/reviewer_queue.md) | Learning Plane Reviewer Queue | **Implemented** — see [`learning_plane/pages/1_Reviewer_Queue.py`](file:///home/krishna/Projects/ControlPlane/learning_plane/pages/1_Reviewer_Queue.py). |
| [`calibration.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/calibration.md) | Learning Plane Calibration | **Implemented** — see [`docs/LEARNING_PLANE.md`](file:///home/krishna/Projects/ControlPlane/docs/LEARNING_PLANE.md). Proposals are recorded only; `low_band` stays locked and no bundle/policy file is written. |
| [`closing_the_loop.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/closing_the_loop.md) | Learning Plane Loop Diagram & KPI Row | **Implemented (demo scope)** — see [`docs/LEARNING_PLANE.md`](file:///home/krishna/Projects/ControlPlane/docs/LEARNING_PLANE.md). Steps ①–③ are real/preview; ④–⑤ (shadow deploy, enforce) are informational only, not implemented. |
