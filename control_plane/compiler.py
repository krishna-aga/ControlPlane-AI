"""
Bundle Compiler CLI and API for ControlPlane.ai
Compiles layered YAML policies into resolved, immutable, SHA-256 hashed JSON bundles.
"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Optional

from control_plane.models import BundleConfig
from control_plane.resolver import load_yaml_policy, resolve_policy


def compute_policy_hash(resolved_dict: dict) -> str:
    """Computes a deterministic SHA-256 hash over the canonical JSON representation of policy parameters."""
    # Exclude metadata fields that vary across compilations
    canonical_payload = {
        k: v for k, v in resolved_dict.items()
        if k not in ["policy_hash", "compiled_at", "description"]
    }
    canonical_json = json.dumps(canonical_payload, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def compile_bundle(
    base_policy_path: str,
    policy_path: str,
    out_path: str,
    pin_version: Optional[str] = None
) -> BundleConfig:
    """
    Compiles layered policy files into an immutable JSON bundle.
    1. Loads base policy and child policy YAMLs.
    2. Resolves inheritance and enforces strict locking rules.
    3. Computes deterministic SHA-256 policy_hash.
    4. Serializes BundleConfig to output path.
    """
    base_policy = load_yaml_policy(base_policy_path)
    child_policy = load_yaml_policy(policy_path)

    resolved = resolve_policy(base_policy, child_policy)

    if pin_version:
        resolved["policy_version"] = pin_version

    # Compute hash and timestamp
    policy_hash = compute_policy_hash(resolved)
    compiled_at = datetime.now(timezone.utc).isoformat()

    resolved["policy_hash"] = policy_hash
    resolved["compiled_at"] = compiled_at

    bundle = BundleConfig(**resolved)

    # Ensure output directory exists
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)

    # Write formatted bundle JSON
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bundle.model_dump(), f, indent=2)

    print(f"[ControlPlane Compiler] Compiled bundle '{bundle.policy_name}' ({bundle.policy_version}) -> {out_path}")
    print(f"[ControlPlane Compiler] Policy SHA-256 Hash: {bundle.policy_hash}")
    return bundle


def main():
    parser = argparse.ArgumentParser(description="ControlPlane.ai Policy Bundle Compiler")
    parser.add_argument(
        "--base",
        default="policies/org_baseline.yaml",
        help="Path to baseline policy YAML (default: policies/org_baseline.yaml)",
    )
    parser.add_argument(
        "--policy",
        required=True,
        help="Path to child use-case policy YAML",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output path for compiled bundle JSON (e.g. bundles/customer_support_bundle.json)",
    )
    parser.add_argument(
        "--pin-version",
        default=None,
        help="Optional version override (e.g. v1.0.1 or pinned tag)",
    )

    args = parser.parse_args()
    try:
        compile_bundle(
            base_policy_path=args.base,
            policy_path=args.policy,
            out_path=args.out,
            pin_version=args.pin_version,
        )
    except Exception as e:
        print(f"[ControlPlane Compiler Error] {e}")
        exit(1)


if __name__ == "__main__":
    main()
