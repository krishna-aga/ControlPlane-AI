# In-Flight PII Redaction & Reversible De-Anonymization Specification

## 1. Overview & Core Architecture

In-Flight PII Redaction intercepts, anonymizes, and de-anonymizes sensitive data passing through the **ControlPlane.ai Gateway** in real time. It ensures sensitive enterprise and personal data never reaches third-party LLM providers while preserving full conversational context for the end user.

```text
                               IN-FLIGHT REDACTION PIPELINE
                               
 User Input Prompt                                                  Upstream LLM (BYOK)
(e.g., "Email john@test.com                                        (Receives sanitized prompt:
  or Aadhaar 9999 8888 7777")                                       "Email [EMAIL_1] or
         │                                                            Aadhaar [AADHAAR_1]")
         ▼                                                                     │
┌──────────────────────────┐     Volatile In-Memory Map                        ▼
│ 1. Regex Input Scanner   ├───────────────────────────────┐        ┌─────────────────────────┐
│  - Email, Phone, Credit  │  {"[EMAIL_1]": "john@test.com",│        │ 3. LLM Response Return  │
│  - Aadhaar, PAN, UPI     │   "[AADHAAR_1]": "9999..."}   │        └────────────┬────────────┘
└────────┬─────────────────┘                               │                     │
         │                                                 ▼                     ▼
         ▼                                      ┌──────────────────────────────────────────┐
┌──────────────────────────┐                    │ 4. Reversible De-Anonymizer              │
│ 2. Placeholder Injection │                    │  - Restores placeholders back to raw PII │
│  - [EMAIL_1], [PAN_1]    │                    │    in client response only               │
└──────────────────────────┘                    └────────────────────┬─────────────────────┘
                                                                     │
                                                                     ▼
                                                             Sanitized Output to Client
                                                            (Volatile Map Destroyed from RAM)
```

---

## 2. Selected Design Decisions

### A. Hybrid Detection Engine (Option C)
* **Input Gate (Pre-LLM)**: Pre-compiled Regex patterns + Luhn/Verhoeff algorithmic checksum matchers ($\sim 1\text{ ms}$ latency).
* **Tier 1 Output Cascade (Post-LLM)**: spaCy / HuggingFace Named Entity Recognition (NER) model for contextual PII detection (person names, locations, organizations).

### B. Reversible De-Anonymization (In-Memory Scope)
1. **Anonymization**: Input Gate replaces detected raw PII values with scoped tokens (`[EMAIL_1]`, `[AADHAAR_1]`).
2. **Volatile Map**: The raw values are temporarily mapped in volatile RAM for the duration of the request lifecycle.
3. **De-Anonymization**: Before returning the LLM response to the client, placeholders present in the output are mapped back to their original values.
4. **Zero-Persistence Privacy**: The volatile mapping is destroyed immediately upon request completion and is **NEVER** stored in logs, disk, or audit ledgers.

---

## 3. Entity Pattern Catalog (Global + Indian Regional)

| Category | Entity Type | Detection Mechanism / Regex Signature | Placeholder |
| :--- | :--- | :--- | :--- |
| **Global** | **Email Address** | `\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z\|a-z]{2,}\b` | `[EMAIL_N]` |
| **Global** | **Credit Card** | `\b(?:\d[ -]*?){13,16}\b` + **Luhn Algorithm Checksum** | `[CREDIT_CARD_N]` |
| **Global** | **IPv4 / IPv6** | `\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b` | `[IP_ADDR_N]` |
| **Global** | **SSN (US)** | `\b\d{3}-\d{2}-\d{4}\b` | `[SSN_N]` |
| **Global** | **API Secrets / Tokens** | `\b(?:sk-[a-zA-Z0-9]{32,}\|AKIA[0-9A-Z]{16})\b` | `[SECRET_N]` |
| **Indian Regional** | **Aadhaar Card** | `\b[2-9]{1}[0-9]{3}\s?[0-9]{4}\s?[0-9]{4}\b` + **Verhoeff Checksum** | `[AADHAAR_N]` |
| **Indian Regional** | **PAN Card** | `\b[A-Z]{5}[0-9]{4}[A-Z]{1}\b` | `[PAN_N]` |
| **Indian Regional** | **Mobile Number** | `\b(?:\+91[\-\s]?)?[6-9]\d{9}\b` | `[PHONE_N]` |
| **Indian Regional** | **UPI ID** | `\b[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z]{2,64}\b` | `[UPI_N]` |

---

## 4. Policy Mode Mapping

* **`redact-and-proceed`**: Replaces PII with placeholders, executes upstream LLM call, de-anonymizes placeholders on return.
* **`block-and-explain`**: Halts immediately on PII detection without invoking upstream LLM.
* **`warn-and-confirm`**: Passes raw PII, logs warning banner.

---

## 5. Ledger & Compliance Guarantee

The audit ledger records **ONLY** non-reversible entity metadata:
```json
{
  "entity_type": "AADHAAR",
  "character_span": [15, 31],
  "confidence_score": 1.0,
  "action": "REDACT"
}
```
Raw values (`9999 8888 7777`) and placeholder mappings (`[AADHAAR_1]`) are **strictly excluded** from ledger storage.
