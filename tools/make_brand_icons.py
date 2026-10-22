#!/usr/bin/env python3
"""Generate the Cynea favicon, touch icon and social card from one mark.

    python tools/make_brand_icons.py

Writes:
    assets/favicon.svg            vector master
    assets/favicon.ico            16 / 32 / 48, each drawn at its own size
    assets/apple-touch-icon.png   180x180
    assets/og-image.jpg           1200x630

The mark: "The Prism"
---------------------
A faceted geometric C. One closed outline (stroke 2.8, bevel joins), two
translucent facets cut into the upper and lower arms (18% and 9% cyan),
and a solid dot in the bowl. The path data below is the canonical copy
from the sister repo (cynea-ai, components/brand-mark.tsx); the pages
inline the same paths. Cyan #00D4FF on the brand dark #050505.

Everything here is drawn from MARK_* below, so the SVG, the three ICO
frames, the touch icon and the social card cannot drift apart.

Centring
--------
The outline is mirror-symmetric about y=26, so vertical centring is
exact. Horizontally the bounding box misleads: the spine and both arm
returns stack three strokes on the right, so the ink's centroid sits to
the right of the box centre (about 28.0 against 27.0) and a box-centred
mark looks pushed right. The mark is placed halfway between its box
centre and its measured ink centroid; fully ink-centred over-corrects.

Small sizes are not the large mark shrunk
-----------------------------------------
At 16px the canonical 2.8 stroke is about 0.6px wide and the 9%/18%
facets are a fraction of one grey level; scaled down they smear into a
cyan haze. So each ICO frame has its own tuning (TUNING): a relatively
heavier stroke, the facets dropped at 16px, a dot snapped to the pixel
grid, and the outline's span chosen so the long vertical spine and the
top and bottom edges land on whole pixels. favicon.svg carries the same idea as a media query, because
Chrome prefers the SVG to the ICO and would otherwise draw the hairline
version in the tab.

Engineering
-----------
Shapes are rasterised at SS times the final size and box-reduced, which is
exact area coverage — no ringing, which matters for cyan on near-black.
The ICO is written by hand so each frame keeps its own drawing (Pillow's
encoder would resample one image to every size). The social card's glow
is computed in float and dithered, because an 8-bit radial ramp this dark
bands visibly.

Fonts: Syne 700 (wordmark) and DM Sans (tagline), variable TTFs vendored
in tools/fonts/ with their OFL licences. Needs Pillow and numpy. Nothing
is downloaded at runtime.
"""

from __future__ import annotations

import functools
import io
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
FONT_DIR = ROOT / "tools" / "fonts"

# ── brand ────────────────────────────────────────────────────────────────
CYAN = (0, 212, 255)        # #00D4FF  the mark
BG = (5, 5, 5)              # #050505  brand dark
TEXT = (245, 245, 245)      # #F5F5F5  wordmark
MUTED = (138, 138, 138)     # #8A8A8A  tagline
TILE_RADIUS = 0.22          # rounded-square corner, fraction of the side

# ── the mark, canonical (path coordinates, before the reference's
#    translate(2,2), which only positions it in its own viewBox) ──────────
MARK_OUTLINE = [(28, 2), (38, 2), (40, 4), (40, 9), (34, 15), (34, 37),
                (40, 43), (40, 48), (38, 50), (28, 50), (18, 43), (14, 30),
                (14, 22), (18, 9)]
MARK_FACETS = [([(22, 12), (34, 6), (34, 15), (27, 23)], 0.18),
               ([(22, 40), (34, 46), (34, 37), (27, 29)], 0.09)]
MARK_DOT = (26, 26, 2.5)    # cx, cy, r
MARK_STROKE = 2.8
SPINE_X = 34                # the long inner vertical; snapped at small sizes
TOP_Y, BOTTOM_Y = 2, 50     # outline extremes; MID_Y is the mirror axis
MID_Y = (TOP_Y + BOTTOM_Y) / 2
SPAN = BOTTOM_Y - TOP_Y     # 48 units between the top and bottom edges

SS = 16                     # supersample factor


def _svg_d(points) -> str:
    return "M" + " L".join(f"{x:g} {y:g}" for x, y in points) + " Z"


# ── geometry ─────────────────────────────────────────────────────────────

def stroke_polygons(points, w):
    """A closed polyline stroked with bevel joins, as a union of polygons.

    Pillow has no stroked paths with joins, so the stroke is built the way
    a renderer builds it: one quad per segment, plus the bevel triangle at
    each vertex on the outside of the turn (the inside is already covered
    by the overlapping quads). Bevel corners are w/2 from the vertex, so
    the stroked bounding box is just the path's box grown by w/2.
    """
    h = w / 2
    n = len(points)
    normals = []
    for i in range(n):
        (x0, y0), (x1, y1) = points[i], points[(i + 1) % n]
        L = math.hypot(x1 - x0, y1 - y0)
        normals.append(((y0 - y1) / L * h, (x1 - x0) / L * h))
    polys = []
    for i in range(n):
        (x0, y0), (x1, y1) = points[i], points[(i + 1) % n]
        nx, ny = normals[i]
        polys.append([(x0 + nx, y0 + ny), (x1 + nx, y1 + ny),
                      (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)])
    for i in range(n):
        vx, vy = points[i]
        (ax, ay), (bx, by) = normals[i - 1], normals[i]
        for s in (1, -1):
            polys.append([(vx, vy), (vx + s * ax, vy + s * ay),
                          (vx + s * bx, vy + s * by)])
    return polys


def ink_centroid_x() -> float:
    """Horizontal centre of the mark's ink, opacity-weighted, measured."""
    k = 20                                   # px per unit, measurement only
    img = render_layer(64 * k, 64 * k, k, 0, 0, stroke=MARK_STROKE,
                       facets=True, ss=1)
    a = np.asarray(img.getchannel("A"), np.float64)
    xs = np.arange(a.shape[1]) + 0.5
    return float((a.sum(axis=0) * xs).sum() / a.sum() / k)


@functools.lru_cache(maxsize=None)
def optical_centre_x() -> float:
    box = (min(x for x, _ in MARK_OUTLINE) + max(x for x, _ in MARK_OUTLINE)) / 2
    return (box + ink_centroid_x()) / 2


# ── rasterising ──────────────────────────────────────────────────────────

def render_layer(W, H, s, tx, ty, *, stroke, facets, dot=MARK_DOT,
                 ss=SS, color=CYAN):
    """The mark alone, RGBA at W x H. Mark unit (x, y) -> (tx + s*x, ty + s*y).

    Pillow fills polygons inclusive of both edges; drawing at `ss` times
    the size with a half-subpixel shift makes that a symmetric 1/ss bloat,
    i.e. nothing at the reduced size.
    """

    def P(pts):
        return [((tx + s * x) * ss - 0.5, (ty + s * y) * ss - 0.5) for x, y in pts]

    def layer_from(mask, opacity):
        if opacity < 1:
            mask = mask.point(lambda v: round(v * opacity))
        lay = Image.new("RGBA", mask.size, (*color, 0))
        lay.putalpha(mask)
        return lay

    big = Image.new("RGBA", (W * ss, H * ss), (*color, 0))

    m = Image.new("L", big.size, 0)
    d = ImageDraw.Draw(m)
    for poly in stroke_polygons(MARK_OUTLINE, stroke):
        d.polygon(P(poly), fill=255)
    big.alpha_composite(layer_from(m, 1.0))

    if facets:
        for pts, op in MARK_FACETS:
            m = Image.new("L", big.size, 0)
            ImageDraw.Draw(m).polygon(P(pts), fill=255)
            big.alpha_composite(layer_from(m, op))

    if dot:
        cx, cy, r = dot
        m = Image.new("L", big.size, 0)
        (x0, y0), (x1, y1) = P([(cx - r, cy - r), (cx + r, cy + r)])
        ImageDraw.Draw(m).ellipse([x0, y0, x1, y1], fill=255)
        big.alpha_composite(layer_from(m, 1.0))

    return big.reduce(ss) if ss > 1 else big


def tile(size, *, radius=TILE_RADIUS, bg=BG):
    """The rounded #050505 square (or full-bleed when radius is 0)."""
    n = size * SS
    big = Image.new("RGBA", (n, n), (*bg, 0))   # transparent corners keep bg rgb
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, n - 1, n - 1], radius=radius * n, fill=(*bg, 255))
    return big.reduce(SS)


def placement(size, span_px, stroke_px, cx_mark, *, snap):
    """Scale and offset that put the mark in a size x size box.

    span_px is the distance between the top and bottom outline centrelines.
    With snap, the spine's left edge and the top edge land on whole pixels
    (pick span_px + stroke_px even and the bottom edge follows).
    """
    s = span_px / SPAN
    tx = size / 2 - s * cx_mark
    ty = size / 2 - s * MID_Y
    if snap:
        left = tx + s * SPINE_X - stroke_px / 2
        tx += round(left) - left
        top = ty + s * TOP_Y - stroke_px / 2
        ty += round(top) - top
    return s, tx, ty


def icon(size, *, span_px, stroke_px, facets, dot_px=None,
         radius=TILE_RADIUS, snap=False, cx_mark=None):
    """The mark on its tile at `size`. dot_px, when given, makes the dot
    that many pixels across with its centre snapped to the pixel grid — at
    16px an unsnapped 1.3px dot straddles four pixels and reads as a smudge.
    dot_px=0 drops the dot."""
    cx_mark = optical_centre_x() if cx_mark is None else cx_mark
    s, tx, ty = placement(size, span_px, stroke_px, cx_mark, snap=snap)
    dot = MARK_DOT
    if dot_px == 0:
        dot = None
    elif dot_px:
        cx, cy, _ = MARK_DOT
        half = 0 if dot_px % 2 == 0 else 0.5            # odd: centre on a pixel
        px = round(tx + s * cx - half) + half
        py = round(ty + s * cy - half) + half
        dot = ((px - tx) / s, (py - ty) / s, dot_px / 2 / s)
    img = tile(size, radius=radius)
    img.alpha_composite(render_layer(size, size, s, tx, ty, stroke=stroke_px / s,
                                     facets=facets, dot=dot))
    return img


# Per-frame tuning for the ICO, chosen by rendering candidates and
# comparing them enlarged. The mark's stroked height is ~80% of every frame.
#   16: 1px stroke (canonical would be 0.65px, a grey hairline). span 12
#       puts the spine, the short left edge and the top edge all on whole
#       pixels (20 units = exactly 5px). No facets: at 16px they are under
#       one grey level. The dot is a snapped 2x2 block; unsnapped it
#       straddles four pixels and fuses with the stroke.
#   32: 2px stroke, facets back (the 18% one reads clearly, the 9% one
#       faintly), a snapped 3px dot.
#   48: 2px stroke, close to canonical (2.1px) and crisp where 2.4 left a
#       half-lit row along the bottom edge; 4px dot.
TUNING = {
    16: dict(span_px=12, stroke_px=1, facets=False, dot_px=2),
    32: dict(span_px=24, stroke_px=2, facets=True, dot_px=3),
    48: dict(span_px=36, stroke_px=2, facets=True, dot_px=4),
}


# ── ICO, written by hand ─────────────────────────────────────────────────

def write_ico(path: pathlib.Path, images: list[Image.Image]) -> None:
    """A multi-size ICO whose entries are independently drawn.

    Pillow's ICO writer resamples one image to every requested size, which
    would undo the per-size tuning. The container is a 6-byte header and a
    16-byte record per image; PNG payloads are valid in ICO and every
    browser in use reads them.
    """
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


# ── fonts ────────────────────────────────────────────────────────────────

def font(name: str, size: float, **axes) -> ImageFont.FreeTypeFont:
    """A vendored variable font with its axes set by name, e.g. wght=700."""
    path = FONT_DIR / name
    if not path.exists():
        sys.exit(f"missing {path} — the brand fonts are vendored in tools/fonts/")
    f = ImageFont.truetype(str(path), size)
    names = {"wght": b"Weight", "opsz": b"Optical size"}
    values = []
    for ax in f.get_variation_axes():
        key = next((k for k, v in names.items() if v == ax["name"]), None)
        v = axes.get(key, ax["default"])
        values.append(min(max(v, ax["minimum"]), ax["maximum"]))
    f.set_variation_by_axes(values)
    return f


def syne(size, wght=700):
    return font("Syne[wght].ttf", size, wght=wght)


def dm_sans(size, wght=400):
    # Browsers set opsz to the CSS px size (font-optical-sizing: auto).
    return font("DMSans[opsz,wght].ttf", size, wght=wght, opsz=size)


def tracked_width(d, text, fnt, track):
    return d.textlength(text, font=fnt) + track * (len(text) - 1)


def draw_tracked(d, x, baseline, text, fnt, fill, track):
    """Letter-spacing for Pillow. Each glyph goes at the advance of the
    prefix before it, so the font's kerning survives the tracking."""
    for i, ch in enumerate(text):
        d.text((x + d.textlength(text[:i], font=fnt) + track * i, baseline),
               ch, font=fnt, fill=fill, anchor="ls")


# ── social card ──────────────────────────────────────────────────────────

def glow(W, H, cx, cy, rx, ry, peak) -> Image.Image:
    """#050505 with a soft cyan radial bloom, computed in float, dithered."""
    y, x = np.mgrid[0:H, 0:W].astype(np.float32)
    r2 = ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2
    a = peak * np.exp(-r2 * 2.2)                       # gaussian falloff
    rgb = np.array(BG, np.float32) * (1 - a[..., None]) \
        + np.array(CYAN, np.float32) * a[..., None]
    rng = np.random.default_rng(7)                     # fixed: reproducible
    rgb += rng.uniform(-0.5, 0.5, rgb.shape).astype(np.float32)
    return Image.fromarray(np.clip(np.round(rgb), 0, 255).astype(np.uint8))


def og_image() -> Image.Image:
    W, H = 1200, 630
    word_px = 150
    f_word = syne(word_px, 700)
    f_tag = dm_sans(38, 400)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    # Lockup proportions from the brand spec: mark height ~1.3x the
    # wordmark's cap height, tracking -0.01em.
    cap = -f_word.getbbox("H", anchor="ls")[1]
    mark_h = 1.3 * cap
    track = -0.01 * word_px
    stroke_u = MARK_STROKE
    s = mark_h / (SPAN + stroke_u)
    xmin = min(x for x, _ in MARK_OUTLINE) - stroke_u / 2
    xmax = max(x for x, _ in MARK_OUTLINE) + stroke_u / 2
    mark_w = (xmax - xmin) * s

    word = "Cynea"
    word_ink_l = f_word.getbbox(word, anchor="ls")[0]   # C's side bearing
    word_w = tracked_width(probe, word, f_word, track) - word_ink_l
    gap = 0.30 * word_px
    lock_w = mark_w + gap + word_w

    tag = "Voice AI that feels human."
    tag_w = probe.textlength(tag, font=f_tag)

    # Block: lockup over tagline, centred, sitting a touch above the middle.
    tag_gap = 64
    tag_asc = -f_tag.getbbox("V", anchor="ls")[1]
    block_h = cap + tag_gap + tag_asc
    top = (H - block_h) / 2 - 12
    baseline = top + cap
    left = (W - lock_w) / 2

    mark_cy = baseline - cap / 2
    img = glow(W, H, cx=W / 2, cy=mark_cy + 20, rx=560, ry=330, peak=0.11).convert("RGBA")

    # The mark, drawn at card resolution: its box left edge at `left`.
    # Rendered into its own small box (supersampling the whole card would
    # be a 19200px canvas) and pasted at an integer offset; the fractional
    # part of the position stays in the layer's own transform.
    tx = left - s * xmin
    ty = mark_cy - s * MID_Y
    ox, oy = int(tx + s * xmin) - 2, int(ty + s * (TOP_Y - stroke_u)) - 2
    bw, bh = int(mark_w) + 6, int(mark_h) + 6
    img.alpha_composite(render_layer(bw, bh, s, tx - ox, ty - oy, stroke=stroke_u,
                                     facets=True), (ox, oy))

    d = ImageDraw.Draw(img)
    draw_tracked(d, left + mark_w + gap - word_ink_l, baseline, word, f_word,
                 (*TEXT, 255), track)
    d.text(((W - tag_w) / 2, baseline + tag_gap + tag_asc), tag, font=f_tag,
           fill=(*MUTED, 255), anchor="ls")
    return img.convert("RGB")


# ── the vector master ────────────────────────────────────────────────────

def svg() -> str:
    # 100-unit tile; the mark's stroked height is 72% of it.
    span_u = 72 / (SPAN + MARK_STROKE) * SPAN
    s = span_u / SPAN
    cx = optical_centre_x()
    tx, ty = 50 - s * cx, 50 - s * MID_Y
    facets = "\n".join(
        f'    <path class="facet" d="{_svg_d(p)}" fill="#00D4FF" fill-opacity="{op:g}"/>'
        for p, op in MARK_FACETS)
    dx, dy, r = MARK_DOT
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100" role="img" aria-label="Cynea">
  <title>Cynea</title>
  <!-- The Prism: Cynea's faceted C, cyan on the brand dark.
       Generated by tools/make_brand_icons.py — edit the mark there.
       Rendered at tab size (Chrome uses this file over the .ico) the
       2.8 stroke is a hairline and the faint facets smear, so small
       renders get a heavier stroke, and the tiniest lose the facets,
       as the ICO frames do. -->
  <style>
    @media (max-width: 40px) {{ .outline {{ stroke-width: 4px; }} }}
    @media (max-width: 24px) {{
      .outline {{ stroke-width: 5px; }}
      .facet {{ display: none; }}
    }}
  </style>
  <rect width="100" height="100" rx="{100 * TILE_RADIUS:g}" fill="#050505"/>
  <g transform="translate({tx:.3f} {ty:.3f}) scale({s:.4f})">
    <path class="outline" d="{_svg_d(MARK_OUTLINE)}" fill="none" stroke="#00D4FF" stroke-width="{MARK_STROKE:g}" stroke-linejoin="bevel"/>
{facets}
    <circle cx="{dx:g}" cy="{dy:g}" r="{r:g}" fill="#00D4FF"/>
  </g>
</svg>
"""


def main() -> int:
    ASSETS.mkdir(exist_ok=True)
    print(f"  optical centre x = {optical_centre_x():.2f} (box 27.00)")

    (ASSETS / "favicon.svg").write_text(svg(), encoding="utf-8")
    print("  favicon.svg")

    write_ico(ASSETS / "favicon.ico",
              [icon(size, snap=True, **t) for size, t in TUNING.items()])
    print("  favicon.ico          16 + 32 + 48, each drawn at its own size")

    # iOS masks to its own rounded square, so the touch icon is full-bleed;
    # the mark's stroked height is 60% of the tile.
    stroke_px = 6
    touch = icon(180, span_px=108 - stroke_px, stroke_px=stroke_px,
                 facets=True, radius=0, snap=True)
    touch.convert("RGB").save(ASSETS / "apple-touch-icon.png", optimize=True)
    print("  apple-touch-icon.png 180x180, full-bleed")

    og_image().save(ASSETS / "og-image.jpg", quality=92, optimize=True,
                    progressive=True, subsampling=0)
    print("  og-image.jpg         1200x630")
    return 0


if __name__ == "__main__":
    sys.exit(main())
