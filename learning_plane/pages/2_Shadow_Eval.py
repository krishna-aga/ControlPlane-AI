"""
Learning Plane — Shadow Eval (Streamlit demo UI).

Offline false-negative estimator: re-judges a random sample of ALLOWED decisions with
a mock T2 judge (T2 itself isn't built yet) and reports how often the judge disagrees
with what the fast pipeline let through. That disagreement rate is an ESTIMATE of the
pipeline's false-negative rate — never ground truth, since the judge is imperfect too.

Run:
    streamlit run learning_plane/app.py
    (then open "Shadow Eval" in the sidebar)
"""

import random
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning_plane import common, shadow_eval  # noqa: E402

st.set_page_config(page_title="Learning Plane — Shadow Eval", layout="wide")

st.title("Shadow Eval")
st.caption(
    "Re-judges a random ~2% sample of ALLOWED decisions offline with a mock T2 judge. "
    "Disagreement = an estimated false-negative rate for the fast pipeline."
)

st.warning(
    "**Not ground truth.** The judge is itself an imperfect model — this number has "
    "error bars and should move review priorities, not stand in as a metric of record.",
    icon="⚠️",
)

policy_names = common.load_policy_names()

if "shadow_results" not in st.session_state:
    sample = shadow_eval.generate_sample(n=200, days=14, seed=7)
    st.session_state.shadow_results = shadow_eval.run_shadow_eval(sample, seed=7)

results = st.session_state.shadow_results

st.divider()

k1, k2, k3 = st.columns(3)
k1.metric("Sample size", results["sample_size"])
k2.metric("Flagged by judge", results["flagged_count"])
k3.metric("Estimated FNR", f"{results['estimated_fnr']:.1%}")

if st.button("Re-run shadow eval (new random sample)"):
    fresh_seed = random.randint(1, 10_000)
    sample = shadow_eval.generate_sample(n=200, days=14, seed=fresh_seed)
    st.session_state.shadow_results = shadow_eval.run_shadow_eval(sample, seed=fresh_seed)
    st.rerun()

st.divider()

chart_col, table_col = st.columns([1, 2])

rows = results["rows"]
df = pd.DataFrame.from_records([
    {
        "request_id": r["request_id"][:12],
        "persona": policy_names.get(r["policy_hash"], r["policy_hash"][:10]),
        "dominant_detector": r["fusion"]["dominant_detector"],
        "fused_risk": r["fusion"]["fused_risk"],
        "judge_score": r["judge"]["judge_score"],
        "flagged": r["judge"]["flagged"],
        "judge_reason": r["judge"]["judge_reason"],
    }
    for r in rows
])

with chart_col:
    st.subheader("Where the judge disagrees")
    by_detector = df.groupby(["dominant_detector", "flagged"]).size().unstack(fill_value=0)
    by_detector = by_detector.rename(columns={True: "judge disagrees", False: "judge agrees"})
    st.bar_chart(by_detector, width="stretch")
    st.caption("Grounding is hardest for fast checks to catch — the judge disagrees with it most.")

with table_col:
    st.subheader(f"Judge-flagged rows ({results['flagged_count']}) — most confident first")
    flagged_df = df[df["flagged"]].sort_values("judge_score", ascending=False).drop(columns=["flagged"])
    if flagged_df.empty:
        st.success("Judge agreed with every sampled ALLOW decision this run.")
    else:
        st.dataframe(flagged_df, width="stretch", height=380, hide_index=True)
