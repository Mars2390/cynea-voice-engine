/* Cynea — browser call client.
 *
 * Microphone in, Kwame out, over one WebSocket. Pairs with
 * cynea/voice_ws.py; the wire contract is documented there.
 *
 * The audio contract
 * ------------------
 * The server is sent 16 kHz mono signed 16-bit little-endian PCM, which is
 * what `AudioChunk(encoding="pcm16")` already means and what the Groq
 * transcriber already wraps in a WAV header. Nothing on either side decodes,
 * resamples or re-encodes an utterance.
 *
 * MediaRecorder would be about ten lines shorter here and would produce
 * WebM/Opus, which needs ffmpeg on the server to become anything the engine
 * can read — and ffmpeg is not on a serverless function. So the capture path
 * is an AudioWorklet that hands back raw frames instead.
 *
 * Turn-taking
 * -----------
 * This side decides when a turn ends, because it is already measuring
 * loudness every frame to draw the meter and therefore knows about silence
 * before a round trip could report it. A turn ends after ~900ms under the
 * noise floor, and the floor is measured from the room rather than assumed:
 * a hard threshold that works in a quiet office cuts every sentence in half
 * in a cafe.
 */
'use strict';

(function () {
  const RATE = 16000;

  // Turn-taking, in seconds and multiples of the measured noise floor.
  const SILENCE_HANG = 0.9;    // quiet this long ends the turn
  const MIN_UTTERANCE = 0.35;  // shorter than this is a cough, not a turn
  const MAX_UTTERANCE = 25;    // hard stop; the server caps at 30
  const OPEN_GATE = 2.2;       // × noise floor to count as speech
  const SHUT_GATE = 1.5;       // × noise floor to stop counting (hysteresis)
  const CALIBRATE_S = 0.6;     // room measured before the first turn

  const el = (id) => document.getElementById(id);

  const ui = {
    start: el('call-start'), stop: el('call-stop'),
    state: el('call-state'), dot: el('call-dot'),
    meter: el('call-meter'), timer: el('call-timer'),
    tape: el('call-tape'), agent: el('call-agent'),
    hint: el('call-hint'), bars: [],
  };

  let ws = null, ctx = null, stream = null, node = null, source = null;
  let buf = [], speaking = false, quietFor = 0, voicedFor = 0;
  let floor = 0.01, calibrating = 0, calibSum = 0, calibN = 0;
  let started = 0, tick = null, agentBusy = false;
  let queue = [], playing = null;

  function socketURL() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const override = localStorage.getItem('cynea_api');
    let base;
    if (override) {
      base = override.replace(/^http/, 'ws').replace(/\/$/, '');
    } else {
      const h = location.hostname;
      const local = h === 'localhost' || h === '127.0.0.1' || h === '' ||
                    location.protocol === 'file:';
      base = local ? 'ws://127.0.0.1:8000' : `${proto}//${location.host}/api`;
    }
    const agent = new URLSearchParams(location.search).get('agent') || 'kwame';
    return `${base}/voice?agent=${encodeURIComponent(agent)}`;
  }

  // ── transcript ─────────────────────────────────────────────────────────
  function say(who, text, opts = {}) {
    const empty = ui.tape.querySelector('.lc-empty');
    if (empty) empty.remove();
    const p = document.createElement('p');
    p.className = 'lc-line lc-' + who + (opts.pending ? ' lc-pending' : '');
    const b = document.createElement('b');
    b.textContent = who === 'you' ? 'You' : (ui.agent.textContent || 'Agent');
    p.appendChild(b);
    p.appendChild(document.createTextNode(text));
    ui.tape.appendChild(p);
    ui.tape.scrollTop = ui.tape.scrollHeight;
    return p;
  }

  function note(text, kind) {
    const p = document.createElement('p');
    p.className = 'lc-note' + (kind ? ' lc-' + kind : '');
    p.textContent = text;
    ui.tape.appendChild(p);
    ui.tape.scrollTop = ui.tape.scrollHeight;
  }

  function setState(text, cls) {
    ui.state.textContent = text;
    ui.dot.className = 'lc-dot' + (cls ? ' ' + cls : '');
  }

  // ── playback ───────────────────────────────────────────────────────────
  // One at a time and in order: two replies overlapping is the single most
  // unconvincing thing a voice demo can do.
  function enqueue(b64) {
    queue.push(b64);
    if (!playing) next();
  }

  function next() {
    const b64 = queue.shift();
    if (!b64) {
      playing = null;
      agentBusy = false;
      if (ws && ws.readyState === 1) setState('Listening', 'live');
      return;
    }
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: 'audio/mpeg' }));
    const a = new Audio(url);
    playing = a;
    agentBusy = true;
    setState('Speaking', 'speaking');
    a.onended = a.onerror = () => { URL.revokeObjectURL(url); next(); };
    a.play().catch(() => { URL.revokeObjectURL(url); next(); });
  }

  function stopPlayback() {
    queue = [];
    if (playing) { try { playing.pause(); } catch (e) {} playing = null; }
    agentBusy = false;
  }

  // ── capture ────────────────────────────────────────────────────────────
  const WORKLET = `
    class Tap extends AudioWorkletProcessor {
      process(inputs) {
        const ch = inputs[0] && inputs[0][0];
        if (ch) this.port.postMessage(ch.slice(0));
        return true;
      }
    }
    registerProcessor('tap', Tap);
  `;

  async function openMic() {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,   // the agent's own voice must not be heard
        noiseSuppression: true,   // as the next turn
        autoGainControl: true,
      },
    });

    // Ask for 16 kHz directly. Where the browser refuses (Safari pins to the
    // hardware rate) the frames are decimated below rather than shipped at
    // the wrong rate, which would transcribe as chipmunk noise.
    ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: RATE });
    await ctx.audioWorklet.addModule(
      URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' })));

    source = ctx.createMediaStreamSource(stream);
    node = new AudioWorkletNode(ctx, 'tap');
    source.connect(node);
    // Not connected to the destination: routing the mic to the speakers is
    // a feedback loop, and the worklet runs without a sink anyway.

    calibrating = Math.ceil(CALIBRATE_S * ctx.sampleRate);
    calibSum = 0; calibN = 0;
    node.port.onmessage = (e) => onFrames(e.data);
  }

  function onFrames(frame) {
    const ratio = ctx.sampleRate / RATE;
    let rms = 0;
    for (let i = 0; i < frame.length; i++) rms += frame[i] * frame[i];
    rms = Math.sqrt(rms / frame.length);

    // Measure the room before trusting the gate.
    if (calibrating > 0) {
      calibrating -= frame.length;
      calibSum += rms; calibN++;
      if (calibrating <= 0) {
        floor = Math.max(0.004, (calibSum / Math.max(1, calibN)) * 1.6);
        ui.hint.textContent = 'Just start talking — it answers when you stop.';
      }
      return;
    }

    meter(rms);

    // Don't record the agent talking. Echo cancellation handles most of it,
    // but a speaker at volume still leaks, and a turn that transcribes the
    // agent's own sentence back to it derails the conversation immediately.
    if (agentBusy) return;

    const secs = frame.length / ctx.sampleRate;
    const loud = rms > floor * (speaking ? SHUT_GATE : OPEN_GATE);

    if (loud) {
      if (!speaking) { speaking = true; voicedFor = 0; setState('Listening', 'hearing'); }
      quietFor = 0;
      voicedFor += secs;
    } else if (speaking) {
      quietFor += secs;
    }

    if (speaking) {
      // Decimate to 16 kHz if the context ignored the requested rate, and
      // convert to the int16 the server is expecting.
      if (ratio > 1.01) {
        const out = new Int16Array(Math.floor(frame.length / ratio));
        for (let i = 0; i < out.length; i++) {
          const s = Math.max(-1, Math.min(1, frame[Math.floor(i * ratio)]));
          out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }
        buf.push(out);
      } else {
        const out = new Int16Array(frame.length);
        for (let i = 0; i < frame.length; i++) {
          const s = Math.max(-1, Math.min(1, frame[i]));
          out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }
        buf.push(out);
      }

      const held = buf.reduce((n, a) => n + a.length, 0) / RATE;
      if ((quietFor >= SILENCE_HANG && voicedFor >= MIN_UTTERANCE) ||
          held >= MAX_UTTERANCE) {
        flush(voicedFor >= MIN_UTTERANCE);
      } else if (quietFor >= SILENCE_HANG) {
        flush(false);                       // too short to be a turn: discard
      }
    }
  }

  function flush(send) {
    const chunks = buf;
    buf = []; speaking = false; quietFor = 0; voicedFor = 0;

    if (!send || !ws || ws.readyState !== 1) return;

    let total = 0;
    for (const c of chunks) total += c.length;
    const pcm = new Int16Array(total);
    let at = 0;
    for (const c of chunks) { pcm.set(c, at); at += c.length; }

    ws.send(pcm.buffer);
    setState('Thinking', 'thinking');
  }

  // ── level meter ────────────────────────────────────────────────────────
  function meter(rms) {
    if (!ui.bars.length) return;
    const level = Math.min(1, rms / Math.max(floor * 6, 0.02));
    for (let i = 0; i < ui.bars.length; i++) {
      const reach = (i + 1) / ui.bars.length;
      ui.bars[i].style.transform =
        'scaleY(' + (0.18 + 0.82 * Math.max(0, Math.min(1, (level - reach * 0.35) * 1.8))).toFixed(3) + ')';
    }
  }

  function clock() {
    const s = Math.floor((Date.now() - started) / 1000);
    ui.timer.textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }

  // ── the call ───────────────────────────────────────────────────────────
  async function start() {
    ui.start.disabled = true;
    setState('Connecting', 'thinking');
    ui.tape.innerHTML = '';

    try {
      await openMic();
    } catch (err) {
      ui.start.disabled = false;
      setState('Microphone blocked', 'bad');
      note(err && err.name === 'NotAllowedError'
        ? 'Microphone permission was declined. Allow it in the address bar, then start the call again.'
        : 'No microphone available: ' + (err && err.message ? err.message : err), 'bad');
      return;
    }

    ws = new WebSocket(socketURL());
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      started = Date.now();
      tick = setInterval(clock, 1000);
      ui.stop.hidden = false;
      ui.hint.textContent = 'Measuring the room…';
      setState('Connected', 'live');
    };

    ws.onmessage = (e) => {
      let m;
      try { m = JSON.parse(e.data); } catch (err) { return; }

      switch (m.type) {
        case 'ready':
          ui.agent.textContent = m.agent || 'Agent';
          break;
        case 'greeting':
        case 'reply':
          say('agent', m.text);
          break;
        case 'you':
          say('you', m.text);
          break;
        case 'audio':
          enqueue(m.data);
          break;
        case 'thinking':
          setState('Thinking', 'thinking');
          break;
        case 'idle':
          setState('Listening', 'live');
          note('Nothing was picked up there — try again a bit louder.');
          break;
        case 'interrupted':
          stopPlayback();
          break;
        case 'busy':
          setState('All lines busy', 'bad');
          note(m.message, 'bad');
          break;
        case 'ended':
          note(m.reason);
          stop();
          break;
        case 'error':
          setState('Error', 'bad');
          note(m.message, 'bad');
          break;
      }
    };

    ws.onerror = () => {
      setState('Cannot reach the server', 'bad');
      note('The call server did not answer. If you are running locally, start it with ' +
           '`uvicorn cynea.api:app --port 8000`.', 'bad');
    };

    ws.onclose = () => { if (tick) teardown(); };
  }

  function teardown() {
    if (tick) { clearInterval(tick); tick = null; }
    stopPlayback();
    if (node) { try { node.port.onmessage = null; node.disconnect(); } catch (e) {} node = null; }
    if (source) { try { source.disconnect(); } catch (e) {} source = null; }
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    if (ctx) { try { ctx.close(); } catch (e) {} ctx = null; }
    buf = []; speaking = false;
    ui.start.disabled = false;
    ui.stop.hidden = true;
    ui.hint.textContent = 'Press start and allow the microphone.';
    setState('Call ended', '');
    meter(0);
  }

  function stop() {
    if (ws && ws.readyState === 1) {
      try { ws.send(JSON.stringify({ action: 'hangup' })); } catch (e) {}
      ws.close();
    }
    ws = null;
    teardown();
  }

  // ── wiring ─────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {
    ui.bars = Array.from(document.querySelectorAll('#call-meter i'));
    ui.start.addEventListener('click', start);
    ui.stop.addEventListener('click', stop);
    addEventListener('beforeunload', () => { if (ws) stop(); });

    if (!navigator.mediaDevices || !window.AudioWorkletNode) {
      ui.start.disabled = true;
      setState('Not supported here', 'bad');
      note('This browser has no AudioWorklet. Chrome, Edge, Firefox and Safari 14.1+ all work.', 'bad');
    }
  });
})();
