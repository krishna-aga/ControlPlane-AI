# ControlPlane.ai

ControlPlane.ai is a real-time, multi-tier Responsible AI governance gateway. It sits
between an application and its upstream LLM, enforcing safety and compliance policy on
every request and response, while keeping *policy* (what the rules are, authored as
layered YAML) fully decoupled from *enforcement* (applying those rules to a live
request in milliseconds). The system is organized into three planes — Control, Data,
and Learning — described in full in [`architecture/README.md`](architecture/README.md),
alongside the architecture diagram at [`docs/image.png`](docs/image.png).

Built for the Accenture Innovation Challenge 2026 (Round 2, Problem Track 1).

## Table of contents

- [Requirements](#requirements)
- [Recommended modules](#recommended-modules)
- [Installation](#installation)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Maintainers](#maintainers)

## Requirements

- Python 3.11 or later.
- The packages listed in [`requirements.txt`](requirements.txt):
  - `pyyaml` and `pydantic` for the Control Plane's policy schema and YAML loading.
  - `streamlit` and `pandas` for the Learning Plane demo UI.
  - `torch`, `sentence-transformers`, and `transformers` for the Tier 1 grounding and
    toxicity detectors, which run real trained models
    (`sentence-transformers/all-MiniLM-L6-v2` and `unitary/toxic-bert`).
- A [Gemini API key](https://ai.google.dev/) if you want to exercise a real upstream
  model call. Without one, the gateway falls back to a mock model adapter, which is
  sufficient for running the test suite and the Learning Plane demo.

No special requirements beyond the above — there is no database and no external
service to provision.

## Recommended modules

- **CPU-only PyTorch.** `torch` and `torchvision` are listed as plain dependencies in
  `requirements.txt`, but installing them from PyPI directly pulls a much larger
  CUDA-enabled build. Unless you have a GPU you intend to use, install the CPU wheel
  first:

  ```bash
  pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
  pip install sentence-transformers transformers
  ```

  `torchvision` itself is unused by this project (there are no vision models here) —
  it works around a `transformers` lazy-import quirk that surfaces under Python's
  `unittest.assertWarns`.

## Installation

1. Clone the repository and create a virtual environment:

   ```bash
   git clone <this-repository-url>
   cd ControlPlane
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies (see [Recommended modules](#recommended-modules) for the
   preferred PyTorch install order):

   ```bash
   pip install -r requirements.txt
   ```

3. Compile the policy bundles the Data Plane and Learning Plane demo expect:

   ```bash
   python3 -m control_plane.compiler \
     --base policies/org_baseline.yaml \
     --policy policies/customer_support.yaml \
     --out bundles/customer_support_bundle.json
   ```

   Repeat for `policies/decision_support.yaml` and `policies/internal_copilot.yaml` if
   you want all three shipped personas rebuilt from source. Pre-compiled bundles are
   already committed under [`bundles/`](bundles/), so this step is only required after
   changing a policy.

4. Run the automated test suite to confirm the install:

   ```bash
   python3 -m unittest discover -s tests -p "test_*.py"
   ```

5. Launch the Learning Plane demo:

   ```bash
   streamlit run learning_plane/app.py
   ```

## Configuration

Policy is configured as layered YAML under [`policies/`](policies/), not in code.
`policies/org_baseline.yaml` is the enterprise floor — it sets and **locks** fields
such as `fail_mode` and the PII/grounding/toxicity thresholds, so no downstream policy
can loosen them. Each use case (`customer_support.yaml`, `decision_support.yaml`,
`internal_copilot.yaml`) inherits from that baseline and may only tighten locked
fields, while remaining free to set its own value for anything left unlocked. See
[`architecture/01_CONTROL_PLANE.md`](architecture/01_CONTROL_PLANE.md) for how locking
direction is decided per field, and
[`docs/POLICY_LOCKING_AND_RISK_NORMALIZATION.md`](docs/POLICY_LOCKING_AND_RISK_NORMALIZATION.md)
for the full field reference.

Two settings most directly change runtime behavior:

- **`pii_mode`** controls what the Input Gate does with detected PII —
  `redact-and-proceed`, `warn-and-confirm`, or `block-and-explain`.
- **`fail_mode`** controls what happens if a detector itself fails at request time —
  `fail_open` lets the request through, `fail_closed` blocks it. See
  [`docs/NO_LATENCY_BUDGET.md`](docs/NO_LATENCY_BUDGET.md) for why there is
  deliberately no latency-budget field alongside these.

To use a real upstream model instead of the mock adapter, set `GEMINI_API_KEY` in your
environment before making a request through the gateway; credentials are read
per-request and are never persisted or logged.

## Troubleshooting

- **`ModuleNotFoundError` for `torch` or a very slow/large `pip install`.** You likely
  installed the default CUDA build of PyTorch from PyPI. Follow the CPU-wheel install
  order in [Recommended modules](#recommended-modules).
- **A test using `assertWarns` fails around `InertCriticalThresholdWarning`.** This is
  the same `transformers` lazy-import quirk noted above — make sure `torchvision` is
  installed even though nothing in this project imports it directly.
- **The demo home page shows "No demo ledger" and stops.** The ledger is seeded data,
  not live traffic, and isn't generated automatically. Run
  `python3 -m learning_plane.seed_demo_ledger` once (writes
  `learning_plane/data/demo_ledger.json`), then reload the page.
- **A model call fails with "No API key supplied for provider 'gemini'."** Set
  `GEMINI_API_KEY`, or omit credentials entirely to fall back to the mock adapter used
  throughout the test suite.

## FAQ

**Is this a finished product?**
No — it's a working prototype built for a competition timeline, with three planes
fully wired end to end and a clearly documented set of scoped-out features. See
[`architecture/README.md`](architecture/README.md) for a box-by-box status table
against the architecture diagram, and
[`architecture/04_ROADMAP.md`](architecture/04_ROADMAP.md) for what's designed but not
yet built (a complexity router, a T2 LLM-judge escalation tier, shadow deploy and
rollback, and a few others).

**Why is there no latency-budget setting in policy?**
It existed and was removed on purpose — the estimates behind it were never
independently measured, so enforcing them would have been a false guarantee. Full
reasoning in [`docs/NO_LATENCY_BUDGET.md`](docs/NO_LATENCY_BUDGET.md).

**Where do I see the detectors and risk-fusion logic in detail?**
[`docs/TIER_0.md`](docs/TIER_0.md), [`docs/TIER_1.md`](docs/TIER_1.md), and
[`docs/RISK_FUSION.md`](docs/RISK_FUSION.md) cover the deterministic checks, the
grounding/toxicity models, and how detector scores are fused into a single
allow/redact/regenerate/flag/block decision, respectively.

## Maintainers

- krishna-aga (krishnaskb600@gmail.com)
