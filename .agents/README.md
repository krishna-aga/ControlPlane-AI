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
| [`run_shadow_eval.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/run_shadow_eval.md) | False-Negative Shadow Estimator | Counterfactually samples ALLOWED traffic to run offline T2 judging. |
| [`verify_ledger.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/verify_ledger.md) | Ledger Integrity Verifier | Verifies cryptographic `prev_hash` chain integrity across audit log records. |
