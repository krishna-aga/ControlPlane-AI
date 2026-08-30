"""
Calibration sweep — the tradeoff curve, per the Learning Plane spec's own framing the
single highest-value artifact in this plane. Calibration never decides a threshold; it
shows the tradeoff at every candidate value so a human can pick a point with eyes open.

`low_band` is a LOCKED field in every bundle (see `bundles/*_bundle.json`
`locked_fields`) — it can't be changed by a persona-level policy override, only by a
new org-baseline compile that a human approves. That's exactly why this exists: a
threshold change here is a proposal, not an edit.

Ground truth for the sweep comes from a dedicated synthetic labelled set (like
`shadow_eval.py`'s sample, this doesn't reuse the 750-row demo ledger — that set has
no independent ground truth, only the fused_risk score the threshold itself produced).
Two overlapping populations are generated — "actually safe" and "actually unsafe" —
so no threshold is perfect; that overlap is *the whole reason a tradeoff exists*.

Usage:
    python -m learning_plane.calibration --n 600 --seed 11
"""

import argparse
import json
import random
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LABELLED_SET_PATH = REPO_ROOT / "learning_plane" / "data" / "calibration_labelled_set.json"

UNSAFE_PREVALENCE = 0.15  # share of real traffic that is actually unsafe
COST_PER_ESCALATION = 3.0   # ₹ — T2 call + amortized human review time
COST_PER_MISSED_INCIDENT = 200.0  # ₹ — illustrative remediation/reputational cost of a miss

BUNDLES_DIR = REPO_ROOT / "bundles"


def current_locked_threshold() -> float:
    """low_band is a LOCKED field — read live from the compiled bundles rather than
    hardcoding it, so this can't silently drift if the org-baseline policy changes."""
    values = set()
    for path in BUNDLES_DIR.glob("*_bundle.json"):
        values.add(float(json.loads(path.read_text())["low_band"]))
    assert len(values) == 1, f"low_band is locked but bundles disagree: {values}"
    return values.pop()


def generate_labelled_set(n: int, seed: int) -> List[Dict]:
    """Two overlapping populations: safe traffic clusters low, unsafe traffic clusters
    high, but they overlap in the 0.2-0.6 range — that overlap is what makes threshold
    choice a genuine tradeoff instead of a solved problem."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        unsafe = rng.random() < UNSAFE_PREVALENCE
        fused_risk = round(rng.betavariate(5, 3) if unsafe else rng.betavariate(2, 8), 4)
        rows.append({"id": i, "fused_risk": fused_risk, "actually_unsafe": unsafe})
    return rows


def sweep(labelled_set: List[Dict], thresholds: List[float]) -> List[Dict]:
    safe = [r for r in labelled_set if not r["actually_unsafe"]]
    unsafe = [r for r in labelled_set if r["actually_unsafe"]]
    points = []
    for t in thresholds:
        fp = sum(1 for r in safe if r["fused_risk"] >= t)
        fn = sum(1 for r in unsafe if r["fused_risk"] < t)
        escalated = sum(1 for r in labelled_set if r["fused_risk"] >= t)

        fp_rate = fp / len(safe) if safe else 0.0
        fn_rate = fn / len(unsafe) if unsafe else 0.0
        escalation_rate = escalated / len(labelled_set) if labelled_set else 0.0
        cost_per_1k = 1000 * (
            escalation_rate * COST_PER_ESCALATION
            + fn_rate * UNSAFE_PREVALENCE * COST_PER_MISSED_INCIDENT
        )
        points.append({
            "threshold": round(t, 4),
            "fp_rate": round(fp_rate, 4),
            "fn_rate": round(fn_rate, 4),
            "escalation_rate": round(escalation_rate, 4),
            "cost_per_1k": round(cost_per_1k, 2),
        })
    return points


def metrics_at(labelled_set: List[Dict], threshold: float) -> Dict:
    return sweep(labelled_set, [threshold])[0]


def load_or_generate_labelled_set(n: int = 600, seed: int = 11) -> List[Dict]:
    if DEFAULT_LABELLED_SET_PATH.exists():
        return json.loads(DEFAULT_LABELLED_SET_PATH.read_text())
    rows = generate_labelled_set(n, seed)
    DEFAULT_LABELLED_SET_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_LABELLED_SET_PATH.write_text(json.dumps(rows, indent=2))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=600)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--out", type=str, default=str(DEFAULT_LABELLED_SET_PATH))
    args = parser.parse_args()

    rows = generate_labelled_set(args.n, args.seed)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, indent=2))

    current = current_locked_threshold()
    at_current = metrics_at(rows, current)
    print(f"wrote {len(rows)} labelled rows to {out_path}")
    print(f"at current locked threshold {current}: {at_current}")


if __name__ == "__main__":
    main()
