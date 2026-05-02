"""Cynea Africa — Edge TTS Synthesizer (SSML-aware).

Microsoft Edge TTS over the free Azure browser endpoint, with first-class
support for SSML-style prosody control (rate / pitch / volume) and
natural pauses.

Why we parse SSML ourselves
---------------------------
The `edge-tts` Python library does NOT parse SSML — if you pass `<speak>`
tags as text, Edge speaks the XML literally ("speak prosody rate zero
point nine break time two hundred milliseconds"). Verified empirically.

To still expose an SSML surface (the API our personas were engineered
against), this module:

  1. Parses SSML into a flat list of `(text, prosody, post_pause_ms)`
     segments via the stdlib XML parser.
  2. Synthesises each segment through `edge_tts.Communicate(...)` with
     the segment's rate / pitch / volume mapped to Edge's parameter
     conventions (rate as "+0%", pitch as "+0Hz", volume as "+0%").
  3. Concatenates the resulting MP3 byte streams. MP3 frames are
     self-contained, so byte-level concatenation works on every
     mainstream player (browsers, ffplay, VLC, html5 audio).
  4. Approximates `<break>` durations by appending trailing punctuation
     to the prior segment (Edge respects "..." and ", " as pauses).
     This avoids a hard dependency on ffmpeg / pydub for silence
     injection.

The cost of this design is one round-trip per text segment (~200-500 ms
each on a healthy connection). For real-time call handling, prefer
caching the welcome message and other common phrases.

Public API (unchanged):
    EdgeTTSSynthesizer.synthesize(request) -> bytes
    EdgeTTSSynthesizer.health_check(timeout) -> dict

New static helper:
    EdgeTTSSynthesizer.text_to_ssml(text) -> str
        Apply auto-detection rules (questions, apologies, numerics,
        fillers, ALL-CAPS emphasis) and return an SSML string.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import tempfile
import xml.etree.ElementTree as ET
from typing import List, Optional

from cynea.models import SynthesisRequest


# ----------------------------------------------------------------------
# Network probe target — Microsoft's free TTS endpoint.
# ----------------------------------------------------------------------

_EDGE_TTS_HOST = "speech.platform.bing.com"
_EDGE_TTS_PORT = 443

# Sniff exception messages to distinguish network errors that don't
# subclass ConnectionError from real synthesis failures.
_NETWORK_HINTS = (
    "getaddrinfo", "name or service", "name resolution", "no address",
    "ssl", "tls", "handshake", "timed out", "timeout", "connection",
    "unreachable", "no route",
    "winerror 10061", "winerror 11001", "winerror 11002", "winerror 11003",
    "no audio",
)


# ----------------------------------------------------------------------
# SSML auto-detection rules (engineered for the personas in
# cynea_africa/persona/*). All rules are case-insensitive on intent.
# ----------------------------------------------------------------------

# Filled pauses that get a small break inserted after them when they
# occur at sentence start. Common across Ghanaian, Kenyan, and
# American/British English.
_FIRST_TOKEN_FILLERS = frozenset({"um", "ah", "mm", "hmm", "erm", "uh"})

# Disfluency / self-correction triggers — get a short break BEFORE.
_PRE_BREAK_TRIGGERS = frozenset({
    "actually", "wait", "sorry", "hold", "well",
})

# Apology stems. When a sentence opens with one of these, the whole
# sentence gets slowed and softened.
_APOLOGY_PREFIXES = ("sorry", "pole", "my apologies", "apologies", "forgive")


# ----------------------------------------------------------------------
# EdgeTTSSynthesizer
# ----------------------------------------------------------------------


class EdgeTTSSynthesizer:
    """Free TTS provider with SSML-style prosody control."""

    VOICES = {
        "en-GB-RyanNeural":  "British male, warm",
        "en-GB-SoniaNeural": "British female, warm",
        "en-GB-LibbyNeural": "British female, soft",
        "en-US-EricNeural":  "American male, professional",
        "en-US-AriaNeural":  "American female, natural",
        "en-ZA-LeahNeural":  "South African female, warm",
        "en-ZA-LukeNeural":  "South African male, deep",
        "en-NG-EzinneNeural":"Nigerian female, natural",
        "en-NG-AbeoNeural":  "Nigerian male, deep",
    }

    def __init__(self, voice: str = "en-GB-RyanNeural"):
        self.voice = voice

    # ------------------------------------------------------------------
    # Public synth
    # ------------------------------------------------------------------

    async def synthesize(self, request: SynthesisRequest) -> bytes:
        """Synthesise `request.text` to MP3 bytes with prosody control.

        If the text is plain, we auto-generate SSML and split into
        segments. If the text already contains `<speak>` / `<prosody>` /
        `<break>` tags, we parse it as SSML directly.

        Raises:
            ImportError:     edge-tts not installed.
            ConnectionError: no network / Microsoft endpoint unreachable.
            RuntimeError:    synthesis failed for some other reason.
        """
        edge_tts = self._import_edge_tts()  # raises ImportError

        text = (request.text or "").strip()
        if not text:
            return b""

        ssml = text if self._looks_like_ssml(text) else self.text_to_ssml(text)

        try:
            segments = self._parse_ssml(ssml)
        except ET.ParseError:
            # Malformed SSML — fall back to a single-segment plain synth
            # rather than failing the call.
            segments = [_Segment(kind="text", text=text, prosody={}, ms=0)]

        if not segments:
            return b""

        # Roll consecutive break durations into the trailing-punctuation
        # of the previous text segment (Edge gives us natural pauses
        # for free at sentence ends; we just nudge it with extra "...").
        segments = self._fold_breaks_into_text(segments)

        # Drop empty segments + breaks (we already folded breaks above).
        segments = [s for s in segments if s.kind == "text" and s.text.strip()]

        if not segments:
            return b""

        voice = request.voice or self.voice
        chunks = []
        for seg in segments:
            mp3 = await self._synth_segment(
                edge_tts=edge_tts,
                text=seg.text,
                voice=voice,
                rate=self._merge_rate(seg.prosody.get("rate"), request.speed),
                pitch=self._ssml_pitch_to_edge(seg.prosody.get("pitch")),
                volume=self._ssml_volume_to_edge(seg.prosody.get("volume")),
            )
            if mp3:
                chunks.append(mp3)

        if not chunks:
            raise RuntimeError("Edge TTS produced no audio for any segment.")

        return b"".join(chunks)

    # ------------------------------------------------------------------
    # Health check (unchanged interface)
    # ------------------------------------------------------------------

    async def health_check(self, *, timeout: float = 2.0) -> dict:
        result = {"installed": False, "reachable": False, "ready": False, "reason": ""}
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
            result["reason"] = f"Cannot reach {_EDGE_TTS_HOST}: {exc}."
            return result

        result["ready"] = True
        return result

    # ------------------------------------------------------------------
    # SSML auto-detection (public so callers can preview the SSML output)
    # ------------------------------------------------------------------

    @staticmethod
    def text_to_ssml(text: str) -> str:
        """Apply auto-detection rules and return an SSML string.

        Rules applied (in order):

          - Sentence ending with `?`        -> wrap in `<prosody pitch="+10%">`
          - Sentence starts "Sorry" / "Pole" -> wrap in `<prosody rate="0.85" volume="soft">`
          - Tokens containing digits        -> per-word `<prosody rate="0.9">`
          - ALL-CAPS words (>=3 letters)    -> per-word `<prosody pitch="+5%">`
          - First-token fillers (um/ah/mm)  -> `<break time="150ms"/>` after
          - Disfluency triggers (actually,
            wait, well, hold)               -> `<break time="100ms"/>` before
          - End of sentence with `.`/`!`    -> `<break time="180ms"/>`
          - End of sentence with `?`        -> `<break time="220ms"/>`
        """
        text = (text or "").strip()
        if not text:
            return "<speak></speak>"

        sentences = _split_sentences(text)
        out: List[str] = ["<speak>"]
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            inner = _sentence_to_ssml_inner(sent)

            # Sentence-level prosody
            attrs = {}
            if sent.endswith("?"):
                attrs["pitch"] = "+10%"
            sent_lower = sent.lower()
            if any(sent_lower.startswith(p) for p in _APOLOGY_PREFIXES):
                attrs["rate"] = "0.85"
                attrs["volume"] = "soft"

            if attrs:
                attr_str = " ".join(f'{k}="{v}"' for k, v in attrs.items())
                out.append(f"<prosody {attr_str}>{inner}</prosody>")
            else:
                out.append(inner)

            # Post-sentence break
            if sent.endswith("?"):
                out.append('<break time="220ms"/>')
            elif sent.endswith(("!",)):
                out.append('<break time="180ms"/>')
            elif sent.endswith("."):
                out.append('<break time="180ms"/>')
            else:
                out.append('<break time="120ms"/>')

        out.append("</speak>")
        return "".join(out)

    # ------------------------------------------------------------------
    # SSML parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_ssml(text: str) -> bool:
        return ("<speak" in text) or ("<prosody" in text) or ("<break" in text)

    def _parse_ssml(self, ssml: str) -> List["_Segment"]:
        """Flatten an SSML string into ordered segments + breaks."""
        ssml = ssml.strip()
        if not ssml.lstrip().startswith("<speak"):
            ssml = f"<speak>{ssml}</speak>"
        root = ET.fromstring(ssml)
        out: List[_Segment] = []
        self._walk(root, [], out)
        return out

    def _walk(self, elem, prosody_stack: list, out: List["_Segment"]) -> None:
        if elem.text and elem.text.strip():
            out.append(_Segment(
                kind="text", text=elem.text, prosody=self._merge_prosody(prosody_stack), ms=0,
            ))
        for child in elem:
            tag = self._localname(child.tag)
            if tag == "break":
                ms = self._parse_time(child.get("time", "100ms"))
                out.append(_Segment(kind="break", text="", prosody={}, ms=ms))
            elif tag == "prosody":
                attrs = {k: child.get(k) for k in ("rate", "pitch", "volume") if child.get(k)}
                self._walk(child, prosody_stack + [attrs], out)
            else:
                # Unknown tag — walk children but ignore the wrapper.
                self._walk(child, prosody_stack, out)
            if child.tail and child.tail.strip():
                out.append(_Segment(
                    kind="text", text=child.tail, prosody=self._merge_prosody(prosody_stack), ms=0,
                ))

    @staticmethod
    def _localname(tag: str) -> str:
        # Strip namespace prefix if any
        return tag.split("}")[-1]

    @staticmethod
    def _merge_prosody(stack: list) -> dict:
        merged: dict = {}
        for layer in stack:
            merged.update(layer)
        return merged

    @staticmethod
    def _parse_time(value: str) -> int:
        """Parse a `<break time="...">` attribute to milliseconds."""
        if not value:
            return 100
        v = value.strip().lower()
        if v.endswith("ms"):
            try:
                return max(0, int(float(v[:-2])))
            except ValueError:
                return 100
        if v.endswith("s"):
            try:
                return max(0, int(float(v[:-1]) * 1000))
            except ValueError:
                return 1000
        try:
            return max(0, int(float(v)))
        except ValueError:
            return 100

    # ------------------------------------------------------------------
    # Break -> punctuation folding
    # ------------------------------------------------------------------

    @staticmethod
    def _fold_breaks_into_text(segments: List["_Segment"]) -> List["_Segment"]:
        """Approximate <break> durations by appending punctuation to the
        previous text segment. Edge TTS pauses naturally on punctuation:

          break   ms <= 100  -> nothing (segment-end silence covers it)
          break   ms <= 250  -> ", "
          break   ms <= 500  -> ". "
          break   ms <= 800  -> ". . "
          break   ms >  800  -> ". . . "
        """
        if not segments:
            return segments
        out: List[_Segment] = []
        for seg in segments:
            if seg.kind == "break":
                if not out or out[-1].kind != "text":
                    continue
                pad = _break_to_punctuation(seg.ms)
                if pad:
                    last = out[-1]
                    # Don't double-stack punctuation.
                    base = last.text.rstrip()
                    if base.endswith((".", "?", "!", ",", ";", ":")):
                        # Already has terminal punctuation; just add the
                        # extra dots/commas after it.
                        out[-1] = _Segment(
                            kind="text", text=last.text + pad,
                            prosody=last.prosody, ms=0,
                        )
                    else:
                        out[-1] = _Segment(
                            kind="text", text=last.text + pad,
                            prosody=last.prosody, ms=0,
                        )
            else:
                out.append(seg)
        return out

    # ------------------------------------------------------------------
    # Per-segment synthesis
    # ------------------------------------------------------------------

    async def _synth_segment(
        self,
        *,
        edge_tts,
        text: str,
        voice: str,
        rate: str,
        pitch: str,
        volume: str,
    ) -> bytes:
        """Synthesise one segment to MP3 bytes."""
        temp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                temp_path = f.name

            communicate = edge_tts.Communicate(
                text=text, voice=voice, rate=rate, pitch=pitch, volume=volume,
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
                return f.read()
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # SSML attribute -> Edge parameter conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_rate(ssml_rate: Optional[str], base_speed: float) -> str:
        """Combine the SSML rate (multiplicative, e.g. "0.9") with the
        request's `speed` field (also multiplicative). Both default to 1.0.
        Edge expects "+N%" or "-N%".
        """
        try:
            base = float(base_speed) if base_speed is not None else 1.0
        except (TypeError, ValueError):
            base = 1.0

        ssml_factor = _ssml_rate_to_factor(ssml_rate)
        combined = base * ssml_factor
        # Clamp to a safe Edge band.
        combined = max(0.5, min(2.0, combined))
        delta = int(round((combined - 1.0) * 100))
        sign = "+" if delta >= 0 else ""
        return f"{sign}{delta}%"

    @staticmethod
    def _ssml_pitch_to_edge(value: Optional[str]) -> str:
        """Convert SSML pitch ("+10%", "-5%", "+50Hz", "high") to
        Edge's "+NHz" format. Heuristic: 1% ≈ 2 Hz around a 200 Hz
        baseline; that's close enough for our small variations.
        """
        if not value:
            return "+0Hz"
        v = value.strip().lower()
        try:
            if v.endswith("hz"):
                hz = int(round(float(v[:-2])))
            elif v.endswith("%"):
                pct = float(v.rstrip("%").lstrip("+"))
                if v.startswith("-"):
                    pct = -float(v.lstrip("-").rstrip("%"))
                hz = int(round(pct * 2.0))
            elif v in ("x-low", "low"):
                hz = -20
            elif v in ("medium", "default"):
                hz = 0
            elif v in ("high", "x-high"):
                hz = 20
            else:
                hz = int(round(float(v)))
        except (TypeError, ValueError):
            hz = 0
        # Edge accepts roughly +/-100Hz comfortably.
        hz = max(-100, min(100, hz))
        sign = "+" if hz >= 0 else ""
        return f"{sign}{hz}Hz"

    @staticmethod
    def _ssml_volume_to_edge(value: Optional[str]) -> str:
        """Convert SSML volume ('soft', 'loud', 'medium', '+0%') to
        Edge's '+N%' format."""
        if not value:
            return "+0%"
        v = value.strip().lower()
        named = {
            "silent":   "-100%",
            "x-soft":   "-40%",
            "soft":     "-25%",
            "medium":   "+0%",
            "default":  "+0%",
            "loud":     "+25%",
            "x-loud":   "+50%",
        }
        if v in named:
            return named[v]
        if v.endswith("%"):
            try:
                pct = float(v.rstrip("%").lstrip("+"))
                if v.startswith("-"):
                    pct = -float(v.lstrip("-").rstrip("%"))
                pct = int(round(max(-100.0, min(100.0, pct))))
                sign = "+" if pct >= 0 else ""
                return f"{sign}{pct}%"
            except (TypeError, ValueError):
                pass
        return "+0%"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _import_edge_tts():
        try:
            import edge_tts  # noqa: F401
            return edge_tts
        except ImportError as exc:
            raise ImportError(
                "edge-tts is not installed. Install with: pip install edge-tts"
            ) from exc

    @staticmethod
    def _looks_like_network_error(exc: BaseException) -> bool:
        msg = str(exc).lower()
        return any(hint in msg for hint in _NETWORK_HINTS)


# ----------------------------------------------------------------------
# Module helpers
# ----------------------------------------------------------------------


class _Segment:
    """Internal flat representation of an SSML element."""

    __slots__ = ("kind", "text", "prosody", "ms")

    def __init__(self, *, kind: str, text: str, prosody: dict, ms: int):
        self.kind = kind            # "text" or "break"
        self.text = text
        self.prosody = prosody      # {"rate": "0.9", "pitch": "+10%", "volume": "soft"}
        self.ms = ms                # only set when kind == "break"

    def __repr__(self) -> str:
        if self.kind == "break":
            return f"_Segment(break, ms={self.ms})"
        return f"_Segment(text={self.text!r}, prosody={self.prosody})"


def _split_sentences(text: str) -> List[str]:
    """Naive sentence splitter that preserves trailing punctuation.

    We don't use NLTK or spaCy here on purpose — the caller's prompts are
    short, mostly under three sentences, and avoiding a heavy NLP dep
    matters more than getting edge cases like "Mr. Smith" right.
    """
    parts = re.split(r"(?<=[.?!])\s+", text.strip())
    return [p for p in (p.strip() for p in parts) if p]


def _sentence_to_ssml_inner(sent: str) -> str:
    """Apply word-level SSML wrapping to a single sentence."""
    pieces: List[str] = []
    tokens = re.findall(r"\S+|\s+", sent)  # keep whitespace tokens
    seen_first_word = False

    for tok in tokens:
        if tok.isspace():
            pieces.append(tok)
            continue

        bare = tok.lower().strip(".,?!;:'\"()[]{}")

        # First-word filler (Um, Ah, Mm, Hmm, Erm, Uh) -> break AFTER.
        if not seen_first_word and bare in _FIRST_TOKEN_FILLERS:
            pieces.append(_xml_escape(tok))
            pieces.append(' <break time="150ms"/>')
            seen_first_word = True
            continue

        # Disfluency / pivot trigger -> break BEFORE.
        if seen_first_word and bare in _PRE_BREAK_TRIGGERS:
            pieces.append('<break time="100ms"/> ')

        seen_first_word = True

        # Numerics -> per-token slow rate.
        if re.search(r"\d", tok):
            pieces.append(f'<prosody rate="0.9">{_xml_escape(tok)}</prosody>')
            continue

        # ALL-CAPS emphasis (3+ alpha chars).
        clean_alpha = re.sub(r"[^A-Za-z]", "", tok)
        if len(clean_alpha) >= 3 and clean_alpha.isupper():
            pieces.append(f'<prosody pitch="+5%">{_xml_escape(tok)}</prosody>')
            continue

        pieces.append(_xml_escape(tok))

    return "".join(pieces)


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
    )


def _ssml_rate_to_factor(value: Optional[str]) -> float:
    """SSML rate values come in many shapes; reduce to a multiplier
    around 1.0. Defaults to 1.0 on parse failure."""
    if not value:
        return 1.0
    v = value.strip().lower()
    named = {
        "x-slow": 0.5, "slow": 0.75, "medium": 1.0,
        "default": 1.0, "fast": 1.25, "x-fast": 1.5,
    }
    if v in named:
        return named[v]
    if v.endswith("%"):
        try:
            pct = float(v.rstrip("%").lstrip("+"))
            if v.startswith("-"):
                pct = -float(v.lstrip("-").rstrip("%"))
            return 1.0 + pct / 100.0
        except (TypeError, ValueError):
            return 1.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 1.0


def _break_to_punctuation(ms: int) -> str:
    """Translate a break duration into trailing punctuation for the
    previous segment. Edge respects "..." and ", " as natural pauses."""
    if ms <= 100:
        return ""
    if ms <= 250:
        return ", "
    if ms <= 500:
        return ". "
    if ms <= 800:
        return ". . "
    return ". . . "


# ----------------------------------------------------------------------
# Provider registration
# ----------------------------------------------------------------------

try:
    from cynea.providers import register_tts
    register_tts("edge_tts", EdgeTTSSynthesizer)
except ImportError:
    pass
