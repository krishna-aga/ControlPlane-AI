"""
In-flight PII detection and reversible de-anonymization for the Input Gate.

Two properties this module is built around:

1. **Spans index the RAW prompt.** Detection never runs on the canonical view. Running
   it there would place every span behind an NFKC fold and a whitespace collapse, so the
   placeholder map would not index the string actually forwarded - the input-side twin
   of the T0-5 span-drift defect in the locking register.

2. **Pattern precedence is explicit.** The catalogue in docs/IN_FLIGHT_PII_REDACTION.md
   has overlapping patterns and states no ordering, so the outcome depended on
   evaluation order. The UPI pattern in particular matches the prefix of every email
   address: "john@test.com" is claimed as UPI 'john@test' unless EMAIL is given
   precedence. Candidates are now gathered from every pattern and resolved by
   (precedence, position), claiming non-overlapping spans.

Checksum-validated entities (Luhn for cards, Verhoeff for Aadhaar) report confidence
1.0; regex-only entities carry a per-pattern confidence. These are detector outputs, not
policy - the bundle supplies thresholds and actions, not what a detector believes.
"""

import re
from typing import Dict, List, Tuple

from data_plane.models import PIIFinding

# --- checksum validators ------------------------------------------------------------

def luhn_valid(digits: str) -> bool:
    """Luhn mod-10, used for payment card numbers."""
    ds = [int(c) for c in digits if c.isdigit()]
    if len(ds) < 13:
        return False
    total, parity = 0, len(ds) % 2
    for i, d in enumerate(ds):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6), (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4), (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2), (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_valid(digits: str) -> bool:
    """Verhoeff checksum, used for Aadhaar numbers."""
    ds = [int(c) for c in digits if c.isdigit()]
    if len(ds) != 12:
        return False
    check = 0
    for i, d in enumerate(reversed(ds)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][d]]
    return check == 0


# --- pattern catalogue --------------------------------------------------------------
# (precedence, entity_type, confidence, validator, pattern)
# Lower precedence claims a span first. EMAIL must outrank UPI; checksum-gated numeric
# types are tried before the looser numeric ones.

_PATTERNS = [
    (10, "EMAIL",       0.95, None,           r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    (20, "SECRET",      0.99, None,           r"\b(?:sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36})\b"),
    (30, "CREDIT_CARD", 1.00, "luhn",         r"\b(?:\d[ -]?){13,19}\b"),
    (40, "AADHAAR",     1.00, "verhoeff",     r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b"),
    (50, "SSN",         0.90, None,           r"\b\d{3}-\d{2}-\d{4}\b"),
    (60, "PAN",         0.90, None,           r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    (70, "PHONE",       0.85, None,           r"\b(?:\+91[\-\s]?)?[6-9]\d{9}\b"),
    (80, "IP_ADDR",     0.80, None,           r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
    (90, "UPI",         0.75, None,           r"\b[a-zA-Z0-9.\-_]{2,64}@[a-zA-Z]{2,64}\b"),
]

_COMPILED = [
    (prec, etype, conf, validator, re.compile(pat))
    for prec, etype, conf, validator, pat in _PATTERNS
]

_VALIDATORS = {"luhn": luhn_valid, "verhoeff": verhoeff_valid}


def _overlaps(span: Tuple[int, int], claimed: List[Tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in claimed)


def scan_pii(raw_prompt: str) -> List[PIIFinding]:
    """
    Detect PII in the RAW prompt. Spans index `raw_prompt` directly.

    Candidates from every pattern are gathered first, then resolved by
    (precedence, start) so a higher-precedence type claims a contested span. A candidate
    whose checksum validator fails is discarded rather than claimed, which lets a lower
    precedence pattern try the same span.
    """
    candidates = []
    for prec, etype, conf, validator, pattern in _COMPILED:
        for match in pattern.finditer(raw_prompt):
            text = match.group(0)
            if validator is not None:
                if not _VALIDATORS[validator](text):
                    continue
            candidates.append((prec, match.start(), match.end(), etype, conf, validator))

    candidates.sort(key=lambda c: (c[0], c[1]))

    findings: List[PIIFinding] = []
    claimed: List[Tuple[int, int]] = []
    for _, start, end, etype, conf, validator in candidates:
        if _overlaps((start, end), claimed):
            continue
        claimed.append((start, end))
        findings.append(PIIFinding(
            entity_type=etype,
            span=(start, end),
            confidence=conf,
            checksum_validated=validator is not None,
        ))

    findings.sort(key=lambda f: f.span[0])
    return findings


def redact(raw_prompt: str, findings: List[PIIFinding]) -> Tuple[str, Dict[str, str]]:
    """
    Replace each finding with a typed placeholder, returning the forwardable prompt and
    the volatile restore map.

    Substitution runs right-to-left so that earlier spans keep their offsets. Identical
    values share one placeholder, so the model sees a consistent entity across the
    prompt and can refer to it coherently.

    The returned map is volatile: it lives for the request only and is never written to
    the ledger. See InputGateResult.ledger_row().
    """
    counters: Dict[str, int] = {}
    by_value: Dict[Tuple[str, str], str] = {}
    restore_map: Dict[str, str] = {}

    for finding in findings:
        value = raw_prompt[finding.span[0]:finding.span[1]]
        key = (finding.entity_type, value)
        if key not in by_value:
            counters[finding.entity_type] = counters.get(finding.entity_type, 0) + 1
            placeholder = f"[{finding.entity_type}_{counters[finding.entity_type]}]"
            by_value[key] = placeholder
            restore_map[placeholder] = value
        finding.placeholder = by_value[key]

    out = raw_prompt
    for finding in sorted(findings, key=lambda f: f.span[0], reverse=True):
        out = out[:finding.span[0]] + (finding.placeholder or "") + out[finding.span[1]:]

    return out, restore_map


def deanonymize(text: str, restore_map: Dict[str, str]) -> str:
    """Restore placeholders to their original values in the response shown to the user."""
    for placeholder, value in restore_map.items():
        text = text.replace(placeholder, value)
    return text
