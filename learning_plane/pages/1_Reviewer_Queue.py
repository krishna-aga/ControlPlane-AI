"""
Learning Plane — Reviewer Queue (Streamlit demo UI).

The audit ledger's FLAG rows are the guardrail's own "I'm not sure" pile. Reviewing
volume-first wastes human time on easy calls; this queue instead ranks rows by how
close the fused risk score sat to a decision-band edge — the closest calls are the
most informative for a human to weigh in on, and are shown first.

A human "Approve" confirms the flag was correct (true positive). "Reject" overturns
it (a false positive) — this is the ledger's only source of false-positive ground
truth, since nothing here can prove a false negative on its own.

Run:
    streamlit run learning_plane/app.py
    (then open "Reviewer Queue" in the sidebar)
"""

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning_plane import common  # noqa: E402

st.set_page_config(page_title="Learning Plane — Reviewer Queue", layout="wide")

policy_names = common.load_policy_names()
rows = common.ensure_ledger_rows()
labels = common.load_reviewer_labels()

st.title("Reviewer Queue")
st.caption(
    "Ranked by uncertainty, not volume — rows whose fused risk sat closest to a "
    "decision-band edge are queued first. This is the ledger's source of "
    "false-positive ground truth."
)

flagged = [r for r in rows if r["action"] == "FLAG"]
pending = [r for r in flagged if r["request_id"] not in labels]
reviewed = [r for r in flagged if r["request_id"] in labels]
pending.sort(key=common.uncertainty)

overturned = sum(1 for r in reviewed if labels[r["request_id"]]["label"] == "reject")

k1, k2, k3, k4 = st.columns(4)
k1.metric("Flagged total", len(flagged))
k2.metric("Pending review", len(pending))
k3.metric("Reviewed", len(reviewed))
k4.metric(
    "Overturned (false positives)",
    overturned,
    f"{overturned / len(reviewed):.0%} of reviewed" if reviewed else None,
)

st.divider()

queue_col, history_col = st.columns([2, 1])

with queue_col:
    st.subheader(f"Queue — {len(pending)} pending, most uncertain first")
    n_show = st.slider("Rows to show", 1, min(30, len(pending)) if pending else 1, min(10, len(pending)) if pending else 1)

    if not pending:
        st.success("Queue is empty — every flagged decision has been reviewed.")
    for row in pending[:n_show]:
        fusion = row.get("fusion") or {}
        low, high = fusion.get("effective_bands", [None, None])
        u = common.uncertainty(row)
        persona = policy_names.get(row["policy_hash"], row["policy_hash"][:10])

        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            with c1:
                st.markdown(
                    f"**{row['request_id'][:12]}** · {persona} · "
                    f"dominant: `{fusion.get('dominant_detector')}` · "
                    f"risk **{fusion.get('fused_risk')}** (band {low}–{high}, "
                    f"±{u:.3f} from edge)"
                )
                st.caption(row["reason"])
            with c2:
                a1, a2 = st.columns(2)
                if a1.button("Approve", key=f"approve_{row['request_id']}", width="stretch"):
                    common.save_reviewer_label(row["request_id"], "approve", row)
                    st.rerun()
                if a2.button("Reject", key=f"reject_{row['request_id']}", width="stretch"):
                    common.save_reviewer_label(row["request_id"], "reject", row)
                    st.rerun()
            with st.expander("Detector scores (types & scores only — never raw values)"):
                st.json(fusion.get("normalized_scores", {}))

with history_col:
    st.subheader("Review history")
    if labels:
        hist = pd.DataFrame.from_records([
            {"request_id": rid[:12], **v} for rid, v in labels.items()
        ]).sort_values("reviewed_at", ascending=False)
        st.dataframe(hist, width="stretch", height=420, hide_index=True)
    else:
        st.info("No reviews recorded yet.")

    if st.button("Reset all reviewer labels", width="stretch"):
        common.reset_reviewer_labels()
        st.rerun()
