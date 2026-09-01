#!/usr/bin/env python3
"""Free voice demo — a scripted call answered entirely from human recordings.

Costs nothing to run: no TTS, no LLM, no API key, no network. Every reply
is a slice of a recording a real person made, chosen by keyword.

    python examples/free_voice_demo.py                 # scripted, prints only
    python examples/free_voice_demo.py --play          # also play the audio
    python examples/free_voice_demo.py --agent amina
    python examples/free_voice_demo.py --interactive   # type your own lines

Playback needs ffplay (ships with ffmpeg). Without it the demo still runs
and prints what it would have played.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from cynea.audio_responder import ManifestMissing, Response, agents, respond, segments_for

# One scripted call per agent, following the four-beat shape of a real one:
# greeting, the substantive question, the commitment, the sign-off.
SCRIPTS: dict[str, list[str]] = {
    "kwame": ["Hello",
              "What are your rates?",
              "I want to book a room",
              "Thank you"],
    "amina": ["Hi there",
              "What is my account balance?",
              "I need to make a transfer",
              "Thanks, bye"],
    "kofi":  ["Good evening",
              "I'd like to order jollof",
              "How long is delivery?",
              "Thank you"],
    "maya":  ["Hello",
              "What appointment slots are available?",
              "Can you confirm that booking?",
              "Thanks, goodbye"],
}

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"


def play(response: Response) -> None:
    if not shutil.which("ffplay"):
        print(f"     {DIM}(ffplay not on PATH — nothing played){RESET}")
        return
    if not response.path.exists():
        print(f"     {YELLOW}missing {response.file}{RESET}")
        return
    subprocess.run(["ffplay", "-v", "error", "-nodisp", "-autoexit", str(response.path)],
                   check=False)


def show(user_text: str, agent: str, do_play: bool) -> Response:
    r = respond(user_text, agent)
    mark = f"{GREEN}->{RESET}" if r.confident else f"{YELLOW}~>{RESET}"
    why = (f"matched {r.matched!r}" if r.confident
           else "no keyword matched, falling back")

    print(f"  {BOLD}You:{RESET}   {user_text}")
    print(f"  {mark} {BOLD}{agent.title()}:{RESET} [{r.intent}] {r.duration:.1f}s  {DIM}{why}{RESET}")
    print(f"     {DIM}{r.text}{RESET}")
    if r.alternatives:
        print(f"     {DIM}also matched: {', '.join(r.alternatives)}{RESET}")
    if do_play:
        play(r)
    print()
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", default="kwame", help="which agent to talk to")
    ap.add_argument("--play", action="store_true", help="play the audio via ffplay")
    ap.add_argument("--interactive", action="store_true", help="type your own lines")
    ap.add_argument("--all", action="store_true", help="run every agent's script")
    args = ap.parse_args()

    try:
        known = agents()
    except ManifestMissing as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.agent not in known:
        print(f"unknown agent {args.agent!r}; have {known}", file=sys.stderr)
        return 1

    targets = known if args.all else [args.agent]

    for agent in targets:
        available = segments_for(agent)
        print(f"\n{BOLD}=== {agent.title()} ==={RESET}  "
              f"{DIM}segments: {', '.join(available)}{RESET}\n")
        for line in SCRIPTS.get(agent, SCRIPTS["kwame"]):
            show(line, agent, args.play)

    if args.interactive:
        agent = args.agent
        print(f"{DIM}Type to {agent.title()}. Ctrl-C or blank line to stop.{RESET}\n")
        while True:
            try:
                line = input("  You:   ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                break
            show(line, agent, args.play)

    print(f"{DIM}Cost: $0.00 — no TTS, no LLM, no network calls.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
