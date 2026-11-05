"""Live voice calls in the browser, over one WebSocket.

Mounted by cynea/api.py at:

    ws://<host>/voice?agent=maya           local
    wss://<host>/api/voice?agent=maya      deployed

The visitor speaks into their microphone, the browser sends the utterance
when they stop talking, and this runs the same CyneaEngine a phone call
would: transcribe, answer, synthesise, send the audio back. The agent is
Maya, on Edge TTS en-US-AriaNeural. No carrier, no
number, no account — which is the point, because the phone number is six
weeks of somebody else's paperwork away and the product works today.

Why this can deploy
-------------------
Every provider on the path is an HTTP call: Groq for transcription and the
model, Edge TTS for speech. Nothing here imports numpy, scipy, torch or
faster-whisper, and nothing shells out to ffmpeg, so the function stays
the same weight as the rest of the control plane. That is deliberate — the
moment this needs local Whisper it stops being deployable next to the API
and has to move to its own machine.

The audio contract, and why it has no conversion in it
------------------------------------------------------
The browser sends **16 kHz mono signed 16-bit little-endian PCM**, which
is exactly what `AudioChunk` already means by "pcm16" and exactly what the
Groq transcriber already wraps in a WAV header. Edge TTS returns MP3,
which every browser plays natively. So an utterance crosses this module
without being decoded, resampled or re-encoded once.

The alternative — MediaRecorder's WebM/Opus — is fewer lines in the
browser and needs ffmpeg on the server to become anything the engine can
read. ffmpeg is not on a serverless function.

Turn-taking
-----------
The browser decides when a turn ends, not the server. It is already
computing loudness per frame to draw the level meter, so it knows about
silence sooner than a round trip could tell it, and Groq's transcription
endpoint takes one complete utterance rather than a stream — there is no
partial-result socket to subscribe to. Sending whole turns is therefore
both simpler and no slower.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Optional

from cynea.models import AgentConfig, AudioChunk

# Imported at module scope, and it has to be.
#
# `from __future__ import annotations` above turns every annotation into a
# string, so FastAPI resolves the `ws: WebSocket` parameter below by looking
# "WebSocket" up in this module's globals. Imported inside register() it is a
# local name, the lookup fails, FastAPI falls back to treating `ws` as an
# ordinary parameter, and the handshake is refused with a bare 403 that says
# nothing about why. The identical code in a module without the __future__
# import works, which is what makes it worth a comment.
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

log = logging.getLogger("cynea.voice_ws")

# The persona, the disclosure rule and the caps are shared with the
# Cloudflare Worker (voice-worker/src/main.py), which carries this same call
# in production. They live in cynea/voice_demo.py so the two cannot drift.
from cynea.voice_demo import (  # noqa: E402
    CALL_ERROR_TEXT,
    DEFAULT_PERSONA,
    MAX_UTTERANCE_BYTES,
    MAX_UTTERANCE_S,
    RATE,
    demo_config as _config,
    max_call_s,
    max_concurrent,
    max_turns,
)

MAX_CALL_S = max_call_s()
MAX_TURNS = max_turns()
MAX_CONCURRENT = max_concurrent()

_live = 0                    # concurrent calls, guarded by _lock
_lock = asyncio.Lock()


class _Full(RuntimeError):
    pass


async def _claim() -> None:
    global _live
    async with _lock:
        if _live >= MAX_CONCURRENT:
            raise _Full()
        _live += 1


async def _release() -> None:
    global _live
    async with _lock:
        _live = max(0, _live - 1)


def register(app) -> None:
    """Attach the /voice socket to a FastAPI app."""
    @app.websocket("/voice")
    async def voice(ws: WebSocket):                       # noqa: C901
        persona = (ws.query_params.get("agent") or DEFAULT_PERSONA).lower()
        await ws.accept()

        async def send(payload: dict) -> None:
            if ws.client_state is WebSocketState.CONNECTED:
                await ws.send_text(json.dumps(payload))

        async def speak(kind: str, text: str, audio: bytes) -> None:
            """Text first, then the audio it belongs to.

            Two frames rather than one so the transcript appears while the
            speech is still arriving — on a slow link that is the
            difference between a live-feeling call and a page that seems
            to have hung.
            """
            await send({"type": kind, "text": text})
            if audio:
                await send({"type": "audio",
                            "format": "mp3",
                            "data": base64.b64encode(audio).decode("ascii")})

        try:
            await _claim()
        except _Full:
            await send({"type": "busy",
                        "message": f"All {MAX_CONCURRENT} demo lines are in use. "
                                   f"Try again in a moment."})
            await ws.close(code=1013)                     # try again later
            return

        engine = None
        started = time.monotonic()
        turns = 0

        try:
            from cynea.engine import CyneaEngine

            # persist=False: this is an anonymous visitor with no workspace
            # to own the row, and agents.user_id is NOT NULL. The demo is
            # not a customer's call log and should not appear in one.
            engine = CyneaEngine(_config(persona), persist=False)

            await send({"type": "ready", "agent": engine.config.name,
                        "persona": persona, "rate": RATE,
                        "max_call_s": MAX_CALL_S})

            greeting = await engine.start()
            if greeting:
                await speak("greeting", str(greeting), greeting.audio)

            while True:
                message = await ws.receive()

                if message.get("type") == "websocket.disconnect":
                    break

                # ── control frames ──────────────────────────────────────
                if (text := message.get("text")) is not None:
                    try:
                        cmd = json.loads(text)
                    except ValueError:
                        continue
                    action = cmd.get("action")
                    if action == "hangup":
                        break
                    if action == "interrupt":
                        # The visitor started talking over the agent. The
                        # engine cancels by sequence id, so the reply
                        # already in flight is dropped rather than played
                        # over the top of the next one.
                        engine.interrupt()
                        await send({"type": "interrupted"})
                    continue

                # ── an utterance ────────────────────────────────────────
                pcm = message.get("bytes")
                if not pcm:
                    continue

                if len(pcm) > MAX_UTTERANCE_BYTES:
                    await send({"type": "error", "message":
                                f"That was longer than {MAX_UTTERANCE_S}s. "
                                f"Say it in a shorter turn."})
                    continue

                if time.monotonic() - started > MAX_CALL_S:
                    await send({"type": "ended",
                                "reason": f"The demo is capped at "
                                          f"{MAX_CALL_S // 60} minutes."})
                    break
                if turns >= MAX_TURNS:
                    await send({"type": "ended",
                                "reason": f"The demo is capped at {MAX_TURNS} turns."})
                    break

                turns += 1
                engine.resume()
                await send({"type": "thinking"})

                try:
                    result = await engine.process_audio(
                        AudioChunk(data=pcm, sample_rate=RATE, channels=1,
                                   encoding="pcm16"))
                except Exception as exc:
                    # The engine raises on a provider failure precisely so
                    # this does not become silence on the line. The caller
                    # hears that it failed; the cause goes to the log.
                    log.exception("voice turn failed")
                    await send({"type": "error", "message": CALL_ERROR_TEXT})
                    continue

                if result is None:
                    # Nothing was said, or barge-in cancelled the turn.
                    await send({"type": "idle"})
                    continue

                if result.user_text:
                    await send({"type": "you", "text": result.user_text})
                await speak("reply", str(result), result.audio)

        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("voice socket failed")
            await send({"type": "error", "message": "The call dropped. Reload to retry."})
        finally:
            await _release()
            if engine is not None:
                try:
                    engine.end_call()
                except Exception:                     # pragma: no cover
                    log.exception("end_call failed")
            if ws.client_state is WebSocketState.CONNECTED:
                await ws.close()

    @app.get("/voice/health", tags=["system"])
    def voice_health():
        """Whether the local socket has its key. The Worker's check really uses it."""
        from cynea import providers

        reg = providers.registered()
        return {
            "ok": bool(os.getenv("GROQ_API_KEY")),
            "reason": None if os.getenv("GROQ_API_KEY") else "GROQ_API_KEY is not set",
            "live": _live,
            "capacity": MAX_CONCURRENT,
            "stt": "groq-stt" in reg.get("stt", []),
            "llm": "groq" in reg.get("llm", []),
            "tts": "edge_tts" in reg.get("tts", []),
            "rate": RATE,
        }
