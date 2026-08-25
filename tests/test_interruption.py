"""Tests for cynea.interruption — barge-in, cancellation, grace period.

This module is the riskiest untested code in the repo: it is stateful, it
decides whether the agent keeps talking over a caller, and every branch is
reachable from a live phone call. These tests pin the behaviour that
matters on a real line rather than every method's surface.
"""

import time

import pytest

from cynea.interruption import InterruptionManager


# ----------------------------------------------------------------------
# Sequence-id cancellation
#
# The core safety property: a reply generated for turn N must never be
# played after the caller has moved on to turn N+1.
# ----------------------------------------------------------------------

def test_sequence_ids_increment():
    m = InterruptionManager()
    assert m.next_sequence_id() < m.next_sequence_id() < m.next_sequence_id()


def test_fresh_sequence_id_is_valid():
    m = InterruptionManager()
    assert m.is_valid(m.next_sequence_id())


def test_interruption_invalidates_in_flight_turn():
    """The whole point: a turn cancelled mid-generation must not play."""
    m = InterruptionManager()
    seq = m.next_sequence_id()

    m.on_agent_speech_started()
    m.fire_interruption()

    assert not m.is_valid(seq), "cancelled turn must not be playable"


def test_turn_issued_after_interruption_is_valid():
    """Cancellation must not poison the conversation permanently."""
    m = InterruptionManager()
    stale = m.next_sequence_id()
    m.on_agent_speech_started()
    m.fire_interruption()

    fresh = m.next_sequence_id()
    assert not m.is_valid(stale)
    assert m.is_valid(fresh), "the next turn must still be able to speak"


def test_revalidate_restores_a_cancelled_turn():
    m = InterruptionManager()
    seq = m.next_sequence_id()
    m.on_agent_speech_started()
    m.fire_interruption()
    assert not m.is_valid(seq)

    m.revalidate(seq)
    assert m.is_valid(seq)


def test_invalidate_pending_cancels_without_an_interruption_event():
    m = InterruptionManager()
    seq = m.next_sequence_id()
    m.invalidate_pending()
    assert not m.is_valid(seq)


# ----------------------------------------------------------------------
# Word-count threshold
#
# A caller saying "mm" while the agent talks is not an interruption.
# Getting this wrong makes the agent stop constantly and feel broken.
# ----------------------------------------------------------------------

def test_short_blip_does_not_interrupt():
    m = InterruptionManager()
    assert not m.should_trigger_interruption("uh", agent_speaking=True)


def test_real_sentence_interrupts():
    m = InterruptionManager()
    assert m.should_trigger_interruption(
        "actually I need a different date", agent_speaking=True
    )


def test_nothing_interrupts_while_the_agent_is_silent():
    """No barge-in when there is nothing to barge into."""
    m = InterruptionManager()
    assert not m.should_trigger_interruption(
        "actually I need a different date", agent_speaking=False
    )


def test_threshold_zero_disables_interruption():
    m = InterruptionManager(word_threshold=0)
    assert not m.should_trigger_interruption(
        "stop talking right now please", agent_speaking=True
    )


@pytest.mark.parametrize("phrase", ["okay", "mm-hm", "yeah", "right"])
def test_accidental_phrases_are_not_interruptions(phrase):
    """Backchannels are the caller agreeing, not the caller cutting in."""
    m = InterruptionManager()
    assert m.is_false_interruption(phrase, agent_speaking=True)


def test_exactly_at_threshold_interrupts():
    """Boundary: >= threshold, not > threshold."""
    m = InterruptionManager(word_threshold=3)
    assert m.should_trigger_interruption("no not that", agent_speaking=True)


def test_just_below_threshold_does_not():
    m = InterruptionManager(word_threshold=3)
    assert not m.should_trigger_interruption("no not", agent_speaking=True)


# ----------------------------------------------------------------------
# Grace period
#
# After the caller stops, wait before speaking. Answering instantly reads
# as an interruption in the other direction.
# ----------------------------------------------------------------------

# `history_length > 2` is the real gate: the welcome message deliberately
# skips the grace period so the first response stays snappy. Mid-call
# turns (history longer than system+welcome) do observe it.
MID_CALL = 4


def test_grace_period_holds_mid_call_audio():
    m = InterruptionManager(grace_period_ms=700)
    m.on_final_user_speech("do you have a room on the fourteenth")
    assert m.audio_send_status(m.next_sequence_id(), MID_CALL) == "wait"


def test_grace_period_expires():
    m = InterruptionManager(grace_period_ms=10)
    m.on_final_user_speech("do you have a room on the fourteenth")
    time.sleep(0.05)
    assert m.audio_send_status(m.next_sequence_id(), MID_CALL) == "send"


def test_welcome_message_bypasses_the_grace_period():
    """First response must not pay the grace delay."""
    m = InterruptionManager(grace_period_ms=5000)
    m.on_final_user_speech("hello")
    assert m.audio_send_status(m.next_sequence_id(), history_length=2) == "send"


def test_audio_is_held_while_the_caller_is_still_talking():
    m = InterruptionManager()
    m.on_interim_user_speech("I was wondering whether")
    assert m.audio_send_status(m.next_sequence_id(), MID_CALL) == "wait"


def test_cancelled_turn_is_blocked_not_merely_delayed():
    m = InterruptionManager(grace_period_ms=10)
    seq = m.next_sequence_id()
    m.on_agent_speech_started()
    m.fire_interruption()
    time.sleep(0.05)
    assert m.audio_send_status(seq, MID_CALL) == "block"


# ----------------------------------------------------------------------
# Speech-state tracking and recovery
# ----------------------------------------------------------------------

def test_interruption_is_idempotent_within_one_user_turn():
    """Documented contract: repeated fires for the same utterance are one
    event. The transcriber emits many interim frames per utterance, so
    counting each one would make the metric meaningless."""
    m = InterruptionManager()
    m.on_agent_speech_started()
    m.fire_interruption()
    m.fire_interruption()
    m.fire_interruption()
    assert m.stats()["interruption_count"] == 1


def test_separate_user_turns_count_separately():
    """A new utterance closes the open event, so the next barge-in counts."""
    m = InterruptionManager()
    for _ in range(3):
        m.on_agent_speech_started()
        m.on_interim_user_speech("no wait that is wrong")
        m.fire_interruption()
        m.on_final_user_speech("no wait that is wrong")  # closes the event
    assert m.stats()["interruption_count"] == 3


def test_stats_exposes_the_keys_the_engine_reports():
    """CyneaEngine.get_metrics() reads these; renaming them breaks it."""
    m = InterruptionManager()
    assert "interruption_count" in m.stats()


def test_recovery_after_interruption_is_recorded():
    m = InterruptionManager()
    m.on_agent_speech_started()
    m.fire_interruption()
    m.mark_recovery()
    assert m.stats()["interruption_count"] == 1


def test_backchannel_requires_a_user_speaking_window():
    """No backchannel before the caller has said anything."""
    m = InterruptionManager()
    assert m.maybe_backchannel() is None
