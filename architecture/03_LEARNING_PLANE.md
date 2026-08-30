# Learning Plane

*Offline — no request waits on it.*

The Learning plane is where the system gets better over time without ever sitting in
the critical path of a live request. It watches what the Data plane decided, gives
humans a way to review the uncertain cases, estimates what the current policy might be
missing, and proposes calibration changes back to the Control plane. A working
Streamlit application demonstrates all four stages end to end, backed by a genuinely
hash-chained ledger — the mechanics here are real, running today against a seeded
demonstration dataset while the system accumulates enough live traffic to run against
production data.

## Audit ledger

Every decision the Data plane makes is recorded as a row that's cryptographically
linked to the row before it — each row's hash incorporates the previous row's hash, so
altering or reordering any past entry breaks verification from that point forward.
This isn't just a design claim: the ledger explorer in the demo lets you tamper with a
row and watch verification fail live. Just as importantly, what's recorded is *never*
raw content — only entity types, character spans, and confidence scores — so the
ledger can be reviewed and audited without ever exposing the sensitive data it's
tracking.

## Reviewer queue

Flagged cases are surfaced to a human reviewer, ranked by uncertainty rather than
simple recency or volume, so the cases most likely to need a human judgment call rise
to the top. Approve/reject decisions are persisted and feed directly into
overturned-rate and false-positive-rate metrics shown in the same view — a reviewer
correcting the system's call is captured, not just displayed.

## Shadow evaluation

This stage estimates how many risky responses the current policy might be *missing* —
by sampling a slice of traffic and re-scoring it against a more thorough judge than
the one used at request time, then measuring how often the two disagree. The sampling,
scoring, and estimated-miss-rate math are all real and run end to end in the demo.

Because the T2 judge itself is still on the Data-plane roadmap (see
[`02_DATA_PLANE.md`](02_DATA_PLANE.md)), this stage runs against a calibrated stand-in
judge rather than a live LLM call, and the sample size in the demo is fixed rather than
a literal live percentage of production traffic — both are clearly labeled as such in
the UI itself, and both become real the moment T2 ships, since this stage is built to
consume a real judge's output the same way it consumes the stand-in's today.

## Calibration

Given a set of labelled outcomes, this stage sweeps candidate risk thresholds and
reports the false-positive/false-negative/cost tradeoff at each one — real analysis a
policy owner could use to decide whether tightening or loosening a band is worth it.
A reviewer can propose a specific candidate threshold directly from this analysis, and
that proposal is persisted for the next stage to act on.

The labelled outcome set driving this analysis is synthetic in the current build,
standing in for a real labelled dataset that would come from accumulated reviewer
decisions over time. The sweep mechanics themselves — the part that would need to be
correct for this to be trustworthy — are real and unaffected by that.

## Feedback loop: proposal → human approval → new version

A proposed calibration change can be reviewed and approved by a human, and that
approval is persisted — this half of the loop is real. What comes after approval —
minting a new policy bundle from the approved change, shadow-deploying it against the
currently live version, and having the Data plane pick it up — is the same
distribution capability flagged as a roadmap item in the Control plane
(see [`01_CONTROL_PLANE.md`](01_CONTROL_PLANE.md)). The demo makes this boundary
explicit: it shows exactly where an approved proposal would hand off to that
mechanism, rather than silently stopping short of it.

## What this demonstrates

Every mechanism in this plane — the hash chain, the uncertainty ranking, the sweep
math, the propose-and-approve workflow — is real, tested code, not a mockup. The
honest scope boundary is upstream and downstream of it: the data feeding it today is
seeded rather than pulled from months of live traffic, and the last mile of the
feedback loop (an approved change becoming a safely deployed new policy version) is
the next milestone once the Control plane's distribution mechanism is built out. See
[`04_ROADMAP.md`](04_ROADMAP.md).
