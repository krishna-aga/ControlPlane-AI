"""
Tier 1 grounding detector - per-sentence cosine similarity against retrieved context.

Per docs/TIER_1.md §2:
  A. The unit is the CLAIM, not the response (T1-2). One cosine over the whole answer
     dilutes a single fabricated sentence with the correct text around it - the same
     weight-dilution defect Tier 0 already fixed twice. Score PER SENTENCE, similarity
     = max over retrieved chunks, response score = the WORST sentence. Deliberately not
     a bundle knob (no `grounding_aggregation`) - taking the worst unit is a correctness
     position, the same argument that removed detector_weights.
  B. `grounding_threshold` (0.6) is an accepted, UNCALIBRATED constant - real sampling
     of the cosine distribution was deliberately deferred (vendoring the model just to
     calibrate it was the thing not done). Nothing here changes that; building the
     detector does not retroactively calibrate its threshold.
  C. Cosine measures TOPIC, not TRUTH (T1-4). A confidently wrong number ("30 days" vs
     "90 days") scores as grounded as a correct one. Stated, not engineered around -
     same treatment T0-8 gives the canary limitation.
  E. Empty retrieval is None, never 0.0 (§2E). A non-RAG request has no ground truth to
     score against; scoring it 0.0 would read as S=1.0 and block everything.

Model identity is verified BY CONSTRUCTION rather than by a runtime equality check: the
model is loaded directly from bundle["grounding_model"], which is a locked, immutable
field (T1-1) - there is no code path that can load a model under one name and silently
reuse it under another, since the cache key IS the requested name.
"""

import re
from functools import lru_cache
from typing import Dict, List, Optional

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@lru_cache(maxsize=4)
def _load_model(model_name: str):
    # Imported lazily so importing this module never costs a torch/sentence-transformers
    # load for a request that has no context_docs and will never call score_grounding.
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(model_name)


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text.strip()) if s.strip()]


def _is_claim(sentence: str, min_tokens: int) -> bool:
    """
    Heuristic claim filter (§2A). "Happy to help." has no ground truth to be checked
    against and would score near-zero similarity, firing on almost every response under
    worst-sentence scoring - the over-flagging failure the problem statement names
    directly. This is a token-count floor: cheap, and wrong sometimes by design, which
    is why grounding's remedy is REGENERATE rather than BLOCK.
    """
    return len(sentence.split()) >= min_tokens


def score_grounding(response_text: str, context_docs: List[str], bundle: Dict) -> Optional[float]:
    """
    Worst-sentence, best-chunk cosine similarity. Returns None (not 0.0) when there is
    no ground truth to score against: no context at all, or nothing in the response
    passes the claim filter. Higher = safer, matching every other raw score's
    convention before fusion.py's inversion (ungrounded = 1 - similarity).
    """
    if not context_docs:
        return None

    min_tokens = int(bundle.get("grounding_min_claim_tokens", 6))
    claims = [s for s in _sentences(response_text) if _is_claim(s, min_tokens)]
    if not claims:
        return None

    model = _load_model(bundle["grounding_model"])
    claim_vecs = model.encode(claims, normalize_embeddings=True)
    chunk_vecs = model.encode(context_docs, normalize_embeddings=True)

    # Vectors are L2-normalized, so the dot product IS the cosine similarity.
    sims = claim_vecs @ chunk_vecs.T                 # (n_claims, n_chunks)
    best_per_claim = sims.max(axis=1)                # best chunk per claim sentence
    return float(best_per_claim.min())                # worst sentence wins
