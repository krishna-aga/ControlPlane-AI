"""
Tier 0 — deterministic output checks.

Runs on the raw upstream output while PII placeholders are still in place, before
de-anonymization. That ordering is load-bearing: it is what makes the `origin`
distinction possible, and it is why spans index the PRE-de-anonymization string.

Four checks:
  1. Canary leak      — exact / folded / truncated match. severity `hard`.
  2. Checksum IDs     — reuses the shared catalog in pii.py. severity `high`, no override.
  3. Secrets          — Track 1 known prefixes (`hard`); Track 2 entropy (`high`).
  4. Blocklist        — org terms, single alternation regex. severity `medium`.

**Why Track 2 is `high` and not `hard` (T0-1).** T0's contract is about CERTAINTY, not
speed. Canary matching, provider prefixes and Luhn/Verhoeff arithmetic are determinations
- they cannot be wrong. Track 2 is entropy plus heuristics; it can be. Giving the only
probabilistic check in the tier the most severe and least reviewable consequence was
backwards, and it made its own false-positive rate unmeasurable, because a hard override
stops the cascade that would produce the evidence (T0-7).

Track 2 instead emits `high`, and reaches BLOCK through the bundle's `t0_floor_severity`
floor in fusion. It keeps the ability to act alone; it loses the unconditional veto it
had not earned.
"""

import math
import re
import time
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from data_plane.canary import PREFIX_MATCH_LEN, CanaryScope
from data_plane.detectors.pii import scan_pii
from data_plane.models import Finding, T0Result

# --- Check 3, Track 1: known credential signatures ----------------------------------
# The prefix IS the evidence - providers prefix keys precisely so scanners find them.
# No entropy analysis required, near-zero false-positive rate, ~90% of the real work.
_TRACK1 = [
    ("OPENAI_KEY",   r"\bsk-[a-zA-Z0-9]{32,}\b"),
    ("AWS_ACCESS_KEY", r"\bAKIA[0-9A-Z]{16}\b"),
    ("GITHUB_PAT",   r"\bghp_[A-Za-z0-9]{36}\b"),
    ("SLACK_TOKEN",  r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ("JWT",          r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    ("PRIVATE_KEY",  r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
_TRACK1_COMPILED = [(name, re.compile(p)) for name, p in _TRACK1]

# --- Check 3, Track 2: generic secrets ----------------------------------------------
# Assignment context is mandatory. Entropy ALONE is never sufficient: git SHAs, UUIDs,
# file hashes, base64 images, minified JS and our own CP-CANARY tokens are all
# high-entropy non-secrets.
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|auth|credential|access[_-]?key)\b"
    r"\s*[:=]\s*[\"']?([A-Za-z0-9+/=_-]{8,})[\"']?"
)
_BEARER = re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+([A-Za-z0-9+/=._-]{8,})")

_SEPARATORS = "-_"

# Documentation placeholders must not hard-block. Applies to Track 1 too.
_PLACEHOLDER_TOKENS = ("your_", "_here", "example", "placeholder", "xxxx", "changeme",
                       "dummy", "sample", "redacted", "<", ">")

# Small embedded word list for the dictionary guard. A passphrase in a password field is
# a REAL credential, so a hit downgrades severity rather than suppressing the finding.
_COMMON_WORDS = {
    "correct", "horse", "battery", "staple", "password", "the", "and", "for", "you",
    "this", "that", "with", "have", "from", "your", "refund", "window", "thirty",
    "please", "return", "policy", "account", "number", "order", "customer", "support",
    "hello", "world", "test", "admin", "user", "login", "secret", "value", "data",
}


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: Dict[str, int] = {}
    for c in s:
        counts[c] = counts.get(c, 0) + 1
    n = len(s)
    return -sum((v / n) * math.log2(v / n) for v in counts.values())


def detect_charset_size(s: str) -> int:
    """
    Classify by CHARACTER CLASS, never by counting distinct characters observed.

    Using the observed count gives a repetitive string a tiny denominator and inflates
    its ratio - the exact opposite of the intent. "aaaaaaaa" would score 0/log2(1).
    """
    if not s:
        return 95
    if all(c in "0123456789abcdefABCDEF" for c in s):
        return 16
    if all(c.isalnum() or c in "+/=" for c in s):
        return 64 if any(c in "+/=" for c in s) else 62
    return 95


def normalized_entropy(s: str) -> float:
    """
    Entropy against the string's own ceiling, so ONE threshold works across every
    charset. Raw entropy has two ceilings - alphabet size and string length - and
    whichever binds is the right denominator.

    Separator stripping is mandatory, not cosmetic. Hyphens impose a double penalty:
    they lower raw entropy AND push charset classification out of hex(16) into
    printable(95), raising the ceiling. Without stripping, a UUID-format API key is a
    systematic false negative (ratio 0.66 vs 0.96).
    """
    core = s.strip(_SEPARATORS).replace("-", "").replace("_", "")
    if len(core) < 2:
        return 0.0
    ceiling = min(math.log2(detect_charset_size(core)), math.log2(len(core)))
    if ceiling <= 0:
        return 0.0
    return shannon_entropy(core) / ceiling


def looks_like_placeholder(value: str) -> bool:
    """Documentation examples must not trigger a block."""
    low = value.lower()
    if any(tok in low for tok in _PLACEHOLDER_TOKENS):
        return True
    # long run of one repeated character
    return bool(re.search(r"(.)\1{5,}", value))


def is_mostly_dictionary_words(value: str) -> bool:
    """Greedy segmentation into common words — detects passphrases, not random keys."""
    core = value.lower().strip(_SEPARATORS).replace("-", "").replace("_", "")
    matched, i = 0, 0
    while i < len(core):
        for size in range(min(10, len(core) - i), 2, -1):
            if core[i:i + size] in _COMMON_WORDS:
                matched += size
                i += size
                break
        else:
            i += 1
    return len(core) > 0 and matched / len(core) >= 0.6


def classify_generic_secret(value: str, bundle: Dict) -> Optional[str]:
    """
    Returns None (not a secret) or the severity to attach.

    **Ordering note (T0-9).** The spec runs the entropy gate BEFORE the dictionary
    guard, which makes that guard unreachable: passphrases have LOW entropy by
    construction, so `correcthorsebatterystaple` is filtered out at the entropy step and
    never reaches the downgrade branch. It is dropped entirely rather than downgraded -
    the opposite of the spec's stated intent that "a passphrase in a password field IS a
    real credential... downgrade, do not suppress."

    The dictionary check therefore runs first. This is safe because Track 2 already
    requires credential-ish assignment context: prose inside `password = "..."` is
    genuinely suspicious, and prose inside `description = "..."` never reaches here.
    """
    if len(value) < int(bundle.get("secret_min_length", 24)):
        return None                      # defer to Track 1 prefix signatures
    if looks_like_placeholder(value):
        return None
    if is_mostly_dictionary_words(value):
        return "medium"                  # downgrade, do not suppress
    if normalized_entropy(value) < float(bundle.get("secret_entropy_ratio_threshold", 0.9)):
        return None
    return "high"                        # T0-1: heuristic, so no hard override


# --- Check 4: blocklist --------------------------------------------------------------

@lru_cache(maxsize=32)
def _compile_blocklist(terms: Tuple[str, ...]) -> Optional[re.Pattern]:
    """
    ONE alternation regex scanning the text once, cached by term tuple.

    A naive per-term loop is O(N x text): a realistic enterprise blocklist of a few
    thousand terms would consume the entire T0 budget by itself (T0-6).
    """
    if not terms:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b", re.I)


# --- origin resolution ----------------------------------------------------------------

def _resolve_origin(matched: str, placeholder_map: Dict[str, str], forwarded_prompt: str) -> str:
    """
    Three-valued, because `warn-and-confirm` forwards raw PII and builds NO placeholder
    map. Checking only the map would label a benign echo of the user's own data
    `model_generated` — inverting the distinction origin exists to draw (T0-4).
    """
    if matched in placeholder_map:
        return "echoed_placeholder"
    if matched and matched in forwarded_prompt:
        return "echoed_from_input"
    return "model_generated"


def run_t0(
    output: str,
    canaries: CanaryScope,
    placeholder_map: Dict[str, str],
    bundle: Dict,
    forwarded_prompt: str = "",
) -> T0Result:
    """
    Run all four checks on the raw upstream output.

    Every check runs even when one fires: T0 costs well under a millisecond, and a
    complete finding list makes a far better audit record than a short-circuited one.
    The short-circuit is downstream — `hard_override` tells the gateway to skip T1/T2.
    """
    started = time.perf_counter()
    findings: List[Finding] = []

    # --- Check 1: canary leak ---------------------------------------------------------
    folded = re.sub(r"\s+", " ", output).lower()
    leaked = set()
    for entity_type, token in canaries.tokens():
        hit_at = output.find(token)
        if hit_at < 0 and token.lower() in folded:
            hit_at = 0
        if hit_at < 0:
            # truncated reproduction: the token's distinctive prefix alone
            stub = token[:len(token) - 16 + PREFIX_MATCH_LEN]
            hit_at = output.find(stub)
        if hit_at >= 0:
            leaked.add("system" if entity_type == "CANARY_SYSTEM" else "context")
            findings.append(Finding(
                check="canary", entity_type=entity_type,
                span=(hit_at, hit_at + len(token)), confidence=1.0,
                severity="hard", hard_override=True,
            ))

    # --- Check 2: checksum-validated identifiers --------------------------------------
    # Reuses the shared catalog rather than duplicating regexes between gate and T0.
    for pii in scan_pii(output):
        matched = output[pii.span[0]:pii.span[1]]
        findings.append(Finding(
            check="pii_id", entity_type=pii.entity_type, span=pii.span,
            confidence=pii.confidence, severity="high", hard_override=False,
            origin=_resolve_origin(matched, placeholder_map, forwarded_prompt),
        ))

    # echoed placeholders: the model repeating [EMAIL_1] we injected ourselves
    for placeholder in placeholder_map:
        at = output.find(placeholder)
        if at >= 0:
            findings.append(Finding(
                check="pii_id", entity_type="PLACEHOLDER", span=(at, at + len(placeholder)),
                confidence=1.0, severity="low", origin="echoed_placeholder",
            ))

    # --- Check 3: secrets --------------------------------------------------------------
    for name, pattern in _TRACK1_COMPILED:
        for m in pattern.finditer(output):
            if looks_like_placeholder(m.group(0)):
                continue                 # documentation example, not a credential
            findings.append(Finding(
                check="secret", entity_type=name, span=(m.start(), m.end()),
                confidence=0.99, severity="hard", hard_override=True,
            ))

    claimed = {(f.span[0], f.span[1]) for f in findings if f.check == "secret"}
    for pattern in (_ASSIGNMENT, _BEARER):
        for m in pattern.finditer(output):
            value = m.group(m.lastindex)
            span = (m.start(m.lastindex), m.end(m.lastindex))
            if any(span[0] < e and s < span[1] for s, e in claimed):
                continue                 # already a Track 1 hit
            severity = classify_generic_secret(value, bundle)
            if severity:
                findings.append(Finding(
                    check="secret", entity_type="GENERIC_SECRET", span=span,
                    confidence=0.75, severity=severity, hard_override=False,
                ))

    # --- Check 4: blocklist -------------------------------------------------------------
    blocklist = _compile_blocklist(tuple(bundle.get("blocklist_terms", []) or ()))
    if blocklist:
        for m in blocklist.finditer(output):
            findings.append(Finding(
                check="blocklist", entity_type="BLOCKLIST_TERM",
                span=(m.start(), m.end()), confidence=1.0, severity="medium",
            ))

    canary_leak = None
    if leaked:
        canary_leak = "both" if len(leaked) == 2 else leaked.pop()

    return T0Result(
        findings=findings,
        hard_override=any(f.hard_override for f in findings),
        canary_leak=canary_leak,
        latency_ms=(time.perf_counter() - started) * 1000.0,
    )
