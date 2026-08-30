"""
Learning Plane — Calibration (Streamlit demo UI).

The tradeoff curve. Calibration never decides a threshold — it sweeps candidates and
shows FP rate / est. FN rate / escalation rate / cost at every point, so a human picks
with eyes open. `low_band` is a LOCKED field (see bundles/*_bundle.json
`locked_fields`), so a candidate here is a proposal, not an edit — nothing on this
page writes to a real policy file.

Run:
    streamlit run learning_plane/app.py
    (then open "Calibration" in the sidebar)
"""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning_plane import calibration  # noqa: E402

st.set_page_config(page_title="Learning Plane — Calibration", layout="wide")

PROPOSALS_PATH = REPO_ROOT / "learning_plane" / "data" / "calibration_proposals.json"

st.title("Calibration")
st.caption(
    "Sweeps candidate thresholds over a labelled set and plots the tradeoff. "
    "Calibration proposes a point — it never decides one."
)

labelled_set = calibration.load_or_generate_labelled_set()
current = calibration.current_locked_threshold()

thresholds = [round(0.05 + 0.025 * i, 4) for i in range(37)]  # 0.05 .. 0.95
sweep_df = pd.DataFrame(calibration.sweep(labelled_set, thresholds))

st.divider()

# -- tradeoff curve -----------------------------------------------------------------

st.subheader("Tradeoff curve")
candidate = st.slider(
    "Candidate threshold", min_value=0.05, max_value=0.95, value=float(current), step=0.01,
    help="Drag to see FP/FN/escalation/cost at that point. This does not change any policy file.",
)

rates_long = sweep_df.melt(
    id_vars=["threshold"], value_vars=["fp_rate", "fn_rate", "escalation_rate"],
    var_name="metric", value_name="rate",
)
rates_chart = alt.Chart(rates_long).mark_line().encode(
    x=alt.X("threshold", title="threshold"),
    y=alt.Y("rate", title="rate", axis=alt.Axis(format="%")),
    color=alt.Color("metric", title=None),
)
current_rule = alt.Chart(pd.DataFrame({"x": [current]})).mark_rule(
    color="gray", strokeDash=[4, 4],
).encode(x="x")
candidate_rule = alt.Chart(pd.DataFrame({"x": [candidate]})).mark_rule(
    color="firebrick",
).encode(x="x")
st.altair_chart(
    (rates_chart + current_rule + candidate_rule).properties(height=320),
    width="stretch",
)
st.caption(
    f"Dashed grey = current locked threshold ({current:.2f}). "
    f"Solid red = candidate ({candidate:.2f})."
)

cost_chart = alt.Chart(sweep_df).mark_line(color="#8a3ffc").encode(
    x=alt.X("threshold", title="threshold"),
    y=alt.Y("cost_per_1k", title="cost per 1k requests (₹)"),
)
st.altair_chart(
    (cost_chart + current_rule + candidate_rule).properties(height=200),
    width="stretch",
)

st.divider()

# -- side-by-side comparison ---------------------------------------------------------

st.subheader("Current (locked) vs. candidate")
m_current = calibration.metrics_at(labelled_set, current)
m_candidate = calibration.metrics_at(labelled_set, candidate)

c1, c2 = st.columns(2)
with c1:
    st.markdown(f"**Current — locked at {current:.2f}**")
    st.metric("FP rate", f"{m_current['fp_rate']:.1%}")
    st.metric("Est. FN rate", f"{m_current['fn_rate']:.1%}")
    st.metric("Escalation rate", f"{m_current['escalation_rate']:.1%}")
    st.metric("Cost / 1k", f"₹{m_current['cost_per_1k']:.2f}")
with c2:
    st.markdown(f"**Candidate — {candidate:.2f}**")
    st.metric("FP rate", f"{m_candidate['fp_rate']:.1%}",
               delta=f"{m_candidate['fp_rate'] - m_current['fp_rate']:+.1%}", delta_color="inverse")
    st.metric("Est. FN rate", f"{m_candidate['fn_rate']:.1%}",
               delta=f"{m_candidate['fn_rate'] - m_current['fn_rate']:+.1%}", delta_color="inverse")
    st.metric("Escalation rate", f"{m_candidate['escalation_rate']:.1%}",
               delta=f"{m_candidate['escalation_rate'] - m_current['escalation_rate']:+.1%}", delta_color="off")
    st.metric("Cost / 1k", f"₹{m_candidate['cost_per_1k']:.2f}",
               delta=f"{m_candidate['cost_per_1k'] - m_current['cost_per_1k']:+.2f}", delta_color="inverse")

st.divider()

# -- propose ---------------------------------------------------------------------------

st.subheader("Propose new policy version")
st.caption(
    "`low_band` is locked — this records a proposal for human approval. It does not "
    "write to any bundle or policy file. Approval → control plane mints a new bundle "
    "hash → shadow deploy → enforce is the next stage of the loop."
)

if st.button("Propose this candidate", type="primary", disabled=(candidate == current)):
    proposal = {
        "from_threshold": current,
        "to_threshold": candidate,
        "metrics_at_current": m_current,
        "metrics_at_candidate": m_candidate,
        "proposed_at": datetime.now(timezone.utc).isoformat(),
    }
    proposal["proposal_hash"] = hashlib.sha256(
        json.dumps(proposal, sort_keys=True).encode("utf-8")
    ).hexdigest()

    history = json.loads(PROPOSALS_PATH.read_text()) if PROPOSALS_PATH.exists() else []
    history.append(proposal)
    PROPOSALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROPOSALS_PATH.write_text(json.dumps(history, indent=2))
    st.session_state.last_proposal = proposal
    st.rerun()

if st.session_state.get("last_proposal"):
    p = st.session_state.last_proposal
    st.success(
        f"Proposal recorded: {p['from_threshold']:.2f} → {p['to_threshold']:.2f} "
        f"(hash `{p['proposal_hash'][:16]}…`). Awaiting human approval — not yet applied."
    )
