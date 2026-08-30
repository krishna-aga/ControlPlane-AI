# Semantic Cache

**Status:** Implemented. `data_plane/cache.py`, wired into `data_plane/gateway.py`.
34 cache tests; **187 passing overall**. Closes a live policy hole (§6) and consumes
`caching_enabled` / `cache_threshold`, which were compiled into every bundle and read by
nothing.

> **A cache hit is a decision to skip the checks.** That is the whole design problem.
> Everything in this document is about when a past verdict may stand in for a fresh one.

---

## 1. Position

The cache sits **after the Input Gate and before the model call**. Neither side is
arbitrary:

```
  load bundle (hash-verified)
      │
  Input Gate ─────────────► BLOCK        the model is never called
      │  FLAG / ALLOW
  mint canaries
      │
  ┌───▼─────────────────────────────┐
  │ SEMANTIC CACHE                  │
  │   servable()?  ──── no ─────────┼──► straight through, reason recorded
  │   lookup(namespace, threshold)  │
  │   hit? ──► re-run T0 ──┬─ clean ┼──► SERVE  (model, T1, T2, fusion all skipped)
  │                        └─ dirty ┼──► evict, fall through
  └───┬─────────────────────────────┘
      │ miss
  upstream model (BYOK, buffered)
      │
  T0 ──► fusion ──► action ──► mask ──► de-anonymize
      │
  storable()? ──► write entry
      │
  ledger row  ◄── written on a HIT too, never skipped
```

**Before the Input Gate would be wrong.** A poisoned prompt could reach a cached answer
without ever being scanned, and the session's injection counters would never see it.
**After the model call would be pointless** — the spend has already happened.

---

## 2. Two invariants

### A. A cache entry is a verdict, not a response

Only a request that was **`ALLOW`ed with nothing found** is storable. Everything else is
refused, and each refusal has a specific failure behind it:

| outcome | why it is not storable |
| :--- | :--- |
| `REDACT` | the masking spans were computed against one specific placeholder-bearing string; replaying the text would replay it *without* the remedy |
| `REGENERATE` | by definition not the answer |
| `FLAG` | carries a review obligation that a replay would silently drop |
| `BLOCK` | must never be replayable at all |
| provider refusal | not an answer |

Storing anything else would cache a remedy alongside its text and then serve the text
alone.

### B. The cache skips the model call and the expensive tiers. It never skips Tier 0.

Every hit re-runs `run_t0()` on the stored text against the bundle **in force right
now**. Measured cost: **0.036 ms**. An entry that no longer passes is **evicted, not
served**, and the request falls through to the model.

The point is not that it catches much — a clean-`ALLOW`-only cache should already pass.
The point is the sentence it lets the system say to an auditor:

> Nothing reaches a client without deterministic checks against the current bundle.

*"Nothing except cache hits"* is a much weaker sentence, and it costs 0.036 ms to avoid
saying it.

---

## 3. The key

Exact on every dimension except the prompt, which is the only fuzzy one:

```
namespace = H(tenant_id, entitlement_scope, policy_hash,
              system_prompt, context_docs, embedder_id)
```

Each field closes a specific leak. None is decoration:

| field | what it stops |
| :--- | :--- |
| `tenant_id` | the obvious one — and its **absence disables caching entirely**, so forgetting to pass an id can never widen sharing |
| `entitlement_scope` | the cache becoming a **side channel around retrieval-time entitlement** — reading documents you are not entitled to retrieve. This is exactly the boundary [`ORIGIN_AND_EXONERATION.md`](ORIGIN_AND_EXONERATION.md) §4 delegates exposure risk to, and a careless cache walks straight through it |
| `policy_hash` | reusing a verdict reached under a **different policy**. Recompiling orphans every entry — self-invalidating, and free |
| `system_prompt` | *"you are a pirate"* serving answers cached under the previous persona |
| `context_docs` | two identical questions over **different retrieved chunks** sharing an answer, i.e. serving text grounded in documents that were not retrieved this time |
| `embedder_id` | comparing vectors from **different models**, which are not comparable at all |

**`context_docs` is hashed before canary planting.** Canaries are minted fresh per
request, so hashing the planted block would produce a different key every time and the
cache would never hit once. Guarded by `test_context_is_hashed_before_canary_planting`.

The namespace is length-prefixed before hashing, so no combination of field values can
collide by rearranging a delimiter.

---

## 4. The PII refusal — the one that actually bites

A request carrying any PII finding is **neither cached nor served from cache**. This is
the subtlest failure in the component and it deserves stating in full.

The Input Gate redacts before forwarding, so both of these prompts forward *identically*:

```
"check the order for alice@example.com"  ──►  "check the order for [EMAIL_1]"
"check the order for bob@example.com"    ──►  "check the order for [EMAIL_1]"
```

Key the cache on the forwarded prompt and those two requests are the same request. Serve
the second from the first's entry and the response is de-anonymized with **Bob's restore
map applied to Alice's answer** — a cross-customer PII leak manufactured entirely by the
cache, with every individual component behaving correctly.

Keying on the *raw* prompt instead merely moves raw PII into a long-lived structure,
violating the storage rule. Neither option is acceptable, so requests carrying PII are
simply not cacheable. Guarded by `test_pii_bearing_requests_are_never_cached`.

**A flagged input is likewise never served from cache.** A flagged input contracts the
output bands ([`RISK_FUSION.md`](RISK_FUSION.md) §6); serving from cache skips fusion, so
the contraction would never happen and the request would receive precisely the
untightened verdict it was denied.

---

## 5. Similarity is not equivalence — and the threshold cannot fix it

**This is the same defect class as cosine grounding** (T1-4 in [`TIER_1.md`](TIER_1.md)),
arriving on a second surface. Measured on the shipped embedder:

| pair | similarity |
| :--- | ---: |
| `"what is your refund policy"` / `"what is your refund policy"` | 1.000 |
| **`"can I get a refund"` / `"can I NOT get a refund"`** | **0.853** |
| `"what is your refund policy"` / `"what's the refund policy"` | 0.641 |
| `"what is your refund policy"` / `"how do I get my money back"` | 0.037 |
| `"what is your refund policy"` / `"how do I reset my password"` | 0.072 |

Read those rows together and the shape of the problem is plain: **the negated question is
the second-closest match in the table.** It scores higher than a genuine paraphrase and
twenty times higher than a true synonym. No threshold separates "same question" from
"opposite question", because the two differ by one short token and similarity measures
surface, not meaning.

What that leaves:

* **The threshold is a safety lever, not a performance knob.** It is the distance at
  which the system stops asking the model and starts guessing. §6 is about who gets to
  set it.
* **The remaining defence is what is *in* the entries.** Only clean `ALLOW` responses are
  stored, so the worst case is a **wrong** answer, not an unsafe one.
* **A wrong answer is unsafe in a regulated workflow** — which is exactly why
  `decision_support` sets `caching_enabled: false`. The persona configuration already
  answers this, and that is the argument for the cache being a policy decision rather
  than an infrastructure one.

Recorded as an accepted limitation in the manner of T0-8, not as an unfinished feature.

---

## 6. The policy hole this closed

`cache_threshold` and `caching_enabled` shipped **unlocked**. Verified against the
resolver before the fix:

```
baseline cache_threshold: 0.90
internal_copilot compiled cache_threshold: 0.85     <- a LOOSENING, accepted
is cache_threshold locked?  False
is caching_enabled locked?  False
```

`internal_copilot` was **already exercising the hole in a committed bundle.** And 0.85 is
not a harmless number: the measured negation similarity is **0.853**, so that persona
would have served the affirmative answer to the negated question. The hole was not
theoretical and the exploit was not hypothetical — it was compiled and committed.

Nothing stopped a tenant setting `0.1` and turning the cache into a general-purpose
bypass of the model call, T0, T1, T2 and fusion together.

**This is P2 with a new field.** P2 caught `toxicity_threshold`, `low_band` and
`high_band` unlocked; the same review never reached the cache fields because nothing
consumed them, and an unconsumed field looks harmless right up until something reads it.

### The fix

| field | direction | rationale |
| :--- | :--- | :--- |
| `cache_threshold` | **`HIGHER_IS_STRICTER`** | a minimum *similarity* before a past verdict is reused, so demanding a closer match is stricter — the same floor-vs-ceiling distinction that made `grounding_threshold` the P1 live bug |
| `caching_enabled` | `BOOL_FALSE_IS_STRICTER` | already registered; now **set** in the baseline and locked, since locking an unset field is a silent no-op (P6) |

`internal_copilot`'s `0.85` override was **removed**, not raised — it was a loosening that
should never have compiled. That persona now inherits `0.90`.

Tenants may disable caching or demand a closer match. They may never do the reverse.

**All three `policy_hash` values changed**, because `locked_fields` is itself a hashed
parameter.

---

## 7. Measured

```
gateway overhead, mock adapter (no network)
  miss path            0.749 ms
  HIT  path            0.232 ms
  T0 revalidation      0.036 ms   (included in the hit path)

with a 120 ms upstream call
  miss path          121.2   ms
  HIT  path            0.550 ms
  saved              120.6   ms per hit
```

The handover blueprint estimated *"return in ~8 ms"*. Actual is **0.23–0.55 ms**, roughly
15–30× under. That estimate was the same class of unvalidated constant as
`T0_ESTIMATED_MS`, which [`NO_LATENCY_BUDGET.md`](NO_LATENCY_BUDGET.md) §2 deleted for
violating the project's own no-hardcoded-constants rule. Recorded as a measurement, and
the saving that matters is the **120 ms upstream call**, not the microseconds of lookup.

---

## 8. The embedder

The shipped `HashingEmbedder` is **lexical, not semantic**: character 3-grams hashed into
256 dimensions, L2-normalized, pure standard library. No model download, no new
dependency.

**It under-hits.** *"how do I get my money back"* scores 0.037 against *"what is your
refund policy"* — a sentence-transformer would match those; this does not. Even a genuine
paraphrase at 0.641 misses the 0.90 threshold.

That failure direction is the safe one: the cost is a model call that could have been
avoided, not a wrong answer served confidently. Calling it semantic would be the claim
this repository keeps declining to make, so the class docstring says lexical and
`test_it_is_lexical_not_semantic_and_says_so` asserts it.

A real embedding model is a drop-in through the `Embedder` protocol. It would raise the
hit rate — and raise the stakes of every limitation in §5, since a model that matches
meaning matches *negated* meaning too.

**Embedder identity is carried in the namespace rather than in the bundle.** Swapping
embedders therefore invalidates the cache instead of silently comparing incomparable
vectors — the self-invalidating property `policy_hash` already gives the bundle. This
deliberately avoids adding a `cache_embedder` policy field: T1-1 argues that a threshold
without its scale is not a policy, and putting the identity in the key enforces that
structurally rather than by validation.

---

## 9. Privacy and audit

**`CacheEntry` holds no prompt.** The response is stored because that is what gets
served; the prompt is needed only to compute its vector and is dropped immediately after.
This is the argument that removed `value` from `Finding` — a structure that cannot carry
user text cannot leak it, and the rule stops depending on future contributors remembering
it.

**A cache hit writes a ledger row.** Without one the audit trail would have holes exactly
where the cheap path ran, and *"why did this user get this answer in March"* would have no
record for the fastest requests. The row carries `served_from`, the similarity that
cleared the threshold, and the `source_request_id` of the original — so a reviewer can
walk from a served answer back to the request that was actually checked.

**A miss records why.** `cache_skip_reason` names the refusing clause. A hit rate with no
denominator explanation is not an answer to a cost review, and *"why was this not cached"*
is the first question anyone asks about one.

---

## 10. Deliberately not built

* **Persistence.** In-memory, bounded at 1024 entries, oldest namespace evicted first.
  Bounding matters for the same reason `SessionState.adjudicated` is bounded: a tenant
  must not be able to grow gateway memory with unique prompts.
* **An ANN index.** A linear scan *within a namespace* is right at this scale, and the
  namespace split already keeps each scan small. Honest about not being an index.
* **TTL / staleness.** Entries live until evicted. `policy_hash` in the key handles
  policy changes; nothing handles the underlying corpus changing beneath a cached answer.
  This is a real gap — see §11.
* **Multi-turn caching.** Single-turn only. A follow-up (*"what about premium
  members?"*) is meaningless without its history, and keying on the full replayed array
  collapses the hit rate to roughly zero. Stated rather than discovered.
* **The complexity router.** Still out of scope; `allow_downrouting` remains the one
  compiled-but-unconsumed bundle field.

## 11. Open

* **No TTL.** A cached answer outlives the document that grounded it. The corpus is not
  in the key — only the chunks retrieved *for that request* are — so a corpus edit does
  not invalidate anything. `require_retrieval` ([`GATEWAY.md`](GATEWAY.md) §9) would
  narrow this by making retrieval mandatory, not close it.
* **The negation hazard is mitigated, not solved** (§5). It gets sharply worse the moment
  a real semantic embedder is fitted, which is the upgrade most likely to be made for
  hit-rate reasons by someone who has not read §5.
* **`cache_threshold: 0.90` is calibrated against nothing.** It happens to sit above the
  measured negation case by 0.047 on *this* embedder. That margin is a property of the
  embedder, not of the number, and it does not travel.
* **T0 revalidation only re-checks Tier 0.** An entry stored under a policy whose T1
  thresholds later tightened is still served, because `policy_hash` would have changed —
  so this is currently unreachable. It becomes reachable if bundles are ever versioned
  more loosely than by hash.
