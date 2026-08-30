# Design Decision: No Latency Budget

**Status:** Implemented. `latency_budget_ms`, `_validate_latency_budget()`,
`LatencyBudgetExceededError` and the `T0/T1/T2_ESTIMATED_MS` constants are removed.
`fail_mode` is now set and locked in the baseline. 70 tests passing.

> **Latency is measured and reported, never negotiated. A tenant cannot buy speed with
> safety in this system.**

---

## 1. What was wrong

`latency_budget_ms` was tenant-settable and unlocked, and `fail_mode` was neither set nor
locked in `org_baseline.yaml`. This compiled cleanly:

```yaml
latency_budget_ms: 45     # exactly T0(5) + T1(40) — passes the feasibility check
fail_mode: fail_open
```

At runtime the budget became the detector timeout, and `fail_open` meant any overrun
**shipped unchecked output**. The result was a bundle that passed every validator, was
SHA-256 attested, looked compliant — and enforced nothing under load. Safety theatre,
reachable by configuration.

Locking the field would not have helped on its own: neither `latency_budget_ms` nor
`fail_mode` was *assigned* in the baseline, and `resolve_policy()` only compares a locked
field when the key is present in the base dump. Locking an unset field is a silent no-op.

## 2. It also broke for honest tenants

`T0_ESTIMATED_MS = 5`, `T1_ESTIMATED_MS = 40`, `T2_ESTIMATED_MS = 600` were unvalidated
module constants — hardcoded guesses, in direct violation of the project's own
no-hardcoded-constants rule. Measured T0 (the Input Gate) is **0.13 ms**, roughly 40×
under its estimate; real T1 with NER and embeddings over a long response comfortably
exceeds 40 ms.

So a tenant innocently choosing 200 ms would see intermittent timeouts, and with
`fail_open` those become intermittent unchecked passes — under exactly the load where
checking matters most.

## 3. One field doing two incompatible jobs

* **Compile-time feasibility** — "do my enabled checks fit?" Fails closed. Safe.
* **Runtime cutoff** — "kill detectors at N ms." With `fail_open`, unsafe.

Splitting them was considered (`latency_target_ms` tenant-owned, `detector_timeout_ms`
org-locked). Removal is better: **the simplest secure design is no mechanism.** There is
no number left for a tenant to under-provision.

## 4. `t2_enabled` is the latency differentiation

The problem statement names latency as a way use cases differ:

> *"Different AI use cases have very different risk tolerance and latency budgets — a
> single, one-size-fits-all checking approach rarely works well everywhere."*

That axis is fully preserved, expressed as a safety decision rather than a millisecond
count a tenant could game:

| Persona | `t2_enabled` | Consequence |
| :--- | :--- | :--- |
| `customer_support` | `false` | skips the ~600 ms judge — fast by doing less checking |
| `internal_copilot` | `false` | same |
| `decision_support` | `true` | pays the judge — slow by doing more checking |

The tenant chooses **how much checking they want**. The latency follows from that choice
rather than being negotiated against it.

## 5. What is kept

**Measurement.** Per-stage `latency_ms` is recorded in every result and every ledger row.
The Input Gate reports 0.13 ms today. Reporting what safety cost is free and it is what
makes any performance claim credible — what is gone is *policy trading against* it.

**`fail_mode`, with a narrower trigger.** It no longer fires on timeouts, because there
are none. It fires on detector **exceptions** and model-call failures. Every doc that
says "hard timeouts on detector execution fall back to fail_open/fail_closed" is now
describing a mechanism that does not exist and should read "detector failures".

**`fail_mode` is now locked.** It is set to `fail_open` in the baseline — the loosest
rung of `ENUM_STRICTNESS` — so tenants may tighten to `fail_closed` and can never loosen.
This closes the other half of the starve vector at zero cost: all three personas keep
their existing behaviour unchanged.

## 6. Honest limitation

In a synchronous prototype, timeout enforcement was never exercised anyway — T0 runs in
0.13 ms and would never have hit a 5 ms cutoff. Removing the budget deletes a control
that was architectural rather than operative. Say that plainly rather than letting it be
discovered.

## 7. Migration

* `PolicyConfig` / `BundleConfig`: `latency_budget_ms` removed.
* `LatencyBudgetExceededError` removed from `models.py` and `control_plane.__all__`.
* `resolver.py`: `_validate_latency_budget()` and the three estimate constants removed;
  `latency_budget_ms` dropped from `HIGHER_IS_STRICTER`.
* All three persona YAMLs: field removed. `org_baseline.yaml`: `fail_mode` set and locked.
* `test_latency_budget_exceeded` replaced by `test_no_latency_budget_field_exists` and
  `test_fail_mode_is_locked_and_tightenable_only`.
* All three bundle hashes changed.
