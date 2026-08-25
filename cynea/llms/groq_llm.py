"""Groq LLM adapter — the model that actually answers the phone.

Groq serves an OpenAI-shaped chat-completions API, so the request body here
is the standard `{model, messages, ...}` shape. We talk to it over plain
httpx rather than the `groq` SDK to keep the dependency surface small and
the streaming path explicit.

Configuration (environment)
---------------------------
    GROQ_API_KEY     required
    GROQ_MODEL       optional, defaults to openai/gpt-oss-20b (see _DEFAULT_MODEL
                     for why this is not llama-3.3-70b-versatile)
    GROQ_BASE_URL    optional, defaults to https://api.groq.com/openai/v1

Failure policy
--------------
This adapter **raises**. It never returns an empty string to paper over a
failed call, because the engine above it can no longer tell the difference
between "the model said nothing" and "the model was unreachable" once the
exception is swallowed. See cynea/engine.py for how those errors surface.

A note on the "anthropic" alias
-------------------------------
`AgentConfig.llm_provider` has historically defaulted to "anthropic" while no
Anthropic adapter existed, so every default-configured engine raised
ValueError before reaching a model. Registering this adapter under both
"groq" and "anthropic" repairs that without breaking stored configs, but the
alias is a compatibility shim, not a claim: resolving it logs a warning
saying which model actually ran. The default in models.py is now "groq" so
new configs name the provider they really use.
"""

from __future__ import annotations

import json
import logging
import os
from typing import AsyncIterator, List, Optional

log = logging.getLogger("cynea.llm.groq")

# The brief asked for llama-3.3-70b-versatile. Probing the account's
# /v1/models on 2026-08-25 returned 13 models and *no Llama family at all*,
# so that name 404s here. gpt-oss-20b is the fastest of the available chat
# models that returns clean, in-persona prose (~450 ms first byte).
#
# It is a *reasoning* model: it spends tokens in a `reasoning` field before
# writing `content`. That is why max_tokens must stay generous -- at 40 it
# burns the whole budget thinking and returns an empty string, which on a
# phone line is an agent that says nothing. _extract() below turns that
# into a loud error instead of silence.
_DEFAULT_MODEL = "openai/gpt-oss-20b"
_DEFAULT_BASE = "https://api.groq.com/openai/v1"
_TIMEOUT_S = 30.0


class GroqLLMError(RuntimeError):
    """Raised when Groq cannot be reached or returns an unusable response."""


class GroqLLM:
    """Chat-completions adapter for Groq.

    Usage:
        llm = GroqLLM()
        text = await llm.generate(messages, system="You are Kwame.")

        async for token in llm.stream(messages, system="..."):
            ...
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 512,
        *,
        _alias: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("GROQ_API_KEY")
        self.model = model or os.getenv("GROQ_MODEL", _DEFAULT_MODEL)
        self.base_url = (base_url or os.getenv("GROQ_BASE_URL", _DEFAULT_BASE)).rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens

        if _alias:
            log.warning(
                "llm_provider=%r resolved to the Groq adapter (model=%s). "
                "No Anthropic adapter is installed; set llm_provider='groq' "
                "to name the provider you are actually using.",
                _alias, self.model,
            )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require_key(self) -> str:
        if not self.api_key:
            raise GroqLLMError(
                "GROQ_API_KEY is not set. Add it to your .env "
                "(get one free at https://console.groq.com/keys), or select a "
                "different llm_provider on the agent config."
            )
        return self.api_key

    def _payload(self, messages: List[dict], system: str, stream: bool) -> dict:
        body = list(messages)
        # The OpenAI shape carries the system prompt as the first message.
        # Only prepend when the caller has not already supplied one.
        if system and not (body and body[0].get("role") == "system"):
            body = [{"role": "system", "content": system}] + body
        return {
            "model": self.model,
            "messages": body,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._require_key()}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _wrap(exc: Exception) -> GroqLLMError:
        name = type(exc).__name__
        if "Timeout" in name:
            return GroqLLMError(f"Groq timed out after {_TIMEOUT_S}s: {exc}")
        if "Connect" in name or "Network" in name:
            return GroqLLMError(f"Groq unreachable (network): {exc}")
        return GroqLLMError(f"Groq request failed: {exc}")

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def generate(self, messages: List[dict], system: str = "") -> str:
        """Return the assistant's full reply. Raises GroqLLMError on failure."""
        import httpx

        payload = self._payload(messages, system, stream=False)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                r = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
        except GroqLLMError:
            raise
        except Exception as exc:
            raise self._wrap(exc) from exc

        if r.status_code == 401:
            raise GroqLLMError("Groq rejected the API key (401). Check GROQ_API_KEY.")
        if r.status_code == 429:
            raise GroqLLMError("Groq rate limit hit (429). Back off and retry.")
        if r.status_code == 404 and "model" in r.text.lower():
            raise GroqLLMError(
                f"Groq has no model named {self.model!r} on this key.\n"
                f"  List what is available:  GET {self.base_url}/models\n"
                f"  Then set GROQ_MODEL to one of them.\n"
                f"  Server said: {r.text[:200]}"
            )
        if r.status_code >= 400:
            raise GroqLLMError(f"Groq returned {r.status_code}: {r.text[:300]}")

        try:
            data = r.json()
        except Exception as exc:
            raise GroqLLMError(f"Groq returned an unreadable body: {r.text[:300]}") from exc
        return self._extract(data)

    def _extract(self, data: dict) -> str:
        """Pull the reply text, refusing to return a silent empty string.

        Reasoning models put their scratchpad in `message.reasoning` and the
        answer in `message.content`. If max_tokens runs out mid-scratchpad,
        `content` comes back empty and the agent says nothing at all. On a
        phone call that is indistinguishable from a dropped line, so it is
        an error here, not an empty reply.
        """
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError) as exc:
            raise GroqLLMError(f"Unexpected Groq response shape: {str(data)[:300]}") from exc

        text = (msg.get("content") or "").strip()
        if text:
            return text

        finish = choice.get("finish_reason")
        if msg.get("reasoning") and finish == "length":
            raise GroqLLMError(
                f"{self.model} used its entire {self.max_tokens}-token budget on "
                f"reasoning and produced no reply. Raise max_tokens (or set "
                f"GROQ_MODEL to a non-reasoning model)."
            )
        raise GroqLLMError(
            f"{self.model} returned an empty reply (finish_reason={finish!r})."
        )

    async def stream(self, messages: List[dict], system: str = "") -> AsyncIterator[str]:
        """Yield reply tokens as they arrive.

        This is what makes the ~600 ms first-audio target reachable: the
        caller can start synthesising on the first clause instead of waiting
        for the full completion.
        """
        import httpx

        payload = self._payload(messages, system, stream=True)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                ) as r:
                    if r.status_code >= 400:
                        body = (await r.aread()).decode("utf-8", "replace")
                        raise GroqLLMError(f"Groq returned {r.status_code}: {body[:300]}")

                    async for line in r.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            return
                        try:
                            delta = json.loads(chunk)["choices"][0].get("delta", {})
                        except (ValueError, KeyError, IndexError):
                            continue  # keep-alive or malformed frame; skip it
                        token = delta.get("content")
                        if token:
                            yield token
        except GroqLLMError:
            raise
        except Exception as exc:
            raise self._wrap(exc) from exc


class _GroqAsAnthropic(GroqLLM):
    """Compatibility shim so llm_provider='anthropic' resolves and warns."""

    def __init__(self, *a, **kw):
        kw.setdefault("_alias", "anthropic")
        super().__init__(*a, **kw)


def register() -> None:
    """Register this adapter under 'groq' and the legacy 'anthropic' name."""
    from cynea.providers import register_llm

    register_llm("groq", GroqLLM)
    register_llm("anthropic", _GroqAsAnthropic)
