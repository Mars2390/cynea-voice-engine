"""Tests for cynea.engine — turn results and the no-silent-failure policy.

The engine used to catch every provider exception, print it, and return
None. That made three different situations indistinguishable to the
caller: nothing was said, the turn was cancelled by barge-in, and the
model was unreachable. Only the last is an incident. These tests pin that
distinction, because losing it again means a phone line that goes dead
without paging anyone.
"""

import asyncio

import pytest

from cynea import providers
from cynea.engine import CyneaEngine, LLMError, STTError, TTSError, TurnResult
from cynea.models import AgentConfig, AudioChunk, Transcription


# ----------------------------------------------------------------------
# Doubles
# ----------------------------------------------------------------------

class OkSTT:
    text = "do you have a double room on the fourteenth"

    async def transcribe(self, audio):
        return Transcription(text=self.text, confidence=0.95,
                             is_final=True, language="en")


class SilentSTT:
    """Caller said nothing intelligible — normal, not an error."""
    async def transcribe(self, audio):
        return None


class BrokenSTT:
    async def transcribe(self, audio):
        raise ConnectionError("whisper host unreachable")


class OkLLM:
    async def generate(self, messages, system=""):
        return "Yes, we have one free."


class BrokenLLM:
    async def generate(self, messages, system=""):
        raise TimeoutError("model timed out")


class OkTTS:
    async def synthesize(self, request):
        return b"ID3fake-audio-bytes"


class BrokenTTS:
    async def synthesize(self, request):
        raise RuntimeError("voice not found")


def _register_all():
    providers.register_stt("t_ok", OkSTT)
    providers.register_stt("t_silent", SilentSTT)
    providers.register_stt("t_broken", BrokenSTT)
    providers.register_llm("t_ok", OkLLM)
    providers.register_llm("t_broken", BrokenLLM)
    providers.register_tts("t_ok", OkTTS)
    providers.register_tts("t_broken", BrokenTTS)


_register_all()


def _cfg(stt="t_ok", llm="t_ok", tts="t_ok"):
    return AgentConfig(
        name="test", system_prompt="You are Kwame.",
        first_message="Adinkra Hotel, Kwame speaking.",
        stt_provider=stt, llm_provider=llm, tts_provider=tts,
    )


CHUNK = AudioChunk(data=b"\x00" * 320, sample_rate=16000)


# ----------------------------------------------------------------------
# Happy path — text AND audio
# ----------------------------------------------------------------------

def test_start_returns_greeting_with_audio():
    eng = CyneaEngine(_cfg())
    turn = asyncio.run(eng.start())
    assert isinstance(turn, TurnResult)
    assert turn.text == "Adinkra Hotel, Kwame speaking."
    assert turn.has_audio, "_synthesize() must run for the greeting"


def test_process_audio_returns_text_and_audio():
    """The fix: the engine used to return text only and never call TTS."""
    eng = CyneaEngine(_cfg())
    asyncio.run(eng.start())
    turn = asyncio.run(eng.process_audio(CHUNK))

    assert turn.text == "Yes, we have one free."
    assert turn.audio == b"ID3fake-audio-bytes"
    assert turn.user_text == OkSTT.text


def test_turn_result_reads_like_the_old_string_return():
    eng = CyneaEngine(_cfg())
    turn = asyncio.run(eng.start())
    assert str(turn) == turn.text
    assert bool(turn) is True
    assert not bool(TurnResult(text=""))


def test_synthesize_false_skips_tts_entirely():
    """Text-only mode for tests and chat transports."""
    eng = CyneaEngine(_cfg(tts="t_broken"), synthesize=False)
    turn = asyncio.run(eng.process_audio(CHUNK))
    assert turn.text and not turn.has_audio


# ----------------------------------------------------------------------
# Silence is not failure
# ----------------------------------------------------------------------

def test_empty_transcript_returns_none_without_raising():
    """The caller simply said nothing. Not an incident."""
    eng = CyneaEngine(_cfg(stt="t_silent"))
    assert asyncio.run(eng.process_audio(CHUNK)) is None


# ----------------------------------------------------------------------
# Failure is loud
# ----------------------------------------------------------------------

@pytest.mark.parametrize("cfg_kwargs,error", [
    ({"stt": "t_broken"}, STTError),
    ({"llm": "t_broken"}, LLMError),
    ({"tts": "t_broken"}, TTSError),
])
def test_provider_failures_raise_rather_than_returning_none(cfg_kwargs, error):
    eng = CyneaEngine(_cfg(**cfg_kwargs))
    with pytest.raises(error):
        asyncio.run(eng.process_audio(CHUNK))


@pytest.mark.parametrize("cfg_kwargs,stage", [
    ({"stt": "t_broken"}, "stt"),
    ({"llm": "t_broken"}, "llm"),
    ({"tts": "t_broken"}, "tts"),
])
def test_on_error_callback_fires_with_the_failing_stage(cfg_kwargs, stage):
    seen = []
    eng = CyneaEngine(_cfg(**cfg_kwargs), on_error=lambda s, e: seen.append((s, e)))
    with pytest.raises(Exception):
        asyncio.run(eng.process_audio(CHUNK))
    assert [s for s, _ in seen] == [stage]


def test_a_broken_alerter_does_not_mask_the_real_error():
    """If on_error itself raises, the original fault must still surface."""
    def bad_alerter(stage, exc):
        raise ValueError("pager is down")

    eng = CyneaEngine(_cfg(llm="t_broken"), on_error=bad_alerter)
    with pytest.raises(LLMError):
        asyncio.run(eng.process_audio(CHUNK))


def test_error_carries_the_original_cause():
    eng = CyneaEngine(_cfg(llm="t_broken"))
    with pytest.raises(LLMError) as info:
        asyncio.run(eng.process_audio(CHUNK))
    assert isinstance(info.value.__cause__, TimeoutError)


# ----------------------------------------------------------------------
# Barge-in still drops the reply, and that is not an error
# ----------------------------------------------------------------------

def test_reply_superseded_by_barge_in_returns_none():
    class BargingLLM:
        def __init__(self):
            self.engine = None

        async def generate(self, messages, system=""):
            self.engine.interrupt()      # caller cuts in mid-generation
            return "a reply nobody waited for"

    barging = BargingLLM()
    providers.register_llm("t_barge", lambda: barging)

    eng = CyneaEngine(_cfg(llm="t_barge"))
    barging.engine = eng
    assert asyncio.run(eng.process_audio(CHUNK)) is None


def test_metrics_reports_state_and_turns():
    eng = CyneaEngine(_cfg())
    asyncio.run(eng.start())
    asyncio.run(eng.process_audio(CHUNK))
    m = eng.get_metrics()
    assert m["turns"] >= 2
    assert "interruption_count" in m
