#!/usr/bin/env python3
"""Generate the Cynea favicon, touch icon and social card from one design.

    python tools/make_brand_icons.py

Writes:
    assets/favicon.svg            vector master
    assets/favicon.ico            16 / 32 / 48, each tuned for its size
    assets/apple-touch-icon.png   180x180
    assets/og-image.jpg           1200x630

Where the mark comes from
-------------------------
Not a generic waveform. The bar heights are the amplitude envelope of
Kwame's greeting, measured by tools/make_voicemark.py — the same recording
and the same measurement the voice mark in the statement banner uses. The
banner mark takes 40 buckets because it has a whole line of type to fill;
this takes 5, because five bars is what survives a 16px browser tab.

So the icon and the on-page mark are the same sentence at two resolutions,
and re-running make_voicemark.py against a different clip changes both.

Why five and not seven
----------------------
A 16px favicon has about 13px of drawing area once the circle's edge is
accounted for. Seven bars leaves each one a single pixel wide with single
pixel gaps, which anti-aliases into a grey smear. Five bars at 2px with
1px gaps fills 14px and stays legible. The envelope is sampled at 5 rather
than decimated from 40, so each bar is a real measurement of a fifth of
the greeting rather than one tick that happened to survive.

Colour
------
The orb is the page's own `.pill-orb` gradient, lifted exactly:
radial-gradient(120% 120% at 32% 26%, #FFC9AE, #FF6B4A 46%, #E24A2E 100%).
Reusing it means the favicon and the hero's live-call pill are the same
object, which is the sort of thing nobody consciously notices and
everybody feels.

Needs ffmpeg on PATH (for the envelope) and Pillow. No downloads.
"""

from __future__ import annotations

import importlib.util
import math
import pathlib
import struct
import sys

from PIL import Image, ImageDraw, ImageFont

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("numpy is required:  pip install numpy")

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
CLIP = ASSETS / "segments" / "kwame" / "greeting.mp3"

# ── brand ────────────────────────────────────────────────────────────────
CORAL_LIGHT = (255, 201, 174)   # #FFC9AE  the orb's highlight
CORAL = (255, 107, 74)          # #FF6B4A  --coral
CORAL_DEEP = (226, 74, 46)      # #E24A2E  --coral-deep
NAVY = (27, 32, 80)             # #1B2050  --navy
INK = (23, 26, 46)              # #171A2E  --ink
DUSK = [                        # --grad-dusk, 142deg
    (0.00, (27, 32, 80)),       # #1B2050
    (0.34, (74, 58, 126)),      # #4A3A7E
    (0.66, (184, 67, 126)),     # #B8437E
    (1.00, (255, 138, 92)),     # #FF8A5C
]

SS = 8            # supersample factor; everything is drawn big and reduced
BARS = 5

FONT_DIR = pathlib.Path("C:/Windows/Fonts")
FONTS = {
    "wordmark": ["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"],
    "body":     ["segoeui.ttf", "arial.ttf"],
    "serif":    ["georgiai.ttf", "georgia.ttf"],
}


def font(role: str, size: int) -> ImageFont.FreeTypeFont:
    for name in FONTS[role]:
        p = FONT_DIR / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def envelope(buckets: int) -> list[float]:
    """Reuse the voice mark's own measurement rather than reimplementing it."""
    spec = importlib.util.spec_from_file_location(
        "make_voicemark", ROOT / "tools" / "make_voicemark.py")
    mv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mv)
    return mv.envelope(CLIP, buckets)


# ── the orb ──────────────────────────────────────────────────────────────

def orb(size: int) -> Image.Image:
    """The coral gradient disc, RGBA, antialiased.

    Built as a numpy field rather than concentric ellipses: a radial
    gradient drawn as 200 stacked circles bands visibly at 180px, and the
    banding is exactly the sort of thing that makes an icon look cheap at
    the one size people see it large.
    """
    n = size * SS
    y, x = np.mgrid[0:n, 0:n].astype(np.float32)
    # Highlight at 32% / 26%, extent 120% of the box — the CSS, verbatim.
    cx, cy, extent = 0.32 * n, 0.26 * n, 1.20 * n
    t = np.clip(np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / extent, 0.0, 1.0)

    rgb = np.zeros((n, n, 3), np.float32)
    stops = [(0.0, CORAL_LIGHT), (0.46, CORAL), (1.0, CORAL_DEEP)]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        m = (t >= t0) & (t <= t1)
        f = ((t - t0) / (t1 - t0))[m][:, None]
        rgb[m] = np.array(c0, np.float32) * (1 - f) + np.array(c1, np.float32) * f

    img = Image.fromarray(rgb.astype(np.uint8), "RGB").convert("RGBA")
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, n - 1, n - 1], fill=255)
    img.putalpha(mask)
    return img.resize((size, size), Image.LANCZOS)


def geometry(env: list[float], *, inset: float, weight: float) -> list[tuple]:
    """Bar positions and sizes in a 0..100 square. One source of truth.

    Two things shape this beyond the raw envelope, and both are the
    difference between a waveform and a barcode.

    **The circle constrains the bars.** A bar at horizontal distance dx
    from the centre can only be as tall as the chord there, so the outer
    bars are shorter whatever the audio says. Without this the bars run
    straight to the disc's edge and the mark reads as vertical stripes in
    a circle rather than a waveform inside one.

    **The envelope is contrast-stretched.** Kwame's five buckets span
    0.52..1.00 — a real measurement, and far too flat to read at any size:
    mapped directly, every bar lands within 15% of its neighbour. The
    stretch preserves the ordering and the relative gaps and spends the
    full height on them. The shape is still his sentence; it is his
    sentence with the contrast turned up.
    """
    lo, hi = min(env), max(env)
    span = (hi - lo) or 1.0
    stretched = [(v - lo) / span for v in env]

    group = 100 * (1 - 2 * inset)
    pitch = group / len(env)
    w = pitch * weight
    r = 50.0

    out = []
    for i, v in enumerate(stretched):
        cx = 100 * inset + pitch * (i + 0.5)
        # The chord at this bar's outer edge, less a margin so the cap
        # never kisses the circle.
        dx = abs(cx - 50) + w / 2
        chord = 2 * math.sqrt(max(0.0, r * r - dx * dx))
        h_env = 100 * (0.30 + 0.52 * v)          # what the audio asks for
        h = min(h_env, chord * 0.80)             # what the circle allows
        h = max(h, w * 1.02)                     # never shorter than round
        out.append((cx, w, h))
    return out


def bars(size: int, env: list[float], *, inset: float, weight: float,
         color=(255, 255, 255, 255)) -> Image.Image:
    """The white bars, centred, rounded caps.

    `inset` and `weight` are passed rather than derived because the small
    sizes need proportionally thicker bars and less padding than the large
    ones — a 16px icon scaled straight down from 180px reads as grey fuzz.
    """
    n = size * SS
    layer = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    k = n / 100.0

    for cx, w, h in geometry(env, inset=inset, weight=weight):
        d.rounded_rectangle(
            [(cx - w / 2) * k, (50 - h / 2) * k, (cx + w / 2) * k, (50 + h / 2) * k],
            radius=w / 2 * k, fill=color)

    return layer.resize((size, size), Image.LANCZOS)


def icon(size: int, *, inset: float, weight: float, env: list[float]) -> Image.Image:
    """Orb plus bars, composited at final size."""
    base = orb(size)
    base.alpha_composite(bars(size, env, inset=inset, weight=weight))
    return base


# ── ICO, written by hand ─────────────────────────────────────────────────

def write_ico(path: pathlib.Path, images: list[Image.Image]) -> None:
    """A multi-size ICO whose entries are independently drawn.

    Pillow's ICO writer resamples one image to every requested size, which
    would undo the per-size tuning above — the 16px entry would be the
    180px art shrunk, which is the exact thing that does not work. The
    container format is a 6-byte header and a 16-byte record per image, so
    writing it directly is cheaper than fighting the encoder. PNG payloads
    are valid in ICO and every browser in use reads them.
    """
    import io
    blobs = []
    for im in images:
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        blobs.append(buf.getvalue())

    header = struct.pack("<HHH", 0, 1, len(images))   # reserved, type=icon, count
    offset = len(header) + 16 * len(images)
    directory, payload = b"", b""
    for im, blob in zip(images, blobs):
        w = 0 if im.width >= 256 else im.width
        h = 0 if im.height >= 256 else im.height
        directory += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        payload += blob
    path.write_bytes(header + directory + payload)


# ── social card ──────────────────────────────────────────────────────────

def dusk_field(w: int, h: int) -> Image.Image:
    """The hero's own 142-degree dusk gradient."""
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    a = math.radians(142 - 90)
    t = (x * math.cos(a) + y * math.sin(a))
    t = (t - t.min()) / (t.max() - t.min())

    rgb = np.zeros((h, w, 3), np.float32)
    for (t0, c0), (t1, c1) in zip(DUSK, DUSK[1:]):
        m = (t >= t0) & (t <= t1)
        f = ((t - t0) / (t1 - t0))[m][:, None]
        rgb[m] = np.array(c0, np.float32) * (1 - f) + np.array(c1, np.float32) * f
    rgb[t < DUSK[0][0]] = DUSK[0][1]
    rgb[t > DUSK[-1][0]] = DUSK[-1][1]
    return Image.fromarray(rgb.astype(np.uint8), "RGB")


def og_image(env5: list[float], env40: list[float]) -> Image.Image:
    W, H = 1200, 630
    img = dusk_field(W, H).convert("RGBA")

    # The scrim leans left rather than lying flat across the whole canvas.
    # A uniform ink wash protects the type and takes the gradient with it —
    # the dusk ramp ends in the brand's coral, and flattening that to brown
    # throws away the one thing that makes the card recognisably Cynea. Type
    # sits in the left two-fifths, so that is where the cover is needed.
    gx = np.linspace(0.62, 0.06, W, dtype=np.float32)[None, :]
    alpha = (np.repeat(gx, H, axis=0) * 255).astype(np.uint8)
    wash = Image.new("RGBA", (W, H), (*INK, 255))
    wash.putalpha(Image.fromarray(alpha, "L"))
    img = Image.alpha_composite(img, wash)
    d = ImageDraw.Draw(img)

    f_kick = font("body", 23)
    f_word = font("wordmark", 132)
    f_sub = font("body", 40)
    f_foot = font("body", 23)

    def tracked(x, y, text, fnt, fill, track):
        """Pillow has no letter-spacing, and the logo is set tight."""
        for ch in text:
            d.text((x, y), ch, font=fnt, fill=fill)
            x += d.textlength(ch, font=fnt) + track
        return x

    L = 88                      # left margin, shared by every row
    mark_px = 116

    # One block, optically centred: mark and kicker on a line, the wordmark
    # under them, then the two lines of copy.
    top = 116
    img.alpha_composite(icon(mark_px, inset=0.15, weight=0.44, env=env5), (L, top))
    d.text((L + mark_px + 26, top + 40), "AI VOICE AGENTS", font=f_kick,
           fill=(255, 255, 255, 165))

    word_y = top + mark_px + 30
    tracked(L - 6, word_y, "CYNEA", f_word, (255, 255, 255, 255), 3.0)

    d.text((L, word_y + 164), "Voice AI for Africa", font=f_sub,
           fill=(255, 255, 255, 232))
    d.text((L, word_y + 224), "Any language. Any accent. 24/7.", font=f_foot,
           fill=(255, 255, 255, 158))

    # The full 40-tick envelope along the foot: the same sentence the five
    # bars above reduce, at the resolution this canvas can afford. It runs
    # the full width so it reads as a floor the card stands on rather than
    # a stripe parked at the bottom.
    # Sized and placed to clear the last line of type by ~60px: at the
    # previous amplitude the bars ran straight through it.
    base_y, amp, left, right = H - 44, 32, 0, W
    pitch = (right - left) / len(env40)
    for i, v in enumerate(env40):
        bh = max(4.0, amp * (0.14 + 0.86 * v))
        cx = left + pitch * (i + 0.5)
        bw = max(3.0, pitch * 0.30)
        # Fade toward the warm side so the bars never fight the gradient.
        a_ = int(96 - 44 * (cx / W))
        d.rounded_rectangle(
            [cx - bw / 2, base_y - bh / 2, cx + bw / 2, base_y + bh / 2],
            radius=bw / 2, fill=(255, 255, 255, a_))

    return img.convert("RGB")


# ── the vector master ────────────────────────────────────────────────────

def svg(env: list[float]) -> str:
    ticks = [
        f'  <rect x="{cx - w/2:.2f}" y="{50 - h/2:.2f}" width="{w:.2f}" '
        f'height="{h:.2f}" rx="{w/2:.2f}" fill="#fff"/>'
        for cx, w, h in geometry(env, inset=0.15, weight=0.44)
    ]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"
     width="100" height="100" role="img" aria-label="Cynea">
  <title>Cynea</title>
  <!-- The bar heights are the amplitude envelope of Kwame's greeting,
       measured by tools/make_voicemark.py — the same recording the voice
       mark in the statement banner is drawn from, sampled at five buckets
       instead of forty so it survives a 16px browser tab. Heights are
       clipped to the circle's chord and contrast-stretched; see geometry()
       for why both are necessary.
       Regenerate with: python tools/make_brand_icons.py -->
  <defs>
    <radialGradient id="c" cx="32%" cy="26%" r="120%">
      <stop offset="0" stop-color="#FFC9AE"/>
      <stop offset=".46" stop-color="#FF6B4A"/>
      <stop offset="1" stop-color="#E24A2E"/>
    </radialGradient>
  </defs>
  <circle cx="50" cy="50" r="50" fill="url(#c)"/>
{chr(10).join(ticks)}
</svg>
"""


def main() -> int:
    if not CLIP.exists():
        sys.exit(f"missing {CLIP} — the mark is derived from it")

    env5 = envelope(BARS)
    env40 = envelope(40)
    print("  envelope  " + " ".join(f"{v:.2f}" for v in env5) + "   (Kwame, 5 buckets)")

    ASSETS.mkdir(exist_ok=True)

    (ASSETS / "favicon.svg").write_text(svg(env5), encoding="utf-8")
    print("  favicon.svg")

    # Small sizes get thicker bars and less padding, or they disappear.
    # 16px is not 48px scaled down: at a browser-tab size the bars have to
    # be proportionally much fatter and the padding much smaller or the
    # whole mark anti-aliases into a plain coral dot. Compared against a
    # 3-bar reduction at the same size, which read as a pause glyph —
    # Kwame's 3-bucket envelope is 0.97/0.85/1.00, too flat to say voice.
    tuning = {16: (0.06, 0.60), 32: (0.11, 0.50), 48: (0.14, 0.45)}
    write_ico(ASSETS / "favicon.ico",
              [icon(s, inset=i, weight=w, env=env5) for s, (i, w) in tuning.items()])
    print("  favicon.ico          16 + 32 + 48, each drawn at its own size")

    # iOS masks to a rounded square and composites on white, so the icon is
    # full-bleed: a transparent circle would sit on a white card.
    touch = Image.new("RGBA", (180, 180), (*INK, 255))
    disc = icon(148, inset=0.15, weight=0.44, env=env5)
    touch.alpha_composite(disc, (16, 16))
    touch.convert("RGB").save(ASSETS / "apple-touch-icon.png", optimize=True)
    print("  apple-touch-icon.png 180x180, coral disc on dusk ink")

    og = og_image(env5, env40)
    og.save(ASSETS / "og-image.jpg", quality=90, optimize=True, progressive=True)
    print(f"  og-image.jpg         1200x630")
    return 0


if __name__ == "__main__":
    sys.exit(main())
