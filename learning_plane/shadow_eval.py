"""
Shadow eval — offline, asynchronous false-negative estimator for the Learning Plane.

Production guardrails can't afford to run an expensive T2 judge on every request, so
only a small random sample of ALLOWED traffic (~2%) gets re-judged offline. Sampling
must be random, not drawn from "interesting" traffic — a biased sample would make the
estimate meaningless. Disagreement (the judge flags something the fast pipeline let
through) is the estimated false-negative rate.

T2 itself isn't built (see docs/TIER_1.md — Tier 1/2 detectors are specified, not
implemented), so this uses a MOCK judge: a stochastic function that looks at the same
fused-risk signal the fast pipeline had, but catches a bit more — exactly like a real,
slower judge model would. Its output is a noisy estimate, not ground truth, and the UI
must say so every time this number is shown.

Rather than deriving the sample from the small 750-row ledger demo (2% of its ~440
ALLOW rows is under 10 — too thin to look like anything on a chart), this generates a
dedicated fixed audit sample of ~200 rows, standing in for "the 2% pull from a much
larger population of real ALLOWED decisions" per the spec's own build plan.

Usage:
    python -m learning_plane.shadow_eval --n 200 --seed 7
"""

import argparse
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLES_DIR = REPO_ROOT / "bundles"
DEFAULT_SAMPLE_PATH = REPO_ROOT / "learning_plane" / "data" / "shadow_sample.json"
DEFAULT_RESULTS_PATH = REPO_ROOT / "learning_plane" / "data" / "shadow_eval_results.json"

# Fast-pipeline ALLOW decisions cluster well under low_band, but a slice sit close to
# the edge — "near misses" the fast checks let through by a narrow margin. Those are
# exactly the rows a slower judge disagrees with most often.
NEAR_EDGE_FRACTION = 0.30
FAR_RANGE = (0.02, 0.15)
NEAR_EDGE_RANGE = (0.16, 0.29)

# A judge disagrees (flags something ALLOW let through) more often the closer the row
# sat to the edge, and detector-specific: grounding/hallucination issues are harder
# for fast checks to catch than PII or toxicity, so the judge catches more of them —
# the gap is kept well above sampling noise at n≈200 so it reads consistently run to run.
BASE_CATCH_PROB = {"pii": 0.02, "toxicity": 0.03, "grounding": 0.20, "t0": 0.02}
NEAR_EDGE_BONUS = 0.12


def load_bundles() -> List[Dict]:
    bundles = []
    for name in ("customer_support_bundle.json", "decision_support_bundle.json", "internal_copilot_bundle.json"):
        data = json.loads((BUNDLES_DIR / name).read_text())
        data["_persona_key"] = name.removesuffix("_bundle.json")
        bundles.append(data)
    return bundles


def make_sample_row(rng: random.Random, bundle: Dict, recorded_at: str) -> Dict:
    near_edge = rng.random() < NEAR_EDGE_FRACTION
    lo, hi = NEAR_EDGE_RANGE if near_edge else FAR_RANGE
    fused_risk = round(rng.uniform(lo, hi), 4)

    candidates = ["pii", "grounding", "toxicity", "t0"]
    dominant = rng.choice(candidates)
    normalized = {d: round(rng.uniform(0.0, fused_risk), 4) for d in candidates}
    normalized[dominant] = fused_risk

    return {
        "request_id": uuid.uuid4().hex,
        "policy_hash": bundle["policy_hash"],
        "action": "ALLOW",
        "recorded_at": recorded_at,
        "fusion": {
            "fused_risk": fused_risk,
            "normalized_scores": normalized,
            "dominant_detector": dominant,
            "effective_bands": [float(bundle["low_band"]), float(bundle["high_band"])],
            "near_edge": near_edge,
        },
    }


def generate_sample(n: int, days: int, seed: int) -> List[Dict]:
    rng = random.Random(seed)
    bundles = load_bundles()
    now = datetime.now(timezone.utc)
    rows = []
    for _ in range(n):
        bundle = rng.choice(bundles)
        ts = (now - timedelta(seconds=rng.uniform(0, days * 86400))).isoformat()
        rows.append(make_sample_row(rng, bundle, ts))
    rows.sort(key=lambda r: r["recorded_at"])
    return rows


def mock_t2_judge(row: Dict, rng: random.Random) -> Dict:
    """A stand-in for the not-yet-built T2 judge: imperfect on purpose, biased toward
    catching what a fast pipeline structurally misses (grounding), and more likely to
    disagree the closer the original decision sat to the escalation band."""
    fusion = row["fusion"]
    dominant = fusion["dominant_detector"]
    catch_prob = BASE_CATCH_PROB[dominant] + (NEAR_EDGE_BONUS if fusion["near_edge"] else 0.0)
    flagged = rng.random() < catch_prob
    judge_score = round(fusion["fused_risk"] + (rng.uniform(0.35, 0.6) if flagged else rng.uniform(-0.05, 0.05)), 4)
    return {
        "flagged": flagged,
        "judge_score": max(0.0, min(judge_score, 0.99)),
        "judge_reason": (
            f"judge disagrees: {dominant} risk understated by fast pipeline"
            if flagged else "judge agrees: within policy"
        ),
    }


def run_shadow_eval(sample: List[Dict], seed: int) -> Dict:
    rng = random.Random(seed)
    results = []
    for row in sample:
        verdict = mock_t2_judge(row, rng)
        results.append({**row, "judge": verdict})

    flagged = [r for r in results if r["judge"]["flagged"]]
    est_fnr = len(flagged) / len(results) if results else 0.0
    return {
        "sample_size": len(results),
        "flagged_count": len(flagged),
        "estimated_fnr": round(est_fnr, 4),
        "rows": results,
    }


def load_or_run(seed: int = 7) -> Dict:
    """Read persisted shadow eval results if present (written by the CLI or a prior
    page visit), otherwise run a fresh default sample. Lets other pages (the closing
    KPI row) show a real estimated FNR without depending on session state."""
    if DEFAULT_RESULTS_PATH.exists():
        return json.loads(DEFAULT_RESULTS_PATH.read_text())
    sample = generate_sample(n=200, days=14, seed=seed)
    results = run_shadow_eval(sample, seed=seed)
    DEFAULT_RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_RESULTS_PATH.write_text(json.dumps(results, indent=2))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=200, help="Audit sample size (default 200)")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--seed", type=int, default=7, help="Sample generation seed")
    parser.add_argument("--judge-seed", type=int, default=None, help="Defaults to --seed if omitted")
    parser.add_argument("--sample-out", type=str, default=str(DEFAULT_SAMPLE_PATH))
    parser.add_argument("--results-out", type=str, default=str(DEFAULT_RESULTS_PATH))
    args = parser.parse_args()

    sample = generate_sample(args.n, args.days, args.seed)
    sample_path = Path(args.sample_out)
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_text(json.dumps(sample, indent=2))

    results = run_shadow_eval(sample, args.judge_seed if args.judge_seed is not None else args.seed)
    results_path = Path(args.results_out)
    results_path.write_text(json.dumps(results, indent=2))

    print(
        f"sampled {results['sample_size']} ALLOWED rows, judge flagged "
        f"{results['flagged_count']} ({results['estimated_fnr']:.2%}) — "
        f"wrote {sample_path} and {results_path}"
    )


if __name__ == "__main__":
    main()
