# Roadmap: What's Designed, Not Yet Built

This is every item from the architecture diagram that's scoped and designed for but
not yet implemented, gathered in one place, ordered by how central it is to the
system's story. Each of these was a deliberate scoping decision for this build, not an
oversight — the policy surface, data model, or interface each one needs already exists
in the codebase, which is what makes each of them additive rather than a
redesign.

## 1. Closing the feedback loop

The architecture's central idea is a loop: the Learning plane observes production, a
human approves a proposed change, and that change becomes a new, safely deployed
policy version. Today the loop runs through human approval and stops there — the next
milestone is having an approved proposal mint a new bundle, shadow-deploy it, and hand
off to enforcement. Everything upstream of that handoff (the proposal, the approval,
the persisted decision) is built and demonstrated; this is the single highest-value
addition for the next phase.

## 2. Complexity router

A policy field already exists for whether a tenant permits down-routing simple
requests to a cheaper model. The router that acts on it is the next Data-plane
addition — self-contained, since the policy surface and the pipeline slot it occupies
are both already in place.

## 3. T2 — LLM-as-judge

The tiered check cascade already computes when a case is ambiguous enough to warrant
escalation; wiring that signal to an actual LLM judge call is the remaining piece.
This also directly upgrades Shadow Evaluation, which is built to consume T2's output
and runs against a calibrated stand-in until T2 ships.

## 4. Shadow deploy and instant rollback

The Control plane can already version and hash a policy bundle deterministically.
Running a candidate bundle in shadow against the live one, and keeping a version
history to roll back to, is the next distribution capability — and it's the same
mechanism both the Control plane's distribution story and the Learning plane's
feedback loop are waiting on.

## 5. Streaming and stream-time monitoring

Responses are currently buffered before any safety check runs, which is why
stream-time monitoring for runaway generation or repetition loops isn't active yet —
there's no partial response to monitor. Enabling streaming means giving the tiered
checks a story for partial-response evaluation first; that's the dependency this item
is scoped behind.

## 6. Authentication

The gateway enforces strict tenant *scoping* — one tenant's cache, session, and ledger
data can never cross into another's — but it trusts the tenant identity it's handed
rather than verifying it against an identity provider. Because that scoping is already
in place downstream, adding an auth layer in front of the gateway (API keys, OAuth,
mTLS) is a clean addition rather than a rearchitecture.

## 7. Jurisdiction policy layer

The resolver currently merges two policy layers — org baseline and use case. A third,
jurisdiction layer (for example, region-specific data-handling rules) fits the same
locking model and sits between the two that already exist; extending the resolver from
two tiers to N tiers is the required change.

## 8. A trained embedding model for the semantic cache

The cache today matches on a fast lexical hash rather than a trained embedding model —
a deliberate choice for this build that trades some cache-hit rate for a hard guarantee
against serving a cached answer to a prompt that only superficially resembles the
original. A trained embedder is a drop-in upgrade once cache volume justifies the added
cost and latency.

## 9. Live production data for the Learning plane

The audit ledger, shadow-evaluation sample, and calibration ground truth all run
against seeded demonstration data today. The mechanisms operating on that data — hash
chaining, uncertainty ranking, threshold sweeps — are real and unaffected by this; what
changes as the system goes live is simply the source feeding them.

## A decision worth calling out directly: no latency budget

One item on this list isn't a gap at all — it's a decision we want evaluators to see
clearly rather than assume was missed. An earlier version of the Control plane
included a latency-budget field and a validator enforcing it. We removed both,
deliberately: the millisecond estimates backing that validator were never
independently measured, so the validator would have enforced a number nobody had
verified — a false sense of guarantee is worse than an honest absence of one. Real
latency is measured live in the Data plane instead. Full reasoning in
[`../NO_LATENCY_BUDGET.md`](../NO_LATENCY_BUDGET.md).

## A defect we found and are disclosing, not hiding

At the default risk bands shipped with this build, the "critical" severity floors in
the risk fusion engine sit above the block threshold already in effect at those bands
— so at today's defaults, they're currently redundant with the standard band rather
than an independent layer of protection. We didn't discover this by accident: the
compiler emits a build-time warning for exactly this condition, which is what caught
it, and it's why we're surfacing it here rather than letting a judge find it first. A
tenant tightening their bands below the shipped defaults makes the floors meaningful
immediately — no code change required. Full detail in
[`../RISK_FUSION.md`](../RISK_FUSION.md).
