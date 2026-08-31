"""Cynea Voice Engine — local microphone and speaker I/O.

For testing a conversation on a laptop. **Not** part of the call path: on
a real call, audio arrives from the carrier and the reply goes back down
the same socket. Playing a reply through the server's speakers would mean
the caller hears nothing and whoever walks past the rack hears everything.

    from cynea.audio import record, play, is_available

    chunk = record(seconds=5)     # -> AudioChunk, 16 kHz mono pcm16
    play(mp3_bytes)               # blocks until finished

Dependencies are optional and imported lazily, so importing `cynea` on a
server with no sound card costs nothing:

    pip install sounddevice soundfile     # capture + playback
    ffmpeg on PATH                        # to decode the MP3 that TTS returns
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import sys
from typing import Optional

from cynea.models import AudioChunk

log = logging.getLogger("cynea.audio")

SAMPLE_RATE = 16000       # what Whisper wants
CHANNELS = 1


class AudioDeviceError(RuntimeError):
    """No usable microphone or speaker."""


_INSTALL_HINT = (
    "Local audio needs sounddevice:\n"
    "    pip install sounddevice soundfile\n"
    "On Linux you may also need PortAudio: apt install libportaudio2"
)


# ----------------------------------------------------------------------
# Capability probing
# ----------------------------------------------------------------------

def _sd():
    try:
        import sounddevice
        return sounddevice
    except (ImportError, OSError) as exc:
        raise AudioDeviceError(f"{_INSTALL_HINT}\n(original error: {exc})") from exc


def is_available() -> dict:
    """Report what this machine can actually do, without raising.

    Returns {"input": bool, "output": bool, "ffmpeg": bool, "detail": str}
    so a caller can degrade gracefully rather than crash on a headless box.
    """
    result = {"input": False, "output": False,
              "ffmpeg": shutil.which("ffmpeg") is not None, "detail": ""}
    try:
        sd = _sd()
        devices = sd.query_devices()
        result["input"] = any(d["max_input_channels"] > 0 for d in devices)
        result["output"] = any(d["max_output_channels"] > 0 for d in devices)
        result["detail"] = f"{len(devices)} devices"
    except AudioDeviceError as exc:
        result["detail"] = str(exc).splitlines()[0]
    except Exception as exc:  # pragma: no cover - driver-specific
        result["detail"] = f"{type(exc).__name__}: {exc}"
    return result


# ----------------------------------------------------------------------
# Recording
# ----------------------------------------------------------------------

def record(seconds: float = 5.0, sample_rate: int = SAMPLE_RATE,
           show_progress: bool = True) -> AudioChunk:
    """Capture from the default microphone and return 16 kHz mono pcm16.

    Blocks for `seconds`. Raises AudioDeviceError when there is no input
    device -- which is the right outcome, because returning silence would
    look like a caller who said nothing.
    """
    sd = _sd()
    import numpy as np

    if not any(d["max_input_channels"] > 0 for d in sd.query_devices()):
        raise AudioDeviceError("No microphone found on this machine.")

    frames = int(seconds * sample_rate)
    if show_progress:
        print(f"  recording {seconds:.0f}s ", end="", flush=True)

    buffer = sd.rec(frames, samplerate=sample_rate, channels=CHANNELS, dtype="int16")

    if show_progress:
        import time
        for _ in range(int(seconds * 2)):
            time.sleep(0.5)
            print(".", end="", flush=True)
        print(" done")
    sd.wait()

    pcm = np.asarray(buffer, dtype="int16").tobytes()

    peak = int(np.abs(np.asarray(buffer, dtype="int32")).max()) if frames else 0
    if peak < 200:
        # Full-scale int16 is 32767. Under ~200 the mic is muted, absent,
        # or nobody spoke. Say so rather than let Whisper hallucinate
        # filler out of noise.
        log.warning("captured audio is near-silent (peak %d/32767)", peak)

    return AudioChunk(data=pcm, sample_rate=sample_rate,
                      channels=CHANNELS, encoding="pcm16")


def peak_level(chunk: AudioChunk) -> int:
    """Loudest sample, 0..32767. Useful for 'did the mic actually work?'."""
    import numpy as np
    if not chunk or not chunk.data:
        return 0
    return int(np.abs(np.frombuffer(chunk.data, dtype="<i2").astype("int32")).max())


# ----------------------------------------------------------------------
# Playback
# ----------------------------------------------------------------------

def _decode_to_pcm(data: bytes, fmt: str = "mp3"):
    """Decode compressed audio to (numpy int16 array, sample_rate)."""
    import numpy as np

    try:
        from pydub import AudioSegment
    except ImportError as exc:
        raise AudioDeviceError(
            "Playing MP3 needs pydub:  pip install pydub  (and ffmpeg on PATH)"
        ) from exc

    if not shutil.which("ffmpeg"):
        raise AudioDeviceError(
            "ffmpeg is not on PATH, so the MP3 that TTS returned cannot be "
            "decoded.\n  Windows: winget install Gyan.FFmpeg\n"
            "  macOS:   brew install ffmpeg\n  Linux:   apt install ffmpeg"
        )

    seg = AudioSegment.from_file(io.BytesIO(data), format=fmt)
    seg = seg.set_channels(1).set_sample_width(2)
    return np.frombuffer(seg.raw_data, dtype="<i2"), seg.frame_rate


def play(data: bytes, fmt: str = "mp3", blocking: bool = True) -> float:
    """Play audio through the default speaker. Returns duration in seconds.

    `data` is whatever the TTS provider returned -- MP3 from edge_tts and
    ElevenLabs, WAV if you asked for it.
    """
    if not data:
        return 0.0

    samples, rate = _decode_to_pcm(data, fmt)
    duration = len(samples) / float(rate)

    sd = _sd()
    if not any(d["max_output_channels"] > 0 for d in sd.query_devices()):
        raise AudioDeviceError("No output device found on this machine.")

    sd.play(samples, samplerate=rate)
    if blocking:
        sd.wait()
    return duration


def save(data: bytes, path: str) -> str:
    """Write audio bytes to disk. The fallback when there is no speaker."""
    with open(path, "wb") as f:
        f.write(data)
    return path


def describe_devices() -> str:
    """Human-readable device list, for a diagnostic banner."""
    try:
        sd = _sd()
        devices = sd.query_devices()
    except AudioDeviceError as exc:
        return str(exc)

    lines = []
    try:
        din, dout = sd.default.device
    except Exception:
        din = dout = None
    for i, d in enumerate(devices):
        role = []
        if d["max_input_channels"] > 0:
            role.append("in")
        if d["max_output_channels"] > 0:
            role.append("out")
        if not role:
            continue
        mark = " *" if i in (din, dout) else "  "
        lines.append(f"{mark}[{i:2d}] {'/'.join(role):7s} {d['name'][:44]}")
    return "\n".join(lines) or "no audio devices"


__all__ = ["record", "play", "save", "is_available", "peak_level",
           "describe_devices", "AudioDeviceError", "SAMPLE_RATE"]
