"""
Input Gate for the ControlPlane.ai Data Plane.

Order of operations, and why:

    raw prompt
      ├─ canonicalize()      -> scan-only view + evasion evidence  (never forwarded)
      │    └─ scan_injection()
      └─ scan_pii()          -> spans on the RAW prompt
           └─ redact()       -> forward prompt + volatile restore map

The two branches are independent by design. Injection detection needs an aggressively
folded view; PII redaction needs untouched offsets. Running them on one shared string
forces a choice between missing disguised injections and corrupting the placeholder map.

Action precedence, stated explicitly because the locking register (T0-3) found the
output side had no such rule:

    `injection_action` governs the disposition of the REQUEST.
    `pii_mode`         governs the handling of PII WITHIN a request that proceeds.

They compose rather than compete. BLOCK from either wins, because both mean the upstream
call must not happen. Otherwise a flagged injection proceeds with its prompt UNMODIFIED
and its score carried into fusion, where it tightens the output cascade.
"""

import json
import time
from functools import lru_cache
from typing import Dict, Tuple

from control_plane.compiler import compute_policy_hash
from data_plane.detectors.injection import scan_injection
from data_plane.detectors.pii import redact, scan_pii
from data_plane.models import InputGateResult
from data_plane.normalizer import canonicalize


class BundleIntegrityError(Exception):
    """Raised when a bundle's stored policy_hash does not match its contents."""


@lru_cache(maxsize=16)
def load_bundle(bundle_path: str) -> Tuple[str, str]:
    """
    Load a compiled bundle, verifying its policy_hash before use.

    This is `compile_bundle.md` Validation Check 3, applied at load rather than only at
    compile: a bundle whose contents no longer hash to its recorded policy_hash has been
    edited after compilation and must not drive enforcement.

    Cached by path. Returns (json_text, policy_hash); callers get a fresh dict via
    get_bundle() so a caller cannot mutate another caller's policy.
    """
    with open(bundle_path, "r", encoding="utf-8") as f:
        bundle = json.load(f)

    stored = bundle.get("policy_hash", "")
    recomputed = compute_policy_hash(bundle)
    if stored != recomputed:
        raise BundleIntegrityError(
            f"{bundle_path}: stored policy_hash {stored[:16]}... does not match "
            f"recomputed {recomputed[:16]}.... The bundle was modified after compilation; "
            f"recompile it rather than editing it."
        )
    return json.dumps(bundle), stored


def get_bundle(bundle_path: str) -> Dict:
    """Return a fresh, hash-verified bundle dict."""
    payload, _ = load_bundle(bundle_path)
    return json.loads(payload)


def process_input(raw_prompt: str, bundle: Dict) -> Tuple[InputGateResult, Dict[str, str]]:
    """
    Run the Input Gate.

    Returns (result, restore_map). The restore map is volatile: hold it for the request
    lifetime to de-anonymize the response, and never persist it. It is returned
    alongside the result rather than inside it so it cannot reach the ledger by
    accident - InputGateResult has no field that could carry it.
    """
    started = time.perf_counter()

    # --- injection branch: canonical view, scan only --------------------------------
    view = canonicalize(raw_prompt)
    injection_risk, injection_findings = scan_injection(view, bundle)

    injection_action = bundle.get("injection_action", "flag")
    injection_threshold = float(bundle.get("injection_threshold", 0.7))
    injection_fired = injection_risk >= injection_threshold

    # --- PII branch: raw prompt, offsets intact -------------------------------------
    pii_mode = bundle.get("pii_mode", "redact-and-proceed")
    pii_findings = scan_pii(raw_prompt)

    restore_map: Dict[str, str] = {}
    forward_prompt = raw_prompt
    if pii_findings and pii_mode == "redact-and-proceed":
        forward_prompt, restore_map = redact(raw_prompt, pii_findings)
    # warn-and-confirm forwards raw PII by design; block-and-explain never forwards.

    # --- disposition ----------------------------------------------------------------
    action = "ALLOW"
    reason = None

    if injection_fired and injection_action == "block":
        action, reason = "BLOCK", (
            f"Prompt injection detected ({injection_findings[0].pattern_name}, "
            f"risk {injection_risk:.2f} >= threshold {injection_threshold:.2f}). "
            f"This policy refuses such requests."
        )
    elif pii_findings and pii_mode == "block-and-explain":
        types = sorted({f.entity_type for f in pii_findings})
        action, reason = "BLOCK", (
            f"Request contains {', '.join(types)} and this policy does not permit "
            f"sensitive data to reach the model."
        )
    elif injection_fired and injection_action == "flag":
        # Deliberately NOT edited. Removing the matched span forwards the remainder of
        # the attack; the score is carried into fusion instead.
        action = "FLAG"

    if action == "BLOCK":
        forward_prompt = ""
        restore_map = {}

    result = InputGateResult(
        action=action,
        blocked_reason=reason,
        forward_prompt=forward_prompt,
        injection_risk=injection_risk,
        injection_findings=injection_findings,
        evasions=view.evasions,
        canonical_text=view.text,
        pii_findings=pii_findings,
        pii_mode=pii_mode,
        policy_hash=bundle.get("policy_hash", ""),
        latency_ms=(time.perf_counter() - started) * 1000.0,
    )
    return result, restore_map
