"""
Unit test suite for Control Plane (ControlPlane.ai)
Tests policy resolution, strict field locking exceptions, latency budget checks, and bundle compilation.
Uses standard library unittest.
"""

import os
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


if __name__ == "__main__":
    unittest.main()
