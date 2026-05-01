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
import os
import tempfile
import wave
from typing import Optional

from cynea.models import AudioChunk, Transcription


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

    def _load_model(self):
        if self._model is None:
            import whisper  # imported lazily so the package is optional
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

        except Exception as exc:  # never crash the call loop on STT failure
            print(f"[WhisperTranscriber] transcription error: {exc}")
            return None
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


# Register with Cynea provider system at import time.
try:
    from cynea.providers import register_stt
    register_stt("whisper", WhisperTranscriber)
except ImportError:
    pass
