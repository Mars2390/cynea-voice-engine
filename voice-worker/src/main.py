"""Cynea live call — the browser voice socket, on Cloudflare.

    wss://cynea-voice.<subdomain>.workers.dev/voice?agent=maya

Vercel functions cannot hold a WebSocket open, so this one route moves here
and everything else stays where it is. The wire contract is unchanged from
cynea/voice_ws.py, which remains the local fallback: the browser sends one
binary frame of 16 kHz mono PCM16 per utterance and gets JSON frames back
(ready / greeting / you / reply / audio / thinking / idle / interrupted /
busy / ended / error). assets/live-call.js needs only its URL changed.

Shape
-----
    Default (Worker)   checks the Origin, picks a free line, forwards
    VoiceSession (DO)  one line = one call at a time, Hibernation API

Lines, not one object per call
------------------------------
The FastAPI socket capped the demo at six concurrent calls with a counter
in process memory. A Worker has no shared process, so the cap is the
topology instead: there are VOICE_DEMO_MAX_CONCURRENT objects named
line-0..line-N, each holds at most one call, and the Worker tries them in
random order. "Is this line free?" is answered by the runtime's own list of
accepted sockets, so a call that drops without a close frame cannot leak a
slot the way a counter would — and the alarm below reclaims any line a
vanished client is still sitting on.

Hibernation
-----------
Sockets are accepted with ctx.acceptWebSocket(), never ws.accept(), and
nothing in this object keeps a timer or a pending promise between turns.
So between utterances — which is most of a call, because the browser sends
whole turns rather than a stream — the object is evicted from memory, the
socket stays open, and no duration is billed. Each message wakes it with a
fresh constructor.

That is why no state lives on `self` across messages. The call's metadata
rides on the socket (serializeAttachment) and the conversation is in
Durable Object storage. Every turn rebuilds the engine from storage, which
means the hibernation path is not a special case: it is the only path, and
every local test exercises it.

Edge TTS without edge-tts
-------------------------
The edge-tts package opens its socket through aiohttp, and a Python Worker
has no raw sockets. The protocol itself is four messages, so
WorkerEdgeTTS speaks it over the runtime's own outbound WebSocket
(fetch with Upgrade: websocket), taking only the constants and the
Sec-MS-GEC token from edge-tts. Everything above the wire — SSML
generation, prosody mapping, break folding — is the existing
EdgeTTSSynthesizer, inherited unchanged.

The model is Cerebras
---------------------
Replies come from Cerebras over its OpenAI-compatible API (CerebrasLLM,
registered as "cerebras"): gpt-oss-120b for conversation, and qwen-3.8-27b
for the fast path — a caller turn of a few words, like "yes" or "thanks",
where waiting on a large reasoning model is the slowest thing on the line.
Speech-to-text is still Whisper, hosted by Groq, because Cerebras serves
language models only; GROQ_API_KEY is read by that one step and nothing else.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import os
import random
import time
import uuid
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

from js import Object, Uint8Array, WebSocketPair, WebSocketRequestResponsePair
from js import fetch as js_fetch
from pyodide.ffi import create_proxy, to_js
from workers import DurableObject, Response, WorkerEntrypoint

# Shared with the FastAPI fallback. These are copied into src/ at build time
# by sync_shared.py; the repo-root packages are the source of truth.
from cynea import providers
from cynea.engine import CyneaEngine
from cynea.models import AudioChunk
from cynea.voice_demo import (
    DEFAULT_PERSONA,
    MAX_UTTERANCE_BYTES,
    MAX_UTTERANCE_S,
    RATE,
    demo_config,
    max_call_s,
    max_concurrent,
    max_turns,
)
from cynea_africa.synthesizer.edge_tts import EdgeTTSSynthesizer, _Segment
from edge_tts import constants as _edge_const
from edge_tts.drm import DRM as _EdgeDRM

# ── environment ──────────────────────────────────────────────────────────
# The providers read os.environ, as they do under uvicorn. A Worker's
# secrets and vars arrive on `env` instead, so they are copied across once
# per isolate. Two secrets are on the call path: CEREBRAS_API_KEY for the
# model and GROQ_API_KEY for Whisper transcription. The others are passed
# through so the shared modules see the same environment they would on
# Vercel, but this socket never persists a call (no workspace owns an
# anonymous visitor's call) and never signs a session.
_ENV_KEYS = (
    "CEREBRAS_API_KEY", "CEREBRAS_MODEL", "CEREBRAS_FAST_MODEL",
    "CEREBRAS_FAST_MAX_WORDS", "CEREBRAS_BASE_URL",
    "GROQ_API_KEY", "GROQ_STT_MODEL", "GROQ_BASE_URL",          # Whisper only
    "ELEVENLABS_API_KEY", "DATABASE_URL", "SESSION_SECRET",
    "VOICE_DEMO_MAX_CALL_S", "VOICE_DEMO_MAX_TURNS", "VOICE_DEMO_MAX_CONCURRENT",
)


def _load_env(env) -> None:
    for key in _ENV_KEYS:
        try:
            value = getattr(env, key)
        except AttributeError:
            continue
        if isinstance(value, str) and value:
            os.environ[key] = value


def _cap_text() -> str:
    secs = max_call_s()
    span = (f"{secs // 60} minute{'s' if secs // 60 != 1 else ''}" if secs % 60 == 0
            else f"{secs} seconds")
    return f"The demo is capped at {span}."


def _json(payload: dict, status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status,
                    headers={"content-type": "application/json"})


# ── Edge TTS over the Workers runtime's WebSocket ────────────────────────
# Protocol constants and the anti-abuse token come from the edge-tts package
# itself, so when Microsoft changes the handshake (it moved Sec-MS-GEC to a
# v2 scheme in 7.3.x, and 7.2.8 is now refused with 403) the fix is a
# version bump in pyproject.toml, not an edit here.
_EDGE_URL = f"https://{_edge_const.BASE_URL}/edge/v1"
_EDGE_TIMEOUT_S = 15.0
_EDGE_PARALLEL = 4          # segments in flight; Workers allow 6 open connections


def _edge_headers() -> dict:
    headers = _EdgeDRM.headers_with_muid(_edge_const.WSS_HEADERS)
    headers.pop("Sec-WebSocket-Version", None)   # the runtime owns the handshake
    headers["Upgrade"] = "websocket"
    return headers


def _edge_voice(short: str) -> str:
    """en-US-AriaNeural -> Microsoft Server Speech Text to Speech Voice (en-US, AriaNeural)."""
    parts = short.split("-", 2)
    if len(parts) == 3 and parts[2].endswith("Neural"):
        lang, region, name = parts
        if "-" in name:
            extra, name = name.split("-", 1)
            region = f"{region}-{extra}"
        return f"Microsoft Server Speech Text to Speech Voice ({lang}-{region}, {name})"
    return short


def _js_date() -> str:
    return time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)",
                         time.gmtime())


def _clean(text: str) -> str:
    # The service rejects C0 control characters (edge-tts' remove_incompatible_characters).
    return "".join(" " if (ord(c) <= 8 or 11 <= ord(c) <= 12 or 14 <= ord(c) <= 31) else c
                   for c in text)


class WorkerEdgeTTS(EdgeTTSSynthesizer):
    """EdgeTTSSynthesizer with the wire swapped for the Workers runtime's.

    Two overrides. `_synth_segment` replaces edge_tts.Communicate with a
    direct socket. `synthesize` runs the segments concurrently instead of
    one after another: the parent splits on sentences and on every number
    (for prosody), and each segment is its own Microsoft round trip, so a
    reply like "Friday at 7 for 4 people" is three handshakes in series.
    Order is preserved by gather(); MP3 frames concatenate cleanly.
    """

    @staticmethod
    def _import_edge_tts():
        return None                     # the library is not used here

    async def synthesize(self, request) -> bytes:
        text = (request.text or "").strip()
        if not text:
            return b""
        ssml = text if self._looks_like_ssml(text) else self.text_to_ssml(text)
        try:
            segments = self._parse_ssml(ssml)
        except Exception:
            segments = [_Segment(kind="text", text=text, prosody={}, ms=0)]
        segments = [s for s in self._fold_breaks_into_text(segments)
                    if s.kind == "text" and s.text.strip()]
        if not segments:
            return b""

        voice = request.voice or self.voice
        gate = asyncio.Semaphore(_EDGE_PARALLEL)

        async def one(seg) -> bytes:
            async with gate:
                return await self._synth_segment(
                    edge_tts=None, text=seg.text, voice=voice,
                    rate=self._merge_rate(seg.prosody.get("rate"), request.speed),
                    pitch=self._ssml_pitch_to_edge(seg.prosody.get("pitch")),
                    volume=self._ssml_volume_to_edge(seg.prosody.get("volume")),
                )

        chunks = [c for c in await asyncio.gather(*(one(s) for s in segments)) if c]
        if not chunks:
            raise RuntimeError("Edge TTS produced no audio for any segment.")
        return b"".join(chunks)

    async def _synth_segment(self, *, edge_tts, text: str, voice: str,
                             rate: str, pitch: str, volume: str) -> bytes:
        url = (f"{_EDGE_URL}?TrustedClientToken={_edge_const.TRUSTED_CLIENT_TOKEN}"
               f"&ConnectionId={uuid.uuid4().hex}"
               f"&Sec-MS-GEC={_EdgeDRM.generate_sec_ms_gec()}"
               f"&Sec-MS-GEC-Version={_edge_const.SEC_MS_GEC_VERSION}")
        try:
            resp = await js_fetch(url, to_js({"headers": _edge_headers()},
                                             dict_converter=Object.fromEntries))
        except Exception as exc:
            raise ConnectionError(f"Edge TTS unreachable: {exc}") from exc

        ws = resp.webSocket
        if not ws:
            raise ConnectionError(f"Edge TTS refused the socket (HTTP {resp.status}).")
        ws.accept()                      # outbound client socket — not a hibernatable one

        inbox: asyncio.Queue = asyncio.Queue()
        handlers = {
            "message": create_proxy(lambda ev: inbox.put_nowait(("message", ev.data))),
            "close": create_proxy(lambda ev: inbox.put_nowait(("close", ev.code))),
            "error": create_proxy(lambda ev: inbox.put_nowait(("error", None))),
        }
        for name, fn in handlers.items():
            ws.addEventListener(name, fn)

        audio = bytearray()
        try:
            ws.send(
                f"X-Timestamp:{_js_date()}\r\n"
                "Content-Type:application/json; charset=utf-8\r\n"
                "Path:speech.config\r\n\r\n"
                '{"context":{"synthesis":{"audio":{"metadataoptions":{'
                '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
                '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
            )
            ws.send(
                f"X-RequestId:{uuid.uuid4().hex}\r\n"
                "Content-Type:application/ssml+xml\r\n"
                f"X-Timestamp:{_js_date()}Z\r\n"       # the trailing Z is Edge's own bug
                "Path:ssml\r\n\r\n"
                "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'>"
                f"<voice name='{_edge_voice(voice)}'>"
                f"<prosody pitch='{pitch}' rate='{rate}' volume='{volume}'>"
                f"{escape(_clean(text))}"
                "</prosody></voice></speak>"
            )
            while True:
                kind, data = await asyncio.wait_for(inbox.get(), _EDGE_TIMEOUT_S)
                if kind == "error":
                    raise ConnectionError("Edge TTS socket errored mid-synthesis.")
                if kind == "close":
                    break
                if isinstance(data, str):
                    if "Path:turn.end" in data:
                        break
                    continue
                # Binary frames arrive as Blob on an outbound socket (measured:
                # Uint8Array over a Blob is silently empty, which is how every
                # reply first came back as "no audio"). Read it, in order.
                if data.constructor.name == "Blob":
                    data = await data.arrayBuffer()
                frame = Uint8Array.new(data).to_bytes()
                if len(frame) < 2:
                    continue
                head_len = int.from_bytes(frame[:2], "big")
                head, body = frame[2:2 + head_len], frame[2 + head_len:]
                if b"Path:audio" in head and body:
                    audio += body
        except asyncio.TimeoutError as exc:
            raise ConnectionError(f"Edge TTS timed out after {_EDGE_TIMEOUT_S:.0f}s.") from exc
        finally:
            # Detach before closing: close() queues a "close" event, and a
            # proxy destroyed while still attached raises "Object has
            # already been destroyed" when that event fires.
            for name, fn in handlers.items():
                ws.removeEventListener(name, fn)
            try:
                ws.close()
            except Exception:
                pass
            for fn in handlers.values():
                fn.destroy()

        if not audio:
            raise RuntimeError("Edge TTS returned no audio for a segment.")
        return bytes(audio)


providers.register_tts("edge_tts", WorkerEdgeTTS)


# ── the model: Cerebras ──────────────────────────────────────────────────
_CEREBRAS_BASE = "https://api.cerebras.ai/v1"
_CEREBRAS_MODEL = "gpt-oss-120b"        # conversation
_CEREBRAS_FAST_MODEL = "qwen-3.8-27b"   # short turns
_CEREBRAS_TIMEOUT_S = 30.0


class CerebrasLLMError(RuntimeError):
    """Cerebras could not be reached or returned nothing usable."""


class _OutOfTokens(CerebrasLLMError):
    """A reasoning model thought until max_tokens ran out; retryable."""


class CerebrasLLM:
    """Chat completions on Cerebras, over its OpenAI-compatible HTTP API.

    Plain httpx rather than the openai SDK, measured: the SDK took the
    bundle from 0.9 MB to 4.19 MB gzipped (2,708 modules, mostly its type
    stubs) — over the Workers Free plan's 3 MB cap — and made cold starts
    load all of it. The endpoint, the body and the reasoning_effort field
    are the same either way; the SDK only wraps them.

    Same contract as the other LLM adapters (`generate(messages, system)`
    returns the reply text or raises), so CyneaEngine does not know which
    provider answered. It raises rather than returning "" — on a phone line
    an empty reply is indistinguishable from a dropped call.

    Model choice is per turn. When the caller's latest turn is at most
    CEREBRAS_FAST_MAX_WORDS words (default 3; 0 turns the fast path off) the
    reply comes from CEREBRAS_FAST_MODEL, otherwise from CEREBRAS_MODEL.
    Both see the full system prompt and history, so the persona and the
    truth rule hold on either path.

    Every Qwen call carries reasoning_effort="none". Without it Qwen spends
    the token budget thinking and returns empty content, so it is set by
    model name, not by path — an override of CEREBRAS_MODEL to a Qwen model
    gets it too.
    """

    def __init__(self, max_tokens: int = 1024):
        self.api_key = os.getenv("CEREBRAS_API_KEY")
        self.base_url = os.getenv("CEREBRAS_BASE_URL", _CEREBRAS_BASE).rstrip("/")
        self.model = os.getenv("CEREBRAS_MODEL", _CEREBRAS_MODEL)
        self.fast_model = os.getenv("CEREBRAS_FAST_MODEL", _CEREBRAS_FAST_MODEL)
        self.fast_max_words = int(os.getenv("CEREBRAS_FAST_MAX_WORDS", "3"))
        self.max_tokens = max_tokens

    def pick_model(self, messages: list) -> str:
        last = next((m.get("content") or "" for m in reversed(messages)
                     if m.get("role") == "user"), "")
        words = len(str(last).split())
        if 0 < words <= self.fast_max_words:
            return self.fast_model
        return self.model

    async def generate(self, messages: list, system: str = "") -> str:
        body = list(messages)
        if system and not (body and body[0].get("role") == "system"):
            body = [{"role": "system", "content": system}] + body
        model = self.pick_model(body)
        try:
            return await self._complete(model, body, self.max_tokens)
        except _OutOfTokens:
            # A reasoning model that thought until the budget ran out. One
            # retry with double the room.
            try:
                return await self._complete(model, body, self.max_tokens * 2)
            except _OutOfTokens:
                raise CerebrasLLMError(
                    f"{model} produced no reply even at {self.max_tokens * 2} tokens.") from None

    async def _complete(self, model: str, body: list, max_tokens: int) -> str:
        import httpx

        if not self.api_key:
            raise CerebrasLLMError("CEREBRAS_API_KEY is not set. "
                                   "Run: pywrangler secret put CEREBRAS_API_KEY")
        payload = {"model": model, "messages": body,
                   "max_tokens": max_tokens, "temperature": 0.7}
        if "qwen" in model.lower():
            payload["reasoning_effort"] = "none"
        try:
            async with httpx.AsyncClient(timeout=_CEREBRAS_TIMEOUT_S) as client:
                r = await client.post(f"{self.base_url}/chat/completions", json=payload,
                                      headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.TimeoutException as exc:
            raise CerebrasLLMError(f"Cerebras timed out after {_CEREBRAS_TIMEOUT_S:.0f}s.") from exc
        except Exception as exc:
            raise CerebrasLLMError(f"Cerebras unreachable: {exc}") from exc

        if r.status_code == 401:
            raise CerebrasLLMError("Cerebras rejected the API key (401). Check CEREBRAS_API_KEY.")
        if r.status_code == 429:
            raise CerebrasLLMError("Cerebras rate limit hit (429).")
        if r.status_code == 404:
            raise CerebrasLLMError(f"Cerebras has no model named {model!r} on this key: "
                                   f"{r.text[:200]}")
        if r.status_code >= 400:
            raise CerebrasLLMError(f"Cerebras returned {r.status_code}: {r.text[:300]}")

        try:
            choice = r.json()["choices"][0]
            text = (choice["message"].get("content") or "").strip()
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise CerebrasLLMError(f"Unexpected Cerebras response: {r.text[:300]}") from exc
        if text:
            return text
        if choice.get("finish_reason") == "length":
            raise _OutOfTokens()
        hint = ' (is reasoning_effort="none" reaching it?)' if "qwen" in model.lower() else ""
        raise CerebrasLLMError(f"{model} returned an empty reply{hint}.")


providers.register_llm("cerebras", CerebrasLLM)


def _call_config(persona: str):
    """The demo persona, answered by Cerebras.

    demo_config() is shared with the FastAPI fallback, which still names
    Groq; the Worker swaps the model provider and keeps everything else —
    prompt, truth rule, voice — identical.
    """
    config = demo_config(persona)
    config.llm_provider = "cerebras"
    return config


# ── the Worker ───────────────────────────────────────────────────────────
class Default(WorkerEntrypoint):
    async def fetch(self, request):
        _load_env(self.env)
        url = urlparse(request.url)
        path = url.path.rstrip("/") or "/"

        if path in ("/voice/health", "/api/voice/health"):
            return self._health()
        if path not in ("/voice", "/api/voice"):
            return _json({"error": "not found", "socket": "/voice?agent=maya"}, 404)

        if (request.headers.get("Upgrade") or "").lower() != "websocket":
            return _json({"error": "this route only speaks WebSocket"}, 426)

        origin = request.headers.get("Origin")
        if origin and not self._origin_allowed(origin):
            # Logged because tail shows a deliberate 403 as outcome "Ok",
            # and the browser only ever sees onerror + 1006.
            print(f"refused origin {origin!r}")
            return _json({"error": f"origin {origin} is not allowed"}, 403)

        agent = (parse_qs(url.query).get("agent") or [DEFAULT_PERSONA])[0].lower()
        lines = list(range(max_concurrent()))
        random.shuffle(lines)

        ns = self.env.VOICE_SESSION
        for n, line in enumerate(lines):
            last = n == len(lines) - 1
            stub = ns.get(ns.idFromName(f"line-{line}"))
            resp = await stub.fetch(
                f"https://voice-session/connect?agent={agent}&last={int(last)}",
                headers={"Upgrade": "websocket"},
            )
            if resp.status != 409:       # 101 with a call, or 101 with "busy"
                return resp
        return _json({"error": "no line answered"}, 503)  # only if lines == 0

    def _origin_allowed(self, origin: str) -> bool:
        try:
            raw = self.env.ALLOWED_ORIGINS
        except AttributeError:
            raw = ""
        patterns = [p.strip() for p in str(raw or "").split(",") if p.strip()]
        return any(fnmatch.fnmatchcase(origin, p) for p in patterns)

    def _health(self):
        reg = providers.registered()
        missing = [k for k in ("CEREBRAS_API_KEY", "GROQ_API_KEY") if not os.getenv(k)]
        llm = CerebrasLLM()
        return _json({
            "ok": not missing,
            "reason": f"not set: {', '.join(missing)}" if missing else None,
            "capacity": max_concurrent(),
            "stt": "groq-stt" in reg.get("stt", []),
            "llm": "cerebras" in reg.get("llm", []),
            "model": llm.model,
            "fast_model": llm.fast_model if llm.fast_max_words > 0 else None,
            "tts": "edge_tts" in reg.get("tts", []),
            "rate": RATE,
            "max_call_s": max_call_s(),
        })


# ── one line ─────────────────────────────────────────────────────────────
class VoiceSession(DurableObject):
    """One phone line: at most one call at a time, hibernating between turns."""

    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        _load_env(env)
        # Answered by the runtime without waking this object, so a client
        # that keeps its connection alive with pings costs nothing.
        ctx.setWebSocketAutoResponse(WebSocketRequestResponsePair.new("ping", "pong"))
        # Serialises turns within one wake. Not state: it is rebuilt on every
        # wake, and a hibernated object has no turn in flight to guard.
        self._turn = asyncio.Lock()

    # ── connect ──────────────────────────────────────────────────────────
    async def fetch(self, request):
        query = parse_qs(urlparse(request.url).query)
        persona = (query.get("agent") or [DEFAULT_PERSONA])[0].lower()
        last = (query.get("last") or ["0"])[0] == "1"

        if len(self.ctx.getWebSockets("call")) > 0:
            if not last:
                return Response("line busy", status=409)
            # Every line is taken. Answer, say so, hang up — the browser
            # shows the message instead of a bare connection failure.
            client, server = WebSocketPair.new().object_values()
            self.ctx.acceptWebSocket(server, to_js(["busy"]))
            server.send(json.dumps({
                "type": "busy",
                "message": f"All {max_concurrent()} demo lines are in use. Try again in a moment.",
            }))
            # Hung up by the alarm a moment from now, not here. Measured:
            # a close() before this 101 is returned is dropped, and so is
            # one from a background task after it — the frame is only sent
            # from inside an event handler. The browser releases the
            # microphone in onclose, so the close has to actually arrive.
            # alarm() re-arms the call's own deadline after it has run.
            await self.ctx.storage.setAlarm(int((time.time() + 0.25) * 1000))
            return Response(None, status=101, web_socket=client)

        client, server = WebSocketPair.new().object_values()
        self.ctx.acceptWebSocket(server, to_js(["call"]))  # Hibernation API

        meta = {"id": uuid.uuid4().hex, "persona": persona,
                "started": time.time(), "turns": 0}
        server.serializeAttachment(json.dumps(meta))

        # A fresh call on this line: drop the last caller's conversation and
        # set the hard stop. The alarm, not the next utterance, ends the
        # call — otherwise an abandoned tab would hold the line forever.
        await self.ctx.storage.delete("history")
        await self.ctx.storage.setAlarm(int((time.time() + max_call_s()) * 1000))

        self._send(server, {"type": "ready", "agent": _call_config(persona).name,
                            "persona": persona, "rate": RATE,
                            "max_call_s": max_call_s()})
        asyncio.ensure_future(self._greet(server, meta))
        return Response(None, status=101, web_socket=client)

    async def _greet(self, ws, meta: dict) -> None:
        async with self._turn:
            try:
                engine = await self._engine(meta)
                greeting = await engine.start()
                await self._save(engine)
                if greeting:
                    self._speak(ws, "greeting", str(greeting), greeting.audio)
            except Exception as exc:
                self._send(ws, {"type": "error",
                                "message": f"{type(exc).__name__}: {exc}"[:300]})

    # ── hibernation handlers ─────────────────────────────────────────────
    async def webSocketMessage(self, ws, message):
        async with self._turn:
            try:
                await self._on_message(ws, message)
            except Exception:
                self._send(ws, {"type": "error", "message": "The call dropped. Reload to retry."})
                self._hang_up(ws, 1011, "internal error")

    async def webSocketClose(self, ws, code, reason, was_clean):
        if "call" in self.ctx.getTags(ws):
            await self._release()
        try:
            ws.close(code, "closing")    # complete the close handshake
        except Exception:
            pass

    async def webSocketError(self, ws, error):
        if "call" in self.ctx.getTags(ws):
            await self._release()

    async def alarm(self, *args):
        """One alarm per object, two jobs: hang up busy callers, end long calls.

        Whichever deadline is still in the future is re-armed, so a busy
        caller's quarter-second alarm cannot cancel the call's time cap.
        """
        now = time.time()
        for ws in self.ctx.getWebSockets("busy"):
            self._hang_up(ws, 1013, "busy")                # try again later

        upcoming = []
        for ws in self.ctx.getWebSockets("call"):
            deadline = json.loads(ws.deserializeAttachment())["started"] + max_call_s()
            if now >= deadline:
                self._send(ws, {"type": "ended", "reason":
                                _cap_text()})
                self._hang_up(ws, 1000, "time limit")
                await self.ctx.storage.delete("history")
            else:
                upcoming.append(deadline)
        if upcoming:
            await self.ctx.storage.setAlarm(int(min(upcoming) * 1000))

    # ── a turn ───────────────────────────────────────────────────────────
    async def _on_message(self, ws, message) -> None:
        meta = json.loads(ws.deserializeAttachment())

        if isinstance(message, str):                      # control frame
            try:
                cmd = json.loads(message)
            except ValueError:
                return
            action = cmd.get("action")
            if action == "hangup":
                self._hang_up(ws, 1000, "hangup")
            elif action == "interrupt":
                # The browser does not send this today; kept so the contract
                # matches voice_ws.py. The engine's sequence ids drop any
                # reply in flight, and the trimmed history is saved.
                engine = await self._engine(meta)
                engine.interrupt()
                await self._save(engine)
                self._send(ws, {"type": "interrupted"})
            return

        pcm = message if isinstance(message, (bytes, bytearray)) \
            else Uint8Array.new(message).to_bytes()
        if not pcm:
            return
        if len(pcm) > MAX_UTTERANCE_BYTES:
            self._send(ws, {"type": "error", "message":
                            f"That was longer than {MAX_UTTERANCE_S}s. Say it in a shorter turn."})
            return
        if time.time() - meta["started"] > max_call_s():
            self._send(ws, {"type": "ended",
                            "reason": _cap_text()})
            self._hang_up(ws, 1000, "time limit")
            return
        if meta["turns"] >= max_turns():
            self._send(ws, {"type": "ended",
                            "reason": f"The demo is capped at {max_turns()} turns."})
            self._hang_up(ws, 1000, "turn limit")
            return

        meta["turns"] += 1
        ws.serializeAttachment(json.dumps(meta))
        self._send(ws, {"type": "thinking"})

        engine = await self._engine(meta)
        try:
            result = await engine.process_audio(
                AudioChunk(data=bytes(pcm), sample_rate=RATE, channels=1, encoding="pcm16"))
        except Exception as exc:
            # Say which stage broke; a demo that fails legibly is worth more
            # than one that just stops.
            self._send(ws, {"type": "error", "message": f"{type(exc).__name__}: {exc}"[:300]})
            return

        if result is None:
            self._send(ws, {"type": "idle"})
            return

        await self._save(engine)
        if result.user_text:
            self._send(ws, {"type": "you", "text": result.user_text})
        self._speak(ws, "reply", str(result), result.audio)

    # ── state ────────────────────────────────────────────────────────────
    async def _engine(self, meta: dict) -> CyneaEngine:
        """The engine for this call, rebuilt from storage.

        persist=False: an anonymous visitor has no workspace to own the
        row, and the demo is not a customer's call log. The system prompt
        comes from the current deploy, not from storage, so a prompt fix
        reaches calls already in progress.
        """
        engine = CyneaEngine(_call_config(meta["persona"]), persist=False)
        saved = await self.ctx.storage.get("history")
        if isinstance(saved, str):
            engine.history.restore(json.loads(saved))
        engine.resume()
        return engine

    async def _save(self, engine: CyneaEngine) -> None:
        await self.ctx.storage.put("history", json.dumps(engine.history.snapshot()))

    async def _release(self) -> None:
        await self.ctx.storage.delete("history")
        # Leave the alarm if a busy caller is still waiting to be hung up.
        if not self.ctx.getWebSockets("busy"):
            await self.ctx.storage.deleteAlarm()

    # ── wire ─────────────────────────────────────────────────────────────
    @staticmethod
    def _send(ws, payload: dict) -> None:
        try:
            ws.send(json.dumps(payload))
        except Exception:
            pass                          # the caller already hung up

    def _speak(self, ws, kind: str, text: str, audio: bytes) -> None:
        """Text first, then its audio, so the transcript leads the speech."""
        self._send(ws, {"type": kind, "text": text})
        if audio:
            self._send(ws, {"type": "audio", "format": "mp3",
                            "data": base64.b64encode(audio).decode("ascii")})

    @staticmethod
    def _hang_up(ws, code: int, reason: str) -> None:
        try:
            ws.close(code, reason)
        except Exception:
            pass
