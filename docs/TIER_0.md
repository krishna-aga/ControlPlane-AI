# Tier 0 — Deterministic Output Checks

**Status:** Implemented. `data_plane/canary.py`, `data_plane/detectors/t0.py`,
`Finding`/`T0Result` in `data_plane/models.py`, categorical floor in
`data_plane/fusion.py`. 37 T0 tests; 107 passing overall.

Implements [`t0_deterministic_checks.md`](../.agents/skills/t0_deterministic_checks.md)
and resolves T0-1, T0-2, T0-4, T0-6 and T0-8 from the locking register. Two new defects
were found during implementation and are recorded here as **T0-9** and **T0-10**.

---

## 1. Position and ordering

T0 runs on the raw upstream output **while PII placeholders are still in place**:

```
model output (may contain [EMAIL_1], [AADHAAR_1])
        │
        ▼
   run_t0()  ◄── placeholders intact; this is what makes `origin` resolvable
        │
        ├─ hard_override ──► skip T1/T2 ──► fusion ──► BLOCK
        ▼
   T1 ──► optional T2 ──► fusion ──► ACTION
        │
        ▼
   mask spans (REDACT)          ◄── MUST happen here
        │
        ▼
   de-anonymization             ◄── spans are invalid after this point
        │
        ▼
   client
```

**The masking-before-de-anonymization constraint is load-bearing (T0-5).** Spans index
the placeholder-bearing string. `[EMAIL_1]` is 9 characters; the restored address might
be 22. Masking after restoration would corrupt the wrong character ranges, and ledger
spans are recorded against the pre-de-anonymization offsets.

---

## 2. The four checks

| Check | Basis | Severity | Override |
| :--- | :--- | :--- | :--- |
| Canary leak | exact string equality | `hard` | **yes** |
| Checksum IDs | Luhn / Verhoeff arithmetic | `high` | no |
| Secrets — Track 1 | provider-issued prefix | `hard` | **yes** |
| Secrets — Track 2 | entropy + heuristics | `high` | **no** (see T0-1) |
| Blocklist | org term list | `medium` | no |

All four run even when one fires. T0 costs well under a millisecond, and a complete
finding list is a far better audit record than a short-circuited one. The short-circuit
is downstream: `hard_override` tells the gateway to skip T1/T2.

### Check 1 — Dual canary

Minted per request by the **gateway**, never the tenant. A tenant who forgets to plant
one silently loses system-prompt leak detection, which is why the gateway owns upstream
payload assembly: *supply the parts, the gateway builds the call.*

| Canary | Placement | Leak means |
| :--- | :--- | :--- |
| `CP-CANARY-SYS-<hex>` | confidential line appended to the system message | system-prompt exfiltration |
| `CP-CANARY-CTX-<hex>` | `[doc-ref: ...]` tag on retrieved chunks | context exfiltration |

The asymmetry matters. The system canary sits in text the model is told never to reveal,
so any reproduction is unambiguous. The context canary sits in text the model is meant to
read and summarise, so it is formatted as **non-content metadata** — a model summarising
substance will not echo a reference tag; *"repeat the documents verbatim"* will.

Matching is exact, plus case-insensitive/whitespace-collapsed, plus an 8-character
prefix for truncated reproduction. On a non-RAG request the context check is **skipped,
not failed**.

### Check 2 — Checksum-validated identifiers

Reuses the shared catalog in `detectors/pii.py` rather than duplicating regexes between
the Input Gate and T0. Checksum validation is what makes the tier trustworthy rather than
noisy:

```
"Your order number is 1234567890123456."  →  zero findings   (fails Luhn)
"Your card 4111111111111111 was charged." →  CREDIT_CARD, confidence 1.0
```

### Check 3 — Secrets, two tracks

**Track 1** — provider prefixes (`sk-`, `AKIA`, `ghp_`, `xox[baprs]-`, JWT triple, PEM
headers). The prefix *is* the evidence; providers prefix keys precisely so scanners find
them. Near-zero false-positive rate, and roughly 90% of the real work.

**Track 2** — generic secrets with no recognisable prefix. Requires **both** an
assignment context (`api_key`, `password`, `Authorization: Bearer`, …) **and** a
high normalized entropy ratio. Never entropy alone: git SHAs, UUIDs, file hashes, base64
images, minified JS and our own canary tokens are all high-entropy non-secrets.

### Check 4 — Blocklist

Terms live **inline in the bundle**, not an external file. If the blocklist lived outside,
`policy_hash` would no longer pin actual enforcement behaviour and the audit guarantee
would develop a hole. Compiled into a **single alternation regex** cached by term tuple,
resolving **T0-6**: a naive per-term loop is O(N × text), and a realistic enterprise list
of a few thousand terms would consume the whole tier budget by itself.

---

## 3. Resolved defects

### T0-1 — Track 2 no longer holds a veto

T0's contract is about **certainty**, not speed. Canary matching, provider prefixes and
Luhn/Verhoeff arithmetic are determinations — they cannot be wrong. Track 2 is entropy
plus heuristics; it can be. Giving the only probabilistic check in the tier the most
severe and least reviewable consequence was backwards, and it made its own
false-positive rate unmeasurable (T0-7): a hard override stops the cascade that would
produce the evidence.

Track 2 now emits `high` and reaches BLOCK through a **categorical floor** instead.

### The floor, and why a plain downgrade was not enough

Measured before adding it — Track 2 at `high`, with T1 also running:

```
T0 alone (nothing else ran)   S_t0=0.75  w=1.00  fused=0.750  → BLOCK
T0 + quiet T1 detectors       S_t0=0.75  w=0.40  fused=0.332  → REDACT   ← diluted
```

A leaked credential degraded to REDACT the moment any T1 detector ran, because the `t0`
weight is 0.4. That is the **P4 defect reappearing on Tier 0** — and note that
`detector_critical_thresholds` contains only `{pii, grounding, toxicity}`, so T0 had no
floor to rescue it.

The new `t0_floor_severity` (locked, default `high`) floors fused risk at `high_band`
when any finding reaches that severity. Compared by **severity rank, never against the
normalized S scale** — T0 has no threshold and so carries none of T1's 0.5-midpoint
semantics (T0-2). Measured after:

```
T0 + quiet T1 detectors       fused=0.700  fired=['t0']  → BLOCK
blocklist only (medium)       fused=0.192  fired=[]      → ALLOW
```

Track 2 loses the unconditional veto it had not earned, and keeps the ability to act.

### T0-2 — T0 is categorical

Scored by `t0_severity_scores` lookup with `t0_aggregation`, not by the piecewise
normalizer. `S_t0` does **not** carry T1's 0.5-midpoint meaning, and T0 is excluded from
`detector_critical_thresholds` for the same reason.

### T0-4 — `origin` is three-valued

`warn-and-confirm` (Profile C) forwards raw PII and builds **no placeholder map**.
Checking only the map would label a benign echo of the user's own card
`model_generated`, inverting the exact distinction `origin` exists to draw.

```
[EMAIL_1] echoed back            → echoed_placeholder   (zero risk contribution)
user's own card echoed back      → echoed_from_input    (Profile C path)
novel identifier in the output   → model_generated      (training-data leakage)
```

Only `model_generated` findings reach fusion.

### T0-8 — canary limitation, documented not engineered

Canary matching detects **naive** exfiltration — verbatim or truncated reproduction. It
does **not** detect adversarially transformed reproduction (base64, reversed,
character-spaced, translated). A canary hit proves exfiltration occurred; its absence
does not prove it did not. Chasing arbitrary transformations is unbounded and would cost
far more than the ~0.01 ms the check takes.

---

## 4. New defects found during implementation

### T0-9 — the spec's dictionary guard is unreachable

The spec's `classify_generic_secret()` runs the entropy gate **before** the dictionary
guard. Passphrases have **low** entropy by construction, so `correcthorsebatterystaple`
is filtered out at the entropy step and never reaches the downgrade branch — it is
dropped entirely rather than downgraded to `medium`. That is the opposite of the spec's
stated intent: *"a passphrase in a password field IS a real credential… downgrade, do not
suppress."*

**Fix:** the dictionary check runs first. Safe, because Track 2 already requires
credential-ish assignment context — prose inside `password = "..."` is genuinely
suspicious, and `description = "..."` never reaches this code.

### T0-10 — the entropy threshold has a large false-negative rate — **open**

The spec's reference table was derived from **hand-constructed all-distinct strings**,
not sampled randomness. Its own motivating example gives away the problem:

```
aB3xK9mP2qL7vN4wR8tY6uZ1   len=24  distinct=24  ratio=1.000
```

Every character distinct means entropy is maximal *by construction*. Real random strings
repeat characters. Measured over 200 samples per length, against the shipped threshold
of `0.9`:

| Charset | Length | mean ratio | missed at 0.9 |
| :--- | ---: | ---: | ---: |
| hex | 24 | 0.868 | **157 / 200** |
| hex | 32 | 0.901 | 89 / 200 |
| hex | 48 | 0.937 | 15 / 200 |
| hex | 64 | 0.957 | 0 / 200 |
| alphanumeric | 24 | 0.926 | 34 / 200 |

At `secret_min_length = 24` — the shipped default — Track 2 misses roughly **78% of
genuinely random hex secrets**. The spec's claim that normalization makes one scalar
threshold valid across every charset does not survive contact with sampled data:
**residual length dependence remains**, because a 24-character draw from a 16-symbol
alphabet cannot approach uniformity.

The spec's own RFC example is also mis-stated: `550e8400-e29b-41d4-a716-446655440000`
is claimed at `0.96` but measures **0.812** — it is a non-random canonical example with
13 distinct characters out of 32.

Separator stripping is nonetheless confirmed load-bearing:

```
6b0d549b-6f03-675a-1600-a35a099950d8
  unstripped → charset classified as printable(95) → ratio 0.687   ✗
  stripped   → charset classified as hex(16)       → ratio 0.857   ✓
```

**Not fixed, because the fix is a policy decision.** Options:

1. **Raise `secret_min_length` to ~48.** Miss rate drops to 7.5%. Defensible — every
   credential the spec cites at 20–40 characters (AWS, GitHub, Slack, OpenAI) carries a
   prefix and is caught by Track 1 anyway.
2. **Lower the threshold to ~0.82.** Catches more hex, but prose sits at 0.73–0.75, so
   the margin shrinks to about 0.07 and false positives rise.
3. **Accept it.** Track 1 does ~90% of the work; Track 2 is a backstop.

Both fields are locked and bundle-driven, so this is exactly the calibration target the
Learning Plane's sweep exists to tune. It should not be left at a guess.

---

## 5. Privacy

`Finding` has **no `value` field**. Spans are sufficient for masking, so matched text is
never needed downstream — and omitting the field makes the type *structurally incapable*
of carrying a raw secret, PII value or canary token into the ledger. The privacy rule
stops depending on every future contributor remembering it.

`canary_leak` is an enum (`null | system | context | both`); canary **values** are never
written. Verified by `test_no_raw_value_secret_or_canary_reaches_the_ledger`.

---

## 6. Control Plane changes

| Field | Default | Direction | Locked |
| :--- | :--- | :--- | :--- |
| `t0_floor_severity` | `high` | `hard → high → medium → low` (lower fires on weaker findings) | **new** |
| `secret_min_length` | `24` | lower is stricter | **newly locked** |
| `secret_entropy_ratio_threshold` | `0.9` | lower is stricter | **newly locked** |

The two secret fields were registered in `LOWER_IS_STRICTER` but never listed in
`org_baseline.yaml`'s `locked_fields` — so until now a tenant could quietly disable
generic secret detection by raising the ratio to `0.99`, exactly as §8C of the spec
warned. Now closed.

---

## 7. Deliberately not built

* **T0-7 evaluation sampling** — routing a fraction of hard-overrides through the full
  cascade for measurement. Learning Plane concern.
* **Per-chunk unique context canaries** — the spec leaves this open; one shared CTX
  canary is the documented default. Per-chunk buys finer forensics (*which* document was
  exfiltrated) at negligible matching cost, and remains a reasonable upgrade.
* **IPv6.**

---

## 8. Open question for fusion

A blocklist-only hit scores `medium` → `0.40 × w_t0 0.4 = 0.16`, below `low_band`, so it
resolves to **ALLOW**. This is the register's open item #7 and it is still unconfirmed:
an org term appearing in output produces a ledger row and no action. Either that is
intended (blocklist is telemetry, not enforcement), or `medium` should sit above
`low_band`, or blocklist should carry its own floor.
