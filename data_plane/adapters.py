"""
Upstream model adapters (BYOK).

**The gateway never holds a key.** Credentials arrive with the request, are used for that
one upstream call, and are dropped. Reading a key from process environment at import
time would make this a single-tenant gateway with our key in it, not BYOK - so
`Credentials` is a per-request argument, and env is a development fallback only.

The gateway also owns **payload assembly**. The tenant supplies the parts - system
prompt, messages, context chunks - and the gateway builds the upstream call. A tenant
handing over one opaque blob makes canary planting into the system role impossible, and
with it system-prompt leak detection (§8A of t0_deterministic_checks.md).

Responses are **buffered, never streamed**. The tiered cascade cannot BLOCK, REDACT or
REGENERATE text the client has already received, so streaming would silently downgrade
the action ladder from enforcement to logging.
"""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Protocol

from pydantic import BaseModel, Field


class ModelCallError(Exception):
    """Upstream call failed. Distinct from a detector failure - there is no output to check."""


class Credentials(BaseModel):
    """
    Per-request BYOK credentials.

    `api_key` must never reach a log line, a ledger row, or an exception message.
    `redacted()` exists so call sites have something safe to print.
    """

    provider: str = "mock"
    model: str = "mock-1"
    api_key: str = ""

    def redacted(self) -> Dict[str, str]:
        return {"provider": self.provider, "model": self.model, "api_key": "<redacted>"}

    @classmethod
    def from_env(cls, provider: str = "gemini", model: str = "gemini-2.0-flash") -> "Credentials":
        """
        DEVELOPMENT ONLY. Real tenants supply credentials with the request; resolving
        from process environment is what makes a gateway single-tenant.
        """
        return cls(provider=provider, model=model, api_key=os.environ.get("GEMINI_API_KEY", ""))


class ModelResponse(BaseModel):
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    finish_reason: str = "stop"
    # Providers run their own safety filters. A provider refusal is a real signal, not an
    # error: it means the upstream declined to answer. Surfaced so the gateway can record
    # it rather than mistaking an empty response for a clean one.
    provider_refused: bool = False


class ModelAdapter(Protocol):
    def generate(self, system_prompt: str, messages: List[Dict[str, str]],
                 credentials: Credentials) -> ModelResponse: ...


class MockAdapter:
    """
    Deterministic adapter for tests and demos.

    Scripted replies keyed by a substring of the last user message, so a demo fires the
    same T0 findings on every run - no network, no key, no rate limit mid-presentation.
    Anything unmatched returns a bland default.
    """

    def __init__(self, scripted: Optional[Dict[str, str]] = None, default: str = "Certainly - happy to help with that."):
        self.scripted = scripted or {}
        self.default = default
        self.calls: List[Dict] = []

    def generate(self, system_prompt: str, messages: List[Dict[str, str]],
                 credentials: Credentials) -> ModelResponse:
        started = time.perf_counter()
        last = messages[-1]["content"] if messages else ""
        text = self.default
        for needle, reply in self.scripted.items():
            if needle.lower() in last.lower():
                text = reply
                break
        # Recorded so tests can assert what the gateway actually SENT upstream - that the
        # canary was planted, and that raw PII was not forwarded.
        self.calls.append({"system_prompt": system_prompt, "messages": messages})
        return ModelResponse(
            text=text,
            input_tokens=sum(len(m["content"].split()) for m in messages),
            output_tokens=len(text.split()),
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )


class GeminiAdapter:
    """
    Google Gemini via the REST API, over stdlib urllib - no SDK dependency to drift.

    Untested against the live endpoint in this repo's test suite by design: the suite
    must run with no network and no key.
    """

    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, timeout_s: float = 30.0):
        self.timeout_s = timeout_s

    def generate(self, system_prompt: str, messages: List[Dict[str, str]],
                 credentials: Credentials) -> ModelResponse:
        if not credentials.api_key:
            raise ModelCallError("No API key supplied for provider 'gemini'.")

        payload: Dict = {
            "contents": [
                {"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]}
                for m in messages
            ]
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        url = self.ENDPOINT.format(model=credentials.model)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                # Header, not a query parameter: a key in a URL lands in access logs,
                # proxy logs and browser history.
                "x-goog-api-key": credentials.api_key,
            },
            method="POST",
        )

        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # The key can appear in a echoed request URL, so never surface the raw body.
            raise ModelCallError(f"Gemini returned HTTP {e.code}") from None
        except Exception as e:
            raise ModelCallError(f"Gemini call failed: {type(e).__name__}") from None

        latency_ms = (time.perf_counter() - started) * 1000.0
        candidates = body.get("candidates") or []
        if not candidates:
            # A refusal by the provider's own safety filter, not a transport failure.
            return ModelResponse(text="", latency_ms=latency_ms,
                                 finish_reason="SAFETY", provider_refused=True)

        candidate = candidates[0]
        finish = candidate.get("finishReason", "STOP")
        parts = candidate.get("content", {}).get("parts", [])
        usage = body.get("usageMetadata", {})
        return ModelResponse(
            text="".join(p.get("text", "") for p in parts),
            input_tokens=usage.get("promptTokenCount", 0),
            output_tokens=usage.get("candidatesTokenCount", 0),
            latency_ms=latency_ms,
            finish_reason=finish,
            provider_refused=(finish == "SAFETY"),
        )


def get_adapter(credentials: Credentials) -> ModelAdapter:
    if credentials.provider == "gemini":
        return GeminiAdapter()
    if credentials.provider == "mock":
        return MockAdapter()
    raise ModelCallError(f"Unknown provider '{credentials.provider}'.")
