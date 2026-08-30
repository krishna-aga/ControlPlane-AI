# ControlPlane.ai — Architecture & Implementation Status

ControlPlane.ai is a real-time, multi-tier Responsible AI governance gateway. It sits
between an application and its upstream LLM, enforcing safety and compliance policy on
every request and response — while keeping *policy* (what the rules are) and
*enforcement* (applying them to a live request) as separate, independently evolving
systems. That separation is the architecture in [`image.png`](../image.png): a
**Control plane** that decides the rules at human speed, a **Data plane** that applies
them in milliseconds, and a **Learning plane** that watches the results offline and
feeds calibration back to the Control plane.

This folder documents, box by box, how each part of that diagram is implemented today.

| Document | Covers |
|---|---|
| [`01_CONTROL_PLANE.md`](01_CONTROL_PLANE.md) | Policy authoring, the resolver, and distribution |
| [`02_DATA_PLANE.md`](02_DATA_PLANE.md) | The gateway, input gate, semantic cache, complexity router, model call, tiered output checks, risk fusion, and session state |
| [`03_LEARNING_PLANE.md`](03_LEARNING_PLANE.md) | The audit ledger, reviewer queue, shadow evaluation, calibration, and the feedback loop back to Control |
| [`04_ROADMAP.md`](04_ROADMAP.md) | What is designed but not yet built, and why |

## Implementation status at a glance

Every box in the diagram maps to real, tested code. A subset are narrower in this
build than their one-line diagram caption implies — most often because we made a
deliberate scoping call under a competition timeline, and in a couple of cases because
we found and documented a real defect rather than shipping it silently. The table
below is a map of exactly where each box stands; the roadmap doc explains each
narrowing in full.

| Plane | Capability | Status |
|---|---|---|
| Control | Policy authoring (layered YAML) | 🟢 Built — two-layer inheritance (org baseline → use case) ships; a third jurisdiction layer is scoped but not yet wired in |
| Control | Resolver (field-level locking) | 🟢 Built — locking is enforced and tested; a latency-budget validator was deliberately removed as a design decision (see roadmap) |
| Control | Distribution (version + hash) | 🟡 Built — deterministic versioning and content hashing ship; shadow deploy and rollback are designed, not yet built |
| Data | Gateway | 🟡 Built — tenant scoping and policy-bundle loading are enforced; authentication is out of scope for this build |
| Data | Input gate | 🟢 Built — prompt-injection scanning and in-place PII redaction both run end to end |
| Data | Semantic cache | 🟢 Built — tenant/scope/policy-aware caching with safety gating ships; today's embedder is a fast lexical hash rather than a trained model |
| Data | Complexity router | 🔴 Planned — the policy field exists; the routing logic itself is next up |
| Data | Model call (BYOK) | 🟡 Built — real upstream calls with retry/backoff ship; streaming and a stream-time runaway monitor are planned |
| Data | Tiered output checks | 🟡 Built — T0 (rules) and T1 (real ML models for grounding/toxicity) both run; T2 (LLM-as-judge) is planned |
| Data | Risk fusion & action engine | 🟢 Built — the full allow/redact/regenerate/flag/block ladder and fail-open/closed both work |
| Data | Session state | 🟢 Built |
| Learning | Audit ledger | 🟢 Built — genuinely hash-chained and tamper-evident; runs against seeded demo data pending live traffic volume |
| Learning | Reviewer queue | 🟢 Built |
| Learning | Shadow evaluation | 🟡 Built as a preview — demonstrates the mechanism using a calibrated mock judge, since T2 itself is still planned |
| Learning | Calibration | 🟢 Built — real threshold-sweep analysis and a proposal workflow ship |
| Learning | Feedback loop → Control plane | 🟡 Built through human approval; minting a new version and shadow-deploying it are the next milestone |

**Legend:** 🟢 fully built and tested · 🟡 built, with a scoped-out portion · 🔴 designed, not yet built

See [`04_ROADMAP.md`](04_ROADMAP.md) for the reasoning behind every 🟡 and 🔴, and what
completing it would take.
