# Tier 0 — Deterministic Output Checks

**Status:** Implemented. `data_plane/canary.py`, `data_plane/detectors/t0.py`,
`Finding`/`T0Result` in `data_plane/models.py`, categorical floor in
`data_plane/fusion.py`. 44 T0 tests; 114 passing overall.

Implements [`t0_deterministic_checks.md`](../.agents/skills/t0_deterministic_checks.md)
and resolves T0-1, T0-2, T0-4, T0-6 and T0-8 from the locking register. Three further
defects were found while implementing and testing it — **T0-9**, **T0-10** and
**T0-11** — all now fixed and recorded in §4.

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

## 4. Defects found during implementation

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

### T0-10 — the entropy threshold missed most real secrets — **fixed**

The spec's reference table was derived from **hand-constructed all-distinct strings**,
not sampled randomness. Its own motivating example gives the problem away:

```
aB3xK9mP2qL7vN4wR8tY6uZ1   len=24  distinct=24  ratio=1.000
```

Twenty-four distinct characters means entropy is maximal *by construction* — and that
string uses a 62-symbol alphanumeric alphabet. Hex has only **16** symbols, so drawing
24 characters makes repeats mathematically unavoidable; real hex secrets land at 12–14
distinct and score 0.84–0.89.

**The threshold was calibrated on a 62-symbol alphabet and applied to a 16-symbol one,
where that score is unreachable.**

Measured against the shipped threshold of `0.9`, before the fix:

| charset | length | mean ratio | missed |
| :--- | ---: | ---: | ---: |
| hex | 24 | 0.868 | **227 / 300** |
| hex | 32 | 0.903 | 127 / 300 |
| hex | 48 | 0.935 | 28 / 300 |
| alphanumeric | 24 | 0.923 | 57 / 300 |

At the shipped `secret_min_length = 24`, Track 2 was missing roughly **78% of genuinely
random hex secrets**.

#### Root cause

`normalized_entropy` divided by the **theoretical maximum**, `log2(k)` — a score
randomness does not reach. The spec's own `min(log2 k, log2 n)` was an attempt to remove
length dependence and did not, because the binding constraint is not the ceiling but the
*sampling bias* of estimating entropy from few draws.

#### Fix: divide by expected-random entropy, not by the maximum

The denominator is now the entropy a genuinely random string of the same length and
alphabet would actually score, via the **Miller–Madow bias correction**:

$$E[H] = \log_2(k) - \frac{k-1}{2n \ln 2}$$

|  | theoretical max | expected random |
| :--- | ---: | ---: |
| hex, 24 chars | 4.000 | **3.549** |
| hex, 48 chars | 4.000 | **3.775** |
| alphanumeric, 24 chars | 4.585 | **4.121** |

It is a grading curve: the question changes from *"how close is this to perfect?"* to
*"how close is this to what randomness actually produces at this length?"* A random
string now scores ≈1.0 at **any** length and **any** alphabet — which is what the spec's
"one scalar threshold across every charset" claim required and never delivered.

#### Measured after the fix — same threshold, same length floor

| charset | length | recall before | **recall after** |
| :--- | ---: | ---: | ---: |
| hex | 24 | 22% | **95.3%** |
| hex | 32 | 58% | **98.0%** |
| hex | 48 | 91% | **100%** |
| alphanumeric | 24 | 81% | **100%** |

The false-positive margin is preserved — non-random strings rise, but far less than
random ones, so the gap widens rather than closing:

| string | before | after |
| :--- | ---: | ---: |
| `password_password_password_1234` | 0.673 | 0.738 |
| `correcthorsebatterystaple` | 0.724 | 0.802 |
| `supportticketreferenceid` | 0.754 | 0.838 |
| `mycompanyname2024internal` | 0.797 | 0.883 |
| 24 identical characters | 0.000 | 0.000 |

Random now clusters at 0.98–1.00 and non-random at 0.74–0.88, so the `0.9` threshold
sits cleanly in a ~0.10 gap. The tightest case is `mycompanyname2024internal` at 0.883 —
and it would have to appear as `api_key = "mycompanyname2024internal"` to be tested at
all.

The originally-reported case:

```
db_password = "8db03397863983ead5ea8804"
  before  ratio 0.838  →  no finding, secret leaks
  after   ratio 0.944  →  GENERIC_SECRET / high
```

Ratios are **clamped to [0, 1]**: Miller–Madow slightly undershoots for large alphabets,
so a maximally-random alphanumeric string computes just above 1.0.

#### Rejected alternatives

* **Raise `secret_min_length` to 48.** Considered and rejected — it *reduces* detection.
  A 24-character secret falls below the floor and is never examined, taking recall at
  that length from 22% to zero. It is an honesty argument, not a recall argument.
* **Lower the threshold to 0.83.** Works, but leaves only ~0.03 of margin above
  `mycompanyname2024internal` and does nothing about the underlying length dependence —
  it would need re-tuning for every new length and charset.

### T0-11 — the assignment regex missed every prefixed key name — **fixed**

Found while tracing the T0-10 case end to end. The pattern used `\b` immediately before
the keyword, but `_` is a word character, so there is no boundary between `_` and
`password`:

```
password = "..."       match
db_password = "..."    NO MATCH   ← missed
my_api_key = "..."     NO MATCH   ← missed
user_token: "..."      NO MATCH   ← missed
```

Prefixed key names are the normal convention in real configuration, so Track 2 was
silently missing most of its actual targets **regardless of any threshold**. A
`[A-Za-z0-9_]*` prefix now precedes the keyword. Regression:
`test_prefixed_key_names_are_matched`.

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
