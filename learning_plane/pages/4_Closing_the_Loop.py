"""
Learning Plane — Closing the Loop (Streamlit demo UI).

Ties the other three pages together into the one diagram the spec calls out as what
separates a learning plane from a logging plane, plus the KPI row a skeptical
stakeholder actually asks for.

Run:
    streamlit run learning_plane/app.py
    (then open "Closing the Loop" in the sidebar)
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning_plane import calibration, common, shadow_eval  # noqa: E402

st.set_page_config(page_title="Learning Plane — Closing the Loop", layout="wide")

PROPOSALS_PATH = REPO_ROOT / "learning_plane" / "data" / "calibration_proposals.json"

# Illustrative unit economics, same style as calibration.py's own assumptions — a
# cache hit skips the entire upstream call plus every tier after it, not just an
# escalation review, so this is priced higher than COST_PER_ESCALATION there.
AVOIDED_COST_PER_CACHE_HIT = 6.0  # ₹

st.title("Closing the Loop")
st.caption(
    "\"This is the part the original diagram was missing, and it's what makes it a "
    "learning plane rather than a logging plane.\""
)

st.divider()

# -- loop diagram ---------------------------------------------------------------------

def load_proposals() -> list:
    if PROPOSALS_PATH.exists():
        return json.loads(PROPOSALS_PATH.read_text())
    return []


def save_proposals(history: list) -> None:
    PROPOSALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROPOSALS_PATH.write_text(json.dumps(history, indent=2))


proposals = load_proposals()
latest = proposals[-1] if proposals else None
approved = bool(latest and latest.get("approved"))

st.subheader("The loop")

with st.container(border=True):
    if latest:
        st.markdown(
            f"**① Calibration proposes** ✅ — "
            f"`low_band` {latest['from_threshold']:.2f} → {latest['to_threshold']:.2f}"
        )
        st.caption(f"Proposed at {latest['proposed_at']} · hash `{latest['proposal_hash'][:16]}…`")
    else:
        st.markdown("**① Calibration proposes** ⚪ — no proposal yet")
        st.caption("Go to the Calibration page, pick a candidate threshold, and click \"Propose this candidate.\"")

with st.container(border=True):
    if not latest:
        st.markdown("**② Human approves** ⚪ — waiting on a proposal")
        st.caption("The gate. Never automatic — a proposal sits here until a person signs off.")
    elif approved:
        st.markdown(f"**② Human approves** ✅ — approved at {latest['approved_at']}")
    else:
        st.markdown("**② Human approves** 🟡 — pending")
        st.caption(
            "Without this gate a feedback loop can drift: reviewers get fatigued and "
            "rubber-stamp things as fine, calibration reads that as over-flagging, "
            "thresholds loosen, fewer things get flagged, fewer get reviewed — the "
            "system quietly relaxes itself into uselessness."
        )
        if st.button("Approve latest proposal", type="primary"):
            proposals[-1]["approved"] = True
            proposals[-1]["approved_at"] = datetime.now(timezone.utc).isoformat()
            save_proposals(proposals)
            st.rerun()

with st.container(border=True):
    if approved:
        st.markdown(f"**③ Control plane mints new version** ✅ — preview id `{latest['proposal_hash'][:16]}…`")
        st.caption(
            "`low_band` stays locked in the real bundles — this identifies the "
            "approved proposal, not a compiled bundle. An actual mint runs a real "
            "org-baseline recompile outside this demo."
        )
    else:
        st.markdown("**③ Control plane mints new version** ⚪ — pending approval")

with st.container(border=True):
    st.markdown("**④ Shadow deploy: v_old vs v_new** 🔜")
    st.caption(
        "Not run in this demo. Runs the candidate side by side with production "
        "traffic before anything switches over — distinct from *shadow eval*, which "
        "audits the current policy, not a candidate one."
    )

with st.container(border=True):
    st.markdown("**⑤ Data plane enforces the new version** 🔜")
    st.caption("Not run in this demo — the next stage after a clean shadow deploy.")

with st.container(border=True):
    st.markdown("**⑥ New decisions flow back to the ledger** ↩")
    st.caption("This is literally the Ledger Explorer page — the loop closes there, not here.")

st.divider()

# -- KPI row ----------------------------------------------------------------------------

st.subheader("Metrics for a skeptical stakeholder")

policy_names = common.load_policy_names()
rows = common.ensure_ledger_rows()
ledger_df = pd.DataFrame.from_records([
    {
        "policy_hash": r["policy_hash"],
        "latency_ms": r.get("latency_ms"),
        "t2_recommended": (r.get("fusion") or {}).get("t2_recommended"),
        "served_from": (r.get("cache") or {}).get("served_from"),
    }
    for r in rows
])

reviewer_labels = common.load_reviewer_labels()
reviewed = list(reviewer_labels.values())
overturned = [v for v in reviewed if v["label"] == "reject"]
fp_rate = len(overturned) / len(reviewed) if reviewed else None

shadow_results = shadow_eval.load_or_run()

labelled_set = calibration.load_or_generate_labelled_set()
current_threshold = calibration.current_locked_threshold()
cost_metrics = calibration.metrics_at(labelled_set, current_threshold)

cache_hits = int((ledger_df["served_from"] == "cache").sum())
cost_avoided = cache_hits * AVOIDED_COST_PER_CACHE_HIT

r1 = st.columns(4)
r1[0].metric("False positive rate", f"{fp_rate:.1%}" if fp_rate is not None else "no reviews yet")
r1[1].metric("Est. false negative rate", f"{shadow_results['estimated_fnr']:.1%}")
r1[2].metric("Escalation rate (% hitting T2)", f"{ledger_df['t2_recommended'].mean():.1%}")
r1[3].metric("Cost / 1k interactions", f"₹{cost_metrics['cost_per_1k']:.2f}")

r2 = st.columns(4)
r2[0].metric("p50 added latency", f"{ledger_df['latency_ms'].quantile(0.5):.0f} ms")
r2[1].metric("p95 added latency", f"{ledger_df['latency_ms'].quantile(0.95):.0f} ms")
r2[2].metric("Override rate", f"{fp_rate:.1%}" if fp_rate is not None else "no reviews yet",
             help="Same source as false positive rate — a reviewer rejecting a flag is an override.")
r2[3].metric("Cost avoided (cache)", f"₹{cost_avoided:,.0f}",
             help=f"{cache_hits} cache hits × ₹{AVOIDED_COST_PER_CACHE_HIT:.0f} avoided upstream cost each, over the demo window.")

with st.expander("Override rate by use case"):
    if not reviewed:
        st.info("No reviews recorded yet — visit the Reviewer Queue page and approve/reject a few rows.")
    else:
        by_persona = {}
        for v in reviewed:
            persona = policy_names.get(v.get("policy_hash"), "unknown (recorded before persona tracking)")
            by_persona.setdefault(persona, {"reviewed": 0, "overturned": 0})
            by_persona[persona]["reviewed"] += 1
            if v["label"] == "reject":
                by_persona[persona]["overturned"] += 1
        breakdown = pd.DataFrame([
            {"persona": p, "reviewed": d["reviewed"], "overturned": d["overturned"],
             "override_rate": d["overturned"] / d["reviewed"]}
            for p, d in by_persona.items()
        ])
        st.dataframe(breakdown, width="stretch", hide_index=True)

st.caption(
    "Sources — FP rate & override rate: Reviewer Queue. Est. FN rate: Shadow Eval. "
    "Escalation rate, latency, cost/1k, cost avoided: the ledger, via Calibration's "
    "cost model at the current locked threshold."
)
