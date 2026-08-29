# Risk Fusion Engine & Action Decision Specification: ControlPlane.ai

## 1. Overview & Core Architecture

The **Risk Fusion Engine** is the core decision-making component of the **Data Plane Pipeline**. It receives normalized safety risk signals from all execution tiers ($T0 \rightarrow T1 \rightarrow \text{optional } T2$), dynamically combines them using persona-specific bundle parameters, applies multi-turn session escalation penalties, and maps the final fused risk score to a graded action on the enforcement ladder (`ALLOW`, `WARN`, `REDACT`, `REGENERATE`, `BLOCK`).

Crucially, **no risk thresholds or detector weights are hardcoded in python enforcement logic**. All parameters are resolved by the Control Plane and dynamically loaded from the compiled `bundle.json`.

---

## 2. Multi-Tier Risk Calculation Model

The risk calculation follows a **3-step mathematical process**:

```text
    ┌──────────────────────────────────────────────────────────┐
    │ 1. Individual Detector Output Normalization (S_i ∈ [0,1])│
    └─────────────────────────────┬────────────────────────────┘
                                  │
                                  ▼
    ┌──────────────────────────────────────────────────────────┐
    │ 2. Dynamic Weighted Linear Fusion (R_base = Σ w_i * S_i)  │
    └─────────────────────────────┬────────────────────────────┘
                                  │
                                  ▼
    ┌──────────────────────────────────────────────────────────┐
    │ 3. Multi-Turn Session Escalation & Hard Overrides        │
    └─────────────────────────────┬────────────────────────────┘
                                  │
                                  ▼
                    Final Fused Risk Score (R_fused)
```

### Step 1: Normalized Detector Scores ($S_i \in [0.0, 1.0]$)

Each detector produces a score normalized between $0.0$ (completely safe) and $1.0$ (critical safety breach):

1. **Input Gate Prompt Injection Score ($S_{\text{inj}}$)**:
   * $S_{\text{inj}} = 0.0$: Clean prompt input.
   * $S_{\text{inj}} = 0.4 \text{ to } 0.5$: Sanitized or warned input.
   * $S_{\text{inj}} = 1.0$: Direct instruction override or jailbreak pattern detected.

2. **Tier 0 Deterministic Checkers ($S_{\text{T0}}$)**:
   * Binary score ($0.0$ or $1.0$) evaluating Luhn/Verhoeff checksums, API secret leaks, canary tokens, and blocklists.

3. **Tier 1 PII Detector ($S_{\text{PII}}$)**:
   * Evaluated relative to the bundle's `pii_threshold` ($T_{\text{pii}}$):
     $$S_{\text{PII}} = \min\left(1.0, \frac{\text{PII Confidence Score}}{T_{\text{pii}}}\right)$$

4. **Tier 1 Grounding / Hallucination Check ($S_{\text{grounding}}$)**:
   * Measures embedding distance between LLM output and source context chunks ($C_{\text{sim}}$) relative to `grounding_threshold` ($T_{\text{ground}}$):
     $$S_{\text{grounding}} = 1.0 - \min\left(1.0, \frac{C_{\text{sim}}}{T_{\text{ground}}}\right)$$

5. **Tier 1 Toxicity Classifier ($S_{\text{toxicity}}$)**:
   * Model probability $P(\text{toxic})$ normalized against `toxicity_threshold` ($T_{\text{tox}}$):
     $$S_{\text{toxicity}} = \min\left(1.0, \frac{P(\text{toxic})}{T_{\text{tox}}}\right)$$

6. **Tier 2 LLM-as-a-Judge ($S_{\text{T2}}$)**:
   * Evaluated only when $T2$ is enabled and $R_{\text{base}}$ falls in the gray zone $[ \text{low\_band}, \text{high\_band} ]$.

---

### Step 2: Dynamic Weighted Fusion ($R_{\text{base}}$)

The base risk score is computed as a weighted linear combination using the `detector_weights` defined in `bundle.json`:

$$R_{\text{base}} = \sum_{i} w_i \cdot S_i \quad \text{where } \sum w_i = 1.0$$

#### Default Weight Allocation (from `org_baseline.yaml`):
```json
{
  "detector_weights": {
    "t0": 0.4,
    "pii": 0.2,
    "grounding": 0.2,
    "toxicity": 0.2
  }
}
```

---

### Step 3: Multi-Turn Session Escalation & Hard Short-Circuits

To calculate the final **Fused Risk Score** ($R_{\text{fused}}$):

1. **Session State Penalty ($\alpha_{\text{session}}$)**:
   The Session State Tracker monitors multi-turn interactions for `session_id`. If prior turns incurred flags or rework counters ($N_{\text{flags}}$), a penalty factor is added:
   $$R_{\text{fused}} = \min\left(1.0, R_{\text{base}} + \alpha_{\text{session}} \cdot N_{\text{flags}}\right)$$

2. **Hard Short-Circuit Override**:
   If any Tier 0 check fails ($S_{\text{T0}} = 1.0$) or an unrecoverable input gate injection is detected ($S_{\text{inj}} = 1.0$), $R_{\text{fused}}$ **immediately hard-overrides to $1.0$**.

---

## 3. Risk-to-Action Decision Matrix

The final $R_{\text{fused}}$ score is compared against the bundle's `low_band` (default `0.3`) and `high_band` (default `0.7`):

```text
  0.0                                low_band (0.3)                  high_band (0.7)                 1.0
  ├──────────────────────────────────────┼───────────────────────────────┼──────────────────────────────┤
  │             ALLOW                    │         WARN / T2 JUDGE       │    REDACT / REGEN / BLOCK    │
  └──────────────────────────────────────┴───────────────────────────────┴──────────────────────────────┘
```

$$
\text{Enforcement Action} = 
\begin{cases} 
\mathbf{ALLOW} & \text{if } R_{\text{fused}} < \text{low\_band} \\
\mathbf{WARN} \text{ / Trigger } T2 & \text{if } \text{low\_band} \le R_{\text{fused}} < \text{high\_band} \\
\mathbf{REDACT} \text{ / } \mathbf{REGENERATE} \text{ / } \mathbf{BLOCK} & \text{if } R_{\text{fused}} \ge \text{high\_band}
\end{cases}
$$

### Action Summary

* **`ALLOW`**: Output is clean. Delivered directly to user.
* **`WARN`**: Gray zone. Triggers Tier 2 LLM-as-a-Judge if budget permits; otherwise appends a warning banner and logs telemetry.
* **`REDACT`**: Sensitive PII spans are masked before returning output.
* **`REGENERATE`**: Ungrounded response triggers a single internal retry against upstream LLM.
* **`BLOCK`**: Output execution is suppressed, returning an error/explanation banner.

---

## 4. Persona Policy Mapping

| Persona | Latency Budget | `t2_enabled` | `pii_mode` | `fail_mode` | `low_band` | `high_band` |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Profile A: Customer Support** (`customer_support.yaml`) | $200\text{ ms}$ | `false` | `redact-and-proceed` | `fail_open` | 0.3 | 0.7 |
| **Profile B: Regulated Decision Support** (`decision_support.yaml`) | $800\text{ ms}$ | `true` | `block-and-explain` | `fail_closed` | 0.3 | 0.7 |
| **Profile C: Internal Copilot** (`internal_copilot.yaml`) | $400\text{ ms}$ | `false` | `warn-and-confirm` | `fail_open` | 0.3 | 0.7 |

---

## 5. Source Code & Specification Traceability

* **Baseline Policies**: [`policies/org_baseline.yaml`](file:///home/krishna/Projects/ControlPlane/policies/org_baseline.yaml)
* **Pydantic Schemas**: [`control_plane/models.py`](file:///home/krishna/Projects/ControlPlane/control_plane/models.py) (`PolicyConfig`, `BundleConfig`)
* **Input Gate Specifications**: [`.agents/skills/prompt injection/input_gate_heuristics.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/prompt%20injection/input_gate_heuristics.md)
* **Data Plane Pipeline**: [`.agents/skills/execute_data_plane.md`](file:///home/krishna/Projects/ControlPlane/.agents/skills/execute_data_plane.md)
