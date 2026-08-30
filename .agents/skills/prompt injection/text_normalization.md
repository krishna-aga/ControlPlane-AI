# Skill & Architecture Spec: Input Text Normalization

## 1. Overview & Purpose
**Text Normalization** is the deterministic pre-processing stage located at the very front of the **Input Gate** in the Data Plane[cite: 2].

Adversaries often disguise prompt injection, jailbreak attempts, or data extraction keywords using character substitutions, invisible Unicode tokens, or multi-encoding tricks to bypass regex pattern matchers and tokenization boundaries[cite: 2]. Text normalization strips away these visual and structural evasions, converting raw text into a standard, canonical format before running heuristic scanners, regex matchers, or ML embedding models[cite: 2].

---

## 2. Core Normalization Operations

```text
Raw Prompt Input
       │
       ▼
 1. Base64 & Hex Payload Decoding ──► Unpacks hidden encoded instruction strings
       │
       ▼
 2. Unicode NFKC Normalization    ──► Resolves homoglyphs & visual lookalike glyphs
       │
       ▼
 3. Invisible Character Stripping ──► Removes zero-width spaces (\u200B) & format marks
       │
       ▼
 4. Leetspeak & Symbol Mapping    ──► Maps character substitutes (@ -> a, 1 -> i, 0 -> o)
       │
       ▼
 5. Whitespace & Noise Collapse   ──► Compresses multi-spaces, repeated punctuation
       │
       ▼
 Clean Canonical Prompt (Passed to Heuristics & Classifiers)