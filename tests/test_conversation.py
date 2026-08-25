"""Tests for cynea.conversation — history, barge-in trim, tool sanitisation.

The behaviour worth pinning here is what happens when a call goes *wrong*:
the caller talks over the agent, a tool call never gets its result, the
same transcript arrives twice. Those paths are where a conversation
history quietly corrupts and the LLM starts 400-ing mid-call.
"""

import pytest

from cynea.conversation import ConversationHistory


def _tool_call(call_id="call_1", name="check_availability"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


# ----------------------------------------------------------------------
# Basic shape
# ----------------------------------------------------------------------

def test_system_prompt_leads_the_message_list():
    h = ConversationHistory()
    h.set_system_prompt("You are Kwame.")
    h.add_user("hello")
    assert h.for_llm()[0] == {"role": "system", "content": "You are Kwame."}


def test_setting_the_system_prompt_twice_does_not_duplicate_it():
    h = ConversationHistory()
    h.set_system_prompt("first")
    h.set_system_prompt("second")
    systems = [m for m in h.for_llm() if m["role"] == "system"]
    assert len(systems) == 1
    assert systems[0]["content"] == "second"


def test_turn_count_ignores_system_and_tool_messages():
    h = ConversationHistory()
    h.set_system_prompt("sys")
    h.add_user("hi")
    h.add_assistant("hello", tool_calls=[_tool_call()])
    h.append_tool_result("call_1", "ok")
    assert h.turn_count == 2  # user + assistant only


def test_for_llm_returns_a_copy_not_the_live_list():
    """Callers mutating the returned list must not corrupt the history."""
    h = ConversationHistory()
    h.add_user("hi")
    out = h.for_llm()
    out.append({"role": "user", "content": "injected"})
    out[0]["content"] = "mutated"
    assert len(h.for_llm()) == 1
    assert h.for_llm()[0]["content"] == "hi"


def test_duplicate_consecutive_user_text_is_not_appended_twice():
    """Transcribers re-emit finals; the same sentence must not double up."""
    h = ConversationHistory()
    h.add_user("do you have a room")
    h.add_user("do you have a room")
    assert len([m for m in h.for_llm() if m["role"] == "user"]) == 1


# ----------------------------------------------------------------------
# Barge-in: pop_unheard
#
# The caller cut in before the agent finished. Anything the agent "said"
# that was never actually heard has to leave the history, or the model
# believes it communicated something the caller never received.
# ----------------------------------------------------------------------

def test_pop_unheard_removes_the_trailing_assistant_turn():
    h = ConversationHistory()
    h.add_user("hi")
    h.add_assistant("Let me check the availability for those dates and...")
    popped = h.pop_unheard()

    assert len(popped) == 1
    assert h.for_llm()[-1]["role"] == "user", "user turn must be the new tail"


def test_pop_unheard_leaves_the_user_turn_intact():
    h = ConversationHistory()
    h.add_user("hi")
    h.add_assistant("unheard")
    h.pop_unheard()
    assert [m["content"] for m in h.for_llm()] == ["hi"]


def test_pop_unheard_removes_a_trailing_tool_result_too():
    """A tool result the caller never heard the summary of is also unheard."""
    h = ConversationHistory()
    h.add_user("hi")
    h.add_assistant("checking", tool_calls=[_tool_call()])
    h.append_tool_result("call_1", "{\"rooms\": 1}")
    popped = h.pop_unheard()
    assert len(popped) >= 2
    assert h.for_llm()[-1]["role"] == "user"


def test_pop_unheard_on_a_user_tail_is_a_no_op():
    h = ConversationHistory()
    h.add_user("hi")
    assert h.pop_unheard() == []
    assert len(h.for_llm()) == 1


def test_pop_unheard_on_empty_history_is_safe():
    assert ConversationHistory().pop_unheard() == []


def test_pop_unheard_returns_messages_in_original_order():
    h = ConversationHistory()
    h.add_user("hi")
    h.add_assistant("first", tool_calls=[_tool_call()])
    h.append_tool_result("call_1", "result")
    popped = h.pop_unheard()
    assert popped[0]["role"] == "assistant"
    assert popped[-1]["role"] == "tool"


# ----------------------------------------------------------------------
# Continuation merge
# ----------------------------------------------------------------------

def test_speech_through_the_grace_period_merges_into_one_user_turn():
    h = ConversationHistory()
    h.add_user("do you have a room")
    merged = h.merge_continuation("on the fourteenth")
    assert merged == "do you have a room on the fourteenth"
    assert not [m for m in h.for_llm() if m["role"] == "user"], \
        "the original user turn should have been consumed by the merge"


def test_merge_after_an_assistant_turn_starts_a_new_user_turn():
    h = ConversationHistory()
    h.add_user("hi")
    h.add_assistant("hello")
    assert h.merge_continuation("I need a room") == "I need a room"


def test_merge_of_empty_text_is_a_no_op():
    h = ConversationHistory()
    h.add_user("hi")
    assert h.merge_continuation("") == ""
    assert len(h.for_llm()) == 1


# ----------------------------------------------------------------------
# Orphaned tool-call sanitisation
#
# This is the one that bites in production: a barge-in drops the assistant
# message carrying `tool_calls`, leaving a tool-role message with no
# parent. OpenAI and Anthropic both reject that with a 400 — mid-call.
# ----------------------------------------------------------------------

def test_orphaned_tool_message_is_stripped():
    h = ConversationHistory()
    h.add_user("hi")
    h.append_tool_result("call_orphan", "{}")   # never had a parent
    roles = [m["role"] for m in h.for_llm()]
    assert "tool" not in roles


def test_tool_message_with_a_matching_parent_survives():
    h = ConversationHistory()
    h.add_user("hi")
    h.add_assistant("checking", tool_calls=[_tool_call("call_1")])
    h.append_tool_result("call_1", "{\"rooms\": 1}")
    roles = [m["role"] for m in h.for_llm()]
    assert "tool" in roles


def test_tool_message_whose_id_does_not_match_is_stripped():
    h = ConversationHistory()
    h.add_user("hi")
    h.add_assistant("checking", tool_calls=[_tool_call("call_1")])
    h.append_tool_result("call_DIFFERENT", "{}")
    assert "tool" not in [m["role"] for m in h.for_llm()]


def test_sanitisation_does_not_mutate_the_stored_history():
    """for_llm() sanitises a copy; the real history keeps the tool result
    so a retry or a transcript export still has it."""
    h = ConversationHistory()
    h.add_user("hi")
    h.append_tool_result("call_orphan", "{}")
    h.for_llm()
    assert any(m.get("role") == "tool" for m in h.messages)


def test_barge_in_then_llm_call_produces_a_valid_message_list():
    """The real sequence: tool call in flight, caller barges in, we ask the
    model again. The result must not contain an orphaned tool message."""
    h = ConversationHistory()
    h.set_system_prompt("You are Kwame.")
    h.add_user("do you have a room")
    h.add_assistant("let me check", tool_calls=[_tool_call("call_1")])
    h.append_tool_result("call_1", "{\"rooms\": 0}")

    h.pop_unheard()                 # caller cut in
    h.add_user("actually never mind")

    msgs = h.for_llm()
    for i, m in enumerate(msgs):
        if m["role"] == "tool":
            parents = [p for p in msgs[:i]
                       if p["role"] == "assistant" and p.get("tool_calls")]
            assert parents, f"orphaned tool message at index {i}"
    assert msgs[-1]["content"] == "actually never mind"


@pytest.mark.parametrize("bad_tool_calls", [None, [], [{}], ["not-a-dict"]])
def test_malformed_tool_calls_do_not_crash_sanitisation(bad_tool_calls):
    """Defensive: a provider returning an odd tool_calls shape must not
    take the call down."""
    h = ConversationHistory()
    h.add_user("hi")
    h.add_assistant("checking", tool_calls=bad_tool_calls)
    h.append_tool_result("call_1", "{}")
    h.for_llm()   # must not raise
