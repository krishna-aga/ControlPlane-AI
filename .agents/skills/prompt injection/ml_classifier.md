# Skill: Input Gate Lightweight ML Classifier

## 1. Skill Overview
* **Skill Identifier:** `skill_ml_injection_classifier`
* **Layer:** Data Plane (Input Gate Pipeline)
* **Purpose:** Catch semantic and rephrased prompt injections that evade static regex rules by utilizing a local DeBERTa/MiniLM classifier (~15–20ms latency)[cite: 2].
* **Execution Environment:** Local CPU inference via HuggingFace transformers (`data_plane/detectors/ml_classifier.py`).

---

## 2. Trigger & Objective
* **When to Trigger:** Invoked immediately after the Heuristic Scanner returns clean or non-blocking results on incoming prompts[cite: 2].
* **Primary Objective:** Compute a robust probability score $P(\text{injection})$ for semantic prompt hijacking[cite: 2].
* **Secondary Objective:** Enforce bundle-defined thresholds dynamically without hardcoding sensitivity values[cite: 2].

---

## 3. Input & Output Contract

### Input Contract
```python
sanitized_prompt: str   # Cleaned text output from the normalization/heuristic layer
bundle: dict            # Loaded JSON bundle containing "injection_threshold" and "pii_mode"