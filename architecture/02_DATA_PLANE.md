# Data Plane

*Applies the rules to this request — milliseconds.*

The Data plane is the request-time pipeline: a prompt comes in, a policy bundle is
already loaded, and every stage below runs in sequence before a response goes back to
the caller. This is the largest and most heavily tested part of the system — eight
stages, most of them fully built, a few deliberately scoped down for this build.

## Gateway

The entry point loads the compiled policy bundle and verifies its hash before trusting
it, so a tampered or stale bundle is rejected rather than silently enforced. Every
request carries a tenant identity and entitlement scope, which the gateway threads
through the entire pipeline — the cache, the session store, and the ledger are all
partitioned by it, so one tenant's data and one tenant's cached answers can never leak
into another's. Bring-your-own-key credentials pass through a single call and are
never persisted.

What's out of scope in this build is authentication itself: the gateway trusts the
tenant identity it's given rather than verifying it against an identity provider. In a
production deployment this is where an auth layer (API keys, OAuth, mTLS) would sit in
front of the gateway — the tenant-scoping the gateway already does downstream of that
check is what makes adding it a clean addition rather than a rearchitecture.

## Input gate

Before a prompt reaches the model, it's canonicalized to strip the tricks used to
evade text scanners — invisible characters, unicode normalization, base64 encoding,
letter-spacing — and scanned for both prompt injection and PII. PII detection isn't
just pattern matching: identifiers like card numbers run through real checksum
validation (Luhn, Verhoeff) to cut false positives, and redaction is span-based, so a
redacted prompt can be precisely reconstructed later if the policy calls for it.
Every action here — block, flag, redact-and-proceed, warn-and-confirm — is driven by
the loaded policy bundle, never hardcoded. This is the most complete stage in the
pipeline.

## Semantic cache

A response can be served from cache instead of re-calling the model, but only under
strict conditions: the cache key incorporates tenant, entitlement scope, the policy
hash itself, the system prompt, and the retrieved context documents, so a persona
change or a different RAG result can never share an answer with an unrelated request.
Nothing containing PII or spanning a multi-turn conversation is ever cached, and every
cache hit is re-validated against the fast deterministic checks before being served,
with automatic eviction if that revalidation fails.

Today's similarity matching runs on a fast lexical hash rather than a trained
embedding model — a deliberate choice that trades some cache-hit rate for a hard
guarantee against false-positive hits (serving a cached answer to a prompt that only
*looks* similar). A trained embedder is a drop-in upgrade to this component once cache
volume justifies the added latency and cost.

## Complexity router

The policy schema already carries the field for this — whether a tenant permits
down-routing simple requests to a cheaper model — but the routing logic that would act
on it hasn't been built yet. This is the cleanest gap in the system: the policy
surface is ready, and the router is a self-contained addition rather than something
that touches the rest of the pipeline.

## Model call — upstream FM (BYOK)

The gateway calls a real upstream model over the tenant's own credentials, with retry
and backoff on rate limits and transient failures. Responses are fully buffered before
any safety check runs, by design: the tiered checks that come next need to see a
complete response, since there's no way to "unsend" tokens that already streamed to a
caller. The tradeoff is that streaming — and the stream-time monitoring the diagram
calls out (catching a runaway generation or a repetition loop mid-stream) — is planned
for a later iteration, once the safety cascade has a story for partial-response
checks.

## Tiered output checks

Every response passes through the tiered cascade the diagram names: **T0**,
deterministic rule-based checks (checksums, canary tokens, blocklists) that run in a
fraction of a millisecond; and **T1**, real trained models running in parallel —
sentence-embedding-based grounding checks against the source documents, and a toxicity
classifier. Both tiers are live and tested against real inputs, not stubs.

**T2**, an LLM-as-judge escalation for cases the first two tiers can't confidently
resolve, is designed but not yet built — fusion already computes *when* T2 should be
invoked, so wiring in the actual judge call is additive, not a redesign. This is also
why Shadow Evaluation in the Learning plane runs against a calibrated stand-in judge
today rather than the real thing.

## Risk fusion and action engine

This is where every detector's score, on its own scale, gets normalized onto a common
risk scale and fused into a single decision. Fusion takes the *maximum* risk across
detectors rather than averaging them, so one high-confidence detector can't be diluted
by several low ones, and a request's own trajectory (repeated risky attempts in the
same session) tightens the bands it's judged against. The full action ladder —
**allow, redact, regenerate, flag, block** — resolves to a concrete remedy driven by
whichever detector triggered it, and the **fail-open / fail-closed** policy setting
genuinely changes behavior when a detector itself fails: fail-closed forces a block
rather than letting an error silently pass a risky response through. This is fully
built and is the most thoroughly tested stage in the pipeline.

One thing worth flagging directly rather than glossing over: at the default risk bands
shipped with this build, the "critical" severity floors sit above the block threshold
already in effect, so they're currently redundant rather than an added layer of
protection. The compiler detects and warns about exactly this condition at build time,
which is what caught it — a tenant tightening their bands below the default
immediately makes the floors meaningful again. Full detail in
[`../RISK_FUSION.md`](../RISK_FUSION.md).

## Session state

Multi-turn conversations carry state across requests — accumulated risk, a count of
distinct attack attempts, and a rework counter that climbs each time a response is
regenerated — all stored as hashes and counters, never as conversation content. This
is fully built and lets the system recognize, for example, a user probing the same
attack repeatedly across turns even when each individual message looks unremarkable on
its own.
