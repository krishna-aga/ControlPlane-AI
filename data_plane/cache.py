"""
Semantic response cache.

A cache hit is a decision to SKIP THE CHECKS. That is the whole design problem, and it
is why this module is mostly refusals rather than lookups: everything here exists to
decide when a past verdict may stand in for a fresh one.

Two invariants hold the design together.

**1. A cache entry is a verdict, not a response.** Only a request that was ALLOWed with
nothing found is storable. A REDACTed answer's masking spans were computed against one
specific placeholder-bearing string, a REGENERATE is by definition not the answer, and a
BLOCK must never be replayable at all. Storing anything but a clean ALLOW would cache a
remedy alongside the text and then replay the text without it.

**2. The cache skips the model call and the expensive tiers. It never skips Tier 0.**
T0 costs well under a millisecond and nothing, so re-running it on a cached response
before delivery is close to free, and it keeps the system-wide claim simple: nothing
reaches a client without deterministic checks against the bundle in force RIGHT NOW.
"Nothing except cache hits" is a much weaker sentence to have to say to an auditor.

The key is exact on every dimension except the prompt, which is the only fuzzy one:

    namespace = H(tenant_id, entitlement_scope, policy_hash,
                  system_prompt, context_docs, embedder_id)

Each of those is load-bearing, and each closes a specific leak - see docs/SEMANTIC_CACHE.md.
"""

import hashlib
import math
import re
from collections import OrderedDict
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

from pydantic import BaseModel, Field

_WS = re.compile(r"\s+")


# ---------------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------------

class Embedder(Protocol):
    """
    Anything that turns text into a unit vector.

    `id` is not decoration. It goes into the cache namespace, so entries built by one
    embedder can never be compared against vectors from another. Swapping the embedder
    silently invalidates the cache instead of silently mis-comparing - the same
    self-invalidating property `policy_hash` gives the bundle.
    """

    id: str

    def embed(self, text: str) -> List[float]: ...


class HashingEmbedder:
    """
    Dependency-free character n-gram embedder.

    **This is LEXICAL, not semantic.** It matches text that shares surface form -
    "what is your refund policy" against "what's the refund policy" - and it does NOT
    match "how do I get my money back", which a sentence-transformer would. Calling it
    semantic would be the claim this repo keeps refusing to make.

    That failure direction is the safe one for a cache: it under-hits, so the cost is a
    model call that could have been avoided, not a wrong answer served confidently. A
    real embedding model is a drop-in via the `Embedder` protocol and would raise the
    hit rate; it would also raise the stakes of every limitation in
    docs/SEMANTIC_CACHE.md §5.
    """

    id = "hashing-char3-256-v1"

    def __init__(self, dims: int = 256, ngram: int = 3) -> None:
        self.dims = dims
        self.ngram = ngram

    def embed(self, text: str) -> List[float]:
        normalized = _WS.sub(" ", text.strip().lower())
        vector = [0.0] * self.dims
        if not normalized:
            return vector

        grams = [normalized[i:i + self.ngram]
                 for i in range(max(len(normalized) - self.ngram + 1, 1))]
        for gram in grams:
            bucket = int.from_bytes(
                hashlib.blake2b(gram.encode("utf-8"), digest_size=4).digest(), "big"
            ) % self.dims
            vector[bucket] += 1.0

        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Both vectors arrive L2-normalized, so the dot product IS the cosine."""
    if len(a) != len(b):
        return 0.0
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


# ---------------------------------------------------------------------------------
# Entries and keys
# ---------------------------------------------------------------------------------

class CacheEntry(BaseModel):
    """
    One stored verdict.

    `prompt_preview` is deliberately absent. The entry holds the response text because
    that is what gets served, but nothing here needs the prompt after its vector is
    computed, and keeping it would put user text in a structure that outlives the
    request - the same argument that removed `value` from `Finding`.
    """

    response: str
    embedding: List[float]
    source_request_id: str
    policy_hash: str
    embedder_id: str
    hits: int = 0


def _digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(len(part).to_bytes(8, "big"))    # length-prefixed: no delimiter collisions
        h.update(part.encode("utf-8"))
    return h.hexdigest()


def namespace_key(
    tenant_id: str,
    entitlement_scope: str,
    policy_hash: str,
    system_prompt: str,
    context_docs: Sequence[str],
    embedder_id: str,
) -> str:
    """
    Everything that must match EXACTLY before two prompts are even comparable.

    * `tenant_id` - obvious, and the reason a missing one disables caching entirely.
    * `entitlement_scope` - without it the cache becomes a side channel for reading
      documents you are not entitled to retrieve. Retrieval-time entitlement is the
      boundary ORIGIN_AND_EXONERATION.md §4 delegates exposure risk to; this is where a
      careless cache would walk straight through it.
    * `policy_hash` - a cached verdict was reached under one policy. Recompiling the
      bundle changes the hash and orphans every entry, which is the correct behaviour
      and costs nothing to implement.
    * `system_prompt` - otherwise "you are a pirate" serves answers cached under the
      previous persona.
    * `context_docs` - two identical questions over different retrieved chunks must not
      share an answer, or the cache serves text grounded in documents that were not
      retrieved this time. Hashed BEFORE canary planting: canaries are minted fresh per
      request, so hashing the planted block would miss on every single lookup.
    * `embedder_id` - vectors from different models are not comparable at all.
    """
    return _digest(tenant_id, entitlement_scope, policy_hash,
                   system_prompt, "\x00".join(context_docs), embedder_id)


# ---------------------------------------------------------------------------------
# Cacheability
# ---------------------------------------------------------------------------------

# Returned as the ledger's `cache_skip_reason`. "Why was this not cached" is a question
# a cost review will ask, and a hit rate with no denominator explanation is not an answer.
def storable(
    action: str,
    bundle: Dict,
    tenant_id: Optional[str],
    restore_map: Dict[str, str],
    t0_finding_count: int,
    input_flagged: bool,
    turn_count: int,
    provider_refused: bool,
) -> Tuple[bool, str]:
    """
    May this exchange be written to the cache?

    Every clause is a refusal with a specific failure behind it. They are checked in
    cheapest-first order, but the order carries no meaning beyond that.
    """
    if not bundle.get("caching_enabled", False):
        return False, "caching disabled by policy"
    if not tenant_id:
        # Fail safe: no tenant means no scope, and an unscoped entry is one every other
        # tenant could match against. Absence of an id must never widen sharing.
        return False, "no tenant_id supplied"
    if action != "ALLOW":
        return False, f"action was {action}, only a clean ALLOW is storable"
    if provider_refused:
        return False, "provider refusal is not an answer"
    if restore_map:
        # THE dangerous case. The forwarded prompt reads "status of order for [EMAIL_1]",
        # so keying on it makes two different customers' requests identical. Serve one
        # from the other's entry and the response is de-anonymized with the WRONG restore
        # map - a cross-customer PII leak produced entirely by the cache. Keying on the
        # raw prompt instead just moves raw PII into a long-lived structure. Neither is
        # acceptable, so a request carrying PII is simply not cacheable.
        return False, "input carried PII; placeholder keys collide across users"
    if t0_finding_count:
        return False, "Tier 0 findings present"
    if input_flagged:
        # A flagged input contracts the output bands. Serving from cache skips fusion,
        # so the contraction never happens - the request would get the untightened
        # verdict it was specifically denied.
        return False, "input was flagged for injection"
    if turn_count != 1:
        # A follow-up ("what about for premium members?") is meaningless without its
        # history, and keying on the full replayed array collapses the hit rate to
        # roughly zero anyway. Single-turn only, stated rather than discovered.
        return False, "multi-turn request"
    return True, ""


def servable(bundle: Dict, tenant_id: Optional[str], restore_map: Dict[str, str],
             input_flagged: bool, turn_count: int) -> Tuple[bool, str]:
    """
    May this request READ from the cache?

    A subset of `storable` - the clauses that can be evaluated before the model call.
    The asymmetry is deliberate: everything knowable up front gates the read, and the
    output-dependent clauses gate the write.
    """
    if not bundle.get("caching_enabled", False):
        return False, "caching disabled by policy"
    if not tenant_id:
        return False, "no tenant_id supplied"
    if restore_map:
        return False, "input carried PII; placeholder keys collide across users"
    if input_flagged:
        return False, "input was flagged for injection"
    if turn_count != 1:
        return False, "multi-turn request"
    return True, ""


# ---------------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------------

class CacheLookup(BaseModel):
    hit: bool = False
    entry: Optional[CacheEntry] = None
    similarity: float = 0.0
    reason: str = ""


class SemanticCache:
    """
    Bounded, in-memory, per-namespace nearest-neighbour lookup.

    A linear scan within a namespace is the right structure at prototype scale and is
    honest about not being an ANN index; the namespace split already keeps each scan
    small. `max_entries` bounds total memory so a tenant cannot grow the process with
    unique prompts, which is the same reasoning that bounds `SessionState.adjudicated`.
    """

    def __init__(self, embedder: Optional[Embedder] = None, max_entries: int = 1024) -> None:
        self.embedder: Embedder = embedder or HashingEmbedder()
        self.max_entries = max_entries
        self._entries: "OrderedDict[str, List[CacheEntry]]" = OrderedDict()
        self._count = 0
        self.lookups = 0
        self.hits = 0

    def embed(self, text: str) -> List[float]:
        return self.embedder.embed(text)

    def lookup(self, namespace: str, prompt: str, threshold: float) -> CacheLookup:
        self.lookups += 1
        bucket = self._entries.get(namespace)
        if not bucket:
            return CacheLookup(reason="namespace empty")

        vector = self.embed(prompt)
        best: Optional[CacheEntry] = None
        best_score = -1.0
        for entry in bucket:
            score = cosine(vector, entry.embedding)
            if score > best_score:
                best, best_score = entry, score

        if best is None or best_score < threshold:
            return CacheLookup(similarity=max(best_score, 0.0),
                               reason=f"best similarity {max(best_score, 0.0):.3f} < threshold {threshold:.3f}")

        self._entries.move_to_end(namespace)
        best.hits += 1
        self.hits += 1
        return CacheLookup(hit=True, entry=best, similarity=best_score,
                           reason=f"similarity {best_score:.3f} >= threshold {threshold:.3f}")

    def store(self, namespace: str, prompt: str, response: str,
              request_id: str, policy_hash: str) -> CacheEntry:
        entry = CacheEntry(
            response=response,
            embedding=self.embed(prompt),
            source_request_id=request_id,
            policy_hash=policy_hash,
            embedder_id=self.embedder.id,
        )
        self._entries.setdefault(namespace, []).append(entry)
        self._entries.move_to_end(namespace)
        self._count += 1
        self._evict()
        return entry

    def evict_entry(self, namespace: str, entry: CacheEntry) -> None:
        """Drop one entry - used when T0 rejects it at serve time."""
        bucket = self._entries.get(namespace)
        if not bucket:
            return
        self._entries[namespace] = [e for e in bucket if e is not entry]
        self._count -= 1
        if not self._entries[namespace]:
            del self._entries[namespace]

    def _evict(self) -> None:
        """Oldest namespace first. Coarse, bounded, and adequate at this scale."""
        while self._count > self.max_entries and self._entries:
            _, bucket = self._entries.popitem(last=False)
            self._count -= len(bucket)

    def stats(self) -> Dict:
        return {
            "entries": self._count,
            "namespaces": len(self._entries),
            "lookups": self.lookups,
            "hits": self.hits,
            "hit_rate": round(self.hits / self.lookups, 4) if self.lookups else 0.0,
            "embedder": self.embedder.id,
        }
