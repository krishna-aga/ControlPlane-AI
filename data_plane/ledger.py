"""
Hash-chained audit ledger.

Every decision lands in a row whose hash covers the previous row's hash, so any edit to
history invalidates every row after it. That is what makes "this policy_hash produced
this decision" auditable rather than merely asserted.

**Privacy is structural.** Rows are assembled from the `ledger_row()` projections of
InputGateResult, T0Result, FusionResult and SessionState - each of which emits types,
spans, scores and counters only. Raw prompts, model output, PII values, the placeholder
restore map and BYOK credentials have no path into this module.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

GENESIS = "0" * 64


def _row_hash(payload: Dict, prev_hash: str) -> str:
    canonical = json.dumps({"prev_hash": prev_hash, **payload}, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Ledger:
    """Append-only, hash-chained. In-memory here; a deployment appends to durable storage."""

    def __init__(self) -> None:
        self.rows: List[Dict] = []

    @property
    def head(self) -> str:
        return self.rows[-1]["row_hash"] if self.rows else GENESIS

    def append(self, payload: Dict) -> Dict:
        prev = self.head
        row = {
            **payload,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "prev_hash": prev,
        }
        row["row_hash"] = _row_hash({k: v for k, v in row.items() if k != "prev_hash"}, prev)
        self.rows.append(row)
        return row

    def verify(self) -> tuple[bool, Optional[int]]:
        """
        Walk the chain. Returns (ok, first_broken_index). A tampered row breaks its own
        hash; a deleted or reordered row breaks the link at that point.
        """
        prev = GENESIS
        for i, row in enumerate(self.rows):
            if row["prev_hash"] != prev:
                return False, i
            expected = _row_hash(
                {k: v for k, v in row.items() if k not in ("prev_hash", "row_hash")}, prev
            )
            if expected != row["row_hash"]:
                return False, i
            prev = row["row_hash"]
        return True, None
