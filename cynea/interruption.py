"""Cynea Voice Engine — Turn-taking and interruption management.

The InterruptionManager is the single source of truth for "is this audio
still wanted?" across the whole pipeline. Components (synthesizer, output
handler, LLM stream consumer) ask it before producing or sending bytes.

Design principles
-----------------
1. **Sequence-id cancellation.** Every assistant turn gets a monotonically
   increasing sequence id. A single set of "valid" ids drives every gate.
   Invalidating pending audio is one line: `sequence_ids = {-1}`. Reserved
   id `-1` is for system audio (welcome message, hold music) which must
   survive a barge-in.

2. **Word-count threshold.** A single short blip ("uh", "yeah", "mm") must
   not interrupt the agent. Default threshold is 3 words; below that, we
   either ignore the transcript or merge it into the current user turn.

3. **Grace period.** After the user stops speaking, we wait
   `incremental_delay` ms before emitting audio so we don't talk over the
   tail of their utterance. Disabled for the first two turns to keep the
   welcome message snappy.

4. **Backchanneling.** When the user pauses mid-utterance for >700 ms but
   hasn't finished, the manager can be polled for a backchannel candidate
   like "mm-hm" or "right" to keep the channel feeling alive.

5. **No threads, no locks.** All state is mutated from a single asyncio
   loop. Coordination is via the sequence-id set, the
   `interruption_event` (set on barge-in), and `audio_done_event`
   (set when the last queued chunk has been delivered).

Usage
-----
    im = InterruptionManager()
    seq = im.next_sequence_id()                 # at the start of an LLM turn
    ...                                          # LLM streams tokens
    if not im.is_valid(seq): break              # synthesizer checks each chunk
    status = im.audio_send_status(seq, history_len)
    # status is one of "send", "block", "wait"

    # On user interim transcript:
    im.on_interim_user_speech(transcript)
    if im.should_trigger_interruption(transcript, agent_speaking=True):
        im.fire_interruption()                  # invalidates pending audio
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional


_DEFAULT_BACKCHANNELS = (
    "Mm-hm.",
    "Right.",
    "Okay.",
    "I see.",
    "Got it.",
)

_DEFAULT_ACCIDENTAL_PHRASES = frozenset({
    "yeah", "yes", "no", "ok", "okay", "uh", "um", "mm", "mhm", "right",
    "sure", "thanks", "thank you", "sorry",
})


@dataclass
class InterruptionEvent:
    """One barge-in record. Used by the metrics dashboard."""
    user_started_at: float
    user_ended_at: Optional[float] = None
    recovered: bool = False


@dataclass
class InterruptionManager:
    """Coordinates turn-taking gates for one call.

    Attributes:
        word_threshold: Minimum word count in a user transcript before we
            treat it as a real interruption (default 3, per Bolna empirical
            tuning). Set to 0 to disable interruption entirely.
        grace_period_ms: How long to wait after the user stops speaking
            before emitting agent audio (helps with end-of-utterance jitter).
        backchannel_after_ms: After this many ms of user-speaking-but-
            paused, suggest a backchannel.
        accidental_phrases: Short phrases that even at >threshold words
            are still treated as not-an-interruption (e.g. "yes that's
            right yes" — three words but clearly an ack).
    """

    word_threshold: int = 3
    grace_period_ms: int = 700
    backchannel_after_ms: int = 800
    accidental_phrases: frozenset = field(default_factory=lambda: _DEFAULT_ACCIDENTAL_PHRASES)
    backchannel_pool: tuple = _DEFAULT_BACKCHANNELS

    # ── runtime state (do not set externally) ────────────────────────────
    _curr_sequence_id: int = 0
    _valid_ids: set = field(default_factory=lambda: {-1})
    _user_speaking: bool = False
    _user_started_at: float = 0.0
    _user_last_interim_at: float = 0.0
    _utterance_ended_at: float = 0.0
    _agent_speaking: bool = False
    _last_backchannel_at: float = 0.0
    _events: list = field(default_factory=list)
    _open_event: Optional[InterruptionEvent] = None
    _interruption_event: asyncio.Event = field(default_factory=asyncio.Event)

    # ====================================================================
    # Sequence-id management — the cancellation primitive
    # ====================================================================

    def next_sequence_id(self) -> int:
        """Allocate a fresh id for an outgoing assistant turn and mark it valid."""
        self._curr_sequence_id += 1
        self._valid_ids.add(self._curr_sequence_id)
        return self._curr_sequence_id

    def is_valid(self, sequence_id: int) -> bool:
        """Cheap check used by every audio-producing component."""
        return sequence_id in self._valid_ids

    def revalidate(self, sequence_id: int) -> None:
        """Re-add an id after a transient invalidation. Used when a turn is
        being recovered (e.g. caller said 'sorry, go ahead')."""
        self._valid_ids.add(sequence_id)

    def invalidate_pending(self) -> None:
        """Drop every in-flight assistant turn. Reserved id -1 survives."""
        self._valid_ids = {-1}

    # ====================================================================
    # Audio gate — what _process_output_loop should do with each chunk
    # ====================================================================

    def audio_send_status(self, sequence_id: int, history_length: int = 0) -> str:
        """Return one of 'send', 'block', 'wait' for a chunk.

        'block' means discard. 'wait' means sleep briefly and ask again.
        'send' means ship it. Welcome message (history_length <= 2)
        bypasses the grace period to keep first response snappy.
        """
        if sequence_id not in self._valid_ids:
            return "block"
        if self._user_speaking:
            return "wait"
        if history_length > 2 and self._utterance_ended_at:
            elapsed_ms = (time.monotonic() - self._utterance_ended_at) * 1000
            if elapsed_ms < self.grace_period_ms:
                return "wait"
        return "send"

    # ====================================================================
    # User-speech lifecycle (called by the transcriber loop)
    # ====================================================================

    def on_interim_user_speech(self, transcript: str) -> None:
        """Call this on every interim transcript event."""
        now = time.monotonic()
        if not self._user_speaking:
            self._user_speaking = True
            self._user_started_at = now
        self._user_last_interim_at = now
        self._utterance_ended_at = 0.0

    def on_final_user_speech(self, transcript: str) -> None:
        """Call this when the transcriber emits a final transcript or
        utterance-end signal."""
        self._user_speaking = False
        self._utterance_ended_at = time.monotonic()
        if self._open_event and self._open_event.user_ended_at is None:
            self._open_event.user_ended_at = self._utterance_ended_at
            self._open_event = None

    def on_agent_speech_started(self) -> None:
        self._agent_speaking = True

    def on_agent_speech_ended(self) -> None:
        self._agent_speaking = False

    # ====================================================================
    # Interruption decision
    # ====================================================================

    def should_trigger_interruption(self, transcript: str, *, agent_speaking: bool) -> bool:
        """Decide whether a partial user transcript counts as a barge-in.

        Returns False when:
        - the agent isn't speaking (nothing to interrupt)
        - word count is below threshold and not in accidental_phrases
        - threshold is set to 0 (interruption disabled)
        """
        if not agent_speaking or self.word_threshold <= 0:
            return False
        cleaned = (transcript or "").strip().lower()
        if not cleaned:
            return False
        if cleaned in self.accidental_phrases:
            return False
        word_count = len(cleaned.split())
        return word_count >= self.word_threshold

    def is_false_interruption(self, transcript: str, *, agent_speaking: bool) -> bool:
        """Mirror of should_trigger_interruption used on FINAL transcripts —
        when True, the assistant should NOT change its turn and the user
        text is still merged into history (handled by ConversationHistory)."""
        if not agent_speaking:
            return False
        cleaned = (transcript or "").strip().lower()
        if not cleaned:
            return True
        return (cleaned in self.accidental_phrases
                or len(cleaned.split()) < self.word_threshold)

    def fire_interruption(self) -> None:
        """Record an interruption and invalidate every pending response.

        Idempotent within a single user turn — calling it twice for the
        same utterance is fine; the second call is a no-op.
        """
        self.invalidate_pending()
        self._interruption_event.set()
        self._interruption_event.clear()

        if self._open_event is None:
            event = InterruptionEvent(
                user_started_at=self._user_started_at or time.monotonic(),
            )
            self._events.append(event)
            self._open_event = event

    def mark_recovery(self) -> None:
        """Mark the most recent open event as 'agent recovered after barge-in'."""
        for e in reversed(self._events):
            if not e.recovered:
                e.recovered = True
                return

    # ====================================================================
    # Backchanneling
    # ====================================================================

    def maybe_backchannel(self) -> Optional[str]:
        """If the user has been speaking but is currently paused for longer
        than `backchannel_after_ms`, return a phrase to play. Returns None
        if it's not time yet, or if we just played one in the last 3 s.
        """
        if not self._user_speaking:
            return None
        now = time.monotonic()
        pause_ms = (now - self._user_last_interim_at) * 1000
        if pause_ms < self.backchannel_after_ms:
            return None
        if now - self._last_backchannel_at < 3.0:
            return None
        # Cycle through the pool deterministically by event count.
        phrase = self.backchannel_pool[len(self._events) % len(self.backchannel_pool)]
        self._last_backchannel_at = now
        return phrase

    # ====================================================================
    # Stats — feeds the metrics dashboard
    # ====================================================================

    def stats(self) -> dict:
        """Snapshot for CallMetrics. Safe to call mid-call."""
        return {
            "interruption_count": len(self._events),
            "barge_in_recovery_count": sum(1 for e in self._events if e.recovered),
            "barge_in_recovery_rate": (
                sum(1 for e in self._events if e.recovered) / len(self._events)
                if self._events else None
            ),
            "events": [
                {
                    "user_started_at": e.user_started_at,
                    "user_ended_at": e.user_ended_at,
                    "duration_s": (
                        (e.user_ended_at - e.user_started_at)
                        if e.user_ended_at else None
                    ),
                    "recovered": e.recovered,
                }
                for e in self._events
            ],
        }
