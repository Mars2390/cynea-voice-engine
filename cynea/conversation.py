"""Cynea Voice Engine — Conversation history and context management.

Why this module exists separately from cynea.models.Conversation
----------------------------------------------------------------
The simple `Conversation` dataclass in models.py is fine for one-shot
turns. Live phone calls need more:

- A **committed** message log (what the user actually heard) and a parallel
  **interim** log (what the assistant *intended* to say before any barge-in).
- **Trim-on-barge-in**: when the user interrupts, anything the assistant
  hadn't yet emitted must be dropped from history, otherwise the LLM
  thinks it said things the user never heard.
- **Tool-call sanitization**: OpenAI/Anthropic both reject conversation
  histories where a `tool` message has no preceding `assistant` with the
  matching `tool_calls` entry. Interruptions can orphan tool messages;
  we sanitize on every read.
- **Duplicate-user suppression**: ASR endpointing sometimes fires twice
  for one utterance; we drop the second one.

This is a pure data-structure module — no I/O, no async. Easy to unit test.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable, Optional


# Roles we drop on barge-in (anything after the last user turn that the user
# definitionally couldn't have heard yet).
_UNHEARD_ROLES = frozenset({"assistant", "tool"})


@dataclass
class ConversationHistory:
    """In-memory message log for one call.

    Two parallel lists:
        _committed: what we believe the user heard (after barge-in trims).
        _interim: what the LLM thinks it said, used to build the next prompt
            optimistically before the user's audio receipts confirm delivery.
    """

    _committed: list = field(default_factory=list)
    _interim: list = field(default_factory=list)

    # ====================================================================
    # System prompt + welcome message
    # ====================================================================

    def set_system_prompt(self, content: str) -> None:
        """Install or replace the leading system message. Idempotent."""
        if not content:
            return
        msg = {"role": "system", "content": content}
        if self._committed and self._committed[0].get("role") == "system":
            self._committed[0] = msg
        else:
            self._committed.insert(0, msg)
        self._sync_interim()

    def append_welcome(self, content: str) -> None:
        """Append the agent's first message. Counts as a committed assistant turn."""
        if not content:
            return
        self._committed.append({"role": "assistant", "content": content})
        self._sync_interim()

    # ====================================================================
    # Turn-by-turn append
    # ====================================================================

    def append_user(self, content: str) -> None:
        """Append a user turn. Drops the duplicate if the last turn was an
        identical user message (handles double-fire from the ASR endpointer).
        """
        if not content or not content.strip():
            return
        if self._is_duplicate_user(content):
            return
        self._committed.append({"role": "user", "content": content.strip()})

    def append_assistant(self, content: str, *, tool_calls: Optional[list] = None) -> None:
        msg: dict = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self._committed.append(msg)

    def append_tool_result(self, tool_call_id: str, content: str) -> None:
        self._committed.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })

    # ====================================================================
    # Barge-in handling
    # ====================================================================

    def pop_unheard(self) -> list:
        """Drop trailing assistant/tool messages — call this when the user
        barges in before the last agent turn finished playing."""
        popped = []
        while self._committed and self._committed[-1].get("role") in _UNHEARD_ROLES:
            popped.append(self._committed.pop())
        return list(reversed(popped))

    def merge_continuation(self, new_user_text: str) -> str:
        """If the last turn was a user turn, merge new text into it and
        return the combined string. Used when the user keeps speaking
        through the grace period — we don't want two separate user turns.
        """
        if not new_user_text:
            return new_user_text
        if self._committed and self._committed[-1].get("role") == "user":
            prev = self._committed.pop()["content"]
            return f"{prev} {new_user_text}".strip()
        return new_user_text

    def trim_last_assistant_to_heard(
        self,
        text_actually_heard: Optional[str],
        truncate: Optional[Callable[[str, Optional[str]], str]] = None,
    ) -> None:
        """Replace the content of the last assistant message with whatever
        the user actually heard (e.g. derived from telephony mark events).

        If `text_actually_heard` is None or empty, the assistant message is
        removed entirely. If a `truncate` callable is given, it is called as
        `truncate(original_content, text_actually_heard)` and used as the
        replacement; otherwise we fall back to a prefix-match.
        """
        for i in range(len(self._committed) - 1, -1, -1):
            msg = self._committed[i]
            if msg["role"] != "assistant":
                continue

            original = msg.get("content") or ""
            if truncate is not None:
                updated = truncate(original, text_actually_heard)
            else:
                updated = self._prefix_match(original, text_actually_heard)

            if not updated or not updated.strip():
                had_tool_calls = bool(msg.get("tool_calls"))
                self._committed.pop(i)
                # Drop dependent tool messages too — orphans break OpenAI.
                if had_tool_calls:
                    while i < len(self._committed) and self._committed[i].get("role") == "tool":
                        self._committed.pop(i)
            else:
                msg["content"] = updated
            return

    @staticmethod
    def _prefix_match(original: str, heard: Optional[str]) -> str:
        if not heard:
            return ""
        # Walk word by word; keep the longest common prefix.
        orig_words = original.split()
        heard_words = heard.split()
        out = []
        for ow, hw in zip(orig_words, heard_words):
            if ow.lower().strip(".,?!") == hw.lower().strip(".,?!"):
                out.append(ow)
            else:
                break
        return " ".join(out)

    # ====================================================================
    # Read API — what we hand to the LLM
    # ====================================================================

    def for_llm(self) -> list:
        """Return a deep-copied, sanitized message list safe to send to an
        OpenAI-shaped LLM client. Always sanitizes orphaned tool messages.
        """
        msgs = copy.deepcopy(self._committed)
        self._sanitize_tool_messages(msgs)
        return [m for m in msgs if not m.get("exclude_from_llm")]

    @property
    def messages(self) -> list:
        """Read-only access to the committed list (no copy — callers must
        not mutate)."""
        return self._committed

    @property
    def interim(self) -> list:
        return self._interim

    @property
    def turn_count(self) -> int:
        """Number of user+assistant turns, ignoring system + tool."""
        return sum(1 for m in self._committed if m.get("role") in ("user", "assistant"))

    def __len__(self) -> int:
        return len(self._committed)

    # ====================================================================
    # Internals
    # ====================================================================

    def _sync_interim(self) -> None:
        self._interim = copy.deepcopy(self._committed)

    def _is_duplicate_user(self, content: str) -> bool:
        if not self._committed:
            return False
        last = self._committed[-1]
        return (
            last.get("role") == "user"
            and (last.get("content") or "").strip() == content.strip()
        )

    @staticmethod
    def _sanitize_tool_messages(msgs: list) -> None:
        """Remove tool-role messages that have no preceding assistant with
        a matching tool_calls entry. OpenAI rejects orphans with a 400."""
        i = 0
        while i < len(msgs):
            if msgs[i].get("role") != "tool":
                i += 1
                continue
            tool_call_id = msgs[i].get("tool_call_id", "")
            found_parent = False
            for j in range(i - 1, -1, -1):
                role = msgs[j].get("role")
                if role == "assistant" and msgs[j].get("tool_calls"):
                    for tc in msgs[j]["tool_calls"]:
                        tc_id = tc.get("id", "") if isinstance(tc, dict) else ""
                        if tc_id == tool_call_id:
                            found_parent = True
                            break
                    break
                if role == "user":
                    break
            if not found_parent:
                msgs.pop(i)
            else:
                i += 1
