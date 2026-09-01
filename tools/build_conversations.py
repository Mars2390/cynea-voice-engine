#!/usr/bin/env python3
"""Stitch the separately-recorded agent and caller takes into two calls.

    python tools/build_conversations.py            # transcribe, cut, stitch
    python tools/build_conversations.py --rebuild  # re-derive from cache, offline

Inputs are four single-take recordings — one speaker each, lines in script
order:

    assets/{kwame,amina}_agent.mp3     the agent's turns
    assets/{kwame,amina}_caller.mp3    the caller's turns

Outputs:

    assets/kwame_full_conversation.mp3
    assets/amina_full_conversation.mp3
    assets/segments/conversations.json     line + word timings, for captions

How the turns are found
-----------------------
Neither obvious method works on its own here, and both look like they
should, so it is worth writing down why.

Silence detection cannot do it. A person pauses mid-sentence about as often
as between sentences, and these takes were not read with a consistent beat
left for the other speaker: amina_agent.mp3 is a single continuous read with
one pause over 800ms in forty-eight seconds, and its seven turns have no
acoustic boundary between them at all.

Whisper's word timings cannot do it either, though they look like they can.
Groq reports contiguous spans — every word's `end` is the next word's
`start`, 156 times out of 158 in that same file — so silence is absorbed
into whichever word precedes it. A word's `start` is a real onset and can be
trusted; its `end` only means "when the next word began" and cannot.

So each is used for the one thing it is good at. The turn *text* is aligned
against the word stream with difflib to find which words belong to which
turn, and a turn's *start* is that first word's onset. Its *end* is snapped
to the nearest real silence in the waveform, found with silencedetect, which
is the only measurement here that knows where speech actually stopped.

The text is the corrected transcript rather than an outline, because a
caption that does not match the audio under it is worse than no caption.

Raw Groq responses are cached under assets/segments/_groq_raw/, so
--rebuild re-cuts and re-stitches with no network and no API call.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SEGDIR = ASSETS / "segments"
RAW = SEGDIR / "_groq_raw"
OUT_JSON = SEGDIR / "conversations.json"

ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3"
TIMEOUT_S = 180.0
RETRIES = 4

# Nudges Whisper toward the spellings this project uses. It is a hint, not a
# guarantee — correct() below is what actually enforces them.
PROMPT = ("A phone call at Adinkra Hotel in Accra, and a Cynea Bank M-Pesa "
          "support call in Nairobi. Names: Kwame, Amina, David Osei. "
          "Words: Akwaaba, asante sana, pole, shillings, M-Pesa.")

# Same corrections the segment cutter applies, plus the nouns these two
# conversations introduce.
FIXUPS = [
    (r"\bodin\s*kra\b", "Adinkra"), (r"\bodin\s*krohotel\b", "Adinkra Hotel"),
    (r"\badinkra\s*hotel\b", "Adinkra Hotel"), (r"\bsinia\b", "Cynea"),
    (r"\bsynea\b", "Cynea"), (r"\bcynia\b", "Cynea"),
    (r"\bm\s*pesa\b", "M-Pesa"), (r"\bmpesa\b", "M-Pesa"),
    (r"\bosei\b", "Osei"), (r"\bo\s*s\s*e\s*i\b", "O-S-E-I"),
    (r"\basante\s*sana\b", "Asante sana"),
]

# Repairs applied to the *heard* stream before it is split into tokens, so
# the recovered words inherit a proportional share of the original span.
#
# Whisper packs badly-heard runs into a single entry. In the full-file pass
# the caller's name is one 3.9-second token reading "D-O-S-E-I."; sent on its
# own, that same slice comes back "David Osei, O-S-E-I." — which is what he
# says, and what the agent's "Thank you, David" is answering. Left packed,
# the turn matches nothing and the alignment cannot place it at all.
HEARD_FIXUPS = [
    (r"^d-?o-?s-?e-?i[.,]?$", "David Osei O-S-E-I"),
]

# ── the turns, as recorded ───────────────────────────────────────────────
# These are the turns that are actually on the four tapes, not a summary of
# them. The distinction matters: the recordings run considerably longer and
# say more than the outline this was built from — Kwame itemises the
# anniversary package, reads the phone number back and states the check-in
# time; Amina explains the two-hour M-Pesa window and offers a reversal.
# Captions have to match the audio word for word or they are worse than no
# captions, so the tape wins and the outline is what it always was: a plan.
#
# Text is the corrected transcript. Order within each speaker's list is the
# order it was read in, which is also the order it is cut in — a turn can be
# split out of a longer take, but turns can never be reordered.
#
# One line is not the transcript's: the caller's name. Whisper packed
# "David Osei, O-S-E-I." into a single 3.9s token reading "D-O-S-E-I.";
# re-transcribing that slice on its own recovered it. The agent answers
# "Thank you, David", so the name had to be right.

SCRIPT: dict[str, dict] = {
    "kwame": {
        "audio": "kwame_full_conversation.mp3",
        "agent": {"name": "Kwame", "role": "Hotel Receptionist",
                  "where": "Adinkra Hotel · Accra", "avatar": "avatar-kwame"},
        "caller": {"name": "David Osei", "role": "Guest",
                   "where": "Calling from Accra", "avatar": None},
        "title": "An anniversary weekend",
        "summary": "A guest books a tenth-anniversary weekend. Kwame recommends "
                   "a suite, prices it, adds a package, and takes the booking.",
        "turns": [
            ("agent",  "Hello? Adinkra Hotel, Kwame speaking. How can I help you today?"),
            ("caller", "Hi, Kwame. Good afternoon. I'm planning something special for my wife. Our tenth anniversary. I want to book a room for the weekend."),
            ("agent",  "Ah, congratulations! Ten years, that's beautiful. Let me see what we can do. Are you looking at this weekend?"),
            ("caller", "Yes. Friday to Sunday. Two nights. I want something, you know, romantic. Not just a standard room."),
            ("agent",  "I understand. For an anniversary, I'd recommend our Executive Suite. King bed, private balcony with ocean view. We can also arrange champagne and flowers in the room before you arrive."),
            ("caller", "That sounds perfect. How much is it?"),
            ("agent",  "The Executive Suite is $200 per night, so for two nights that's $400. Breakfast is included. Would you like me to add the champagne and flowers?"),
            ("caller", "Yes, please. How much is that extra?"),
            ("agent",  "The Anniversary Package is an additional $50. It includes a bottle of champagne, fresh flowers, and chocolates. Total would be $450 for the weekend."),
            ("caller", "That's fine. Let's do it."),
            ("agent",  "Perfect. Let me take your details. Your full name."),
            ("caller", "David Osei. O-S-E-I."),
            ("agent",  "Thank you, David. And the best number to reach you?"),
            ("caller", "0244567890."),
            ("agent",  "024-456-7890. Correct? And an email for the confirmation?"),
            ("caller", "Yes, that's right. david.osei at gmail.com."),
            ("agent",  "Let me confirm. Executive Suite, Friday to Sunday, two nights. Anniversary package with champagne and flowers. Total $450. Check-in is from 2 p.m. Is everything correct?"),
            ("caller", "Perfect. Thank you, Kwame."),
            ("agent",  "You're most welcome, David. We'll have everything ready for you and your wife. Enjoy your anniversary. We'll see you Friday. Bye now!"),
            ("caller", "Thank you. Bye."),
        ],
    },
    "amina": {
        "audio": "amina_full_conversation.mp3",
        "agent": {"name": "Amina", "role": "Bank Support",
                  "where": "Cynea Bank · Nairobi", "avatar": "avatar-amina"},
        "caller": {"name": "Caller", "role": "Customer",
                   "where": "Calling from Nairobi", "avatar": None},
        "title": "A transfer that has not landed",
        "summary": "An M-Pesa transfer is stuck. Amina finds it, escalates it, "
                   "and commits to a callback window and a reversal rather than "
                   "an apology.",
        "turns": [
            ("agent",  "Hello, this is Amina from Cynea Bank. How can I help you today?"),
            ("caller", "Hi, Amina. I sent money through M-Pesa two hours ago, and the person hasn't received it."),
            ("agent",  "I'm so sorry to hear that. Let me check this for you right away."),
            ("caller", "The money left my account, but they're telling me they got nothing."),
            ("agent",  "Can I have the phone number you sent the money to?"),
            ("caller", "0712-345-678."),
            ("agent",  "Thank you. Let me pull up the transaction. Um, I can see it here. It's showing as processing. Sometimes M-Pesa transactions can take up to two hours. But since it's been that long, I'll escalate this to our M-Pesa team now."),
            ("caller", "How long will that take? I need this sorted out. The person is waiting for this money."),
            ("agent",  "I completely understand your frustration. I've marked this as urgent. The team will call you back within 30 minutes. And if the money hasn't reflected in the next hour, we'll reverse it back to your account."),
            ("caller", "Okay. 30 minutes. Fine, but if I don't hear back I'll be calling again."),
            ("agent",  "That's completely fair. You'll get a call from our team. Let me also send you an SMS with the reference number."),
            ("caller", "Sawa. Thank you."),
            ("agent",  "Sawa, pole for the inconvenience. We'll sort this out. Anything else I can help with?"),
            ("caller", "No, that's all."),
            ("agent",  "Asante sana for calling. Have a good day."),
        ],
    },
}

# ── stitching parameters ─────────────────────────────────────────────────
LEAD_IN = 0.10      # air kept before a line's first word
TAIL = 0.16         # air kept after its last word
FADE = 0.05         # in/out, so no line opens or closes on a click
GAP = 0.30          # the pause between turns
GAP_AFTER_Q = 0.38  # a beat longer when answering a question
TARGET_DBFS = -20.0 # both takes are levelled to this, so one call not two


def read_key() -> str:
    """GROQ_API_KEY from the environment, falling back to .env."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("GROQ_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def correct(text: str) -> str:
    for wrong, right in FIXUPS:
        text = re.sub(wrong, right, text, flags=re.IGNORECASE)
    return text


def norm(token: str) -> str:
    """Comparison form: letters and digits only, lowercased.

    Numbers are the awkward case. The script says "Two hundred dollars" and
    Whisper writes "$200"; the alignment below tolerates that because it
    only needs most tokens to match, not all of them.
    """
    return re.sub(r"[^a-z0-9]", "", token.lower())


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_duration(path: pathlib.Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)])
    return float(out.stdout.strip())


def mean_dbfs(path: pathlib.Path) -> float:
    """volumedetect's mean_volume, used to level the two takes to each other."""
    out = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
               "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", out.stderr)
    return float(m.group(1)) if m else TARGET_DBFS


# ── transcription ────────────────────────────────────────────────────────

def fetch(client, path: pathlib.Path, key: str, refresh: bool) -> dict:
    cached = RAW / f"{path.stem}.json"
    if cached.exists() and not refresh:
        print(f"  cached  {path.name}")
        return json.loads(cached.read_text(encoding="utf-8"))
    if not key:
        raise SystemExit(
            f"{path.name} has no cached transcript and GROQ_API_KEY is not set.\n"
            "Set the key, or run with --rebuild once a cache exists.")

    for attempt in range(1, RETRIES + 1):
        with path.open("rb") as fh:
            r = client.post(
                ENDPOINT,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (path.name, fh, "audio/mpeg")},
                data={"model": MODEL, "response_format": "verbose_json",
                      "language": "en", "prompt": PROMPT,
                      "timestamp_granularities[]": ["word", "segment"]},
                timeout=TIMEOUT_S,
            )
        if r.status_code == 200:
            payload = r.json()
            RAW.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                              encoding="utf-8")
            print(f"  fetched {path.name}")
            return payload
        if r.status_code in (429, 500, 502, 503) and attempt < RETRIES:
            wait = float(r.headers.get("retry-after", 2 * attempt))
            print(f"    {r.status_code}, retrying in {wait:.0f}s", flush=True)
            time.sleep(wait)
            continue
        raise SystemExit(f"Groq returned {r.status_code} for {path.name}: {r.text[:300]}")
    raise SystemExit(f"Groq did not answer for {path.name}")


def expand(words: list[dict]) -> list[list]:
    """One token per entry, with a timing each.

    Groq sometimes packs several tokens into one entry ("is $120." is a
    single 'word' spanning two seconds). Left packed, an index alignment
    slips by however many tokens were hidden. The span is divided between
    the tokens in proportion to their length.
    """
    out: list[list] = []
    for entry in words:
        raw = str(entry.get("word", ""))
        for wrong, right in HEARD_FIXUPS:
            raw = re.sub(wrong, right, raw, flags=re.IGNORECASE)
        tokens = raw.split()
        if not tokens:
            continue
        start, end = float(entry["start"]), float(entry["end"])
        if len(tokens) == 1:
            out.append([tokens[0], start, end])
            continue
        total = sum(len(t) for t in tokens) or len(tokens)
        at = start
        for tok in tokens:
            share = (end - start) * len(tok) / total
            out.append([tok, at, at + share])
            at += share
    return out


# ── silence map ─────────────────────────────────────────────────────────

def silences(path, floor_db=-34, min_d=0.16):
    """Every stretch of near-silence in the take, as (start, end).

    This is the only reading of the audio that knows where a person stopped
    talking; the word timings do not, because Groq runs each word up to the
    next one. Turn ends are snapped onto these.
    """
    out = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
               "-af", "silencedetect=noise=%ddB:d=%s" % (floor_db, min_d),
               "-f", "null", "-"])
    spans, start = [], None
    for m in re.finditer(r"silence_(start|end):\s*(-?[\d.]+)", out.stderr):
        kind, at = m.group(1), float(m.group(2))
        if kind == "start":
            start = at
        elif start is not None:
            spans.append((start, at))
            start = None
    if start is not None:
        spans.append((start, probe_duration(path)))
    return spans


# ── alignment ────────────────────────────────────────────────────────────

def align(turns, heard, quiet, total):
    """Locate each turn in the take and give it a clean pair of cut points.

    Both sides are the same speech in the same order, so this is a sequence
    alignment rather than a search. difflib matches the turns' tokens to the
    heard tokens, and a turn's onset is the first heard word it matched.

    Tokens that fail to match carry no timing of their own — a misheard
    name, "$200" against "two hundred" — and are interpolated across the gap
    afterwards, which is why a turn does not have to transcribe perfectly
    for its bounds to come out right.
    """
    turn_tokens, owner = [], []
    for i, line in enumerate(turns):
        for tok in line.split():
            if norm(tok):
                turn_tokens.append(norm(tok))
                owner.append(i)

    heard_tokens = [norm(w[0]) for w in heard]
    sm = difflib.SequenceMatcher(a=turn_tokens, b=heard_tokens, autojunk=False)
    hit = {}
    for a0, b0, size in sm.get_matching_blocks():
        for k in range(size):
            hit[a0 + k] = b0 + k

    matched = len(hit)
    print("    aligned %d/%d tokens (%.0f%%)"
          % (matched, len(turn_tokens), 100.0 * matched / max(1, len(turn_tokens))))
    if matched < len(turn_tokens) * 0.6:
        raise SystemExit("alignment is too weak to trust — the take and the "
                         "turn list have diverged, check SCRIPT")

    bounds = []
    for i in range(len(turns)):
        idxs = [hit[j] for j in range(len(turn_tokens)) if owner[j] == i and j in hit]
        if not idxs:
            raise SystemExit("turn %d matched no words at all:\n  %r" % (i, turns[i]))
        bounds.append((min(idxs), max(idxs)))

    result = []
    for i, (lo, hi) in enumerate(bounds):
        onset = heard[lo][1]
        last_onset = heard[hi][1]
        # Where the next turn begins, which this one has to finish before.
        ceiling = heard[bounds[i + 1][0]][1] if i + 1 < len(bounds) else total

        # End: the first silence opening after the last word started.
        # Falling back to the ceiling keeps a turn whole where the speaker
        # ran straight on into the next one with no pause to find.
        stop = None
        for qs, qe in quiet:
            if last_onset + 0.12 <= qs <= ceiling + 0.05:
                stop = qs
                break
        if stop is None:
            stop = max(last_onset + 0.35, ceiling - 0.05)

        # Start: a fixed pad before the onset, and nothing cleverer.
        #
        # This used to snap to the preceding silence the way the end does,
        # which sounds symmetrical and is not. The end only has to avoid
        # clipping a word, but the start also fixes where the caption is
        # due: the reveal time is (clip start -> onset), so any variation
        # in how far back the cut opened lands directly on the caption as
        # drift. Measured against a re-transcription of the stitched file,
        # snapping cost up to 1.04s. A fixed pad makes the offset exactly
        # LEAD_IN for every turn, so the caption cannot drift at all, and
        # the fade-in covers the consonant the snap was protecting.
        begin = max(0.0, onset - LEAD_IN)

        toks = [t for t in turns[i].split() if norm(t)]
        js = [j for j in range(len(turn_tokens)) if owner[j] == i]
        spans = []
        for n, j in enumerate(js):
            spans.append([toks[n], heard[hit[j]][1], None] if j in hit
                         else [toks[n], None, None])
        result.append((begin, onset, stop, word_times(spans, onset, stop)))
    return result


def word_times(spans, start, stop):
    """Fill in every word's start, then give each an end.

    Only onsets are real. A word's end is taken as the next word's onset
    (the last runs to the turn's end), which is exactly what the reveal
    needs: a word is due when it is spoken and stays up until the next
    arrives. Words Whisper missed are spread across the gap they fell in,
    so the typewriter neither stalls nor dumps the rest of the line at once.
    """
    n = len(spans)
    for i, sp in enumerate(spans):
        if sp[1] is not None:
            continue
        prev = next((spans[k][1] for k in range(i - 1, -1, -1)
                     if spans[k][1] is not None), start)
        nxt = next((spans[k][1] for k in range(i + 1, n)
                    if spans[k][1] is not None), stop)
        run_len = 1
        k = i + 1
        while k < n and spans[k][1] is None:
            run_len += 1
            k += 1
        spans[i][1] = prev + max(0.05, (nxt - prev) / (run_len + 1))
    for i in range(1, n):
        if spans[i][1] < spans[i - 1][1]:
            spans[i][1] = spans[i - 1][1] + 0.02
    for i in range(n):
        spans[i][2] = spans[i + 1][1] if i + 1 < n else stop
        if spans[i][2] <= spans[i][1]:
            spans[i][2] = spans[i][1] + 0.05
    return spans


# ── stitching ────────────────────────────────────────────────────────────

def stitch(key_name, spec, cuts, gains):
    """One ffmpeg pass: trim every turn out of its take, level it, and
    concatenate with silence between. Encoding once rather than per turn
    keeps the joins clean and avoids stacking generations of lossy codec."""
    dest = ASSETS / spec["audio"]
    src = {"agent": ASSETS / ("%s_agent.mp3" % key_name),
           "caller": ASSETS / ("%s_caller.mp3" % key_name)}

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(src["agent"]), "-i", str(src["caller"]),
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono"]
    idx = {"agent": 0, "caller": 1}

    parts, chain, timeline, at = [], [], [], 0.0
    take_pos = {"agent": 0, "caller": 0}

    for n, (who, text) in enumerate(spec["turns"]):
        begin, onset, stop, words = cuts[who][take_pos[who]]
        take_pos[who] += 1

        a, b = begin, stop + TAIL
        dur = b - a
        label = "l%d" % n
        chain.append(
            "[%d:a]atrim=start=%.4f:end=%.4f,asetpts=PTS-STARTPTS,"
            "volume=%.2fdB,afade=t=in:st=0:d=%s,afade=t=out:st=%.4f:d=%s[%s]"
            % (idx[who], a, b, gains[who], FADE, max(0.0, dur - FADE), FADE, label)
        )
        parts.append("[%s]" % label)

        # Where this turn lands in the finished file. Source time maps to
        # output time by a single offset per turn, so the word timings move
        # with it and stay exact — this is a build plan, not a measurement.
        shift = at - a
        timeline.append({
            "speaker": who,
            "start": round(at, 3),
            "voice": round(onset + shift, 3),
            "end": round(at + dur, 3),
            "text": text,
            "words": [[w[0], round(w[1] + shift, 3), round(w[2] + shift, 3)]
                      for w in words],
        })
        at += dur

        if n < len(spec["turns"]) - 1:
            gap = GAP_AFTER_Q if text.rstrip().endswith("?") else GAP
            g = "g%d" % n
            chain.append("[2:a]atrim=start=0:end=%s,asetpts=PTS-STARTPTS[%s]" % (gap, g))
            parts.append("[%s]" % g)
            at += gap

    chain.append("".join(parts) + "concat=n=%d:v=0:a=1[out]" % len(parts))
    cmd += ["-filter_complex", ";".join(chain), "-map", "[out]",
            "-c:a", "libmp3lame", "-q:a", "4", "-ar", "44100", "-ac", "1",
            str(dest)]

    r = run(cmd)
    if r.returncode != 0:
        raise SystemExit("ffmpeg failed for %s:\n%s" % (key_name, r.stderr[-1500:]))

    real = probe_duration(dest)
    print("  wrote %s  %.2fs  (%d turns, planned %.2fs)"
          % (dest.name, real, len(spec["turns"]), at))
    if abs(real - at) > 0.35:
        print("    ! planned and actual duration differ by %.2fs" % abs(real - at))

    return {
        "audio": "assets/%s" % spec["audio"],
        "duration": round(real, 3),
        "title": spec["title"],
        "summary": spec["summary"],
        "agent": spec["agent"],
        "caller": spec["caller"],
        "lines": timeline,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true",
                    help="re-cut and re-stitch from cached transcripts, no network")
    ap.add_argument("--refresh", action="store_true",
                    help="re-transcribe even where a cache exists")
    args = ap.parse_args()

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("ffmpeg and ffprobe must be on PATH")

    key = "" if args.rebuild else read_key()
    client = None
    if not args.rebuild:
        try:
            import httpx
        except ImportError:
            raise SystemExit("httpx is required (pip install httpx), or use --rebuild")
        client = httpx.Client()

    out = {}
    try:
        for name, spec in SCRIPT.items():
            print("%s:" % name)
            cuts, gains = {}, {}
            for who in ("agent", "caller"):
                path = ASSETS / ("%s_%s.mp3" % (name, who))
                if not path.exists():
                    raise SystemExit("missing %s" % path)
                payload = fetch(client, path, key, args.refresh)
                heard = expand(payload.get("words") or [])
                if not heard:
                    raise SystemExit("%s: transcript carries no word timings" % path.name)

                turns = [t for w, t in spec["turns"] if w == who]
                total = probe_duration(path)
                quiet = silences(path)
                print("    %s: %d turns, %d words heard, %d silences"
                      % (who, len(turns), len(heard), len(quiet)))
                cuts[who] = align(turns, heard, quiet, total)

                level = mean_dbfs(path)
                gains[who] = TARGET_DBFS - level
                print("    %s: level %+.1f dBFS -> gain %+.1f dB"
                      % (who, level, gains[who]))
            out[name] = stitch(name, spec, cuts, gains)
    finally:
        if client:
            client.close()

    SEGDIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    total = sum(len(c["lines"]) for c in out.values())
    print("\nwrote %s  (%d conversations, %d turns)"
          % (OUT_JSON.relative_to(ROOT), len(out), total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
