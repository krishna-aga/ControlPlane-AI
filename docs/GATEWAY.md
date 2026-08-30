# Data Plane Gateway

**Status:** Implemented. `data_plane/gateway.py`, `adapters.py`, `session.py`,
`ledger.py`. 23 integration tests; 138 passing overall.

The gateway is what turns eight independently-tested components into a system. Everything
else in the suite tests a component in isolation; this layer owns the **order**, which is
where the contract constraints actually live.

---

## 1. Pipeline

```
  load bundle  ──── policy_hash verified at load; a bundle edited after
      │             compilation cannot drive enforcement
      ▼
  Input Gate ──────────────────► BLOCK   (the model is never called)
      │  FLAG / ALLOW
      ▼
  mint canaries, assemble payload        ◄── the GATEWAY builds the call
      │
      ▼
  upstream model (BYOK, buffered)
      │
      ├─ ModelCallError ───────► BLOCK   (not a fail_mode decision — see §5)
      ├─ provider refusal ─────► BLOCK
      ▼
  T0 ──── hard_override ───────► BLOCK   (T1/T2 skipped; decision already final)
      │
      ▼
  T1 / T2   ── not built (§7)
      │
      ▼
  fusion   ── input risk tightens the bands
      │
      ▼
  apply action: mask spans               ◄── MUST precede de-anonymization (T0-5)
      │
      ▼
  de-anonymize for the client
      │
      ▼
  ledger row: types, spans, scores — never values
```

Measured end to end on the mock adapter: **0.33 ms** of gateway overhead.

---

## 2. Entry point

```python
process_request(
    messages,        # full replayed conversation — the tenant owns this
    bundle_path,     # which policy applies
    session_id,      # risk posture only
    credentials,     # BYOK: provider + model + key, per request
    system_prompt,   # tenant's; the gateway appends the canary
    context_docs,    # RAG chunks, tagged with the context canary
)
```

Library first, HTTP later. The signature takes exactly what an HTTP handler would
deserialize from a request body, so adding a transport does not move this boundary.

---

## 3. BYOK: the gateway holds no key

Credentials arrive **with the request**, are used for that one upstream call, and are
dropped. Reading `GEMINI_API_KEY` from process environment at import would make this a
single-tenant gateway with our key in it — not BYOK. `Credentials.from_env()` exists and
is labelled development-only.

The key must never reach a log line, a ledger row, or an exception message:

* `Credentials.redacted()` gives call sites something safe to print.
* The ledger records `provider` and `model`, never `api_key`.
* `GeminiAdapter` sends the key as an `x-goog-api-key` **header**, not a query parameter —
  a key in a URL lands in access logs, proxy logs and browser history.
* HTTP errors surface the status code only; the response body can echo the request.

Guarded by `test_no_secret_pii_or_credential_reaches_the_ledger` and
`test_key_is_not_carried_on_the_result`.

## 4. The gateway owns payload assembly

The tenant supplies the parts; the gateway builds the call. This is not stylistic — the
canary must go in the **system role**, and a tenant handing over one opaque blob makes
system-prompt leak detection impossible (§8A of `t0_deterministic_checks.md`). A tenant
who forgets to plant a canary silently loses the check, so it cannot be their job.

Responses are **buffered, never streamed.** The cascade cannot `BLOCK`, `REDACT` or
`REGENERATE` text the client has already received; streaming would silently downgrade the
action ladder from enforcement to logging.

---

## 5. Model failure is not detector failure

`fail_mode` answers *"we could not verify the output."* When the upstream call fails
there **is** no output to verify, so `fail_mode` does not apply — the request errors
either way. Provider refusals (`finishReason: SAFETY`) are surfaced separately as
`provider_refused` rather than mistaken for a clean empty response.

---

## 6. Multi-turn: tenant owns the conversation, gateway owns the posture

Because the tenant replays its full history each turn, the gateway **sees** everything and
**retains** nothing — all the detection benefit of context, none of the storage liability.

An injection planted at turn 1 is re-scanned at turn 20, and that is correct: the attack
is still in the payload the model reads, so the risk is genuinely still live. What was
wrong was *counting* it twenty times. So a finding is a property of **content**, not of a
request:

| | behaviour |
| :--- | :--- |
| **live risk** | max over findings in *this* array, deduped. One poisoned turn counts once whether it is turn 1 or turn 40 — and drops on its own if the tenant removes that turn. |
| **distinct attacks** | increments only on a finding hash never seen before. One attack replayed twenty times counts **once**; three different attempts count three. |

The cache cannot be gamed: the gateway computes the hash from the content the tenant
sent, so any modification misses the cache and forces a full scan. It can only skip work
on byte-identical content — it fails safe.

`distinct_attacks` is **capped at 3 and does not decay**. An attacker cannot drive the
bands to zero and deadlock a session; *"the session is burned, start a new one"* is an
honest behaviour to explain, and they would open a new `session_id` anyway, so a deadlock
is pure downside. `adjudicated` is bounded at 512 entries so a tenant cannot grow gateway
memory with unique messages.

What is stored per session is **content-free**: hashes and counters, no message text.

---

## 7. Not built

* **T1** — NER PII, embedding grounding, toxicity. Their `DetectorSignals` fields stay
  `None`, which marks them *inapplicable* so fusion renormalizes rather than scoring an
  absent detector as zero.
* **T2 LLM judge** — `decision_support` sets `t2_enabled: true` and `fusion.t2_recommended`
  is computed, but nothing consumes it yet.
* **Complexity router** — still cut from prototype scope. `allow_downrouting` is now the
  only compiled-but-unconsumed bundle field; the semantic cache was **built** and
  consumes `caching_enabled` and `cache_threshold`
  (see [`SEMANTIC_CACHE.md`](SEMANTIC_CACHE.md)).
* **Retrieval gating** — refusing when no context chunk clears a similarity floor, so an
  off-topic request is refused before paying for a model call. Designed, not built; see
  §9.

---

## 8. Defect found by integration

Exactly the class of bug that isolation testing cannot reach.

The credit-card pattern was `\b(?:\d[ -]?){13,19}\b`. Its final repetition may end on a
**separator**, so the span ran one character past the number:

```
"My card 4111111111111111 was charged"
  captured  '4111111111111111 '        ← trailing space swallowed
  forwarded 'My card [CREDIT_CARD_1]was charged'     ← space gone
  restored  'I see the charge on 4111111111111111 .' ← stray space
```

Invisible while `scan_pii` was tested alone — the entity type and Luhn result were both
correct. Only redaction and restoration through the full path revealed it. Now anchored
on a digit at both ends: `\b\d(?:[ -]?\d){12,18}\b`. Regression:
`test_span_stops_at_the_last_digit`.

---

## 9. Retrieval gating — designed, not built

A support bot asked to debug code retrieves nothing relevant, yet still pays for a model
call. `grounding_threshold` already catches this **on the output**, after the spend; the
saving requires an **input**-side gate.

Two fields, locked, `BOOL_TRUE_IS_STRICTER`:

```yaml
require_retrieval: false          # refuse when nothing clears the floor
retrieval_min_similarity: 0.5     # retrieval always returns top-k, so "available" needs a floor
```

Per persona: `customer_support` **true** (narrow domain, cost control), `decision_support`
**true** (regulated — answering from model knowledge is the failure it exists to
prevent), `internal_copilot` **false** (employees legitimately ask general questions).

Two hazards to settle first: a retrieval outage becomes a mass refusal (the `fail_mode`
question applied to retrieval), and conversational turns — *"hi"*, *"thanks"*, *"get me a
human"* — retrieve nothing and would be refused.

Side effect worth noting: with `require_retrieval: true`, grounding is *always*
applicable for that persona, so the applicable detector set stops varying and the
renormalization inconsistency in `RISK_FUSION.md` largely disappears there.
