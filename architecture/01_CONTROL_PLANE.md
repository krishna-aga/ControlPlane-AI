# Control Plane

*Decides what the rules are — human speed.*

The Control plane is where policy is authored, merged, and turned into an immutable,
verifiable artifact the rest of the system can trust. It never touches a live request;
its output is a compiled policy bundle that the Data plane loads once and enforces
many times.

## Policy authoring

Policy is written as plain YAML and validated against a strict schema — an unknown
field or typo fails to load rather than being silently ignored, so a misconfigured
policy is caught at authoring time, not in production. Policies are layered: an
**org baseline** sets enterprise-wide defaults and locks the fields that must never be
loosened downstream, and a **use-case policy** (e.g. a customer-support bot, a
regulated decision-support tool, an internal copilot) inherits from it and specializes
further. Three such use-case policies ship today, each compiling to its own bundle.

The diagram's third layer — jurisdiction — is scoped for a future iteration. The
current resolver merges exactly two layers; adding a jurisdiction tier (for example,
region-specific data-handling rules sitting between the org baseline and the use case)
is a natural extension of the same locking model, not a redesign, and is the first
item on the control-plane roadmap.

## Resolver

The resolver is the heart of the Control plane: it merges a child policy onto its
parent and enforces **field-level locking** in a specific direction — a parent can
lock a field so that a child may only tighten it, never loosen it. This is what lets
an enterprise baseline guarantee, for example, that no downstream team can weaken the
PII threshold below a compliance floor while still letting that team set stricter
limits for their own use case. Locking is direction-aware per field (some fields are
stricter when lower, some when higher, some are booleans or enums with their own
ordering), and the resolver validates structural coherence after every merge — for
instance, rejecting a policy where risk bands are defined in the wrong order, or where
a "critical" threshold sits in an inconsistent place relative to the rest of the
policy. This is the most heavily tested part of the codebase: the automated suite
exercises locking in both directions, across every field type, plus deterministic
hash reproducibility.

One thing the resolver deliberately does **not** do is validate a latency budget,
despite that being a natural-sounding responsibility for a policy layer. We built that
validator, then removed it: the millisecond estimates it would have enforced were
never independently measured, so a fake guarantee was worse than none. Real latency is
measured live in the Data plane instead. The full reasoning is in
[`../NO_LATENCY_BUDGET.md`](../NO_LATENCY_BUDGET.md) — it's a deliberate design
decision, not an unfinished feature.

## Distribution

Every compiled policy bundle carries a version and a deterministic SHA-256 hash
computed over its canonical contents, so two independently compiled bundles with
identical policy inputs are provably identical, and any tampering after the fact is
detectable. This hash is load-bearing for the rest of the system — the Data plane
verifies it on every bundle load, and the semantic cache uses it as part of its cache
key so that a policy change can never silently serve a stale decision.

What the diagram calls out beyond that — **shadow deploy** and **instant rollback** —
is designed but not yet built. The distribution mechanism today is: compile a bundle,
hash it, ship it. There's no mechanism yet to run a candidate bundle side-by-side with
the live one for comparison, and no versioned history to roll back to if a new bundle
underperforms. This is the same capability the Learning plane's feedback loop is
waiting on to close the last mile from "a human approved this change" to "the change
is safely live" — see [`04_ROADMAP.md`](04_ROADMAP.md) for what closing it involves.
