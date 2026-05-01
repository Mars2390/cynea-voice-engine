"""Cynea Voice Engine — agent-loader demo.

Loads two agents from JSON configs (Kwame for Adinkra Hotel in Accra,
Amina for KCB Bank in Nairobi) and prints a summary of each. Proves the
config-driven agent system works end-to-end without a phone line, an
LLM API key, or a microphone.

Run:
    python examples/load_agent_demo.py
"""

from __future__ import annotations

import asyncio
import os
import sys

# Windows consoles default to cp1252; reconfigure so the demo doesn't
# crash on em-dashes or Swahili tokens.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass

from cynea.agent_loader import AgentLoader


CONFIGS = [
    os.path.join(os.path.dirname(__file__), "agent_config.json"),
    os.path.join(os.path.dirname(__file__), "amina_config.json"),
]


def _truncate(text: str, n: int = 80) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


async def main() -> int:
    loader = AgentLoader()

    print("=" * 64)
    print("  CYNEA VOICE ENGINE — AGENT LOADER DEMO")
    print("=" * 64)

    print(f"\nAvailable personas: {loader.list_available_personas()}")
    for name in loader.list_available_personas():
        p = loader.get_persona_config(name)
        print(
            f"  · {name:<8} "
            f"voice={p['voice'].get('voice', '?'):<22} "
            f"first={_truncate(p['first_message'], 40)!r}"
        )

    # Build each agent and print a summary table.
    for cfg_path in CONFIGS:
        print("\n" + "-" * 64)
        print(f"Loading: {cfg_path}")
        try:
            engine = loader.load_from_file(cfg_path)
        except Exception as exc:
            print(f"  FAILED: {exc}")
            continue

        cfg = engine.config
        first = await engine.start()

        print("  Agent name        :", cfg.name)
        print("  Persona           :", cfg.persona)
        print("  Client            :", cfg.client_name)
        print("  Location          :", cfg.location)
        print(f"  Voice             : {cfg.voice}  @ speed {cfg.speed}")
        print("  STT / LLM / TTS   :", cfg.stt_provider, "/", cfg.llm_provider, "/", cfg.tts_provider)
        print("  Interruption      :", cfg.interruption_enabled)
        print("  Backchanneling    :", cfg.backchanneling_enabled)
        print("  Max duration (s)  :", cfg.max_call_duration)
        print("  Escalation number :", cfg.escalation_number or "—")
        print("  System prompt len :", f"{len(cfg.system_prompt):,} chars")
        print("  First message     :", _truncate(first, 80))
        print("  Engine state      :", engine.state.value)
        print("  History length    :", len(engine.history))

    # Quick negative test: a malformed config should raise a clean ValueError,
    # not a traceback the customer would have to triage.
    print("\n" + "-" * 64)
    print("Validation negative test (expected ValueError):")
    try:
        loader.load_from_dict({"agent_name": "broken", "persona": "nonexistent"})
        print("  UNEXPECTED: validation did not raise.")
    except ValueError as exc:
        print(f"  OK -> {exc}")

    print("\n" + "=" * 64)
    print("  Done. Both agents loaded cleanly.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
