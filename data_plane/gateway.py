"""
ControlPlane.ai Data Plane gateway.

    load bundle (hash-verified)
        │
    Input Gate ──────────────► BLOCK (never calls the model)
        │  FLAG / ALLOW
    mint canaries, assemble payload      ◄── the gateway builds the call, not the tenant
        │
    upstream model (BYOK, buffered)
        │
    T0 ──── hard_override ───► skip T1/T2
        │
    T1: grounding (if context_docs), toxicity   ◄── T2 not built - see docs/GATEWAY.md
        │
    fusion (input risk tightens the bands)
        │
    apply action: mask spans          ◄── MUST precede de-anonymization (T0-5)
        │
    de-anonymize for the client
        │
    ledger row (types, spans, scores - never values)

Library first, HTTP later: `process_request()` takes exactly what an HTTP handler would
deserialize from a request body, so adding a transport does not move this boundary.
"""

import time
import uuid
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from data_plane import canary
from data_plane.adapters import Credentials, ModelAdapter, ModelCallError, ModelResponse, get_adapter
from data_plane.cache import SemanticCache, namespace_key, servable, storable
from data_plane.detectors.grounding import score_grounding
from data_plane.detectors.pii import deanonymize
from data_plane.detectors.t0 import run_t0
from data_plane.detectors.toxicity import score_toxicity
from data_plane.fusion import fuse
from data_plane.input_gate import get_bundle, process_input
from data_plane.ledger import Ledger
from data_plane.models import DetectorSignals, FusionResult, InputGateResult, T0Result
from data_plane.session import SessionState, SessionStore, message_hash


class GatewayResult(BaseModel):
    action: str                      # ALLOW | REDACT | REGENERATE | FLAG | BLOCK
    response: str                    # delivered text, or the refusal explanation
    fused_risk: float = 0.0
    reason: str = ""
    served_from: str = "upstream"

    request_id: str = ""
    policy_hash: str = ""
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    provider_refused: bool = False

    cache_similarity: float = 0.0
    cache_source_request_id: str = ""
    cache_skip_reason: str = ""

    input_gate: Optional[InputGateResult] = None
    t0: Optional[T0Result] = None
    fusion: Optional[FusionResult] = None
    ledger_row: Dict = Field(default_factory=dict)


class Gateway:
    """Holds only cross-request state that is content-free: sessions and the ledger."""

    def __init__(self, adapter: Optional[ModelAdapter] = None,
                 sessions: Optional[SessionStore] = None,
                 ledger: Optional[Ledger] = None,
                 cache: Optional[SemanticCache] = None):
        self.adapter = adapter
        self.sessions = sessions or SessionStore()
        self.ledger = ledger or Ledger()
        self.cache = cache or SemanticCache()

    # -- helpers ---------------------------------------------------------------------

    def _scan_history(self, messages: List[Dict[str, str]], bundle: Dict,
                      session: SessionState) -> tuple[InputGateResult, Dict[str, str], float]:
        """
        Run the Input Gate over the replayed history.

        Only messages this session has not adjudicated before are scanned; the rest reuse
        their stored risk. That keeps per-turn cost flat as a conversation grows, and it
        stops one replayed attack being counted as twenty separate attacks.

        The gate result returned is for the LATEST user message, since that is the text
        being answered. Risk is the max across the live array, because an injection
        planted at turn 1 is still in the payload the model reads.
        """
        live_risk = 0.0
        latest_result: Optional[InputGateResult] = None
        latest_map: Dict[str, str] = {}

        for message in messages:
            h = message_hash(message["role"], message["content"])
            if session.seen(h):
                live_risk = max(live_risk, session.adjudicated[h])
                continue
            result, restore = process_input(message["content"], bundle)
            session.record(h, result.injection_risk, result.action in ("FLAG", "BLOCK"))
            live_risk = max(live_risk, result.injection_risk)
            if message["role"] == "user":
                latest_result, latest_map = result, restore

        if latest_result is None:
            # Every message was already adjudicated; re-derive the latest for its
            # forward_prompt and placeholder map, which are per-request and not cached.
            last_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
            content = last_user["content"] if last_user else ""
            latest_result, latest_map = process_input(content, bundle)

        return latest_result, latest_map, live_risk

    @staticmethod
    def _run_t0(text: str, scope, restore_map: Dict[str, str], bundle: Dict,
                forward_prompt: str) -> tuple[Optional[T0Result], bool]:
        """
        Runs T0 and reports whether it threw, rather than letting the exception cross
        into fusion indistinguishably from "no findings" (T1-7). Returns (result, failed).
        """
        try:
            return run_t0(text, scope, restore_map, bundle, forward_prompt), False
        except Exception:
            return None, True

    @staticmethod
    def _run_grounding(text: str, context_docs: List[str], bundle: Dict) -> tuple[Optional[float], bool]:
        """Same T1-7 treatment as _run_t0: a thrown detector is 'failed', not 'None'."""
        try:
            return score_grounding(text, context_docs, bundle), False
        except Exception:
            return None, True

    @staticmethod
    def _run_toxicity(text: str, bundle: Dict) -> tuple[Optional[float], bool]:
        try:
            return score_toxicity(text, bundle), False
        except Exception:
            return None, True

    @staticmethod
    def _apply_action(action: str, text: str, t0: Optional[T0Result]) -> str:
        """
        Execute the action on the output.

        REDACT masks by SPAN, and this must happen BEFORE de-anonymization: spans index
        the placeholder-bearing string, and `[EMAIL_1]` is nine characters where the
        restored address might be twenty-two. Masking after restoration would blank the
        wrong ranges (T0-5). Right-to-left so earlier spans stay valid.
        """
        if action != "REDACT" or t0 is None:
            return text
        risky = sorted(
            (f for f in t0.findings if f.origin == "model_generated"),
            key=lambda f: f.span[0], reverse=True,
        )
        for finding in risky:
            start, end = finding.span
            text = text[:start] + f"[{finding.entity_type}_REDACTED]" + text[end:]
        return text

    # -- entry point ------------------------------------------------------------------

    def process_request(
        self,
        messages: List[Dict[str, str]],
        bundle_path: str,
        session_id: str,
        credentials: Optional[Credentials] = None,
        system_prompt: str = "",
        context_docs: Optional[List[str]] = None,
        tenant_id: Optional[str] = None,
        entitlement_scope: str = "",
    ) -> GatewayResult:
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        bundle = get_bundle(bundle_path)
        credentials = credentials or Credentials()
        context_docs = context_docs or []
        session = self.sessions.get(session_id)

        gate, restore_map, live_injection_risk = self._scan_history(messages, bundle, session)

        def finish(action: str, response: str, reason: str,
                   t0: Optional[T0Result] = None, fusion: Optional[FusionResult] = None,
                   model: Optional[ModelResponse] = None,
                   served_from: str = "upstream", similarity: float = 0.0,
                   source_request_id: str = "", skip_reason: str = "") -> GatewayResult:
            latency = (time.perf_counter() - started) * 1000.0
            row = self.ledger.append({
                "request_id": request_id,
                "policy_hash": bundle.get("policy_hash", ""),
                "action": action,
                "reason": reason,
                "provider": credentials.provider,
                "model": credentials.model,     # never the api_key
                "input_gate": gate.ledger_row(),
                "t0": t0.ledger_row() if t0 else None,
                "fusion": fusion.ledger_row() if fusion else None,
                "session": session.ledger_row(),
                "input_tokens": model.input_tokens if model else 0,
                "output_tokens": model.output_tokens if model else 0,
                "latency_ms": round(latency, 3),
                # A cache hit still writes a row. Without one the audit trail has holes
                # exactly where the cheap path ran, and "why did this user get this
                # answer in March" would have no record for the fastest requests.
                "cache": {
                    "served_from": served_from,
                    "similarity": round(similarity, 4),
                    "source_request_id": source_request_id,
                    "skip_reason": skip_reason,
                },
            })
            return GatewayResult(
                action=action, response=response, reason=reason,
                fused_risk=fusion.fused_risk if fusion else 0.0,
                request_id=request_id, policy_hash=bundle.get("policy_hash", ""),
                latency_ms=latency,
                input_tokens=model.input_tokens if model else 0,
                output_tokens=model.output_tokens if model else 0,
                provider_refused=bool(model and model.provider_refused),
                served_from=served_from, cache_similarity=similarity,
                cache_source_request_id=source_request_id, cache_skip_reason=skip_reason,
                input_gate=gate, t0=t0, fusion=fusion, ledger_row=row,
            )

        # --- Input Gate refusal: the model is never called ---------------------------
        if gate.action == "BLOCK":
            return finish("BLOCK", gate.blocked_reason or "Request refused by policy.",
                          gate.blocked_reason or "input gate")

        # --- assemble the payload. The GATEWAY plants the canary, not the tenant ------
        scope = canary.mint(has_context=bool(context_docs))

        # --- semantic cache -----------------------------------------------------------
        # Placed AFTER the Input Gate and before the model call. Before the gate would
        # let a poisoned prompt reach a cached answer without ever being scanned; after
        # the model call there is nothing left to save.
        cache_ok, cache_skip = servable(bundle, tenant_id, restore_map,
                                        gate.action == "FLAG", len(messages))
        namespace = ""
        if cache_ok:
            namespace = namespace_key(
                tenant_id or "", entitlement_scope, bundle.get("policy_hash", ""),
                system_prompt, context_docs, self.cache.embedder.id,
            )
            found = self.cache.lookup(namespace, gate.forward_prompt,
                                      float(bundle["cache_threshold"]))
            if found.hit and found.entry is not None:
                # THE CACHE NEVER SKIPS TIER 0. Re-run it on the stored text against the
                # bundle in force right now, so the invariant "nothing reaches a client
                # unchecked by T0" holds without a cache-shaped exception. A stored entry
                # that no longer passes is evicted rather than served.
                revalidated, t0_failed = self._run_t0(found.entry.response, scope, {},
                                                      bundle, gate.forward_prompt)
                if t0_failed or revalidated.findings:
                    self.cache.evict_entry(namespace, found.entry)
                    cache_skip = (
                        "cached entry's Tier 0 revalidation failed (T1-7); evicted, "
                        "not served" if t0_failed else
                        "cached entry failed Tier 0 revalidation; evicted"
                    )
                else:
                    fusion = fuse(DetectorSignals(injection_risk=live_injection_risk), bundle)
                    return finish(
                        "ALLOW", found.entry.response,
                        f"Served from cache ({found.reason}).",
                        t0=revalidated, fusion=fusion, served_from="cache",
                        similarity=found.similarity,
                        source_request_id=found.entry.source_request_id,
                    )
            else:
                cache_skip = found.reason
        upstream_system = canary.plant_system(system_prompt, scope)
        context_block = canary.plant_context(context_docs, scope)

        upstream_messages = [dict(m) for m in messages[:-1]] if len(messages) > 1 else []
        user_content = gate.forward_prompt          # PII placeholdered, injection NOT edited
        if context_block:
            user_content = f"{context_block}\n\n{user_content}"
        upstream_messages.append({"role": "user", "content": user_content})

        # --- upstream call (buffered) -------------------------------------------------
        adapter = self.adapter or get_adapter(credentials)
        try:
            model = adapter.generate(upstream_system, upstream_messages, credentials)
        except ModelCallError as e:
            # A model failure is NOT a detector failure: there is no output to check, so
            # fail_mode does not apply. It is an error either way.
            return finish("BLOCK", "The upstream model could not be reached.", str(e))

        if model.provider_refused:
            return finish("BLOCK", "The upstream provider declined to answer this request.",
                          f"provider refusal ({model.finish_reason})", model=model)

        # --- T0, then fusion -----------------------------------------------------------
        t0, t0_failed = self._run_t0(model.text, scope, restore_map, bundle, gate.forward_prompt)
        detector_status = {"t0": "failed"} if t0_failed else {}

        if t0 is not None and t0.hard_override:
            # Skipping T1/T2 is correct for cost - the decision is already final. Fusion
            # still runs so the ledger row carries the same shape as every other row.
            signals = DetectorSignals(
                t0_severities=t0.severities,
                injection_risk=live_injection_risk,
                input_flagged=(gate.action == "FLAG"),
                detector_status=detector_status,
            )
            fusion = fuse(signals, bundle)
            return finish("BLOCK", "This response was blocked before delivery.",
                          "Tier 0 hard override", t0=t0, fusion=fusion, model=model)

        # --- T1: grounding (only when there is context to check against) + toxicity ----
        grounding_similarity, grounding_failed = (
            self._run_grounding(model.text, context_docs, bundle) if context_docs
            else (None, False)
        )
        toxicity_probability, toxicity_failed = self._run_toxicity(model.text, bundle)
        if grounding_failed:
            detector_status["grounding"] = "failed"
        if toxicity_failed:
            detector_status["toxicity"] = "failed"

        signals = DetectorSignals(
            t0_severities=t0.severities if t0 else [],
            grounding_similarity=grounding_similarity,
            toxicity_probability=toxicity_probability,
            injection_risk=live_injection_risk,
            input_flagged=(gate.action == "FLAG"),
            detector_status=detector_status,
        )

        fusion = fuse(signals, bundle)
        action = fusion.action

        if action == "BLOCK":
            return finish("BLOCK", "This response was blocked before delivery.",
                          fusion.reason, t0=t0, fusion=fusion, model=model)

        # --- masking BEFORE de-anonymization, then restore for the client --------------
        text = self._apply_action(action, model.text, t0)
        text = deanonymize(text, restore_map)

        if action == "REGENERATE":
            session.rework_count += 1

        write_ok, write_skip = storable(
            action, bundle, tenant_id, restore_map, len(t0.findings) if t0 else 0,
            gate.action == "FLAG", len(messages), bool(model.provider_refused),
        )
        # A response NO detector could verify must never seed the cache, and this covers
        # T1 as well as T0. The distinction matters: the hit path re-runs T0 but never
        # re-runs T1, so a transient grounding or toxicity failure written into an entry
        # is never checked again - one flaky detector call becomes a permanently
        # unverified answer served to every subsequent match. That is strictly worse than
        # the T0 case, which at least gets revalidated on serve. This is the cache half
        # of T1-7: `fail_mode` decides what to DELIVER when verification failed; the
        # cache decides what to REMEMBER, and remembering an unverified answer outlives
        # the request that produced it.
        _DETECTOR_NAMES = {"t0": "Tier 0", "grounding": "grounding", "toxicity": "toxicity"}
        unverified = sorted(_DETECTOR_NAMES.get(d, d)
                            for d, s in detector_status.items() if s == "failed")
        if unverified:
            write_ok, write_skip = False, (
                f"{', '.join(unverified)} could not verify this response (T1-7); not cached")
        if write_ok and namespace:
            self.cache.store(namespace, gate.forward_prompt, text, request_id,
                             bundle.get("policy_hash", ""))
        elif not write_ok:
            cache_skip = write_skip

        return finish(action, text, fusion.reason, t0=t0, fusion=fusion, model=model,
                      skip_reason=cache_skip)


def process_request(messages, bundle_path, session_id, credentials=None,
                    system_prompt="", context_docs=None, tenant_id=None,
                    entitlement_scope="", gateway=None) -> GatewayResult:
    """
    Module-level convenience entry point. Prefer `Gateway` when state must persist.

    Note that a fresh `Gateway` carries an empty cache, so this path never hits. That is
    correct rather than unfortunate: a cache shared across gateway instances would be a
    cache shared across whatever isolation the caller thought it had.
    """
    return (gateway or Gateway()).process_request(
        messages, bundle_path, session_id, credentials, system_prompt, context_docs,
        tenant_id, entitlement_scope,
    )
