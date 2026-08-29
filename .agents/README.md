# .agents Directory & Multi-Agent Skill Index

This directory maintains persistent memory, skill specifications, and agent operational guidelines for the **ControlPlane.ai** codebase.

## Directory Structure
```
.agents/
├── README.md                 # Agent manifest & skills index
└── skills/                   # Modular technical skill specifications
    ├── compile_bundle.md      # Control Plane policy compilation & locking
    ├── execute_data_plane.md  # Real-time Data Plane execution pipeline
    ├── run_shadow_eval.md     # Learning Plane false-negative counterfactual estimation
    └── verify_ledger.md       # Audit ledger cryptographic hash-chain verification
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
| [`execute_data_plane.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/execute_data_plane.md) | Data Plane Gateway Pipeline | Intercepts requests, executes T0/T1/T2 checks, semantic caching, and risk fusion. |
| [`run_shadow_eval.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/run_shadow_eval.md) | False-Negative Shadow Estimator | Counterfactually samples ALLOWED traffic to run offline T2 judging. |
| [`verify_ledger.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/verify_ledger.md) | Ledger Integrity Verifier | Verifies cryptographic `prev_hash` chain integrity across audit log records. |
