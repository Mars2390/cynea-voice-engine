"""The Cloudflare Worker's call handling, without Cloudflare.

voice-worker/src/main.py imports the Workers runtime (`js`, `pyodide`, `workers`),
which only exist inside workerd. They are stubbed here with just enough behaviour
to run the real handlers: the fetch handlers, the turn, the alarm, the limits.

What this cannot catch is anything about the runtime itself: hibernation, the
Blob frames, close frames that never send. Those are in voice-worker's memory of
traps and are checked in workerd and in a browser. To mimic hibernation, every
message here goes to a brand new VoiceSession sharing only the storage and the
socket, because that is all that survives a real one.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── the runtime, stubbed ─────────────────────────────────────────────────
class FakeSocket:
    def __init__(self):
        self.sent, self.closed = [], None
        self._attachment = None

    def send(self, data):
        self.sent.append(json.loads(data))

    def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def serializeAttachment(self, value):
        self._attachment = value

    def deserializeAttachment(self):
        return self._attachment

    def types(self):
        return [m["type"] for m in self.sent]

    def of(self, kind):
        return [m for m in self.sent if m["type"] == kind]


class FakeStorage:
    def __init__(self):
        self.data, self.alarm = {}, None

    async def get(self, key):
        return self.data.get(key)

    async def put(self, key, value):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)

    async def setAlarm(self, at_ms):
        self.alarm = at_ms

    async def deleteAlarm(self):
        self.alarm = None


class FakeCtx:
    def __init__(self):
        self.storage = FakeStorage()
        self.sockets = []          # (socket, tags)

    def setWebSocketAutoResponse(self, pair):
        pass

    def acceptWebSocket(self, ws, tags):
        self.sockets.append((ws, list(tags)))

    def getWebSockets(self, tag):
        return [ws for ws, tags in self.sockets if tag in tags and ws.closed is None]

    def getTags(self, ws):
        return next(tags for s, tags in self.sockets if s is ws)


class FakeResponse:
    def __init__(self, body=None, status=200, headers=None, web_socket=None):
        self.body, self.status, self.headers, self.web_socket = body, status, headers, web_socket


class FakeRequest:
    def __init__(self, url, headers=None):
        self.url, self.headers = url, _Headers(headers or {})


class _Headers:
    def __init__(self, values):
        self._values = {k.lower(): v for k, v in values.items()}

    def get(self, key):
        return self._values.get(key.lower())


class _Pair:
    @staticmethod
    def new():
        return types.SimpleNamespace(object_values=lambda: (FakeSocket(), FakeSocket()))


def _install_runtime():
    js = types.ModuleType("js")
    js.Object = types.SimpleNamespace(fromEntries=dict)
    js.Uint8Array = types.SimpleNamespace(new=lambda data: data)
    js.WebSocketPair = _Pair
    js.WebSocketRequestResponsePair = types.SimpleNamespace(new=lambda a, b: (a, b))
    js.AbortSignal = types.SimpleNamespace(timeout=lambda ms: ms)

    async def _no_network(*a, **k):
        raise AssertionError("the Worker tried to reach the network unexpectedly")
    js.fetch = _no_network

    ffi = types.ModuleType("pyodide.ffi")
    ffi.create_proxy = lambda fn: fn
    ffi.to_js = lambda value, **k: value
    pyodide = types.ModuleType("pyodide")
    pyodide.ffi = ffi

    workers = types.ModuleType("workers")

    class DurableObject:
        def __init__(self, ctx, env):
            self.ctx, self.env = ctx, env

    workers.DurableObject = DurableObject
    workers.Response = FakeResponse
    workers.WorkerEntrypoint = type("WorkerEntrypoint", (), {})
    sys.modules.update({"js": js, "pyodide": pyodide, "pyodide.ffi": ffi, "workers": workers})


@pytest.fixture(scope="module")
def main():
    saved = {k: sys.modules.get(k) for k in ("js", "pyodide", "pyodide.ffi", "workers")}
    _install_runtime()
    spec = importlib.util.spec_from_file_location("voice_worker_main",
                                                  ROOT / "voice-worker" / "src" / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    for key, value in saved.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value


# ── fake providers, so a turn runs the real engine ───────────────────────
class Stage:
    """What the fake providers do. Reset per test."""
    said = ["I would like a table"]
    reply = "Sure, a table for you."
    prompts: list = []
    histories: list = []
    voices: list = []
    fail: str = ""


class FakeSTT:
    async def transcribe(self, audio):
        from cynea.models import Transcription
        if Stage.fail == "stt":
            raise RuntimeError("Groq rejected the API key (401) gsk_secret")
        text = Stage.said.pop(0) if Stage.said else "and another thing"
        return Transcription(text=text, confidence=1.0, is_final=True)


class FakeLLM:
    def __init__(self, *a, **k):
        pass

    async def generate(self, messages, system=""):
        if Stage.fail == "llm":
            raise RuntimeError("Cerebras returned 503: upstream detail")
        Stage.prompts.append(system)
        Stage.histories.append([dict(m) for m in messages])
        return Stage.reply


class FakeTTS:
    def __init__(self, *a, **k):
        pass

    async def synthesize(self, request):
        if Stage.fail == "tts":
            raise RuntimeError("Edge TTS unreachable")
        Stage.voices.append(request.voice)
        return b"mp3-bytes"


@pytest.fixture()
def worker(main, monkeypatch):
    from cynea import providers

    Stage.said = ["I would like a table"]
    Stage.reply = "Sure, a table for you."
    Stage.prompts, Stage.histories, Stage.voices, Stage.fail = [], [], [], ""
    for name in ("_STT", "_LLM", "_TTS"):
        if hasattr(providers, name):
            monkeypatch.setattr(providers, name, dict(getattr(providers, name)))
    providers.register_stt("groq-stt", FakeSTT)
    providers.register_llm("cerebras", FakeLLM)
    providers.register_tts("edge_tts", FakeTTS)

    for key in ("VOICE_DEMO_MAX_CALL_S", "VOICE_DEMO_MAX_TURNS", "VOICE_DEMO_MAX_CONCURRENT",
                "VOICE_DEMO_IDLE_S", "VOICE_DEMO_CALLS_PER_HOUR", "SENTRY_DSN", "API_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VOICE_DEMO_MAX_CALL_S", "600")      # as wrangler.toml sets it
    return main


def session(main, ctx=None):
    """A brand new object on the same storage and sockets, as after hibernation."""
    return main.VoiceSession(ctx or FakeCtx(), types.SimpleNamespace())


async def settle():
    for task in asyncio.all_tasks() - {asyncio.current_task()}:
        await task


async def connect(main, ctx, url="https://voice-session/connect?agent=maya&last=0"):
    resp = await session(main, ctx).fetch(FakeRequest(url))
    await settle()
    return resp


def server_socket(ctx, tag="call"):
    return ctx.getWebSockets(tag)[0]


AUDIO = b"\0\1" * 16000


async def say(main, ctx, ws, audio=AUDIO):
    await session(main, ctx).webSocketMessage(ws, audio)


# ── answering ────────────────────────────────────────────────────────────
def test_a_call_gets_ready_then_a_greeting_with_audio(worker):
    async def go():
        ctx = FakeCtx()
        resp = await connect(worker, ctx)
        ws = server_socket(ctx)
        assert resp.status == 101
        assert ws.types() == ["ready", "greeting", "audio"]
        assert ws.of("ready")[0]["agent"] == "Maya" and ws.of("ready")[0]["idle_s"] == 180
        assert ws.of("greeting")[0]["text"].startswith("Hello, this is Maya")
    asyncio.run(go())


def test_a_fresh_call_drops_the_last_callers_conversation(worker):
    async def go():
        ctx = FakeCtx()
        ctx.storage.data["history"] = json.dumps([{"role": "user", "content": "last caller"}])
        ctx.storage.data["saved"] = json.dumps({"name": "Their Agent"})
        await connect(worker, ctx)
        assert "last caller" not in ctx.storage.data["history"]
        assert "saved" not in ctx.storage.data
    asyncio.run(go())


def test_a_line_in_use_passes_the_caller_on(worker):
    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx)
        assert (await connect(worker, ctx)).status == 409
    asyncio.run(go())


def test_when_every_line_is_taken_the_caller_is_told(worker):
    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx)
        resp = await connect(worker, ctx, "https://voice-session/connect?agent=maya&last=1")
        assert resp.status == 101
        busy = server_socket(ctx, "busy")
        assert busy.of("busy")[0]["message"].startswith("All 6 demo lines")
        assert ctx.storage.alarm is not None        # hung up by the alarm
        await session(worker, ctx).alarm()
        assert busy.closed == (1013, "busy")
        assert server_socket(ctx).closed is None    # the call in progress is untouched
    asyncio.run(go())


# ── turns ────────────────────────────────────────────────────────────────
def test_a_turn_returns_words_then_reply_then_audio(worker):
    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx)
        ws = server_socket(ctx)
        ws.sent.clear()
        await say(worker, ctx, ws)
        assert ws.types() == ["thinking", "you", "reply", "audio"]
        assert ws.of("you")[0]["text"] == "I would like a table"
        assert ws.of("reply")[0]["text"] == "Sure, a table for you."
    asyncio.run(go())


def test_the_conversation_survives_a_rebuilt_object(worker):
    """No state on self: each turn is a new VoiceSession, as after hibernation."""
    async def go():
        Stage.said = ["My name is Ama", "What is my name?"]
        ctx = FakeCtx()
        await connect(worker, ctx)
        ws = server_socket(ctx)
        await say(worker, ctx, ws)
        await say(worker, ctx, ws)
        seen = json.dumps(Stage.histories[-1])
        assert "My name is Ama" in seen and "Sure, a table for you." in seen
        assert json.loads(ws.deserializeAttachment())["turns"] == 2
    asyncio.run(go())


@pytest.mark.parametrize("stage,detail", [("stt", "gsk_secret"), ("llm", "upstream detail"),
                                          ("tts", "unreachable")])
def test_a_provider_failure_reaches_the_caller_as_a_plain_sentence(worker, capsys, stage, detail):
    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx)
        ws = server_socket(ctx)
        ws.sent.clear()
        Stage.fail = stage
        await say(worker, ctx, ws)
        error = ws.of("error")[0]["message"]
        assert error == worker.CALL_ERROR_TEXT and detail not in json.dumps(ws.sent)
        assert ws.closed is None                # the caller can try again
    asyncio.run(go())
    assert detail in capsys.readouterr().out    # the cause is in the log


def test_a_greeting_that_fails_does_the_same(worker):
    async def go():
        Stage.fail = "tts"
        ctx = FakeCtx()
        await connect(worker, ctx)
        ws = server_socket(ctx)
        assert ws.of("error")[0]["message"] == worker.CALL_ERROR_TEXT
    asyncio.run(go())


def test_a_long_utterance_is_refused(worker):
    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx)
        ws = server_socket(ctx)
        ws.sent.clear()
        await say(worker, ctx, ws, b"\0" * (worker.MAX_UTTERANCE_BYTES + 1))
        assert ws.types() == ["error"] and "shorter turn" in ws.sent[0]["message"]
    asyncio.run(go())


def test_the_turn_cap_ends_the_call(worker, monkeypatch):
    monkeypatch.setenv("VOICE_DEMO_MAX_TURNS", "1")

    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx)
        ws = server_socket(ctx)
        await say(worker, ctx, ws)
        await say(worker, ctx, ws)
        assert ws.closed == (1000, "turn limit") and "ended" in ws.types()
    asyncio.run(go())


@pytest.mark.parametrize("frame", ['not json', '{"action":"nonsense"}', "{}"])
def test_junk_control_frames_are_ignored(worker, frame):
    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx)
        ws = server_socket(ctx)
        ws.sent.clear()
        await session(worker, ctx).webSocketMessage(ws, frame)
        assert ws.sent == [] and ws.closed is None
    asyncio.run(go())


def test_hangup_closes_and_clears_the_line(worker):
    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx)
        ws = server_socket(ctx)
        await session(worker, ctx).webSocketMessage(ws, '{"action":"hangup"}')
        assert ws.closed == (1000, "hangup")
        await session(worker, ctx).webSocketClose(ws, 1000, "hangup", True)
        assert "history" not in ctx.storage.data and ctx.storage.alarm is None
    asyncio.run(go())


def test_a_refused_callers_goodbye_frame_is_ignored(worker):
    """Measured: it arrived on a socket with no call attached and threw."""
    async def go():
        ctx = FakeCtx()
        ws = FakeSocket()
        ctx.acceptWebSocket(ws, ["busy"])
        await session(worker, ctx).webSocketMessage(ws, '{"action":"hangup"}')
        assert ws.sent == []
    asyncio.run(go())


# ── the alarm ────────────────────────────────────────────────────────────
def _age(ws, started=0.0, last=None):
    meta = json.loads(ws.deserializeAttachment())
    meta["started"] = time.time() - started
    meta["last"] = time.time() - (last if last is not None else started)
    ws.serializeAttachment(json.dumps(meta))


def test_first_alarm_is_the_silence_limit_not_the_call_cap(worker):
    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx)
        assert abs(ctx.storage.alarm / 1000 - (time.time() + 180)) < 5
    asyncio.run(go())


def test_a_silent_call_is_ended_and_told_why(worker):
    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx)
        ws = server_socket(ctx)
        _age(ws, started=181)
        await session(worker, ctx).alarm()
        assert ws.closed == (1000, "idle")
        assert ws.of("ended")[0]["reason"] == "The call ended after 3 minutes of silence."
        assert "history" not in ctx.storage.data
    asyncio.run(go())


def test_a_turn_moves_the_silence_deadline(worker):
    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx)
        ws = server_socket(ctx)
        _age(ws, started=170)
        await say(worker, ctx, ws)
        _age(ws, started=181, last=11)
        await session(worker, ctx).alarm()
        assert ws.closed is None
        assert abs(ctx.storage.alarm / 1000 - (time.time() + 169)) < 5
    asyncio.run(go())


def test_the_call_cap_holds_however_chatty(worker):
    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx)
        ws = server_socket(ctx)
        _age(ws, started=601, last=1)
        await session(worker, ctx).alarm()
        assert ws.closed == (1000, "time limit")
        assert ws.of("ended")[0]["reason"] == "The demo is capped at 10 minutes."
    asyncio.run(go())


# ── calls per address ────────────────────────────────────────────────────
def test_the_limit_answers_204_until_it_is_reached(worker, monkeypatch):
    monkeypatch.setenv("VOICE_DEMO_CALLS_PER_HOUR", "2")

    async def go():
        ctx = FakeCtx()
        limit = FakeRequest("https://voice-session/limit")
        assert (await session(worker, ctx).fetch(limit)).status == 204
        assert (await session(worker, ctx).fetch(limit)).status == 204
        resp = await session(worker, ctx).fetch(limit)
        assert resp.status == 101
        ws = server_socket(ctx, "busy")
        message = ws.of("limit")[0]["message"]
        assert "limit of 2 demo calls an hour" in message and "about 60 minutes" in message
        await session(worker, ctx).alarm()
        assert ws.closed == (1013, "busy")
    asyncio.run(go())


def test_old_calls_fall_out_of_the_count_and_nothing_lingers(worker, monkeypatch):
    monkeypatch.setenv("VOICE_DEMO_CALLS_PER_HOUR", "1")

    async def go():
        ctx = FakeCtx()
        limit = FakeRequest("https://voice-session/limit")
        ctx.storage.data["calls"] = json.dumps([time.time() - 3601])
        assert (await session(worker, ctx).fetch(limit)).status == 204

        ctx.storage.data["calls"] = json.dumps([time.time() - 3601])
        await session(worker, ctx).alarm()
        assert "calls" not in ctx.storage.data
    asyncio.run(go())


def test_addresses_are_hashed_and_ipv6_counts_per_slash_64(worker):
    a = worker._address_key("2001:db8:1:2:aaaa::1")
    b = worker._address_key("2001:db8:1:2:bbbb::2")
    c = worker._address_key("2001:db8:1:3::1")
    assert a == b != c
    v4 = worker._address_key("203.0.113.7")
    assert v4 != worker._address_key("203.0.113.8")
    assert "203.0.113.7" not in v4 and v4.startswith("ip-")


# ── health ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status,ok,phrase", [
    (200, True, None),
    (401, False, "rejected it"),
    (403, False, "rejected it"),
    (429, True, "rate limiting"),
    (503, False, "having trouble"),
    (400, False, "refused the check"),
])
def test_what_a_providers_answer_means(worker, status, ok, phrase):
    check = worker._judge_key("reply", "Cerebras", "CEREBRAS_API_KEY", status)
    assert check["ok"] is ok
    if phrase:
        assert phrase in check["why"]


def test_a_dead_key_says_exactly_what_to_run(worker):
    why = worker._judge_key("transcription", "Groq", "GROQ_API_KEY", 401)["why"]
    assert "GROQ_API_KEY" in why and "pywrangler secret put GROQ_API_KEY" in why


def test_a_working_key_without_the_model_is_not_healthy(worker):
    check = worker._judge_key("reply", "Cerebras", "CEREBRAS_API_KEY", 200,
                              {"other-model"}, ("gpt-oss-120b",))
    assert check["ok"] is False and "gpt-oss-120b" in check["why"]


class FakeFetchResponse:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def text(self):
        return json.dumps(self._body)


def _fake_probes(worker, monkeypatch, groq=200):
    calls = []

    async def fetch(url, init=None):
        calls.append(url)
        if "groq" in url:
            return FakeFetchResponse(groq, {"data": [{"id": "whisper-large-v3"}]})
        return FakeFetchResponse(200, {"data": [{"id": "gpt-oss-120b"}, {"id": "qwen-3.8-27b"}]})

    async def voice(self, **k):
        return b"audio"
    monkeypatch.setattr(worker, "js_fetch", fetch)
    monkeypatch.setattr(worker.WorkerEdgeTTS, "_synth_segment", voice)
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("CEREBRAS_API_KEY", "x")
    monkeypatch.setenv("GROQ_BASE_URL", "https://groq.test")
    return calls


def test_health_is_503_and_names_the_dead_secret(worker, monkeypatch):
    _fake_probes(worker, monkeypatch, groq=401)

    async def go():
        resp = await session(worker).fetch(FakeRequest("https://voice-session/health"))
        body = json.loads(resp.body)
        assert resp.status == 503
        assert body["ok"] is False and body["failed"] == ["GROQ_API_KEY"]
        assert "pywrangler secret put GROQ_API_KEY" in body["reason"]
    asyncio.run(go())


def test_health_is_200_when_everything_answers_and_never_shows_a_key(worker, monkeypatch):
    _fake_probes(worker, monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_supersecret")

    async def go():
        resp = await session(worker).fetch(FakeRequest("https://voice-session/health"))
        assert resp.status == 200 and json.loads(resp.body)["ok"] is True
        assert "gsk_supersecret" not in resp.body
    asyncio.run(go())


def test_health_probes_at_most_once_a_minute(worker, monkeypatch):
    calls = _fake_probes(worker, monkeypatch)

    async def go():
        ctx = FakeCtx()
        first = await session(worker, ctx).fetch(FakeRequest("https://voice-session/health"))
        n = len(calls)
        second = await session(worker, ctx).fetch(FakeRequest("https://voice-session/health"))
        assert len(calls) == n
        assert json.loads(first.body)["cached"] is False
        assert json.loads(second.body)["cached"] is True
    asyncio.run(go())


def test_a_missing_secret_is_reported_without_calling_anyone(worker, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    async def go():
        check = await worker._probe_key("transcription", "Groq", "GROQ_API_KEY", "https://x")
        assert check["ok"] is False and "not set" in check["why"]
    asyncio.run(go())


# ── test calls on saved settings ─────────────────────────────────────────
SAVED = {"agent_id": "a1", "name": "Night Desk", "persona": "amina", "role": "Bank Support",
         "system_prompt": "SAVED PROMPT", "first_message": "Night desk, this is the AI.",
         "voice": "en-NG-AbeoNeural", "speed": 1.2}
TICKET_URL = "https://voice-session/connect?agent=maya&last=0&ticket=tick.et"


def _config_api(worker, monkeypatch, status=200):
    asked = []

    async def fetch(url, init=None):
        asked.append(url)
        return FakeFetchResponse(status, SAVED)
    monkeypatch.setattr(worker, "js_fetch", fetch)
    monkeypatch.setenv("API_URL", "https://api.test/api")
    return asked


def test_a_ticket_call_answers_with_the_saved_agent(worker, monkeypatch):
    asked = _config_api(worker, monkeypatch)

    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx, TICKET_URL)
        ws = server_socket(ctx)
        assert asked == ["https://api.test/api/call-config?ticket=tick.et"]
        ready = ws.of("ready")[0]
        assert ready["agent"] == "Night Desk" and ready["own_agent"] is True
        assert ws.of("greeting")[0]["text"] == "Night desk, this is the AI."
        assert Stage.voices[-1] == "en-NG-AbeoNeural"

        await say(worker, ctx, ws)
        assert Stage.prompts[-1].startswith("SAVED PROMPT")
        assert Stage.voices[-1] == "en-NG-AbeoNeural"
    asyncio.run(go())


@pytest.mark.parametrize("status", [403, 500])
def test_a_refused_ticket_gets_no_call_and_no_substitute(worker, monkeypatch, status):
    _config_api(worker, monkeypatch, status)

    async def go():
        ctx = FakeCtx()
        resp = await connect(worker, ctx, TICKET_URL)
        assert resp.status == 101 and ctx.getWebSockets("call") == []
        ws = server_socket(ctx, "busy")
        assert ws.of("ended")[0]["reason"] == worker.TICKET_REFUSED_TEXT
        assert ws.of("greeting") == []
        await session(worker, ctx).alarm()
        assert ws.closed is not None
    asyncio.run(go())


def test_a_refused_ticket_does_not_hold_the_line(worker, monkeypatch):
    _config_api(worker, monkeypatch, 403)

    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx, TICKET_URL)
        resp = await connect(worker, ctx)
        assert resp.status == 101 and len(ctx.getWebSockets("call")) == 1
    asyncio.run(go())


def test_with_no_api_url_a_ticket_is_refused_not_ignored(worker, monkeypatch):
    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx, TICKET_URL)
        assert ctx.getWebSockets("call") == []
    asyncio.run(go())


def test_the_next_demo_call_on_that_line_is_not_the_customers_agent(worker, monkeypatch):
    _config_api(worker, monkeypatch)

    async def go():
        ctx = FakeCtx()
        await connect(worker, ctx, TICKET_URL)
        ws = server_socket(ctx)
        await session(worker, ctx).webSocketClose(ws, 1000, "bye", True)
        ws.closed = (1000, "bye")
        assert "saved" not in ctx.storage.data

        await connect(worker, ctx)
        assert server_socket(ctx).of("ready")[0]["agent"] == "Maya"
    asyncio.run(go())


# ── the model client ─────────────────────────────────────────────────────
def _capture_cerebras(worker, monkeypatch, replies):
    import httpx

    seen = []
    queue = list(replies)

    def handler(request):
        seen.append(json.loads(request.content))
        status, body = queue.pop(0)
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **k: real(transport=httpx.MockTransport(handler), **k))
    monkeypatch.setenv("CEREBRAS_API_KEY", "x")
    monkeypatch.delenv("CEREBRAS_MODEL", raising=False)
    monkeypatch.delenv("CEREBRAS_FAST_MODEL", raising=False)
    monkeypatch.delenv("CEREBRAS_FAST_MAX_WORDS", raising=False)
    return seen


def _ok(text, finish="stop"):
    return 200, {"choices": [{"message": {"content": text}, "finish_reason": finish}]}


def test_short_turns_take_the_fast_model_with_reasoning_off(worker, monkeypatch):
    seen = _capture_cerebras(worker, monkeypatch, [_ok("Yes.")])
    text = asyncio.run(worker.CerebrasLLM().generate(
        [{"role": "user", "content": "yes please"}], system="sys"))
    assert text == "Yes." and seen[0]["model"] == "qwen-3.8-27b"
    assert seen[0]["reasoning_effort"] == "none"
    assert seen[0]["messages"][0] == {"role": "system", "content": "sys"}


def test_longer_turns_take_the_main_model(worker, monkeypatch):
    seen = _capture_cerebras(worker, monkeypatch, [_ok("Sure.")])
    asyncio.run(worker.CerebrasLLM().generate(
        [{"role": "user", "content": "I would like to book a table"}]))
    assert seen[0]["model"] == "gpt-oss-120b" and "reasoning_effort" not in seen[0]


def test_running_out_of_tokens_retries_once_with_more_room(worker, monkeypatch):
    seen = _capture_cerebras(worker, monkeypatch, [_ok("", "length"), _ok("Done.")])
    llm = worker.CerebrasLLM(max_tokens=100)
    assert asyncio.run(llm.generate([{"role": "user", "content": "tell me a long thing now"}])) == "Done."
    assert [s["max_tokens"] for s in seen] == [100, 200]


@pytest.mark.parametrize("status,body,phrase", [
    (401, {}, "rejected the API key"),
    (429, {}, "rate limit"),
    (404, {}, "no model named"),
    (500, {}, "returned 500"),
    (200, {"choices": []}, "Unexpected"),
    (200, {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}, "empty reply"),
])
def test_every_way_the_model_can_fail_raises(worker, monkeypatch, status, body, phrase):
    _capture_cerebras(worker, monkeypatch, [(status, body)])
    with pytest.raises(worker.CerebrasLLMError, match=phrase):
        asyncio.run(worker.CerebrasLLM().generate(
            [{"role": "user", "content": "a turn of several words"}]))


def test_a_lost_voice_segment_is_tried_once_more(worker, monkeypatch):
    tries = []

    async def flaky(self, **k):
        tries.append(k)
        if len(tries) == 1:
            raise ConnectionError("Edge TTS socket errored mid-synthesis.")
        return b"audio"
    monkeypatch.setattr(worker.WorkerEdgeTTS, "_synth_segment", flaky)

    from cynea_africa.synthesizer.edge_tts import SynthesisRequest
    request = SynthesisRequest(text="Hello there.", voice="en-US-AriaNeural")
    assert asyncio.run(worker.WorkerEdgeTTS().synthesize(request)) == b"audio"
    assert len(tries) == 2


def test_errors_are_reported_over_the_runtimes_fetch_when_there_is_a_dsn(worker, monkeypatch):
    sent = []

    async def fetch(url, init=None):
        sent.append((url, init))
        return FakeFetchResponse(200, {})
    monkeypatch.setattr(worker, "js_fetch", fetch)

    async def go():
        await worker._report(ValueError("boom"), "turn")
        assert sent == []                                  # no DSN, nothing sent
        monkeypatch.setenv("SENTRY_DSN", "https://pub@o1.ingest.test/9")
        await worker._report(ValueError("boom"), "turn")
        url, init = sent[0]
        assert url == "https://o1.ingest.test/api/9/envelope/"
        assert "sentry_key=pub" in init["headers"]["X-Sentry-Auth"]
        assert '"service": "voice-worker"' in init["body"]
    asyncio.run(go())
