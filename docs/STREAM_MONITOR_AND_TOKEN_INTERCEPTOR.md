# Upstream Call & Stream Monitor Specification: ControlPlane.ai

## 1. Overview & Purpose

The **Upstream Call & Stream Monitor** wraps third-party foundation model API calls (BYOK — Bring Your Own Key) using non-blocking asynchronous streaming. As tokens stream back chunk-by-chunk, an **In-Flight Token Interceptor** monitors generation in real time.

If a **repetition loop** (model getting stuck in an infinite phrase loop) or a **token runaway** (exceeding $2\times$ baseline expected token count) is detected, the interceptor immediately issues an in-flight stop command, terminating the stream with **0 ms added latency overhead**.

```text
                                STREAM MONITORING PIPELINE
                                
 User Prompt ──► [Gateway] ──► [Upstream LLM Stream (BYOK)]
                                           │
                                           ▼ (Asynchronous Token Chunks)
                               ┌───────────────────────┐
                               │ Async Stream Monitor  │
                               └───────────┬───────────┘
                                           │
                    ┌──────────────────────┴──────────────────────┐
                    ▼                                             ▼
       [Repetition Loop Check]                        [Token Runaway Check]
  (Sliding 4-gram window tracking)              (Exceeds 2x baseline max tokens)
                    │                                             │
                    └──────────────────────┬──────────────────────┘
                                           │
                                (Violation Triggered?)
                                           │
                       ┌───────────────────┴───────────────────┐
                      YES                                      NO
                       │                                       │
                       ▼                                       ▼
            [Abort Stream (0ms)]                      [Forward Token]
          Append [STREAM_TRUNCATED]                            │
                       │                                       ▼
                       └───────────────────────────────► Pass to Tier 1 Cascade
```

---

## 2. Token Interception Algorithms

### A. Repetition Loop Interceptor (Sliding N-Gram Window)
* **Problem**: Models can suffer from degeneration loops (repeating identical sentences continuously, wasting quota and compute).
* **Algorithm**:
  1. Maintain a sliding window buffer of the last $M$ tokens (e.g., $M = 64$).
  2. Extract sliding $N$-grams (where $N = 4$).
  3. If any 4-gram repeats sequentially $\ge 3$ times in the sliding window, trigger an immediate stream abort.

```python
# Conceptual Stream Interceptor Loop
async for chunk in upstream_stream:
    token = chunk.text
    buffer.append(token)
    
    if detect_ngram_repetition(buffer, n=4, max_repeats=3):
        stream_cancelled = True
        yield "[STREAM_INTERRUPTED_REPETITION_LOOP]"
        break
```

### B. Token Runaway Cut-off ($2\times$ Baseline)
* **Problem**: Prompt injection or unexpected model behavior can cause runaway generation until max model context limit is exhausted.
* **Algorithm**:
  1. Determine dynamic baseline budget $T_{\text{baseline}} = \min(\text{prompt\_length} \times 3, \text{configured\_max\_tokens})$.
  2. Set hard cutoff threshold: $T_{\text{cutoff}} = 2 \times T_{\text{baseline}}$.
  3. If total generated tokens exceed $T_{\text{cutoff}}$, issue an immediate abort signal.

---

## 3. Zero-Latency Overhead Guarantee

Because the stream monitor operates **inside the asynchronous iteration loop** (`async for chunk in stream`) while tokens are transmitted across the network:
* Detection algorithms run on small token buffers in memory ($\sim 0.01\text{ ms}$ per chunk).
* Zero sequential latency is added to overall request execution time.

---

## 4. Telemetry & Ledger Logging

When a stream interruption occurs, the incident is logged in the interaction telemetry:
* `stream_interrupted`: `true`
* `interruption_reason`: `"REPETITION_LOOP"` | `"TOKEN_RUNAWAY"`
* `tokens_generated_before_abort`: `142`
* `cost_saved_est`: `₹0.42`
