# Finding Origin & Context Exoneration

**Status:** Specified, **not implemented**. No code has been written for this.
**Depends on:** T1 NER (not built). The T0 half could ship independently.
**Scope:** `data_plane/detectors/t0.py`, a future `data_plane/detectors/ner.py`,
`data_plane/models.py`, `control_plane/resolver.py`, `policies/org_baseline.yaml`.

---

## 1. The problem

A support bot replies:

> *"Thanks **John**, your **Acme** order ships to **Mumbai**. Reference card
> **4111111111111111**."*

Four entities. Naively every one is a PII finding, so the response is redacted into
uselessness. But they are not equally dangerous, and what separates them is **where each
came from**.

T0 already draws part of this distinction (T0-4) with three origin values. It is not
enough, because it has no notion of the retrieved documents.

---

## 2. Part 1 — one shared origin resolver, four values

For every finding, answer *"where did this text come from?"*

| origin | meaning | risk? |
| :--- | :--- | :--- |
| `echoed_placeholder` | a `[EMAIL_1]` the gateway injected itself | no |
| `echoed_from_input` | present in the user's own message | no |
| **`echoed_from_context`** | **present in the RAG chunks the gateway sent** | **depends — see §3** |
| `model_generated` | the model produced it from nowhere | yes |

### Why it must be shared

Cards, Aadhaar and secrets are found by **T0** (regex + checksum). Names, orgs and places
would be found by **T1 NER**. Two independent resolvers will eventually disagree about
the same string in the same response. One function, called by both tiers:

```python
resolve_origin(text, placeholder_map, forwarded_prompt, context_block, canaries) -> str
```

T0's existing `_resolve_origin()` is replaced by it; T1 uses it from the start.

### The canary trap — do not miss this

**The canary token is planted inside the context block.** A naive *"does this text appear
in the context?"* check would therefore find the canary there, label a canary leak as
`echoed_from_context`, and exonerate it — silently disabling the strongest check in
Tier 0.

The canary must be stripped from the context block before any matching. A test should
assert that a leaked canary is never labelled `echoed_from_context`.

### Matching caveats

Substring matching is cheap but imprecise. Known gaps, worth stating rather than
pretending away: inflection (`Mumbai` vs `Mumbai's`), case, and tokenisation. Erring
toward *not* exonerating is the safe direction — a missed exoneration causes a needless
redaction, while a false exoneration lets a real finding through.

---

## 3. Part 2 — `context_exonerates`

Origin alone is not enough. In the example, `Acme`, `Mumbai` and the card **all share the
same origin** — they were all in the retrieved chunks — yet they are not equally safe.

### The insight

The two kinds of entity fail in completely different ways:

* **Names, orgs, places → the risk is FABRICATION.** A hallucinated "policy manager Sarah
  Johnson" is dangerous because she does not exist, or because she surfaced from training
  data. If the name came from *your document*, there is no fabrication. Origin answers
  the question.

* **Cards, Aadhaar, SSNs, API keys → the risk is EXPOSURE.** A real credit card is
  dangerous **regardless of where it came from**. Knowing it came from your corpus does
  not make it safe — it makes it *worse*, because a hallucinated card is fake while a
  corpus card belongs to a real person who is not in this conversation.

### The field

```yaml
context_exonerates:      # entity types that CONTEXT origin forgives
  - PERSON
  - ORG
  - GPE
```

Cards, Aadhaar and secrets are simply **absent**, so corpus origin never forgives them.

### The decision rule

```
echoed_placeholder   → not a risk
echoed_from_input    → not a risk
echoed_from_context  → not a risk IF entity_type in context_exonerates
                       RISK otherwise
model_generated      → RISK
```

Applied to the example:

| entity | origin | in list? | verdict |
| :--- | :--- | :--- | :--- |
| John | `echoed_from_input` | — | safe |
| Acme | `echoed_from_context` | ORG ✓ | safe |
| Mumbai | `echoed_from_context` | GPE ✓ | safe |
| **4111111111111111** | `echoed_from_context` | ✗ | **RISK → redacted** |

The last row is the whole point: **a real card sitting in the corpus, surfaced to a
customer.**

### Locking direction — new machinery required

A *shorter* list forgives fewer things, so **shorter is stricter**. A tenant may remove
`ORG` (making organisation names risky too) but may never add `CREDIT_CARD`.

The resolver's registry handles scalars, booleans, enums and maps. It has **no set
direction**. This needs:

```python
SET_SUBSET_IS_STRICTER = {"context_exonerates"}
```

plus a validator asserting the child's set is a subset of the base's. `blocklist_terms`
sits in the same unlocked category and would benefit from the same mechanism — though
note its direction is the opposite: a *longer* blocklist is stricter.

---

## 4. Part 3 — the boundary, stated honestly

Exonerating `PERSON` from context removes the **fabrication** risk. It does **not** remove
the **exposure** risk.

If the retrieved chunk was an HR record:

> *"Sarah Johnson, employee ID 4471, approved this policy."*

…then Sarah is real, she is in the corpus, and the rule waves her straight out to an
external customer.

**The gateway cannot decide this.** Whether Sarah should be visible to *this* user is an
access-control question — should that chunk have been retrievable at all? That belongs at
retrieval time. The architecture already names the right home: `entitlement_scope`, which
appears in the semantic cache key `(tenant_id, entitlement_scope, policy_hash, embedding)`.

The statement to make, in the docs and in the pitch:

> **Context origin exonerates fabrication risk, not exposure risk. Exposure is delegated
> to retrieval-time entitlement, which this prototype does not implement.**

This is deliberately not claimed as solved. The problem statement warns to *"assume a mix
of well-governed and loosely governed internal data sources"* — "trust everything in the
corpus" is precisely the assumption it says not to make. Naming the boundary is a
stronger position than hiding it.

### Rejected first draft

An earlier version of this design marked `PERSON` + context as unconditionally **safe**,
citing that same PS quote in support. That was wrong in the case the quote is about: names
are **both** a fabrication risk *and* an exposure risk, and the neat split only covers the
first. The rule survives; the claim attached to it does not.

---

## 5. Interaction with the risk bands

If `low_band` moves to `0.5` (under consideration — see [`RISK_FUSION.md`](RISK_FUSION.md)),
the NER confidences resolve as:

```
PERSON  0.85 → S 0.625 → above the band → acts
GPE     0.75 → S 0.469 → below         → no action
ORG     0.70 → S 0.437 → below         → no action
```

So in practice the three-row matrix **collapses to one row**: whether corpus-sourced
*person names* get redacted. Still worth building — the card row is unaffected and is the
severe case — but the scope is narrower than the matrix suggests, and that should temper
how much machinery it justifies.

---

## 6. Implementation checklist

1. `resolve_origin()` as a shared function taking `context_block` and `canaries`; strip
   canaries before matching. Replace T0's `_resolve_origin()` with it.
2. Add `echoed_from_context` to `Finding.origin`, the ledger schema, and
   `T0Result.severities`' risk filter.
3. Add `context_exonerates` to `PolicyConfig` / `BundleConfig`, default
   `[PERSON, ORG, GPE]`; set and lock it in `org_baseline.yaml` (a locked field must be
   assigned — see P6).
4. Add `SET_SUBSET_IS_STRICTER` and `_validate_locked_set()` to `resolver.py`.
5. Apply the §3 decision rule wherever findings are filtered for risk.
6. Recompile all three bundles; update the recorded hashes.
7. Tests: the canary-in-context trap; a corpus card is still a risk; a corpus org is not;
   set-lock allows removal and rejects addition; the four-value matrix end to end.

## 7. Open

* **Depends on T1 NER**, which is not built. The T0 half (`echoed_from_context` for cards
  and secrets) could ship on its own and is the higher-value half, since it is the card
  row that carries the severe case.
* Substring matching is the assumed mechanism; nothing better is proposed.
* `blocklist_terms` needs the same set-locking machinery, in the opposite direction.
