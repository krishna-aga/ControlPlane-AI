"""
Learning Plane — Ledger Explorer (Streamlit demo UI).

Loads the demo ledger written by `learning_plane/seed_demo_ledger.py` into a real
`data_plane.ledger.Ledger`, so "Verify Chain" and "Tamper Row" below call the exact
same `.verify()` the audit system uses — nothing in this page fakes the hash chain.

Run:
    streamlit run learning_plane/app.py
"""

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent

# `streamlit run` sets sys.path[0] to this file's own directory, not the repo root, so
# the sibling `data_plane` / `learning_plane` packages are otherwise unimportable.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning_plane import common  # noqa: E402

st.set_page_config(page_title="ControlPlane.ai — Learning Plane", layout="wide")

policy_names = common.load_policy_names()
rows = common.ensure_ledger_rows()
ledger = common.rows_to_ledger(rows)

# -- header + KPIs ----------------------------------------------------------------------

st.title("ControlPlane.ai — Learning Plane")
st.caption("Audit Ledger Explorer · hash-chained, tamper-evident, privacy-safe by construction")

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Total decisions", len(rows))
k2.metric("Chain length", len(ledger.rows))
action_counts = pd.Series([r["action"] for r in rows]).value_counts()
k3.metric("Blocked", int(action_counts.get("BLOCK", 0)))
k4.metric("Flagged for review", int(action_counts.get("FLAG", 0)))
cache_hits = sum(1 for r in rows if (r.get("cache") or {}).get("served_from") == "cache")
k5.metric("Served from cache", cache_hits)

st.divider()

# -- integrity panel ---------------------------------------------------------------------

left, right = st.columns([2, 1])

with right:
    st.subheader("Chain integrity")
    ok, broken_index = ledger.verify()
    if ok:
        st.success(f"VALID — all {len(ledger.rows)} rows verified, genesis to head.")
    else:
        st.error(f"CORRUPTED — chain breaks at row index {broken_index}. "
                 "Every row after this point is now unverifiable.")

    st.write("")
    tamper_idx = st.number_input(
        "Row index to tamper with", min_value=0, max_value=max(len(rows) - 1, 0),
        value=st.session_state.get("tampered_index") or 0, step=1,
    )
    tcol1, tcol2 = st.columns(2)
    if tcol1.button("Tamper this row", type="primary", width="stretch"):
        target = st.session_state.ledger_rows[tamper_idx]
        fusion = target.get("fusion")
        if fusion is not None:
            fusion["fused_risk"] = round(min(fusion.get("fused_risk", 0.0) + 0.3, 0.99), 4)
        target["action"] = "ALLOW"
        target["reason"] = "quietly overwritten after the fact"
        st.session_state.tampered_index = tamper_idx
        st.rerun()
    if tcol2.button("Reset to clean seed", width="stretch"):
        st.session_state.ledger_rows = common.load_ledger_rows()
        st.session_state.tampered_index = None
        st.rerun()

    st.caption(
        "Tamper flips row `action` to ALLOW and inflates `fused_risk` "
        "*without* recomputing `row_hash` — exactly what an after-the-fact edit "
        "to the log would look like."
    )

with left:
    st.subheader("Filters")
    f1, f2, f3 = st.columns(3)
    persona_filter = f1.multiselect("Persona", sorted(set(policy_names.values())))
    action_filter = f2.multiselect("Action", sorted(set(r["action"] for r in rows)))
    cache_filter = f3.selectbox("Served from", ["(any)", "cache", "upstream"])

    df = common.summary_table(rows, policy_names)
    if persona_filter:
        df = df[df["persona"].isin(persona_filter)]
    if action_filter:
        df = df[df["action"].isin(action_filter)]
    if cache_filter != "(any)":
        df = df[df["served_from"] == cache_filter]

    st.dataframe(
        df.drop(columns=["row_hash"]),
        width="stretch", height=420, hide_index=True,
    )

st.divider()

# -- row inspector ------------------------------------------------------------------------

st.subheader("Row inspector")
options = [f'{i} · {r["request_id"][:12]} · {r["action"]} · {r["recorded_at"]}' for i, r in enumerate(rows)]
sel = st.selectbox("Pick a row to inspect (types, spans and scores only — never raw values)", options)
sel_idx = int(sel.split(" · ")[0])
st.json(rows[sel_idx], expanded=False)
