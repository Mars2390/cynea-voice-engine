"""Prove a deployed call survives a silence, and that a long one ends the call.

    uv run --with websockets python check_idle.py wss://cynea-voice.<subdomain>.workers.dev 120
    uv run --with websockets python check_idle.py wss://cynea-voice.<subdomain>.workers.dev 200

Opens a call, waits for the greeting, then sends NOTHING for the given number
of seconds — no data and no protocol pings, the way a browser tab behaves
while a visitor thinks.

Shorter than the server's silence limit (VOICE_DEMO_IDLE_S, sent in the ready
frame): it then hangs up with a control frame. A clean 1000 "hangup" close
means the socket stayed open, the object woke from hibernation to handle the
frame, and the call's state survived the wake.

Longer than the limit: the server must end the call itself, with an "ended"
frame and a 1000 "idle" close, about when it said it would.
"""

import asyncio
import json
import sys
import time

import websockets


async def main(base: str, idle_s: int) -> int:
    url = base.rstrip("/") + "/voice?agent=maya"
    # ping_interval=None: the websockets library pings every 20s by default,
    # which would keep the connection busy and prove nothing.
    async with websockets.connect(url, ping_interval=None, max_size=None) as ws:
        t0 = time.monotonic()
        limit = None
        while True:
            frame = json.loads(await ws.recv())
            if frame["type"] == "ready":
                limit = frame.get("idle_s")
            if frame["type"] == "audio":
                break
        print(f"greeting received after {time.monotonic() - t0:.1f}s; "
              f"server ends silent calls after {limit}s; going silent for {idle_s}s")

        try:
            frame = await asyncio.wait_for(ws.recv(), idle_s)
            if limit and idle_s >= limit and json.loads(frame).get("type") == "ended":
                at = time.monotonic() - t0
                ok = abs(at - limit) < 30
                print(f"{'PASS' if ok else 'FAIL'}: ended by the server after {at:.0f}s: "
                      f"{json.loads(frame).get('reason')}")
                return 0 if ok else 1
            print("FAIL: the server sent something during the silence")
            return 1
        except asyncio.TimeoutError:
            pass                                     # silence, as intended
        except websockets.ConnectionClosed as e:
            print(f"FAIL: closed after {time.monotonic() - t0:.0f}s: {e.rcvd}")
            return 1

        await ws.send(json.dumps({"action": "hangup"}))
        try:
            await asyncio.wait_for(ws.recv(), 15)
        except websockets.ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else None
            reason = e.rcvd.reason if e.rcvd else ""
            ok = code == 1000 and reason == "hangup"
            print(f"{'PASS' if ok else 'FAIL'}: open for {time.monotonic() - t0:.0f}s, "
                  f"then woke and hung up cleanly ({code} {reason!r})")
            return 0 if ok else 1
        print("FAIL: no close after the hangup frame")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 120)))
