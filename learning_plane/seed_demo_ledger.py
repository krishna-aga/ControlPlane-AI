"""
Seed a demo audit ledger for the Learning Plane Streamlit app.

Real gateway traffic (`data_plane/gateway.py`) is too sparse to screen-record from a
cold start, so this generates synthetic rows in EXACTLY the shape `Gateway.finish()`
writes — same keys, same nesting, same privacy rule (types/spans/scores, never values)
— across the three real bundle personas in `bundles/`. The hash chain is computed with
the SAME function `data_plane.ledger.Ledger` uses, so `Ledger.verify()` and the
"tamper a row" demo in the app are genuine, not staged.

Usage:
    python -m learning_plane.seed_demo_ledger --per-persona 250 --out learning_plane/data/demo_ledger.json
"""

import argparse
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from data_plane.ledger import Ledger, _row_hash

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLES_DIR = REPO_ROOT / "bundles"

PII_ENTITY_TYPES = ["EMAIL", "PHONE", "CREDIT_CARD", "AADHAAR", "SSN"]
INJECTION_PATTERNS = [
    "ignore_previous_instructions",
    "role_override",
    "system_prompt_exfiltration",
    "delimiter_escape",
    "encoded_payload_smuggling",
]
PROVIDERS = [("gemini", "gemini-3.5-flash-lite"), ("mock", "mock-model")]

# Action weights per persona, tuned to match each bundle's posture (see context.md
# §3): decision_support is fail_closed with t2 on, so it blocks/flags far more.
ACTION_WEIGHTS = {
    "customer_support": {"ALLOW": 0.62, "REDACT": 0.16, "FLAG": 0.08, "BLOCK": 0.07, "REGENERATE": 0.07},
    "decision_support": {"ALLOW": 0.45, "REDACT": 0.10, "FLAG": 0.18, "BLOCK": 0.22, "REGENERATE": 0.05},
    "internal_copilot": {"ALLOW": 0.68, "REDACT": 0.11, "FLAG": 0.10, "BLOCK": 0.05, "REGENERATE": 0.06},
}

BAND_RANGE = {
    "ALLOW": (0.02, 0.29),
    "REDACT": (0.30, 0.55),
    "REGENERATE": (0.32, 0.60),
    "FLAG": (0.40, 0.69),
    "BLOCK": (0.72, 0.99),
}


def load_bundles() -> List[Dict]:
    bundles = []
    for name in ("customer_support_bundle.json", "decision_support_bundle.json", "internal_copilot_bundle.json"):
        data = json.loads((BUNDLES_DIR / name).read_text())
        data["_persona_key"] = name.removesuffix("_bundle.json")
        bundles.append(data)
    return bundles


def weighted_action(rng: random.Random, persona_key: str) -> str:
    weights = ACTION_WEIGHTS[persona_key]
    return rng.choices(list(weights), weights=list(weights.values()), k=1)[0]


def make_pii_findings(rng: random.Random, n: int) -> List[Dict]:
    findings = []
    cursor = 0
    for _ in range(n):
        entity = rng.choice(PII_ENTITY_TYPES)
        start = cursor + rng.randint(3, 40)
        end = start + rng.randint(6, 16)
        cursor = end
        findings.append({
            "entity_type": entity,
            "character_span": [start, end],
            "confidence_score": round(rng.uniform(0.7, 1.0), 4),
            "checksum_validated": entity in ("CREDIT_CARD", "AADHAAR"),
        })
    return findings


def make_input_gate_row(rng: random.Random, bundle: Dict, action: str, injected: bool) -> Dict:
    injection_risk = round(rng.uniform(0.72, 0.97), 4) if injected else round(rng.uniform(0.0, 0.2), 4)
    patterns = [rng.choice(INJECTION_PATTERNS)] if injected else []
    pii_n = rng.choice([0, 0, 0, 1, 1, 2]) if action in ("REDACT", "FLAG", "BLOCK") else rng.choice([0, 0, 0, 0, 1])
    return {
        "action": "FLAG" if injected else "ALLOW",
        "injection_risk": injection_risk,
        "injection_patterns": patterns,
        "evasions": [{"kind": rng.choice(["base64", "homoglyph", "whitespace_padding"]), "count": 1}]
                    if injected and rng.random() < 0.3 else [],
        "pii_findings": make_pii_findings(rng, pii_n),
        "policy_hash": bundle["policy_hash"],
        "latency_ms": round(rng.uniform(1.5, 9.0), 3),
    }


def make_t0_row(rng: random.Random, bundle: Dict, action: str) -> Optional[Dict]:
    if action != "BLOCK" or rng.random() > 0.5:
        # Not every BLOCK is a T0 hard override (fusion can block too), and most
        # non-BLOCK rows carry no T0 finding at all.
        if rng.random() > 0.08:
            return None
    check = rng.choice(["canary", "pii_id", "secret", "blocklist"])
    severity = "hard" if action == "BLOCK" and rng.random() < 0.6 else rng.choice(["high", "medium", "low"])
    return {
        "hard_override": severity == "hard",
        "canary_leak": rng.choice(["system", "context", None]) if check == "canary" else None,
        "findings": [{
            "check": check,
            "entity_type": rng.choice(PII_ENTITY_TYPES) if check == "pii_id" else check,
            "character_span": [rng.randint(0, 200), rng.randint(200, 400)],
            "confidence_score": round(rng.uniform(0.6, 1.0), 4),
            "severity": severity,
            "origin": "model_generated",
        }],
        "latency_ms": round(rng.uniform(1.0, 6.0), 3),
    }


def make_fusion_row(rng: random.Random, bundle: Dict, action: str, injected: bool) -> Dict:
    low, high = float(bundle["low_band"]), float(bundle["high_band"])
    lo, hi = BAND_RANGE[action]
    fused_risk = round(rng.uniform(lo, hi), 4)

    candidates = ["pii", "grounding", "toxicity", "t0"]
    dominant = rng.choice(candidates)
    normalized = {d: round(rng.uniform(0.0, 0.3), 4) for d in candidates}
    normalized[dominant] = fused_risk
    raw = {
        "pii": round(rng.uniform(0.5, 1.0), 4) if rng.random() < 0.6 else None,
        "grounding": round(1.0 - fused_risk * rng.uniform(0.8, 1.0), 4) if rng.random() < 0.6 else None,
        "toxicity": round(rng.uniform(0.0, fused_risk), 4) if rng.random() < 0.6 else None,
    }
    critical = bundle.get("detector_critical_thresholds", {})
    critical_fired = [d for d in ("pii", "grounding", "toxicity")
                       if d in critical and normalized.get(d, 0.0) >= float(critical[d])]
    if action == "BLOCK" and not critical_fired and rng.random() < 0.5:
        critical_fired = [dominant] if dominant in critical else []

    bands_tightened = injected
    eff_low = round(low * (1 - float(bundle.get("input_risk_tightening", 0.5)) * 0.4), 4) if injected else low
    eff_high = round(high * (1 - float(bundle.get("input_risk_tightening", 0.5)) * 0.4), 4) if injected else high

    return {
        "action": action,
        "fused_risk": fused_risk,
        "raw_scores": {k: v for k, v in raw.items() if v is not None},
        "normalized_scores": normalized,
        "critical_fired": critical_fired,
        "dominant_detector": dominant,
        "effective_bands": [eff_low, eff_high],
        "bands_tightened": bands_tightened,
        "t2_recommended": bool(bundle.get("t2_enabled")) and eff_low <= fused_risk <= eff_high,
    }


REASONS = {
    "ALLOW": "within policy bands",
    "REDACT": "PII span masked before delivery",
    "REGENERATE": "grounding below threshold; response reworked",
    "FLAG": "fused risk in escalation band; routed to reviewer queue",
    "BLOCK": "fused risk exceeded high_band",
}


def make_row(rng: random.Random, bundle: Dict, request_id: str) -> Dict:
    persona_key = bundle["_persona_key"]
    action = weighted_action(rng, persona_key)
    injected = action in ("FLAG", "BLOCK") and rng.random() < 0.35

    input_gate = make_input_gate_row(rng, bundle, action, injected)
    t0 = make_t0_row(rng, bundle, action)
    fusion = make_fusion_row(rng, bundle, action, injected)

    served_from, similarity, source_request_id, skip_reason = "upstream", 0.0, "", ""
    if bool(bundle.get("caching_enabled")) and action == "ALLOW" and rng.random() < 0.22:
        served_from = "cache"
        similarity = round(rng.uniform(float(bundle["cache_threshold"]), 1.0), 4)
        source_request_id = uuid.uuid4().hex
    elif not bool(bundle.get("caching_enabled")):
        skip_reason = "caching disabled by policy"
    elif action != "ALLOW":
        skip_reason = "only a clean ALLOW is storable"

    provider, model = rng.choice(PROVIDERS)

    return {
        "request_id": request_id,
        "policy_hash": bundle["policy_hash"],
        "action": action,
        "reason": REASONS[action],
        "provider": provider,
        "model": model,
        "input_gate": input_gate,
        "t0": t0,
        "fusion": fusion,
        "session": {
            "session_id": uuid.uuid4().hex[:16],
            "distinct_attacks": rng.choice([0, 0, 0, 1, 2]) if injected else 0,
            "rework_count": 1 if action == "REGENERATE" else 0,
            "messages_adjudicated": rng.randint(1, 8),
        },
        "input_tokens": rng.randint(20, 400),
        "output_tokens": rng.randint(15, 600),
        "latency_ms": round(rng.uniform(40.0, 850.0) if bundle.get("t2_enabled") else rng.uniform(15.0, 220.0), 3),
        "cache": {
            "served_from": served_from,
            "similarity": similarity,
            "source_request_id": source_request_id,
            "skip_reason": skip_reason,
        },
    }


def append_at(ledger: Ledger, payload: Dict, recorded_at: str) -> Dict:
    """Same hashing as Ledger.append, with a caller-supplied timestamp so demo traffic
    can be spread across a realistic window instead of clustering at generation time."""
    prev = ledger.head
    row = {**payload, "recorded_at": recorded_at, "prev_hash": prev}
    row["row_hash"] = _row_hash({k: v for k, v in row.items() if k != "prev_hash"}, prev)
    ledger.rows.append(row)
    return row


def seed(per_persona: int, days: int, seed: int) -> Ledger:
    rng = random.Random(seed)
    bundles = load_bundles()
    ledger = Ledger()

    now = datetime.now(timezone.utc)
    total = per_persona * len(bundles)
    timestamps = sorted(now - timedelta(seconds=rng.uniform(0, days * 86400)) for _ in range(total))

    rows_plan = []
    for bundle in bundles:
        for _ in range(per_persona):
            rows_plan.append(bundle)
    rng.shuffle(rows_plan)

    for bundle, ts in zip(rows_plan, timestamps):
        request_id = uuid.uuid4().hex
        payload = make_row(rng, bundle, request_id)
        append_at(ledger, payload, ts.isoformat())

    return ledger


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-persona", type=int, default=250,
                        help="Rows per bundle persona (default 250; ~750 total across 3 personas)")
    parser.add_argument("--days", type=int, default=14, help="Spread rows over this many past days")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str, default=str(REPO_ROOT / "learning_plane" / "data" / "demo_ledger.json"))
    args = parser.parse_args()

    ledger = seed(args.per_persona, args.days, args.seed)
    ok, broken = ledger.verify()
    assert ok, f"generated ledger failed self-verification at row {broken}"

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(ledger.rows, indent=2))
    print(f"wrote {len(ledger.rows)} rows to {out_path} (chain head {ledger.head[:12]}..., verified OK)")


if __name__ == "__main__":
    main()
