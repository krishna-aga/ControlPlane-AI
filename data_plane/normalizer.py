"""
Input canonicalization for the ControlPlane.ai Input Gate.

Produces a CanonicalView used ONLY for detection. The raw prompt is never mutated here
and the canonical text is never forwarded upstream.

Why that separation is load-bearing:

  * base64 expansion appends decoded content inline. If the canonical text were
    forwarded, a prompt carrying an encoded payload would reach the model with that
    payload helpfully decoded into plaintext - the canonicalizer would be assembling
    the attack it exists to detect.
  * NFKC folding and whitespace collapse silently rewrite code blocks, identifiers and
    deliberate formatting.
  * Every positional transform shifts character offsets, so PII spans taken from
    canonical text would no longer index the string being sent.

Evasions are reported rather than silently repaired: knowing a phrase arrived wrapped in
zero-width joiners is signal about intent, and it is what the demo puts on screen.
"""

import base64
import binascii
import re
import unicodedata
from typing import List

from data_plane.models import CanonicalView, Evasion

# Zero-width and bidirectional format characters. Cf|Cc categories minus ordinary
# whitespace: these render as nothing, so they exist to break up a token.
INVISIBLE_RE = re.compile(r"[​-‏‪-‮⁠-⁤﻿­]")

# Base64 candidates. Length floor of 16 keeps ordinary words and hex ids out.
B64_CANDIDATE_RE = re.compile(r"\b[A-Za-z0-9+/]{16,}={0,2}\b")

WHITESPACE_RE = re.compile(r"\s+")

# Runs of intra-word separators used to split keywords ("i g n o r e", "i-g-n-o-r-e").
SPACED_LETTERS_RE = re.compile(r"\b(?:[A-Za-z][ \-_.]){3,}[A-Za-z]\b")

_MIN_DECODED_LEN = 8
_MIN_PRINTABLE_RATIO = 0.85


def _decode_b64(token: str) -> str | None:
    """Return decoded text only if it looks like deliberately hidden natural language."""
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(text) < _MIN_DECODED_LEN:
        return None
    printable = sum(c.isprintable() or c.isspace() for c in text)
    if printable / len(text) < _MIN_PRINTABLE_RATIO:
        return None
    return text


def canonicalize(raw_prompt: str) -> CanonicalView:
    """
    Build the scan-only canonical view of a prompt and record what had to be undone.

    Detection passes run against the RAW string so recorded offsets stay meaningful;
    the transform chain runs separately to produce the text detectors scan.
    """
    evasions: List[Evasion] = []

    # --- detection passes over the RAW prompt (offsets stay valid) ----------------
    invisible_hits = [m.start() for m in INVISIBLE_RE.finditer(raw_prompt)]
    if invisible_hits:
        evasions.append(Evasion(
            kind="invisible_char",
            detail=f"{len(invisible_hits)} zero-width or bidi format character(s) removed",
            count=len(invisible_hits),
            offsets=invisible_hits[:32],
        ))

    spaced = [m.group(0) for m in SPACED_LETTERS_RE.finditer(raw_prompt)]
    if spaced:
        evasions.append(Evasion(
            kind="whitespace_padding",
            detail=f"character-separated token(s): {', '.join(spaced[:3])}",
            count=len(spaced),
            offsets=[m.start() for m in SPACED_LETTERS_RE.finditer(raw_prompt)][:32],
        ))

    # --- transform chain ----------------------------------------------------------
    text = INVISIBLE_RE.sub("", raw_prompt)

    folded = unicodedata.normalize("NFKC", text)
    if folded != text:
        evasions.append(Evasion(
            kind="homoglyph",
            detail="NFKC folding changed the text (homoglyph or compatibility form)",
            count=1,
        ))
    text = folded

    # Rejoin character-separated tokens so "i g n o r e" matches the keyword patterns.
    text = SPACED_LETTERS_RE.sub(lambda m: re.sub(r"[ \-_.]", "", m.group(0)), text)

    decoded_parts: List[str] = []
    for match in B64_CANDIDATE_RE.finditer(text):
        decoded = _decode_b64(match.group(0))
        if decoded is not None:
            decoded_parts.append(decoded)
            evasions.append(Evasion(
                kind="base64",
                detail=f"decoded base64 payload: {decoded[:60]!r}",
                count=1,
                offsets=[match.start()],
            ))

    text = WHITESPACE_RE.sub(" ", text).strip()

    # Decoded payloads are APPENDED to the scan surface, never spliced into anything
    # forwarded. Safe only because CanonicalView.text is scan-only.
    if decoded_parts:
        text = text + " ‖DECODED‖ " + " ".join(decoded_parts)

    return CanonicalView(text=text, evasions=evasions)
