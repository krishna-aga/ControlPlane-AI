"""
Live Gateway Demo (Streamlit demo UI).

Every other page in this app is the LEARNING plane - it looks backward at decisions
already made. This page drives the DATA plane live: type a prompt, and it calls the
real `Gateway.process_request()` against the real Gemini API (BYOK, key read from
.env) - no mocked model, no scripted response. Input Gate, Tier 0, and fusion are the
actual production code path; only the model call is external.

Run:
    streamlit run learning_plane/app.py
    (then open "Live Gateway Demo" in the sidebar)
"""

import os
import sys
import uuid
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_plane.adapters import Credentials, GeminiAdapter  # noqa: E402
from data_plane.gateway import Gateway  # noqa: E402

MODEL = "gemini-3.5-flash"   # gemini-2.0-flash and gemini-2.5-flash were both retired for
                              # new users (404); gemini-3.6-flash hit its daily quota mid-
                              # session - Gemini quotas are tracked PER MODEL on one key,
                              # so this one has a separate, unused quota. Verified live.


def _load_dotenv(path: Path) -> None:
    """
    Minimal .env loader - no python-dotenv in requirements.txt, and one file of KEY=VALUE
    lines doesn't earn a new dependency. Never overwrites a variable already set in the
    real environment.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(REPO_ROOT / ".env")

st.set_page_config(page_title="Learning Plane — Live Gateway Demo", layout="wide")

BUNDLES = {
    "customer_support": REPO_ROOT / "bundles" / "customer_support_bundle.json",
    "decision_support": REPO_ROOT / "bundles" / "decision_support_bundle.json",
    "internal_copilot": REPO_ROOT / "bundles" / "internal_copilot_bundle.json",
}
PERSONA_LABELS = {
    "customer_support": "Customer Support (redact-and-proceed, fail_open)",
    "decision_support": "Decision Support (block-and-explain, fail_closed)",
    "internal_copilot": "Internal Copilot (warn-and-confirm, fail_open)",
}
ACTION_COLOR = {
    "ALLOW": "🟢", "REDACT": "🔵", "REGENERATE": "🟣", "FLAG": "🟠", "BLOCK": "🔴",
}


def get_gateway() -> Gateway:
    if "demo_gateway" not in st.session_state:
        st.session_state.demo_gateway = Gateway(adapter=GeminiAdapter())
    return st.session_state.demo_gateway


# -- header -------------------------------------------------------------------------------

st.title("Live Gateway Demo")
st.caption(
    "Type a prompt, run it through the real gateway against the real Gemini API, and "
    "see the action it takes and the answer actually delivered."
)

if not os.environ.get("GEMINI_API_KEY"):
    st.error(
        "No GEMINI_API_KEY found in the environment or .env. Requests below will "
        "cleanly BLOCK with \"upstream model could not be reached\" until one is set."
    )

gateway = get_gateway()

# A form batches every widget inside it and only submits on the button click below - no
# Ctrl+Enter, no click-away-to-commit. That is what actually fixes "I have to do
# something else before Run picks up what I typed": plain widgets rerun (and can
# resubmit stale values) on their own triggers, form widgets never do.
with st.form("run_form"):
    persona = st.selectbox("Persona", list(BUNDLES), format_func=lambda p: PERSONA_LABELS[p])
    system_prompt = st.text_area(
        "System prompt", "You are a helpful assistant.", height=80,
        help="Put something worth hiding in here (e.g. an internal instruction or a fake "
             "secret) and ask the model to reveal it below - the gateway plants a fresh "
             "canary into this text on every request and Tier 0 checks the answer for a leak.",
    )
    prompt = st.text_area("Prompt", height=120, placeholder="Type a prompt to send through the gateway...")
    submitted = st.form_submit_button("Run", type="primary")

if submitted:
    if not prompt.strip():
        st.warning("Type a prompt first.")
    else:
        creds = Credentials(provider="gemini", model=MODEL, api_key=os.environ.get("GEMINI_API_KEY", ""))
        with st.spinner("Calling Gemini and running the checks..."):
            result = gateway.process_request(
                [{"role": "user", "content": prompt}], str(BUNDLES[persona]), uuid.uuid4().hex,
                credentials=creds, system_prompt=system_prompt,
            )
        st.session_state.last_result = result
        st.rerun()

result = st.session_state.get("last_result")
if result:
    st.subheader(f"{ACTION_COLOR.get(result.action, '⚪')} {result.action}")
    st.caption(result.reason)
    if result.action == "BLOCK":
        st.error(result.response)
    else:
        st.write(result.response)

st.divider()

# -- session ledger ---------------------------------------------------------------------

st.subheader("Session ledger")
st.caption(
    "A real, hash-chained `data_plane.ledger.Ledger` — in-memory for this browser "
    "session only, separate from the seeded ledger the other pages read."
)
if st.button("Reset session"):
    for k in ("demo_gateway", "last_result"):
        st.session_state.pop(k, None)
    st.rerun()

ledger = gateway.ledger
ok, broken = ledger.verify()
st.write("Chain integrity: " + ("✅ VALID" if ok else f"❌ BROKEN at row {broken}"))
if ledger.rows:
    df = pd.DataFrame([
        {"#": i, "action": row["action"], "reason": row["reason"][:80],
         "fused_risk": (row.get("fusion") or {}).get("fused_risk"),
         "served_from": (row.get("cache") or {}).get("served_from"),
         "row_hash": row["row_hash"][:12] + "…"}
        for i, row in enumerate(ledger.rows)
    ])
    # fused_risk is a precise float (fusion.py rounds to 4dp) - force that precision in
    # the display too, since a column that happens to be all-zero (every clean request
    # fuses to exactly 0.0) otherwise gets auto-formatted as a bare integer and reads as
    # "did this even record a number" rather than "this is genuinely 0.0000".
    df["fused_risk"] = df["fused_risk"].astype(float)
    st.dataframe(
        df, width="stretch", hide_index=True,
        column_config={"fused_risk": st.column_config.NumberColumn(format="%.4f")},
    )
