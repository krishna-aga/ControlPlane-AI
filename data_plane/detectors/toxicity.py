"""
Tier 1 toxicity detector - unitary/toxic-bert, worst-sentence, weighted-label scoring.

Per docs/TIER_1.md §3:
  A. One scalar collapses six unequal labels (T1-6). Raw = max(P_label * weight_label),
     read from toxicity_label_weights (locked, MAP_HIGHER_IS_STRICTER) - a threat and an
     obscenity are not the same finding, and under max aggregation zeroing a weight
     silences that label outright rather than diluting it.
  B. The model is itself a documented source of bias (the Jigsaw training corpus
     over-scores AAVE, reclaimed slurs, and bare identity-term mentions). This module
     does not correct for that - see the doc for why the action ladder routes toxicity
     to FLAG (human review) rather than an autonomous BLOCK, the correct posture for a
     classifier with a known demographic error profile.
  D. Worst sentence, same argument as grounding §2A - one toxic sentence inside a long
     polite answer must not average away.

**Label-name mismatch, verified against the live model.** `unitary/toxic-bert` emits the
original Kaggle competition column names - toxic, severe_toxic, obscene, threat, insult,
identity_hate - not Detoxify's names, which is what the spec's toxicity_label_weights
table (and this file, before this comment existed) assumed. Matched blind, every lookup
misses and the detector silently scores 0.0 forever - the same failure shape as T0-10
(a threshold applied against the wrong alphabet). `_LABEL_ALIASES` below is what makes
the two naming conventions the same six labels.

Model identity is verified BY CONSTRUCTION: loaded directly from bundle["toxicity_model"],
same reasoning as grounding.py.
"""

import re
from functools import lru_cache
from typing import Dict, List

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# unitary/toxic-bert's raw label -> the canonical name toxicity_label_weights is keyed
# by. obscene/threat/insult already agree; the other three don't.
_LABEL_ALIASES = {
    "toxic": "toxicity",
    "severe_toxic": "severe_toxicity",
    "identity_hate": "identity_attack",
}


@lru_cache(maxsize=4)
def _load_pipeline(model_name: str):
    # Imported lazily so importing this module never costs a torch/transformers load
    # for a request path that never calls score_toxicity.
    from transformers import pipeline
    return pipeline("text-classification", model=model_name, top_k=None)


def _sentences(text: str) -> List[str]:
    found = [s.strip() for s in _SENTENCE_SPLIT.split(text.strip()) if s.strip()]
    return found or ([text.strip()] if text.strip() else [])


def score_toxicity(response_text: str, bundle: Dict) -> float:
    """
    Worst-sentence, label-weighted toxicity score: max(P_label * weight) over every
    label, over every sentence. Unlike grounding this is always applicable (no ground
    truth required) - callers should still skip it on empty text.
    """
    sentences = _sentences(response_text)
    if not sentences:
        return 0.0

    clf = _load_pipeline(bundle["toxicity_model"])
    weights = bundle.get("toxicity_label_weights", {})

    worst = 0.0
    for sentence_scores in clf(sentences):
        for entry in sentence_scores:
            canonical = _LABEL_ALIASES.get(entry["label"], entry["label"])
            worst = max(worst, entry["score"] * weights.get(canonical, 0.0))
    return worst
