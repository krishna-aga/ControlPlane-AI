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

from control_plane.models import (
    PolicyConfig,
    PolicyLockingError,
    LatencyBudgetExceededError,
)
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

    def test_latency_budget_exceeded(self):
        """Verify LatencyBudgetExceededError when budget cannot fit enabled T2 judge (645ms required)."""
        base = PolicyConfig(latency_budget_ms=200)
        child = PolicyConfig(t2_enabled=True, latency_budget_ms=200)

        with self.assertRaises(LatencyBudgetExceededError) as ctx:
            resolve_policy(base, child)
        
        self.assertIn("Latency budget of 200ms is insufficient", str(ctx.exception))

    def test_hash_reproducibility(self):
        """Verify that identical policy settings produce identical SHA-256 policy_hash."""
        resolved_a = {"policy_name": "test", "pii_threshold": 0.5, "latency_budget_ms": 500}
        resolved_b = {"latency_budget_ms": 500, "pii_threshold": 0.5, "policy_name": "test"}

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

    def test_weights_must_sum_to_one(self):
        base = PolicyConfig()
        child = PolicyConfig(detector_weights={"t0": 5.0, "pii": 5.0})
        with self.assertRaises(ValueError) as ctx:
            resolve_policy(base, child)
        self.assertIn("must sum to 1.0", str(ctx.exception))

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

    def test_valid_baseline_still_compiles(self):
        """The shipped baseline must satisfy every new validator."""
        base = load_yaml_policy("policies/org_baseline.yaml")
        for persona in ["customer_support", "decision_support", "internal_copilot"]:
            resolved = resolve_policy(base, load_yaml_policy(f"policies/{persona}.yaml"))
            self.assertAlmostEqual(sum(resolved["detector_weights"].values()), 1.0)
            self.assertLessEqual(resolved["low_band"], resolved["high_band"])


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
