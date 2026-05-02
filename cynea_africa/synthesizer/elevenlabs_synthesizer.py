"""Cynea Africa — ElevenLabs Synthesizer.

Premium TTS via the ElevenLabs API. Higher quality than Edge TTS, but
not free — every synthesised character counts against your monthly
ElevenLabs character quota.

Why we ship both Edge TTS and ElevenLabs
----------------------------------------
Edge TTS is the default Cynea stack — free, decent quality, fits the
"zero infrastructure cost" promise. ElevenLabs is the upgrade path
when a customer wants premium voices and is willing to pay for them.
Both providers expose the same interface (`synthesize`, `health_check`)
so swapping is one config change in the agent JSON.

Default voice IDs used by the Cynea-shipped personas:
    Kwame   "JBFqnCBsd6RMkjVDRZzb"   George — British male, warm
    Amina   "EXAVITQu4vr4xnSDxMaL"   Bella  — American female, warm

Configuration
-------------
Set in your `.env` file:
    ELEVENLABS_API_KEY=sk_...
    ELEVENLABS_MODEL=eleven_turbo_v2_5    # optional; turbo is the default

`python-dotenv` is loaded automatically at synthesizer construction.

Error model
-----------
Mirrors EdgeTTSSynthesizer:
    ImportError      `elevenlabs` package missing.
    ConnectionError  No network, DNS failure, or api.elevenlabs.io unreachable.
    RuntimeError     Auth, quota, invalid voice, or any other server-side error.
"""

from __future__ import annotations

import asyncio
import os
import socket
from typing import Optional

from cynea.models import SynthesisRequest


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

_ELEVENLABS_HOST = "api.elevenlabs.io"
_ELEVENLABS_PORT = 443

# `eleven_turbo_v2_5` is ElevenLabs' fast model: ~250-300 ms first-byte
# latency, decent quality, ~50% the cost of multilingual_v2. Override via
# the ELEVENLABS_MODEL env var.
_DEFAULT_MODEL = "eleven_turbo_v2_5"
_DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"

# Substrings that suggest a network problem inside an arbitrary
# Exception subclass. Same heuristic the Edge synthesizer uses.
_NETWORK_HINTS = (
    "getaddrinfo", "name or service", "name resolution", "no address",
    "ssl", "tls", "handshake", "timed out", "timeout", "connection",
    "unreachable", "no route",
    "winerror 10061", "winerror 11001", "winerror 11002", "winerror 11003",
)


# Friendly-name mapping. Not used in lookup — the synthesizer accepts
# voice IDs verbatim. Kept here so callers can `print(VOICES[id])`.
VOICES = {
    "JBFqnCBsd6RMkjVDRZzb": "George — British male, warm (Cynea: Kwame)",
    "EXAVITQu4vr4xnSDxMaL": "Bella  — American female, warm (Cynea: Amina)",
}


class ElevenLabsSynthesizer:
    """ElevenLabs TTS provider — same surface as EdgeTTSSynthesizer."""

    # Re-export at the class level for callers that introspect.
    VOICES = VOICES

    def __init__(
        self,
        voice: str = "JBFqnCBsd6RMkjVDRZzb",
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self._load_dotenv()
        self.voice = voice
        self.model = model or os.getenv("ELEVENLABS_MODEL", _DEFAULT_MODEL)
        self.api_key = api_key or os.getenv("ELEVENLABS_API_KEY")
        self._client = None  # lazy

    # ------------------------------------------------------------------
    # Public surface — mirrors EdgeTTSSynthesizer
    # ------------------------------------------------------------------

    async def synthesize(self, request: SynthesisRequest) -> bytes:
        """Synthesise `request.text` to MP3 bytes.

        Raises:
            ImportError:     elevenlabs SDK not installed.
            ConnectionError: no network / endpoint unreachable.
            RuntimeError:    auth, quota, invalid voice, or other failure.
        """
        text = (request.text or "").strip()
        if not text:
            return b""

        client = self._get_client()  # may raise ImportError, RuntimeError
        voice_id = request.voice or self.voice
        model_id = self.model
        output_format = _DEFAULT_OUTPUT_FORMAT

        def _sync_call() -> bytes:
            stream = client.text_to_speech.convert(
                voice_id=voice_id,
                text=text,
                model_id=model_id,
                output_format=output_format,
            )
            # The SDK returns Iterator[bytes]; collect into a single
            # bytestring. For streaming use cases this is the wrong
            # shape — the Cynea engine doesn't stream TTS today, so
            # collecting is the right call.
            return b"".join(stream)

        try:
            audio = await asyncio.to_thread(_sync_call)
        except (ConnectionError, asyncio.TimeoutError, socket.gaierror, OSError) as exc:
            raise ConnectionError(
                f"No internet connection or ElevenLabs unreachable: {exc}"
            ) from exc
        except Exception as exc:
            raise self._classify_exception(exc) from exc

        if not audio:
            raise RuntimeError(
                "ElevenLabs returned empty audio (possibly an unsupported "
                "voice ID or zero-length text)."
            )
        return audio

    async def health_check(self, *, timeout: float = 2.0) -> dict:
        """Probe SDK + API key + endpoint reachability. Never raises.

        Returns a dict with:
            installed (bool):  elevenlabs SDK importable
            configured (bool): ELEVENLABS_API_KEY set
            reachable (bool):  TCP-443 reach to api.elevenlabs.io
            ready (bool):      all three above are true
            reason (str):      human-readable detail when not ready
        """
        result = {
            "installed": False,
            "configured": False,
            "reachable": False,
            "ready": False,
            "reason": "",
        }

        try:
            self._import_elevenlabs()
            result["installed"] = True
        except ImportError as exc:
            result["reason"] = str(exc)
            return result

        if not self.api_key:
            result["reason"] = (
                "ELEVENLABS_API_KEY is not set. Add it to your .env file: "
                "ELEVENLABS_API_KEY=sk_..."
            )
            return result
        result["configured"] = True

        try:
            fut = asyncio.open_connection(_ELEVENLABS_HOST, _ELEVENLABS_PORT)
            reader, writer = await asyncio.wait_for(fut, timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            result["reachable"] = True
        except asyncio.TimeoutError:
            result["reason"] = (
                f"Cannot reach {_ELEVENLABS_HOST}:{_ELEVENLABS_PORT} within "
                f"{timeout:.1f}s — check your network or firewall."
            )
            return result
        except socket.gaierror as exc:
            result["reason"] = (
                f"DNS failed for {_ELEVENLABS_HOST}: {exc}. "
                "Likely no internet connection."
            )
            return result
        except OSError as exc:
            result["reason"] = f"Cannot reach {_ELEVENLABS_HOST}: {exc}."
            return result

        result["ready"] = True
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _load_dotenv() -> None:
        """Best-effort .env load. Silent if python-dotenv isn't installed."""
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

    @staticmethod
    def _import_elevenlabs():
        """Import the SDK. Tries the modern path first, falls back to the
        top-level alias if needed. Raises ImportError with a clear message
        when neither is available."""
        try:
            from elevenlabs.client import ElevenLabs
            return ElevenLabs
        except ImportError:
            try:
                from elevenlabs import ElevenLabs as _Legacy  # type: ignore
                return _Legacy
            except ImportError as exc:
                raise ImportError(
                    "elevenlabs SDK is not installed. Install with: "
                    "pip install elevenlabs"
                ) from exc

    def _get_client(self):
        """Lazy-construct the ElevenLabs client; cache for reuse.

        Raises ImportError if the SDK is missing, RuntimeError if no API
        key is configured.
        """
        if self._client is not None:
            return self._client
        ElevenLabs = self._import_elevenlabs()
        if not self.api_key:
            raise RuntimeError(
                "ELEVENLABS_API_KEY is not set. Add it to your .env file: "
                "ELEVENLABS_API_KEY=sk_..."
            )
        # 30s timeout matches typical phone-call patience.
        self._client = ElevenLabs(api_key=self.api_key, timeout=30.0)
        return self._client

    @staticmethod
    def _classify_exception(exc: Exception) -> Exception:
        """Map an arbitrary SDK exception to one of the three documented
        exception classes (ImportError / ConnectionError / RuntimeError).
        """
        msg = str(exc).lower()
        cls_name = type(exc).__name__.lower()

        # Auth — ElevenLabs raises ApiError / UnauthorizedError / 401 in msg.
        if any(k in msg for k in ("401", "unauthorized", "invalid api key", "missing_permissions")):
            return RuntimeError(
                "ElevenLabs authentication failed. Check ELEVENLABS_API_KEY "
                f"in your .env file. (original: {exc})"
            )
        # Quota / rate limit
        if any(k in msg for k in ("429", "quota_exceeded", "rate limit", "too many requests")):
            return RuntimeError(
                f"ElevenLabs rate-limited or quota exceeded: {exc}"
            )
        # Voice not found
        if "voice" in msg and ("not found" in msg or "404" in msg):
            return RuntimeError(f"ElevenLabs voice not found: {exc}")
        # Network surfaced as plain Exception (httpx wraps some)
        if any(h in msg for h in _NETWORK_HINTS) or "connection" in cls_name or "timeout" in cls_name:
            return ConnectionError(f"ElevenLabs network error: {exc}")
        # Anything else is a server-side or client-side synthesis failure.
        return RuntimeError(f"ElevenLabs synthesis failed: {exc}")


# ----------------------------------------------------------------------
# Provider registration
# ----------------------------------------------------------------------

try:
    from cynea.providers import register_tts
    register_tts("elevenlabs", ElevenLabsSynthesizer)
except ImportError:
    pass
