"""
Unit test suite for Control Plane (ControlPlane.ai)
Tests policy resolution, strict field locking exceptions, latency budget checks, and bundle compilation.
Uses standard library unittest.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from pydantic import ValidationError

from control_plane.models import BundleConfig, PolicyConfig, PolicyLockingError
from control_plane.resolver import resolve_policy, load_yaml_policy
from control_plane.compiler import compile_bundle, compute_policy_hash


class TestControlPlane(unittest.TestCase):

    def test_compile_all_personas(self):
        """Verify that all three target persona YAML policies compile successfully into bundles."""
        personas = ["customer_support", "decision_support", "internal_copilot"]
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            for persona in personas:
                policy_file = f"policies/{persona}.yaml"
                out_file = os.path.join(tmp_dir, f"{persona}_bundle.json")
                
                bundle = compile_bundle(
                    base_policy_path="policies/org_baseline.yaml",
                    policy_path=policy_file,
                    out_path=out_file,
                )
                
                self.assertTrue(os.path.exists(out_file))
                self.assertIsNotNone(bundle.policy_hash)
                self.assertEqual(len(bundle.policy_hash), 64)  # SHA-256 hex string length

    def test_strict_locking_exception(self):
        """Verify that attempting to loosen a locked field raises PolicyLockingError."""
        base = PolicyConfig(
            pii_threshold=0.8,
            locked_fields=["pii_threshold"]
        )
        # Child attempts looser threshold (0.9 catches LESS PII -> looser)
        child = PolicyConfig(pii_threshold=0.9)

        with self.assertRaises(PolicyLockingError) as ctx:
            resolve_policy(base, child)
        
        self.assertIn("LOCKED by parent policy", str(ctx.exception))

    def test_stricter_override_allowed(self):
        """Verify that child policy can specify a STRICTER threshold for a locked field."""
        base = PolicyConfig(
            pii_threshold=0.8,
            locked_fields=["pii_threshold"]
        )
        # Child specifies stricter threshold (0.5 catches MORE PII -> stricter)
        child = PolicyConfig(pii_threshold=0.5)

        resolved = resolve_policy(base, child)
        self.assertEqual(resolved["pii_threshold"], 0.5)

    def test_no_latency_budget_field_exists(self):
        """
        Latency is measured and reported, never negotiated - a tenant cannot buy speed
        with safety. The removed budget allowed a safety-theatre bundle: minimum budget
        plus fail_open meant any detector overrun shipped unchecked output, while still
        compiling to a clean, hash-attested bundle.

        `t2_enabled` is now the only depth knob. See docs/NO_LATENCY_BUDGET.md
        """
        self.assertNotIn("latency_budget_ms", PolicyConfig.model_fields)
        self.assertNotIn("latency_budget_ms", BundleConfig.model_fields)

        # An unknown field must not silently reappear through a policy file either.
        resolved = resolve_policy(PolicyConfig(), PolicyConfig(t2_enabled=True))
        self.assertNotIn("latency_budget_ms", resolved)

    def test_fail_mode_is_locked_and_tightenable_only(self):
        """
        fail_mode is the other half of the starve vector: a short budget only shipped
        unchecked output because fail_open was reachable. It is now SET and locked in
        the baseline at its loosest rung, so tenants may tighten and never loosen.
        """
        base = load_yaml_policy("policies/org_baseline.yaml")
        self.assertIn("fail_mode", base.locked_fields)

        tightened = resolve_policy(base, PolicyConfig(fail_mode="fail_closed"))
        self.assertEqual(tightened["fail_mode"], "fail_closed")

        stricter_base = PolicyConfig(fail_mode="fail_closed", locked_fields=["fail_mode"])
        with self.assertRaises(PolicyLockingError):
            resolve_policy(stricter_base, PolicyConfig(fail_mode="fail_open"))

    def test_hash_reproducibility(self):
        """Verify that identical policy settings produce identical SHA-256 policy_hash."""
        resolved_a = {"policy_name": "test", "pii_threshold": 0.5, "toxicity_threshold": 0.7}
        resolved_b = {"toxicity_threshold": 0.7, "pii_threshold": 0.5, "policy_name": "test"}

        hash_a = compute_policy_hash(resolved_a)
        hash_b = compute_policy_hash(resolved_b)

        self.assertEqual(hash_a, hash_b)


class TestLockingDirection(unittest.TestCase):
    """
    Regression tests for P1: grounding_threshold is a SIMILARITY FLOOR, so raising it
    demands more grounding and is STRICTER. It was previously grouped with the risk
    ceilings, which inverted the lock - tenants could loosen it and were blocked from
    tightening it. See docs/POLICY_LOCKING_AND_RISK_NORMALIZATION.md
    """

    def _base(self):
        return PolicyConfig(
            grounding_threshold=0.6,
            toxicity_threshold=0.7,
            locked_fields=["grounding_threshold", "toxicity_threshold"],
        )

    def test_grounding_threshold_tightening_allowed(self):
        """Raising grounding_threshold demands MORE grounding -> stricter -> allowed."""
        resolved = resolve_policy(self._base(), PolicyConfig(grounding_threshold=0.9))
        self.assertEqual(resolved["grounding_threshold"], 0.9)

    def test_grounding_threshold_loosening_rejected(self):
        """Lowering grounding_threshold demands LESS grounding -> looser -> rejected."""
        with self.assertRaises(PolicyLockingError):
            resolve_policy(self._base(), PolicyConfig(grounding_threshold=0.1))

    def test_toxicity_threshold_loosening_rejected(self):
        """P2: toxicity_threshold is now locked, so it cannot be raised to disable detection."""
        with self.assertRaises(PolicyLockingError):
            resolve_policy(self._base(), PolicyConfig(toxicity_threshold=1.0))

    def test_enum_tightening_allowed(self):
        """P3: locked enums must be tightenable, not frozen."""
        base = PolicyConfig(pii_mode="redact-and-proceed", locked_fields=["pii_mode"])
        resolved = resolve_policy(base, PolicyConfig(pii_mode="block-and-explain"))
        self.assertEqual(resolved["pii_mode"], "block-and-explain")

    def test_enum_loosening_rejected(self):
        base = PolicyConfig(pii_mode="redact-and-proceed", locked_fields=["pii_mode"])
        with self.assertRaises(PolicyLockingError):
            resolve_policy(base, PolicyConfig(pii_mode="warn-and-confirm"))


class TestInjectionPolicy(unittest.TestCase):
    """
    Input Gate policy fields. injection_action is a three-rung ladder
    (allow -> flag -> block) with no 'sanitize': lexical removal of a matched
    injection span forwards the remainder of the attack. 'flag' proceeds with the
    prompt unmodified and routes the score into fusion. See docs/INPUT_GATE.md
    """

    def _baseline(self):
        return load_yaml_policy("policies/org_baseline.yaml")

    def test_injection_action_tightening_allowed(self):
        resolved = resolve_policy(self._baseline(), PolicyConfig(injection_action="block"))
        self.assertEqual(resolved["injection_action"], "block")

    def test_injection_action_loosening_rejected(self):
        with self.assertRaises(PolicyLockingError):
            resolve_policy(self._baseline(), PolicyConfig(injection_action="allow"))

    def test_injection_threshold_tightening_allowed(self):
        """injection_threshold is a risk ceiling -> lowering catches more -> stricter."""
        resolved = resolve_policy(self._baseline(), PolicyConfig(injection_threshold=0.4))
        self.assertEqual(resolved["injection_threshold"], 0.4)

    def test_injection_threshold_loosening_rejected(self):
        with self.assertRaises(PolicyLockingError):
            resolve_policy(self._baseline(), PolicyConfig(injection_threshold=0.95))

    def test_baseline_sets_every_field_it_locks(self):
        """
        A field listed in locked_fields but never assigned in the baseline is skipped
        by the strictness check entirely (resolve_policy only compares when the key is
        present in the base dump), so the lock would silently do nothing. Guard the
        whole baseline, not just the injection fields.
        """
        baseline = self._baseline()
        assigned = baseline.model_dump(exclude_unset=True)
        for field in baseline.locked_fields:
            with self.subTest(field=field):
                self.assertIn(
                    field, assigned,
                    f"'{field}' is locked but unset in org_baseline.yaml - the lock is a no-op",
                )

    def test_personas_resolve_expected_injection_policy(self):
        expected = {
            "customer_support": ("flag", 0.7),
            "decision_support": ("block", 0.5),
            "internal_copilot": ("flag", 0.7),
        }
        baseline = self._baseline()
        for persona, (action, threshold) in expected.items():
            with self.subTest(persona=persona):
                resolved = resolve_policy(baseline, load_yaml_policy(f"policies/{persona}.yaml"))
                self.assertEqual(resolved["injection_action"], action)
                self.assertEqual(resolved["injection_threshold"], threshold)


class TestStructuralValidators(unittest.TestCase):
    """Regression tests for P5: configurations that previously compiled but should not."""

    def test_inverted_bands_rejected(self):
        base = PolicyConfig(low_band=0.3, high_band=0.7)
        with self.assertRaises(ValueError) as ctx:
            resolve_policy(base, PolicyConfig(low_band=0.8, high_band=0.3))
        self.assertIn("exceeds high_band", str(ctx.exception))

    def test_detector_weights_is_gone(self):
        """
        Removed with the move to max-aggregation. It also closed a hole: the sum-to-1.0
        validator accepted {t0: 2.0, pii: -1.0} and {nonsense: 1.0}, since it checked
        only the total and never the signs or the key set.
        """
        self.assertNotIn("detector_weights", PolicyConfig.model_fields)
        self.assertNotIn("detector_weights", BundleConfig.model_fields)

    def test_critical_threshold_below_detection_rejected(self):
        """
        Critical thresholds are on the normalized S scale where 0.5 is the detection
        threshold. Below 0.5 the 'critical' floor would fire before normal detection.
        """
        base = PolicyConfig()
        child = PolicyConfig(detector_critical_thresholds={"toxicity": 0.3})
        with self.assertRaises(ValueError) as ctx:
            resolve_policy(base, child)
        self.assertIn("normalized S scale", str(ctx.exception))

    def test_inert_critical_threshold_warns_not_rejects(self):
        """
        T1-8: a critical value >= high_band can never fire before the band arithmetic
        already blocks - legal (a tenant may tighten high_band later), so it warns
        rather than raises. The shipped baseline ships exactly this shape.
        """
        from control_plane.resolver import InertCriticalThresholdWarning
        base = load_yaml_policy("policies/org_baseline.yaml")
        with self.assertWarns(InertCriticalThresholdWarning) as ctx:
            resolved = resolve_policy(base, PolicyConfig())
        self.assertIn("inert", str(ctx.warning))
        self.assertLessEqual(resolved["low_band"], resolved["high_band"])   # still compiles

    def test_critical_threshold_below_high_band_does_not_warn(self):
        base = load_yaml_policy("policies/org_baseline.yaml")
        child = PolicyConfig(
            detector_critical_thresholds={"pii": 0.6, "grounding": 0.6, "toxicity": 0.6})
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            resolve_policy(base, child)
        from control_plane.resolver import InertCriticalThresholdWarning
        self.assertFalse(any(issubclass(w.category, InertCriticalThresholdWarning) for w in caught))

    def test_valid_baseline_still_compiles(self):
        """The shipped baseline must satisfy every new validator."""
        base = load_yaml_policy("policies/org_baseline.yaml")
        for persona in ["customer_support", "decision_support", "internal_copilot"]:
            resolved = resolve_policy(base, load_yaml_policy(f"policies/{persona}.yaml"))
            self.assertLessEqual(resolved["low_band"], resolved["high_band"])


class TestLockedMaps(unittest.TestCase):
    """
    P4c: maps need entry-by-entry comparison. A scalar check cannot express "every entry
    at least as strict", and the generic fallback would freeze the map entirely rather
    than allowing legitimate tightening.
    """

    def _baseline(self):
        return load_yaml_policy("policies/org_baseline.yaml")

    def test_both_maps_are_locked_by_the_baseline(self):
        locked = self._baseline().locked_fields
        self.assertIn("detector_critical_thresholds", locked)
        self.assertIn("t0_severity_scores", locked)

    def test_key_removal_is_rejected(self):
        """
        The load-bearing guard. Without it a tenant evades every per-key check by simply
        omitting the key - dropping the grounding and toxicity floors entirely.
        """
        with self.assertRaises(PolicyLockingError) as ctx:
            resolve_policy(self._baseline(),
                           PolicyConfig(detector_critical_thresholds={"pii": 0.9}))
        self.assertIn("cannot be removed", str(ctx.exception))

    def test_critical_thresholds_lower_is_stricter(self):
        base = self._baseline()
        tightened = resolve_policy(
            base, PolicyConfig(detector_critical_thresholds={"pii": 0.7, "grounding": 0.8, "toxicity": 0.9}))
        self.assertEqual(tightened["detector_critical_thresholds"]["pii"], 0.7)

        with self.assertRaises(PolicyLockingError):
            resolve_policy(base, PolicyConfig(
                detector_critical_thresholds={"pii": 1.0, "grounding": 1.0, "toxicity": 1.0}))

    def test_t0_severity_scores_higher_is_stricter(self):
        """
        Matters more since fusion moved to max aggregation: the table is read directly
        rather than diluted, so zeroing it silences Tier 0's contribution outright.
        """
        base = self._baseline()
        tightened = resolve_policy(base, PolicyConfig(
            t0_severity_scores={"hard": 1.0, "high": 0.9, "medium": 0.6, "low": 0.3}))
        self.assertEqual(tightened["t0_severity_scores"]["medium"], 0.6)

        with self.assertRaises(PolicyLockingError):
            resolve_policy(base, PolicyConfig(
                t0_severity_scores={"hard": 0.0, "high": 0.0, "medium": 0.0, "low": 0.0}))

    def test_a_non_mapping_override_is_rejected(self):
        with self.assertRaises(PolicyLockingError):
            resolve_policy(self._baseline(), PolicyConfig(t0_severity_scores={}))


class TestUnassignedLocks(unittest.TestCase):
    """P6: a lock is only meaningful against a value."""

    def test_locking_a_field_the_baseline_never_assigns_is_rejected(self):
        """
        The strictness check only fires when the key is present in the base dump, so
        such a lock silently permits any override. It must fail at compile instead.
        """
        with self.assertRaises(PolicyLockingError) as ctx:
            resolve_policy(PolicyConfig(locked_fields=["cache_threshold"]),
                           PolicyConfig(cache_threshold=0.01))
        self.assertIn("never assigns", str(ctx.exception))

    def test_child_may_lock_a_field_it_sets_itself(self):
        """
        Only BASE-declared locks are checked. A child locking a field it assigns is
        constraining downstream layers, not creating a no-op.
        """
        resolved = resolve_policy(
            PolicyConfig(), PolicyConfig(cache_threshold=0.5, locked_fields=["cache_threshold"]))
        self.assertEqual(resolved["cache_threshold"], 0.5)
        self.assertIn("cache_threshold", resolved["locked_fields"])

    def test_shipped_baseline_assigns_everything_it_locks(self):
        base = load_yaml_policy("policies/org_baseline.yaml")
        assigned = set(base.model_dump(exclude_unset=True))
        self.assertEqual(sorted(set(base.locked_fields) - assigned), [])


class TestUnknownKeysRejected(unittest.TestCase):
    """P8: a typo must not compile clean with enforcement silently unchanged."""

    def test_typo_in_a_policy_is_rejected(self):
        with self.assertRaises(ValidationError):
            PolicyConfig(**{"pii_treshold": 0.1})

    def test_removed_fields_are_rejected_rather_than_ignored(self):
        for gone in ("detector_weights", "latency_budget_ms"):
            with self.subTest(field=gone):
                with self.assertRaises(ValidationError):
                    PolicyConfig(**{gone: 1})

    def test_valid_policies_still_load(self):
        for name in ("org_baseline", "customer_support", "decision_support", "internal_copilot"):
            with self.subTest(policy=name):
                self.assertIsNotNone(load_yaml_policy(f"policies/{name}.yaml"))


class TestHashDeterminism(unittest.TestCase):
    """
    Regression cover for the policy_hash reproducibility defect.

    locked_fields was built with list(set(...)). Python randomizes string hashing
    per process, so the list order - and therefore the SHA-256 over the canonical
    payload - changed on every run. json.dumps(sort_keys=True) sorts dict keys but
    NOT list elements, so nothing downstream corrected for it.

    test_hash_reproducibility above cannot catch this: it hashes two hand-written
    dicts inside a single interpreter. The defect only shows across processes.
    """

    SEEDS = ["0", "1", "42", "12345", "99999"]

    # Compiles a persona and prints "<hash>\t<canonical locked_fields json>".
    PROBE = (
        "from control_plane.resolver import load_yaml_policy, resolve_policy;"
        "from control_plane.compiler import compute_policy_hash;"
        "r = resolve_policy("
        "    load_yaml_policy('policies/org_baseline.yaml'),"
        "    load_yaml_policy('policies/%s.yaml'));"
        "import json, sys;"
        "sys.stdout.write(compute_policy_hash(r) + chr(9) + json.dumps(r['locked_fields']))"
    )

    def _compile_under_seed(self, persona: str, seed: str):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        proc = subprocess.run(
            [sys.executable, "-c", self.PROBE % persona],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        policy_hash, locked_json = proc.stdout.split("\t")
        return policy_hash, json.loads(locked_json)

    def test_hash_stable_across_processes(self):
        """Identical inputs must produce an identical hash under any PYTHONHASHSEED."""
        for persona in ["customer_support", "decision_support", "internal_copilot"]:
            with self.subTest(persona=persona):
                results = [self._compile_under_seed(persona, s) for s in self.SEEDS]
                hashes = {h for h, _ in results}
                self.assertEqual(
                    len(hashes), 1,
                    f"{persona} produced {len(hashes)} distinct hashes across "
                    f"PYTHONHASHSEED values {self.SEEDS}: {sorted(hashes)}",
                )

    def test_locked_fields_ordering_is_canonical(self):
        """
        The ordering itself must be canonical, not merely incidentally stable -
        it is what the hash is taken over.
        """
        for persona in ["customer_support", "decision_support", "internal_copilot"]:
            with self.subTest(persona=persona):
                for seed in self.SEEDS:
                    _, locked = self._compile_under_seed(persona, seed)
                    self.assertEqual(locked, sorted(locked))

    def test_committed_bundle_hashes_are_reproducible(self):
        """
        Recompiling a shipped bundle must reproduce the policy_hash recorded in it.
        This is the guarantee the Data Plane's load-by-policy_hash depends on, and
        the check compile_bundle.md lists as Validation Check 3.
        """
        for persona in ["customer_support", "decision_support", "internal_copilot"]:
            with self.subTest(persona=persona):
                bundle_path = f"bundles/{persona}_bundle.json"
                if not os.path.exists(bundle_path):
                    self.skipTest(f"{bundle_path} not present")
                with open(bundle_path, encoding="utf-8") as f:
                    stored = json.load(f)
                recomputed, _ = self._compile_under_seed(persona, "0")
                self.assertEqual(
                    recomputed, stored["policy_hash"],
                    f"{bundle_path} is stale or was compiled under the "
                    f"non-deterministic hash; recompile it.",
                )


if __name__ == "__main__":
    unittest.main()
