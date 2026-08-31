"""Cynea Africa — Groq speech-to-text.

Hosted Whisper through Groq's OpenAI-shaped transcription endpoint. This
is the STT that actually runs today: local `openai-whisper` pulls ~2 GB of
torch and needs ffmpeg, which is a heavy default for a first deploy.

    POST https://api.groq.com/openai/v1/audio/transcriptions
    model: whisper-large-v3

Registered as **"groq-stt"**.

On "streaming"
--------------
Groq's transcription endpoint is **not** a streaming API. It takes one
complete audio file and returns one complete transcript; there is no
partial-result socket to subscribe to.

`stream_transcribe()` therefore does the honest thing available: it
re-transcribes a growing buffer at a fixed cadence and yields each result
as an interim `Transcription` (`is_final=False`), then one final result
when the caller stops. The text improves as more audio arrives, which is
what an interim transcript is *for* — but note the cost model, because it
is easy to get wrong:

    every window re-sends ALL audio so far

so an utterance transcribed at 5 windows costs roughly 5x a single pass,
not 1x. `interim_interval_s` trades latency against spend. At the default
of 1.5s a ten-second utterance costs about 6 passes. For real-time barge-in
this is worth it; for batch transcription of recordings, call
`transcribe()` once instead.

If sub-second interim latency matters more than cost, the right answer is
a provider with a genuine streaming socket (Deepgram, AssemblyAI), not a
tighter loop here.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import wave
from typing import AsyncIterator, Optional

from cynea.models import AudioChunk, Transcription

log = logging.getLogger("cynea.stt.groq")

_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
_DEFAULT_MODEL = "whisper-large-v3"
_TIMEOUT_S = 30.0

# Whisper is trained on 16 kHz audio. Carriers send 8 kHz; upsampling
# before the request measurably improves accuracy on African-accented
# English, where the discriminating detail sits in the higher formants.
_TARGET_RATE = 16000


class GroqSTTError(RuntimeError):
    """Raised when Groq cannot be reached or returns an unusable response."""


class GroqSTT:
    """Speech-to-text provider backed by Groq's hosted Whisper.

        stt = GroqSTT()
        result = await stt.transcribe(chunk)          # one complete utterance

        async for partial in stt.stream_transcribe(chunks):
            print(partial.text, partial.is_final)

    Environment:
        GROQ_API_KEY    required
        GROQ_STT_MODEL  optional, defaults to whisper-large-v3
    """

    def __init__(
        self,
        model: Optional[str] = None,
        language: Optional[str] = "en",
        api_key: Optional[str] = None,
        interim_interval_s: float = 1.5,
        prompt: Optional[str] = None,
    ):
        self.model = model or os.getenv("GROQ_STT_MODEL", _DEFAULT_MODEL)
        self.language = language
        self.api_key = api_key or os.getenv("GROQ_API_KEY")
        self.interim_interval_s = interim_interval_s
        # Whisper accepts a prompt to bias decoding toward expected
        # vocabulary. Passed straight through when set.
        #
        # Measured, not assumed: on assets/kwame_test_1.mp3 a prompt of
        # "Kwame, Adinkra Hotel, Accra, Ghana" did NOT stop "Kwame" being
        # transcribed as "QAM". Treat this as a knob worth trying per
        # deployment, not a fix for proper nouns. If agent names must be
        # transcribed exactly, post-process against a known-names list.
        self.prompt = prompt

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self.api_key)

    async def warmup(self) -> None:
        """Nothing to load; present so providers are interchangeable."""
        return None

    def _require_key(self) -> str:
        if not self.api_key:
            raise GroqSTTError(
                "GROQ_API_KEY is not set, so hosted transcription is "
                "unavailable.\n"
                "  Get a free key at https://console.groq.com/keys and add "
                "it to .env,\n"
                "  or install local Whisper: pip install openai-whisper"
            )
        return self.api_key

    # ------------------------------------------------------------------
    # Audio framing
    # ------------------------------------------------------------------

    @staticmethod
    def _to_pcm16(audio: AudioChunk) -> bytes:
        """Decode μ-law / A-law to linear PCM16. Passes PCM16 through."""
        encoding = (audio.encoding or "pcm16").lower()
        if encoding == "pcm16":
            return audio.data
        try:
            import audioop
        except ImportError:  # Python 3.13 removed audioop from the stdlib
            try:
                import audioop_lts as audioop  # type: ignore
            except ImportError:
                raise GroqSTTError(
                    f"Audio is {encoding!r} and this Python has no audioop "
                    f"module to decode it. Install audioop-lts, or have the "
                    f"telephony layer send pcm16."
                )
        if encoding == "mulaw":
            return audioop.ulaw2lin(audio.data, 2)
        if encoding == "alaw":
            return audioop.alaw2lin(audio.data, 2)
        raise GroqSTTError(f"Unsupported encoding {encoding!r}")

    @classmethod
    def _wav_bytes(cls, audio: AudioChunk) -> bytes:
        """Wrap a chunk as a 16 kHz mono WAV in memory."""
        pcm = cls._to_pcm16(audio)
        rate = audio.sample_rate or _TARGET_RATE

        if rate != _TARGET_RATE:
            try:
                import audioop
            except ImportError:
                try:
                    import audioop_lts as audioop  # type: ignore
                except ImportError:
                    audioop = None
            if audioop is not None:
                pcm, _ = audioop.ratecv(pcm, 2, audio.channels or 1, rate,
                                        _TARGET_RATE, None)
                rate = _TARGET_RATE
            else:
                # Send at the original rate rather than fail: Whisper
                # resamples server-side, just less well than we would.
                log.debug("no audioop; sending %d Hz audio unresampled", rate)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(audio.channels or 1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(pcm)
        return buf.getvalue()

    @staticmethod
    def _duration_s(audio: AudioChunk) -> float:
        frames = len(audio.data) / 2.0 if (audio.encoding or "pcm16") == "pcm16" \
                 else float(len(audio.data))
        return frames / float(audio.sample_rate or _TARGET_RATE) / (audio.channels or 1)

    # ------------------------------------------------------------------
    # One-shot transcription
    # ------------------------------------------------------------------

    async def transcribe(self, audio: AudioChunk) -> Optional[Transcription]:
        """Transcribe a complete utterance. None when there was no speech."""
        if not audio or not audio.data:
            return None

        # Whisper rejects anything under ~0.1s and returns hallucinated
        # filler for near-silence. Cheaper to skip than to send.
        if self._duration_s(audio) < 0.15:
            return None

        return await self._post(self._wav_bytes(audio), is_final=True)

    async def _post(self, wav: bytes, is_final: bool) -> Optional[Transcription]:
        import httpx

        data = {"model": self.model, "response_format": "verbose_json"}
        if self.language:
            data["language"] = self.language
        if self.prompt:
            data["prompt"] = self.prompt

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                r = await client.post(
                    _ENDPOINT,
                    headers={"Authorization": f"Bearer {self._require_key()}"},
                    files={"file": ("utterance.wav", io.BytesIO(wav), "audio/wav")},
                    data=data,
                )
        except GroqSTTError:
            raise
        except Exception as exc:
            log.error("Groq STT unreachable: %s", exc)
            raise GroqSTTError(f"Groq STT unreachable: {exc}") from exc

        if r.status_code == 401:
            raise GroqSTTError("Groq rejected the API key (401). Check GROQ_API_KEY.")
        if r.status_code == 429:
            raise GroqSTTError("Groq rate limit hit (429) on transcription.")
        if r.status_code == 404:
            raise GroqSTTError(
                f"Groq has no STT model named {self.model!r}. "
                f"List available models: GET https://api.groq.com/openai/v1/models"
            )
        if r.status_code >= 400:
            raise GroqSTTError(f"Groq STT returned {r.status_code}: {r.text[:300]}")

        try:
            payload = r.json()
        except Exception as exc:
            raise GroqSTTError(f"Groq STT returned an unreadable body: {r.text[:200]}") from exc

        text = (payload.get("text") or "").strip()
        if not text:
            return None

        return Transcription(
            text=text,
            confidence=self._confidence(payload),
            is_final=is_final,
            language=payload.get("language") or self.language,
        )

    @staticmethod
    def _confidence(payload: dict) -> float:
        """Derive 0..1 confidence from per-segment average log-probability.

        verbose_json gives avg_logprob per segment; exp() maps it back to a
        probability. Falls back to 0.9 when segments are absent, which is
        a claim about the response shape, not about the audio.
        """
        segments = payload.get("segments") or []
        if not segments:
            return 0.9
        import math
        logprobs = [s.get("avg_logprob", -1.0) for s in segments]
        return max(0.0, min(1.0, math.exp(sum(logprobs) / len(logprobs))))

    # ------------------------------------------------------------------
    # Interim results
    # ------------------------------------------------------------------

    async def stream_transcribe(
        self,
        chunks: AsyncIterator[AudioChunk],
        *,
        sample_rate: int = _TARGET_RATE,
    ) -> AsyncIterator[Transcription]:
        """Yield interim transcripts as audio arrives, then a final one.

        Each interim re-transcribes everything heard so far -- see the
        module docstring on what that costs. Interim results carry
        `is_final=False`; exactly one `is_final=True` is yielded at the
        end, unless no speech was detected at all.
        """
        buffer = bytearray()
        last_emit = 0.0
        last_text = ""
        loop = asyncio.get_event_loop()
        started = loop.time()

        async for chunk in chunks:
            if not chunk or not chunk.data:
                continue
            buffer.extend(self._to_pcm16(chunk))

            elapsed = loop.time() - started
            buffered_s = len(buffer) / 2.0 / sample_rate

            if elapsed - last_emit < self.interim_interval_s or buffered_s < 0.4:
                continue
            last_emit = elapsed

            try:
                partial = await self._post(
                    self._wav_bytes(AudioChunk(data=bytes(buffer),
                                               sample_rate=sample_rate)),
                    is_final=False,
                )
            except GroqSTTError as exc:
                # An interim failing is not fatal: the final pass may still
                # succeed, and dropping the call over a partial would be
                # worse than a slightly late transcript.
                log.warning("interim transcription failed, continuing: %s", exc)
                continue

            if partial and partial.text != last_text:
                last_text = partial.text
                yield partial

        if not buffer:
            return

        final = await self._post(
            self._wav_bytes(AudioChunk(data=bytes(buffer), sample_rate=sample_rate)),
            is_final=True,
        )
        if final:
            yield final


# Register at import time. cynea.providers imports this module, which is
# what actually puts it in the registry.
try:
    from cynea.providers import register_stt
    register_stt("groq-stt", GroqSTT)
except ImportError:  # pragma: no cover
    pass


__all__ = ["GroqSTT", "GroqSTTError"]
