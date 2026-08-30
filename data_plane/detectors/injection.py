"""
Prompt-injection heuristics for the Input Gate.

Scans the CANONICAL view (see data_plane/normalizer.py) and reports findings with their
matched text, so the gate can show what was detected and what obfuscation was undone to
reach it.

Scoring: severity is `max` over matched patterns - consistent with the bundle's
`t0_aggregation: max` default, and it avoids a pile of weak matches summing past a
strong one. An evasion penalty from the bundle is then added, because a phrase that
arrives deliberately disguised carries more evidence of intent than the same phrase sent
plainly.
"""

import re
from typing import Dict, List, Tuple

from data_plane.models import CanonicalView, InjectionFinding

# (name, severity, pattern). Severities are relative weights within the input gate and
# are compared against the bundle's injection_threshold.
_PATTERNS: List[Tuple[str, float, str]] = [
    ("INSTRUCTION_OVERRIDE", 0.90,
     r"(?i)\b(ignore|disregard|forget|bypass|override)\s+(all\s+|any\s+)?"
     r"(of\s+)?(the\s+)?(previous|prior|above|earlier|system|initial)\s+"
     r"(instructions?|rules?|prompts?|directives?|guidelines?)"),

    ("SYSTEM_PROMPT_EXTRACTION", 0.85,
     r"(?i)\b(output|print|reveal|show|display|repeat|echo|dump|tell\s+me)\s+"
     r"(the\s+|your\s+)?(system\s+prompt|initial\s+instructions|developer\s+message|"
     r"system\s+message|configuration|prompt\s+above)"),

    ("ROLE_HIJACK", 0.75,
     r"(?i)\b(you\s+are\s+now|from\s+now\s+on\s+you|act\s+as|pretend\s+to\s+be|"
     r"roleplay\s+as|enter\s+(developer|debug|god)\s+mode|dan\s+mode|jailbreak\s+mode)\b"),

    ("DELIMITER_INJECTION", 0.80,
     r"(?i)(<\s*/?\s*system\s*>|\[\s*/?\s*inst\s*\]|<\|\s*im_(start|end)\s*\|>|"
     r"-{2,}\s*(begin|end)\s+system\s+prompt\s*-{2,})"),

    ("FAKE_AUTHORITY", 0.70,
     r"(?i)```(markdown|json|yaml)?\s*(system|admin|root|developer)\s*:"),

    ("GUARDRAIL_PROBE", 0.55,
     r"(?i)\b(what\s+are\s+your\s+(rules|instructions|restrictions)|"
     r"repeat\s+everything\s+above|say\s+nothing\s+about\s+(your|the)\s+(rules|prompt))\b"),

    ("EXFIL_TRANSFORM", 0.65,
     r"(?i)\b(base64|rot13|reverse[d]?|backwards|spell\s+out|with\s+a\s+space\s+between)\b"
     r"[^.]{0,40}\b(the\s+)?(system\s+prompt|instructions|secret|confidential|above)\b"),
]

COMPILED: List[Tuple[str, float, re.Pattern]] = [
    (name, severity, re.compile(pattern)) for name, severity, pattern in _PATTERNS
]


def scan_injection(view: CanonicalView, bundle: Dict) -> Tuple[float, List[InjectionFinding]]:
    """
    Score a canonical view for prompt injection.

    Returns (risk, findings). Risk is max(severity) plus the bundle's evasion penalty
    when the canonicalizer had to undo an obfuscation, clamped to 1.0.

    Every constant is read from the bundle - no thresholds are baked in here.
    """
    findings: List[InjectionFinding] = []
    for name, severity, pattern in COMPILED:
        match = pattern.search(view.text)
        if match:
            findings.append(InjectionFinding(
                pattern_name=name,
                severity=severity,
                matched_text=match.group(0),
                span=(match.start(), match.end()),
            ))

    if not findings:
        return 0.0, []

    risk = max(f.severity for f in findings)

    if view.had_evasion:
        risk += float(bundle.get("injection_evasion_penalty", 0.0))

    return min(risk, 1.0), findings
