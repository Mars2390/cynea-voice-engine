#!/usr/bin/env python3
"""Drive the real landing page in a browser and prove the captions track the audio.

    pip install playwright && playwright install chromium
    python tools/check_captions.py            # headless, exits non-zero on failure
    python tools/check_captions.py --headed   # watch it happen
    python tools/check_captions.py --shot     # save screenshots to the scratch dir

The caption engine is time-driven, so nothing static can tell you whether it
works. This serves index.html over HTTP (fetch() needs an origin), starts a
clip, seeks to fixed points, and checks the words revealed at each point are
exactly the words due by then. Chromium is launched with a silent autoplay
policy so the audio plays without a user gesture.

Checked for each surface:
  - the phone transcript, the agent cards, and the chat bubbles
  - words revealed at time t == words whose start <= t   (the actual sync)
  - one and only one word carries `.now`
  - the collapsed surface takes no vertical space until a clip starts
  - the page still works when captions.page.json is unreachable
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import pathlib
import socketserver
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
CAPTIONS = ROOT / "assets" / "segments" / "captions.page.json"
SHOTS = pathlib.Path(os.environ.get("TEMP", "/tmp")) / "cynea-caption-shots"


class Range(http.server.SimpleHTTPRequestHandler):
    """Static files with HTTP Range support.

    Chromium will not seek a media element the server cannot serve ranges
    for — it silently snaps currentTime back. SimpleHTTPRequestHandler does
    not implement Range, so without this the seeks below appear to work in
    Python and do nothing in the browser.
    """

    def log_message(self, *args):                            # noqa: D102
        pass

    def send_head(self):
        header = self.headers.get("Range")
        if not header or not header.startswith("bytes="):
            return super().send_head()

        path = self.translate_path(self.path)
        try:
            size = os.path.getsize(path)
            handle = open(path, "rb")
        except OSError:
            return super().send_head()

        first, _, last = header[6:].partition("-")
        try:
            start = int(first) if first else 0
            end = int(last) if last else size - 1
        except ValueError:
            handle.close()
            return super().send_head()
        end = min(end, size - 1)
        if start > end:
            handle.close()
            self.send_error(416)
            return None

        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        handle.seek(start)
        self.wfile.write(handle.read(end - start + 1))
        handle.close()
        return None


def serve() -> tuple[str, socketserver.TCPServer]:
    handler = functools.partial(Range, directory=str(ROOT))

    class Quiet(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def handle_error(self, request, client_address):    # noqa: D102
            pass                                            # broken pipes on audio seeks

    httpd = Quiet(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


def due(track: dict, t: float, keep: int = 99) -> list[str]:
    """The words that should be on screen at time t.

    `keep` mirrors how many lines the surface holds. The phone and the cards
    show one line at a time, so words from lines that have already rolled off
    are not expected to still be in the DOM; a chat bubble keeps all of them.
    """
    current = -1
    for i, line in enumerate(track["lines"]):
        if line["start"] <= t + 0.02:
            current = i
    if current < 0:
        return []

    out: list[str] = []
    for line in track["lines"][max(0, current - keep + 1):current + 1]:
        out.extend(w[0] for w in line["words"] if w[1] <= t)
    return out


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passes = 0

    def ok(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passes += 1
            print(f"  PASS  {label}")
        else:
            self.failures.append(f"{label}: {detail}")
            print(f"  FAIL  {label}  {detail}")


def revealed(page, selector: str) -> list[str]:
    return page.eval_on_selector_all(
        f"{selector} .cap-line:not(.out) .cap-w.in", "els => els.map(e => e.textContent)")


def now_count(page, selector: str) -> int:
    return page.eval_on_selector_all(
        f"{selector} .cap-line:not(.out) .cap-w.now", "els => els.length")


def probe(page, t: float) -> float:
    """Park the clip at t and return where it actually landed."""
    page.evaluate("""t => { const p = document.getElementById('player');
                            p.pause(); p.currentTime = t; }""", t)
    page.wait_for_timeout(700)              # reveal + let any retiring line go
    return page.evaluate("() => document.getElementById('player').currentTime")


def play_and_probe(page, checks: Checks, label: str, click: str, surface: str,
                   track: dict, probes: list[float], shot: bool,
                   keep: int) -> None:
    """Start a clip, jump to each probe time, and compare on-screen to due."""
    page.click(click)
    page.wait_for_selector(f"{surface}.on", timeout=5000)

    for t in probes:
        # Pause before seeking. The engine keeps ticking while paused, so it
        # settles on exactly this instant; left playing, the clock would run
        # on during the wait below and the comparison would chase it.
        at = probe(page, t)
        expected = due(track, at, keep)
        seen = revealed(page, surface)
        checks.ok(f"{label} @ {at:.1f}s — {len(expected)} words revealed",
                  seen == expected,
                  f"expected {expected[-4:]!r}, saw {seen[-4:]!r} "
                  f"({len(expected)} vs {len(seen)})")

    checks.ok(f"{label} — exactly one word highlighted", now_count(page, surface) == 1,
              f"{now_count(page, surface)} carry .now")

    if shot:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.locator(surface).screenshot(path=str(SHOTS / f"{label}.png"))

    page.evaluate("() => { const p=document.getElementById('player'); p.pause(); "
                  "dispatchEvent(new Event('cynea:audiostop')); }")
    page.wait_for_timeout(420)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--headed", action="store_true", help="show the browser")
    ap.add_argument("--shot", action="store_true", help="save screenshots")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed:\n"
              "    pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 1

    if not CAPTIONS.exists():
        print(f"missing {CAPTIONS}. Run tools/sync_landing_data.py first.", file=sys.stderr)
        return 1

    tracks = json.loads(CAPTIONS.read_text(encoding="utf-8"))
    base, httpd = serve()
    checks = Checks()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not args.headed,
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/index.html", wait_until="load")

        print("\nphone transcript (hero)")
        play_and_probe(page, checks, "phone", "#call-accept", "#cap-phone" if
                       page.locator("#cap-phone").count() else ".cap-phone",
                       tracks["assets/amina_real_voice.mp3"], [1.5, 6.0, 14.0], args.shot, keep=1)

        print("\nagent card")
        card = ".agent:nth-of-type(1) .cap-card"
        play_and_probe(page, checks, "card", ".agent:nth-of-type(1) .agent-play", card,
                       tracks["assets/kwame_card_demo.mp3"], [2.0, 9.0, 17.0], args.shot, keep=1)

        print("\nchat bubble")
        page.click("#chat .chat-chip:nth-child(2)")          # "What are your rates?"
        page.wait_for_selector("#chat-log .bub.them .cap-w", timeout=5000)
        bubble = "#chat-log .bub.them:last-of-type .say"
        track = tracks["assets/segments/kwame/pricing.mp3"]
        for t in (1.0, 4.0, 7.5):
            at = probe(page, t)
            expected, seen = due(track, at), revealed(page, bubble)
            checks.ok(f"bubble @ {at:.1f}s — {len(expected)} words revealed",
                      seen == expected,
                      f"expected {expected[-4:]!r}, saw {seen[-4:]!r}")

        # A bubble is the message itself: when the clip ends the whole reply
        # has to be standing, not half-revealed.
        page.evaluate("() => { const p=document.getElementById('player'); p.pause(); "
                      "dispatchEvent(new Event('cynea:audioended')); }")
        page.wait_for_timeout(300)
        whole = " ".join(w for line in track["lines"] for w, _, _ in
                         [(x[0], x[1], x[2]) for x in line["words"]])
        checks.ok("bubble keeps its full text after the clip ends",
                  " ".join(revealed(page, bubble)) == whole,
                  f"saw {' '.join(revealed(page, bubble))[:60]!r}")

        # Idle surfaces must not reserve space.
        heights = page.eval_on_selector_all(
            ".cap:not(.on)", "els => els.map(e => e.getBoundingClientRect().height)")
        checks.ok("collapsed surfaces take no vertical space",
                  all(h < 1 for h in heights), f"heights {heights}")

        checks.ok("no page errors", not errors, "; ".join(errors[:3]))

        # Captions must be an enhancement, never a dependency.
        print("\nwithout captions.page.json")
        page2 = browser.new_page()
        page2.route("**/captions.page.json", lambda route: route.abort())
        broken: list[str] = []
        page2.on("pageerror", lambda e: broken.append(str(e)))
        page2.goto(f"{base}/index.html", wait_until="load")
        page2.click("#chat .chat-chip:nth-child(2)")
        page2.wait_for_timeout(600)
        text = page2.eval_on_selector("#chat-log .bub.them:last-of-type .say",
                                      "e => e.textContent.trim()")
        checks.ok("chat still shows its reply when captions 404", bool(text), f"got {text!r}")
        checks.ok("no page errors without captions", not broken, "; ".join(broken[:3]))

        browser.close()

    httpd.shutdown()
    print(f"\n{checks.passes} passed, {len(checks.failures)} failed")
    if args.shot:
        print(f"screenshots in {SHOTS}")
    for failure in checks.failures:
        print(f"  {failure}")
    return 1 if checks.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
