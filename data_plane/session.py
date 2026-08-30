"""
Session risk posture.

**The tenant owns the conversation; the gateway owns the risk posture.** The tenant
replays its full message history on every call, so the gateway SEES all of it and
RETAINS none of it - all the detection benefit of context with none of the storage
liability. What is kept per session is content-free: message hashes and counters.

The problem this solves: because history is replayed, an injection planted at turn 1 is
re-scanned at turn 20. Suppressing that would be wrong - the attack is still in the
payload the model reads, so the risk is genuinely still live. What was wrong is
COUNTING it twenty times, which ratchets the bands into a permanent block.

So a finding is treated as a property of CONTENT, not of a request:

  * live risk        - aggregated over findings present in THIS array, deduped. One
                       poisoned turn contributes once whether it is turn 1 or turn 40.
                       Drops on its own if the tenant removes that turn.
  * distinct attacks - increments only on a finding hash never seen before. One attack
                       replayed twenty times counts ONCE; three different attempts count
                       three.

The cache cannot be gamed: the gateway computes the hash from the content the tenant
sent, so any modification misses the cache and triggers a full scan. It can only ever
skip work on byte-identical content, which fails safe.

`distinct_attacks` is CAPPED and does not decay. An attacker cannot drive the bands to
zero and deadlock a session, and "the session is burned, start a new one" is an honest
behaviour to explain - they would open a new session_id anyway, so a deadlock is pure
downside.
"""

import hashlib
from typing import Dict, Optional

from pydantic import BaseModel, Field

# Bounded so a tenant cannot grow gateway memory by sending unique messages forever.
MAX_ADJUDICATED = 512
MAX_DISTINCT_ATTACKS = 3


def message_hash(role: str, content: str) -> str:
    """Content identity. Never reversible to the message, and never the message itself."""
    return hashlib.sha256(f"{role}\x00{content}".encode("utf-8")).hexdigest()[:32]


class SessionState(BaseModel):
    """
    Per-session risk posture. Holds NO message text - hashes and counters only, which
    keeps it consistent with the ledger privacy rule rather than depending on anyone
    remembering to strip it.
    """

    session_id: str
    adjudicated: Dict[str, float] = Field(default_factory=dict)   # message_hash -> risk
    distinct_attacks: int = 0
    rework_count: int = 0

    def seen(self, h: str) -> bool:
        return h in self.adjudicated

    def record(self, h: str, risk: float, is_violation: bool) -> None:
        """Record a NEW adjudication. Counts a distinct attack at most once per content."""
        if h in self.adjudicated:
            return
        if len(self.adjudicated) >= MAX_ADJUDICATED:
            self.adjudicated.pop(next(iter(self.adjudicated)))
        self.adjudicated[h] = risk
        if is_violation:
            self.distinct_attacks = min(self.distinct_attacks + 1, MAX_DISTINCT_ATTACKS)

    def escalation(self) -> float:
        """
        Extra tightening from repeated DISTINCT attacks, in [0, 1]. Capped, never decays.
        """
        return self.distinct_attacks / MAX_DISTINCT_ATTACKS

    def ledger_row(self) -> Dict:
        return {
            "session_id": self.session_id,
            "distinct_attacks": self.distinct_attacks,
            "rework_count": self.rework_count,
            "messages_adjudicated": len(self.adjudicated),
        }


class SessionStore:
    """In-memory session store. A real deployment swaps this for a shared cache."""

    def __init__(self) -> None:
        self._sessions: Dict[str, SessionState] = {}

    def get(self, session_id: str) -> SessionState:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState(session_id=session_id)
        return self._sessions[session_id]

    def reset(self, session_id: Optional[str] = None) -> None:
        if session_id is None:
            self._sessions.clear()
        else:
            self._sessions.pop(session_id, None)
