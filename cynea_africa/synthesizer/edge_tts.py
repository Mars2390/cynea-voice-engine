"""Cynea Africa — Edge TTS Synthesizer.

Free text-to-speech using Microsoft Edge TTS. Natural voices, zero API
cost, requires only an internet connection.

Error model
-----------
Three distinct exception types so callers can react appropriately:

  ImportError      The `edge-tts` package is not installed.
                   Resolution: `pip install edge-tts`.
  ConnectionError  No internet, DNS failure, or Microsoft's TTS endpoint
                   is unreachable. Resolution: check the network.
  RuntimeError     The package is installed and the network is fine, but
                   synthesis itself failed (e.g. invalid voice, malformed
                   text, server returned an error). Resolution: inspect
                   `__cause__` for the underlying exception.

Use `health_check()` upfront to avoid raising mid-call.
"""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
from typing import Optional

from cynea.models import SynthesisRequest


# Microsoft's Edge TTS endpoint. Used for the network reachability probe;
# the actual synthesis is driven by the edge-tts library.
_EDGE_TTS_HOST = "speech.platform.bing.com"
_EDGE_TTS_PORT = 443


# Substrings in exception messages that suggest a network problem rather
# than a real synthesis problem. The edge-tts library wraps several
# transport errors that don't subclass ConnectionError, so we sniff them.
_NETWORK_HINTS = (
    "getaddrinfo",
    "name or service",
    "name resolution",
    "no address",
    "ssl",
    "tls",
    "handshake",
    "timed out",
    "timeout",
    "connection",
    "unreachable",
    "no route",
    "winerror 10061",
    "winerror 11001",
    "winerror 11002",
    "winerror 11003",
    "no audio",
)


class EdgeTTSSynthesizer:
    """Free TTS provider using Microsoft Edge TTS."""

    # Available natural voices (subset; see edge-tts list_voices for the full set)
    VOICES = {
        "en-GB-RyanNeural": "British male, warm",
        "en-GB-SoniaNeural": "British female, warm",
        "en-GB-LibbyNeural": "British female, soft",
        "en-US-EricNeural": "American male, professional",
        "en-US-AriaNeural": "American female, natural",
        "en-ZA-LeahNeural": "South African female, warm",
        "en-ZA-LukeNeural": "South African male, deep",
        "en-NG-EzinneNeural": "Nigerian female, natural",
        "en-NG-AbeoNeural": "Nigerian male, deep",
    }

    def __init__(self, voice: str = "en-GB-RyanNeural"):
        self.voice = voice

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def synthesize(self, request: SynthesisRequest) -> bytes:
        """Synthesize one chunk of text to MP3 bytes.

        Raises:
            ImportError:     edge-tts is not installed.
            ConnectionError: no network / endpoint unreachable.
            RuntimeError:    synthesis failed for some other reason.
        """
        edge_tts = self._import_edge_tts()  # raises ImportError

        voice = request.voice or self.voice
        rate = self._format_rate(request.speed)

        temp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                temp_path = f.name

            communicate = edge_tts.Communicate(
                text=request.text, voice=voice, rate=rate,
            )
            try:
                await communicate.save(temp_path)
            except (ConnectionError, asyncio.TimeoutError, socket.gaierror, OSError) as exc:
                raise ConnectionError(
                    f"No internet connection or Edge TTS endpoint unreachable: {exc}"
                ) from exc
            except Exception as exc:
                if self._looks_like_network_error(exc):
                    raise ConnectionError(f"No internet connection: {exc}") from exc
                raise RuntimeError(f"Edge TTS synthesis failed: {exc}") from exc

            with open(temp_path, "rb") as f:
                audio = f.read()

            if not audio:
                # edge-tts can write an empty file on partial failure.
                raise RuntimeError(
                    "Edge TTS returned empty audio (possibly an unsupported voice "
                    "or rate value)."
                )
            return audio

        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    async def health_check(self, *, timeout: float = 2.0) -> dict:
        """Check whether TTS is currently usable. Never raises.

        Returns a dict with:
            installed (bool):  edge-tts package importable
            reachable (bool):  TCP-443 reach to the TTS endpoint
            ready (bool):      both above are true
            reason (str):      human-readable detail when not ready
        """
        result = {
            "installed": False,
            "reachable": False,
            "ready": False,
            "reason": "",
        }
        try:
            self._import_edge_tts()
            result["installed"] = True
        except ImportError as exc:
            result["reason"] = str(exc)
            return result

        try:
            fut = asyncio.open_connection(_EDGE_TTS_HOST, _EDGE_TTS_PORT)
            reader, writer = await asyncio.wait_for(fut, timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            result["reachable"] = True
        except asyncio.TimeoutError:
            result["reason"] = (
                f"Cannot reach {_EDGE_TTS_HOST}:{_EDGE_TTS_PORT} within "
                f"{timeout:.1f}s — check your network or firewall."
            )
            return result
        except socket.gaierror as exc:
            result["reason"] = (
                f"DNS failed for {_EDGE_TTS_HOST}: {exc}. "
                "Likely no internet connection."
            )
            return result
        except OSError as exc:
            result["reason"] = (
                f"Cannot reach {_EDGE_TTS_HOST}: {exc}."
            )
            return result

        result["ready"] = True
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _import_edge_tts():
        """Lazy-import the edge-tts package; re-raise as a clearer ImportError."""
        try:
            import edge_tts  # noqa: F401
            return edge_tts
        except ImportError as exc:
            raise ImportError(
                "edge-tts is not installed. Install with: pip install edge-tts"
            ) from exc

    @staticmethod
    def _format_rate(speed: float) -> str:
        """Convert a speed multiplier (0.5–2.0) to Edge's '+N%' / '-N%' form.

        speed=1.0 -> '+0%';  speed=0.95 -> '-5%';  speed=1.2 -> '+20%'.
        Out-of-range values are clamped to a safe band.
        """
        try:
            s = float(speed)
        except (TypeError, ValueError):
            s = 1.0
        s = max(0.5, min(2.0, s))
        delta = int(round((s - 1.0) * 100))
        sign = "+" if delta >= 0 else ""  # negatives already carry '-'
        return f"{sign}{delta}%"

    @staticmethod
    def _looks_like_network_error(exc: BaseException) -> bool:
        msg = str(exc).lower()
        return any(hint in msg for hint in _NETWORK_HINTS)


# Register with Cynea provider system at import time.
try:
    from cynea.providers import register_tts
    register_tts("edge_tts", EdgeTTSSynthesizer)
except ImportError:
    pass
