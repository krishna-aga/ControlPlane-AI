# Skill: Verify Audit Ledger Integrity (`verify_ledger`)

## Trigger / Objective
Audit ledger tamper-detection verification. Traverse audit log records sequentially and verify cryptographic hash chaining (`hash = sha256(json.dumps(record, prev_hash))`) to prove to compliance officers that logs have not been altered or deleted after an incident.

## Input & Output Contracts
* **Inputs:**
  * `ledger_path` (str): Path to audit ledger file/table.
* **Outputs:**
  * `LedgerVerificationResult`: Total records verified, tamper status (`VALID` | `CORRUPTED`), corrupted line number (if any).

## Execution CLI Commands
```bash
python -m learning_plane.verify_ledger \
  --ledger logs/audit_ledger.jsonl
```

## Validation Checks
1. Every record must contain `prev_hash` matching the `hash` of the preceding line.
2. Confirm no raw PII values (credit card numbers, emails, passwords) exist in any record field.
