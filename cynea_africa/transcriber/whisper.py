"""Cynea Africa — Whisper Transcriber.

Free local speech-to-text using OpenAI Whisper. The "base" model fits in
~1.5 GB of RAM and runs on CPU, which is the target for our 8 GB deploys.

Limitations to be aware of:
- This is a non-streaming, file-based transcriber. For live phone audio you
  must accumulate frames in a VAD-gated buffer upstream and only call this
  once an utterance is endpointed. See cynea/interruption.py for the
  endpointing signals.
- Africa's Talking and most SIP carriers send 8 kHz μ-law. We accept either
  16-bit PCM or μ-law via AudioChunk.encoding; μ-law is decoded with
  audioop before being written to a WAV file Whisper can read.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import wave
from typing import Optional

from cynea.models import AudioChunk, Transcription

log = logging.getLogger("cynea.stt")

_INSTALL_HINT = (
    "openai-whisper is not installed, so local transcription is unavailable.\n"
    "  Fix (either one):\n"
    "    1. pip install openai-whisper      # local, free, ~1.5 GB RAM for 'base'\n"
    "    2. set GROQ_API_KEY and use stt_provider='groq_whisper'  # hosted\n"
    "  Note that openai-whisper also needs ffmpeg on PATH."
)


class WhisperUnavailable(RuntimeError):
    """Raised when the whisper package cannot be imported."""


class WhisperTranscriber:
    """Local STT provider using OpenAI Whisper.

    Args:
        model_size: One of {"tiny", "base", "small", "medium", "large"}.
            "base" is the recommended default on 8 GB hardware.
        language: ISO code passed to Whisper. None lets Whisper auto-detect,
            which is slower but useful for code-switching demos.
    """

    def __init__(self, model_size: str = "base", language: Optional[str] = "en"):
        self.model_size = model_size
        self.language = language
        self._model = None

    @staticmethod
    def is_available() -> bool:
        """True when the whisper package can actually be imported."""
        try:
            import whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def _load_model(self):
        if self._model is None:
            try:
                import whisper  # imported lazily so the package is optional
            except ImportError as exc:
                raise WhisperUnavailable(_INSTALL_HINT) from exc
            self._model = whisper.load_model(self.model_size)
        return self._model

    async def warmup(self) -> None:
        """Pre-load the model so the first real call doesn't pay the import +
        load latency. Call this at agent boot."""
        await asyncio.to_thread(self._load_model)

    async def transcribe(self, audio: AudioChunk) -> Optional[Transcription]:
        """Transcribe a complete utterance to text. Returns None on empty/error."""
        if not audio or not audio.data:
            return None

        temp_path: Optional[str] = None
        try:
            model = await asyncio.to_thread(self._load_model)
            pcm16 = self._to_pcm16(audio)

            # Whisper reads from disk; write a temp WAV.
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                temp_path = f.name
            with wave.open(temp_path, "wb") as wf:
                wf.setnchannels(audio.channels or 1)
                wf.setsampwidth(2)
                wf.setframerate(audio.sample_rate or 16000)
                wf.writeframes(pcm16)

            kwargs = {"fp16": False}
            if self.language:
                kwargs["language"] = self.language
            result = await asyncio.to_thread(model.transcribe, temp_path, **kwargs)

            text = (result.get("text") or "").strip()
            if not text:
                return None

            return Transcription(
                text=text,
                confidence=self._estimate_confidence(result),
                is_final=True,
                language=result.get("language") or self.language,
            )

        except WhisperUnavailable:
            raise
        except Exception as exc:
            # Do NOT swallow this. A caller who hears nothing because STT
            # failed is indistinguishable, from the outside, from a caller
            # who said nothing -- and only one of those is a bug worth
            # paging someone about. The engine decides what to do.
            log.error("Whisper transcription failed: %s", exc, exc_info=True)
            raise
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _to_pcm16(audio: AudioChunk) -> bytes:
        """Normalize an AudioChunk's payload to 16-bit linear PCM for Whisper.

        Python 3.13 removed the stdlib audioop module, so we ship a pure-Python
        G.711 fallback for μ-law and A-law (the two codecs we actually see on
        Twilio + Africa's Talking + most SIP trunks).
        """
        encoding = (getattr(audio, "encoding", None) or "pcm16").lower()
        if encoding == "mulaw":
            return _mulaw_to_pcm16(audio.data)
        if encoding == "alaw":
            return _alaw_to_pcm16(audio.data)
        return audio.data


# ----------------------------------------------------------------------
# G.711 decoders (ITU-T reference tables, pure Python, no deps)
# Built once at import; decode is a constant-time lookup per byte.
# ----------------------------------------------------------------------

def _build_mulaw_table() -> bytes:
    out = bytearray(512)
    for i in range(256):
        u = ~i & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        if sign:
            sample = -sample
        sample = max(-32768, min(32767, sample))
        out[2 * i] = sample & 0xFF
        out[2 * i + 1] = (sample >> 8) & 0xFF
    return bytes(out)


def _build_alaw_table() -> bytes:
    out = bytearray(512)
    for i in range(256):
        a = i ^ 0x55
        sign = a & 0x80
        exponent = (a >> 4) & 0x07
        mantissa = a & 0x0F
        if exponent == 0:
            sample = (mantissa << 4) + 8
        else:
            sample = ((mantissa << 4) + 0x108) << (exponent - 1)
        if sign:
            sample = -sample
        sample = max(-32768, min(32767, sample))
        out[2 * i] = sample & 0xFF
        out[2 * i + 1] = (sample >> 8) & 0xFF
    return bytes(out)


_MULAW_TABLE = _build_mulaw_table()
_ALAW_TABLE = _build_alaw_table()


def _mulaw_to_pcm16(data: bytes) -> bytes:
    out = bytearray(len(data) * 2)
    for i, b in enumerate(data):
        out[2 * i] = _MULAW_TABLE[2 * b]
        out[2 * i + 1] = _MULAW_TABLE[2 * b + 1]
    return bytes(out)


def _alaw_to_pcm16(data: bytes) -> bytes:
    out = bytearray(len(data) * 2)
    for i, b in enumerate(data):
        out[2 * i] = _ALAW_TABLE[2 * b]
        out[2 * i + 1] = _ALAW_TABLE[2 * b + 1]
    return bytes(out)

    @staticmethod
    def _estimate_confidence(result: dict) -> float:
        """Whisper doesn't return a confidence score directly. We estimate one
        from segment avg_logprob: clamp(exp(avg_logprob), 0, 1)."""
        segments = result.get("segments") or []
        if not segments:
            return 0.0
        import math
        logprobs = [s.get("avg_logprob", -10.0) for s in segments]
        avg = sum(logprobs) / len(logprobs)
        return max(0.0, min(1.0, math.exp(avg)))


class GroqWhisperTranscriber:
    """Hosted Whisper via Groq -- the fallback when the local model is absent.

    Same `transcribe()` contract as WhisperTranscriber, so the two are
    interchangeable through the provider registry. Useful when you do not
    want a 1.5 GB model (and ffmpeg) inside the container, or on hosts with
    no spare RAM.

    Environment:
        GROQ_API_KEY   required
        GROQ_STT_MODEL optional, defaults to whisper-large-v3-turbo
    """

    _ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
    _DEFAULT_MODEL = "whisper-large-v3-turbo"

    def __init__(self, model: Optional[str] = None, language: Optional[str] = "en",
                 api_key: Optional[str] = None):
        self.model = model or os.getenv("GROQ_STT_MODEL", self._DEFAULT_MODEL)
        self.language = language
        self.api_key = api_key or os.getenv("GROQ_API_KEY")

    def is_available(self) -> bool:
        return bool(self.api_key)

    async def warmup(self) -> None:
        """No model to load -- present so the two transcribers match."""
        return None

    async def transcribe(self, audio: AudioChunk) -> Optional[Transcription]:
        if not audio or not audio.data:
            return None
        if not self.api_key:
            raise WhisperUnavailable(
                "GROQ_API_KEY is not set, so the hosted Whisper fallback is "
                "unavailable either.\n" + _INSTALL_HINT
            )

        import httpx

        # Groq wants a real audio file; reuse the local WAV framing.
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(audio.channels or 1)
            wf.setsampwidth(2)
            wf.setframerate(audio.sample_rate or 16000)
            wf.writeframes(WhisperTranscriber._to_pcm16(self, audio))
        buf.seek(0)

        data = {"model": self.model, "response_format": "verbose_json"}
        if self.language:
            data["language"] = self.language

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    self._ENDPOINT,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files={"file": ("utterance.wav", buf, "audio/wav")},
                    data=data,
                )
        except Exception as exc:
            log.error("Groq Whisper unreachable: %s", exc)
            raise RuntimeError(f"Groq Whisper unreachable: {exc}") from exc

        if r.status_code >= 400:
            log.error("Groq Whisper returned %s: %s", r.status_code, r.text[:300])
            raise RuntimeError(f"Groq Whisper returned {r.status_code}: {r.text[:300]}")

        payload = r.json()
        text = (payload.get("text") or "").strip()
        if not text:
            return None
        return Transcription(
            text=text,
            confidence=0.9,   # Groq does not return per-segment logprobs here
            is_final=True,
            language=payload.get("language") or self.language,
        )


def best_available_stt(**kwargs):
    """Return local Whisper when importable, else the hosted Groq fallback.

    Lets a deploy stay working whether or not the 1.5 GB model shipped in
    the image, without the caller having to branch on it.
    """
    if WhisperTranscriber.is_available():
        return WhisperTranscriber(**kwargs)
    hosted = GroqWhisperTranscriber(language=kwargs.get("language", "en"))
    if hosted.is_available():
        log.warning("openai-whisper not installed; using hosted Groq Whisper.")
        return hosted
    raise WhisperUnavailable(_INSTALL_HINT)


# Register with Cynea provider system at import time.
try:
    from cynea.providers import register_stt
    register_stt("whisper", WhisperTranscriber)
    register_stt("groq_whisper", GroqWhisperTranscriber)
except ImportError:
    pass
