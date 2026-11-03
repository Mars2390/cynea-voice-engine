"""Prove a deployed call survives a long silence — and that the line wakes up.

    uv run --with websockets python check_idle.py wss://cynea-voice.<subdomain>.workers.dev 330

Opens a call, waits for the greeting, then sends NOTHING for the given number
of seconds — no data and no protocol pings, the way a browser tab behaves
while a visitor thinks. Then it hangs up with a control frame. If the
Durable Object answers that with a clean 1000 "hangup" close, three things
are true at once: the socket stayed open, the object woke from hibernation
to handle the frame, and the call's state survived the wake.
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
        while json.loads(await ws.recv())["type"] != "audio":
            pass
        print(f"greeting received after {time.monotonic() - t0:.1f}s; going silent for {idle_s}s")

        try:
            await asyncio.wait_for(ws.recv(), idle_s)
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
    sys.exit(asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 330)))
