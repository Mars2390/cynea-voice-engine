#!/usr/bin/env python3
"""Drive the real page and check the call captions against the real audio.

    python tools/check_conversation_sync.py
    python tools/check_conversation_sync.py --headed --shot

tools/check_conversations.py proves the JSON and the mp3 describe the same
timeline. This proves the *page* does: it answers each call in Chromium,
parks the audio at a series of instants, and asserts that the words showing
on screen at that instant are exactly the words due by then — no more, no
fewer, in order.

It reuses the Range-serving handler from tools/check_captions.py. Chromium
will not seek a media element the server cannot serve ranges for; it
silently snaps currentTime back, and a test that does not know this passes
while measuring nothing.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from check_captions import serve                      # noqa: E402  (Range server)

CONV = ROOT / "assets/segments/conversations.json"
SHOTS = pathlib.Path(__file__).resolve().parent.parent / "_shots"


def due(conv: dict, t: float) -> list[str]:
    """Every word that should be revealed at time t.

    The tape keeps the whole call on screen, so this accumulates from the
    start rather than from the current line.
    """
    out: list[str] = []
    for line in conv["lines"]:
        for word, at, _ in line["words"]:
            if at <= t:
                out.append(word)
    return out


def speaker_at(conv: dict, t: float) -> str | None:
    who = None
    for line in conv["lines"]:
        if line["start"] <= t + 0.02:
            who = line["speaker"]
    return who


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--shot", action="store_true")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed:\n"
              "    pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 1

    conv = json.loads(CONV.read_text(encoding="utf-8"))
    base, httpd = serve()
    failures: list[str] = []
    passes = 0

    def ok(label: str, cond: bool, detail: str = "") -> None:
        nonlocal passes
        if cond:
            passes += 1
            print(f"  PASS  {label}")
        else:
            failures.append(f"{label}: {detail}")
            print(f"  FAIL  {label}  {detail}")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{base}/index.html", wait_until="networkidle")
            page.wait_for_timeout(800)

            for key, cv in conv.items():
                print(f"{key}:")
                card = f'[data-call="{key}"]'
                page.locator(card).scroll_into_view_if_needed()
                page.wait_for_timeout(400)

                # Idle state: nothing claiming to be live, no words showing,
                # and no Restart on a call that has never played. That last
                # one is here because `hidden` alone did not hide it — an
                # author display rule on the button's class outranks the UA
                # stylesheet, so the attribute was inert.
                ok(f"{key} starts idle",
                   page.locator(f"{card} [data-state]").inner_text().strip().lower()
                   == "incoming call")
                ok(f"{key} offers no Restart before it has played",
                   not page.locator(f"{card} [data-restart]").is_visible())
                ok(f"{key} shows no words before it has played",
                   page.eval_on_selector_all(f"{card} .ct-w.in", "e => e.length") == 0)
                ok(f"{key} shows no empty turn labels before it has played",
                   page.eval_on_selector_all(
                       f"{card} .ct", "els => els.filter(e => "
                       "getComputedStyle(e).display !== 'none').length") == 0)

                page.click(f"{card} [data-answer]")
                page.wait_for_selector(f"{card}.on", timeout=8000)
                page.wait_for_timeout(500)

                total = cv["duration"]
                probes = [total * f for f in (0.08, 0.26, 0.44, 0.62, 0.8, 0.94)]
                for t in probes:
                    at = page.evaluate(
                        """t => { const p = document.getElementById('player');
                                  p.pause(); p.currentTime = t;
                                  return p.currentTime; }""", t)
                    page.wait_for_timeout(420)
                    at = page.evaluate("() => document.getElementById('player').currentTime")

                    # offsetParent, not just the class. A word whose turn
                    # is still display:none matches ".ct-w.in" perfectly
                    # well and is invisible on screen — which is exactly
                    # the bug this missed the first time: the tape was
                    # dropping whole turns on a seek and every word
                    # assertion here still passed.
                    seen = page.eval_on_selector_all(
                        f"{card} .ct-w.in",
                        "els => els.filter(e => e.offsetParent !== null)"
                        "          .map(e => e.textContent)")
                    want = due(cv, at)
                    # A word due within a frame of now may or may not have
                    # landed; compare on the stable prefix.
                    tol = due(cv, at - 0.12)
                    good = (seen == want) or (len(tol) <= len(seen) <= len(want)
                                              and seen == want[:len(seen)])
                    ok(f"{key} @ {at:5.1f}s — {len(seen)}/{len(want)} words",
                       good,
                       f"expected …{want[-4:]}, saw …{seen[-4:]}")

                    shown = page.eval_on_selector_all(
                        f"{card} .ct", "els => els.filter(e => e.offsetParent !== null).length")
                    want_turns = sum(1 for L in cv["lines"] if L["start"] <= at + 0.02)
                    ok(f"{key} @ {at:5.1f}s — {shown}/{want_turns} turns on screen",
                       shown == want_turns,
                       "a turn was skipped rather than shown")

                    who = speaker_at(cv, at)
                    cls = page.get_attribute(card, "class") or ""
                    ok(f"{key} @ {at:5.1f}s — {who} is lit",
                       f"talk-{who}" in cls, cls)

                if args.shot:
                    SHOTS.mkdir(parents=True, exist_ok=True)
                    page.locator(card).screenshot(path=str(SHOTS / f"call-{key}.png"))

                # Run it to the end and check it says so.
                page.evaluate("""() => { const p = document.getElementById('player');
                                         p.currentTime = Math.max(0, p.duration - 0.35);
                                         p.play(); }""")
                page.wait_for_timeout(1800)
                state = page.locator(f"{card} [data-state]").inner_text().strip().lower()
                ok(f"{key} reports the call ended", state == "call ended", state)
                ok(f"{key} stops claiming to be live",
                   "on" not in (page.get_attribute(card, "class") or "").split())

                bar = page.eval_on_selector(f"{card} [data-bar]",
                                            "e => parseFloat(e.style.width)")
                ok(f"{key} progress bar is full", bar >= 99.0, f"{bar}%")

            # Only one call at a time: answering the second must stand the
            # first down, because they share the one <audio> element.
            keys = list(conv.keys())
            if len(keys) > 1:
                page.click(f'[data-call="{keys[0]}"] [data-answer]')
                page.wait_for_timeout(900)
                page.click(f'[data-call="{keys[1]}"] [data-answer]')
                page.wait_for_timeout(1200)
                a = (page.get_attribute(f'[data-call="{keys[0]}"]', "class") or "").split()
                b = (page.get_attribute(f'[data-call="{keys[1]}"]', "class") or "").split()
                ok("answering the second call stands the first down",
                   "on" not in a and "on" in b, f"{a} / {b}")

                # ...and an agent card taking the element over stands it down too.
                page.locator(".agent-play").first.scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                page.click(".agent-play")
                page.wait_for_timeout(1000)
                b2 = (page.get_attribute(f'[data-call="{keys[1]}"]', "class") or "").split()
                ok("an agent card taking over stands the call down", "on" not in b2, str(b2))

            ok("no JavaScript errors", not errors, " | ".join(errors[:3]))
            browser.close()
    finally:
        httpd.shutdown()

    print(f"\n{passes} passed, {len(failures)} failed")
    if failures:
        for f in failures:
            print("  " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
