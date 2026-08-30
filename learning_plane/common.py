"""
Shared helpers for the Learning Plane Streamlit pages (app.py + pages/*).

Every entry-point script inserts the repo root onto sys.path before importing this
module — `streamlit run` sets sys.path[0] to the running script's own directory, so
the sibling `data_plane` package is otherwise unimportable regardless of cwd.
"""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from data_plane.ledger import Ledger

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER_PATH = REPO_ROOT / "learning_plane" / "data" / "demo_ledger.json"
REVIEWER_LABELS_PATH = REPO_ROOT / "learning_plane" / "data" / "reviewer_labels.json"
BUNDLES_DIR = REPO_ROOT / "bundles"


@st.cache_data
def load_policy_names() -> dict:
    names = {}
    for path in BUNDLES_DIR.glob("*_bundle.json"):
        data = json.loads(path.read_text())
        names[data["policy_hash"]] = data.get("policy_name", path.stem)
    return names


@st.cache_data
def load_ledger_rows(path: Path = DEFAULT_LEDGER_PATH) -> list:
    return json.loads(path.read_text())


def rows_to_ledger(rows: list) -> Ledger:
    ledger = Ledger()
    ledger.rows = copy.deepcopy(rows)
    return ledger


def summary_table(rows: list, policy_names: dict) -> pd.DataFrame:
    records = []
    for r in rows:
        fusion = r.get("fusion") or {}
        cache = r.get("cache") or {}
        records.append({
            "recorded_at": r["recorded_at"],
            "request_id": r["request_id"][:12],
            "persona": policy_names.get(r["policy_hash"], r["policy_hash"][:10]),
            "action": r["action"],
            "reason": r["reason"],
            "fused_risk": fusion.get("fused_risk"),
            "dominant_detector": fusion.get("dominant_detector"),
            "served_from": cache.get("served_from"),
            "latency_ms": r.get("latency_ms"),
            "row_hash": r["row_hash"],
        })
    df = pd.DataFrame.from_records(records)
    df["recorded_at"] = pd.to_datetime(df["recorded_at"])
    return df.sort_values("recorded_at", ascending=False).reset_index(drop=True)


def ensure_ledger_rows() -> list:
    """Load the demo ledger into session state once, shared across every page."""
    if "ledger_rows" not in st.session_state:
        if not DEFAULT_LEDGER_PATH.exists():
            st.error(
                f"No demo ledger at {DEFAULT_LEDGER_PATH}. Generate one first:\n\n"
                "`python -m learning_plane.seed_demo_ledger`"
            )
            st.stop()
        st.session_state.ledger_rows = load_ledger_rows()
        st.session_state.tampered_index = None
    return st.session_state.ledger_rows


def uncertainty(row: dict) -> float:
    """Distance from the fused risk to the nearest decision-band edge — smaller
    means the model was closer to flipping the decision, i.e. less certain."""
    fusion = row.get("fusion") or {}
    risk = fusion.get("fused_risk")
    bands = fusion.get("effective_bands")
    if risk is None or not bands:
        return float("inf")
    low, high = bands
    return min(abs(risk - low), abs(risk - high))


def load_reviewer_labels() -> dict:
    if REVIEWER_LABELS_PATH.exists():
        return json.loads(REVIEWER_LABELS_PATH.read_text())
    return {}


def save_reviewer_label(request_id: str, label: str, row: dict) -> None:
    labels = load_reviewer_labels()
    fusion = row.get("fusion") or {}
    labels[request_id] = {
        "label": label,
        "action": row["action"],
        "fused_risk": fusion.get("fused_risk"),
        "dominant_detector": fusion.get("dominant_detector"),
        "policy_hash": row.get("policy_hash"),
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    REVIEWER_LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    REVIEWER_LABELS_PATH.write_text(json.dumps(labels, indent=2, sort_keys=True))


def reset_reviewer_labels() -> None:
    if REVIEWER_LABELS_PATH.exists():
        REVIEWER_LABELS_PATH.unlink()
