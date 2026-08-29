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


if __name__ == "__main__":
    unittest.main()
