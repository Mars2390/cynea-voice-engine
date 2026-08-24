#!/usr/bin/env python3
"""
optimize_images.py - Compress the Cynea landing-page imagery.

Strategy
--------
Every photo on index.html is rendered through `object-fit: cover` with the
default `object-position: 50% 50%`, i.e. the browser already centre-crops the
source down to the box the CSS gives it and throws the rest away. So we can
centre-crop to that same aspect ratio on disk and lose *nothing* visually,
then resize to 2x the largest CSS box the element ever occupies (retina).

Each image is encoded to WebP at q=80 and, if it lands over its byte budget,
the quality is stepped down until it fits. A same-size JPEG is emitted next to
it as the <picture> fallback for browsers without WebP.

Usage:  python optimize_images.py [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass, field

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("Pillow is required:  pip install Pillow")

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
# Full-resolution masters live here. Several outputs share a name with their
# source (avatar-maya.jpg -> avatar-maya.jpg), so encoding in place would
# clobber the master and make a second run recompress its own output. We stash
# the masters once and always read from the stash, which keeps this idempotent.
ORIGINALS = os.path.join(ASSETS, "_originals")

# Quality ladder: start at the requested 80 and back off only if over budget.
QUALITY_LADDER = [80, 76, 72, 68, 64, 60, 56, 52]


@dataclass
class Spec:
    """One source image and the box it actually renders into."""

    src: str  # filename inside assets/
    out: str  # output basename (no extension)
    box: tuple[int, int]  # target pixel size = 2x the largest CSS box
    budget_kb: int  # WebP size budget
    note: str = ""
    cover: bool = True  # True => CSS uses object-fit:cover, safe to centre-crop
    _results: dict = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Display-size analysis (CSS px at the widest breakpoint each element reaches)
#
#   .hl-photo    aspect-ratio 16/9, full wrap width -> 1152x648, 2x-ish at 1600
#   .stmt-tile   aspect-ratio 14/9, clamps to 190px wide -> 560 covers 2x
#   .slide-card  img max-width:1100px, natural 3:2 -> master's native 1500 is
#                the ceiling and keeps dashboard text readable
#   .pill-orb    38px thumbnails -> 240px square is ample at 3x
# ---------------------------------------------------------------------------

SPECS: list[Spec] = [
    # --- Highlights: one full-bleed landscape banner ----------------------
    Spec("usecase-callcenter.jpg", "hl-callcenter", (1600, 900), 170, "highlights banner 16/9"),
    # --- Statement banner 2: small warm tile set between the words --------
    Spec("usecase-hotel.jpg", "tile-hotel", (560, 360), 60, "statement tile 14/9"),
    # --- Product screenshots: shown at 1100px, must stay readable ---------
    Spec("shot-agents.png", "shot-agents", (1500, 0), 110, "carousel slide", cover=False),
    Spec("shot-dashboard.png", "shot-dashboard", (1500, 0), 110, "carousel slide", cover=False),
    # --- Agent avatars: small square thumbnails ---------------------------
    Spec("avatar-kwame.jpg", "avatar-kwame", (240, 240), 22, "square thumb"),
    Spec("avatar-kofi.jpg", "avatar-kofi", (240, 240), 22, "square thumb"),
    Spec("amina .jpg", "avatar-amina", (240, 240), 22, "square thumb (renamed)"),
    Spec("avatar-maya.jpg", "avatar-maya", (240, 240), 22, "square thumb"),
    # --- Remaining industry photos: kept for FAQ / future sections --------
    Spec("usecase-banking.jpg", "tile-banking", (560, 360), 60, "industry tile"),
    Spec("usecase-restaurant.jpg", "tile-restaurant", (560, 360), 60, "industry tile"),
    Spec("usecase-clinic.jpg", "tile-clinic", (560, 360), 60, "industry tile"),
]


def prepare(img: Image.Image, spec: Spec) -> Image.Image:
    """Centre-crop to the rendered aspect ratio, then downscale to the box."""
    img = ImageOps.exif_transpose(img)  # honour camera orientation before cropping
    if img.mode != "RGB":
        img = img.convert("RGB")

    tw, th = spec.box

    if not spec.cover:
        # Rendered at natural aspect ratio (width:100%; height:auto) - width only.
        if img.width <= tw:
            return img
        th = round(img.height * tw / img.width)
        return img.resize((tw, th), Image.LANCZOS)

    # object-fit:cover -> the browser centre-crops to `box` anyway. Do it here.
    # ImageOps.fit crops to the target aspect around the centre, then resizes.
    tw = min(tw, img.width)
    th = max(1, round(tw * spec.box[1] / spec.box[0]))
    return ImageOps.fit(img, (tw, th), method=Image.LANCZOS, centering=(0.5, 0.5))


def encode(img: Image.Image, path: str, fmt: str, budget_kb: int) -> tuple[int, int]:
    """Write `img` stepping quality down until it fits the budget. -> (kb, q)."""
    last = (0, 0)
    for q in QUALITY_LADDER:
        if fmt == "webp":
            img.save(path, "WEBP", quality=q, method=6)
        else:
            img.save(path, "JPEG", quality=q, optimize=True, progressive=True)
        kb = os.path.getsize(path) // 1024
        last = (kb, q)
        if kb <= budget_kb:
            return last
    return last  # ladder exhausted; keep the smallest we managed


def master_for(spec: Spec, dry_run: bool) -> str | None:
    """Return the path to the full-res master, stashing it on first run."""
    stashed = os.path.join(ORIGINALS, spec.src)
    if os.path.exists(stashed):
        return stashed

    live = os.path.join(ASSETS, spec.src)
    if not os.path.exists(live):
        return None
    if dry_run:
        return live

    os.makedirs(ORIGINALS, exist_ok=True)
    shutil.move(live, stashed)
    return stashed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    print(f"{'image':<24}{'before':>10}{'after':>10}{'q':>4}  {'dimensions':<13} saved")
    print("-" * 78)

    before_total = after_total = 0
    over_budget: list[str] = []
    missing: list[str] = []

    for spec in SPECS:
        src_path = master_for(spec, args.dry_run)
        if src_path is None:
            missing.append(spec.src)
            continue

        before_kb = os.path.getsize(src_path) // 1024
        before_total += before_kb

        with Image.open(src_path) as im:
            out = prepare(im, spec)

        if args.dry_run:
            print(f"{spec.out:<24}{before_kb:>8} KB{'  (dry)':>10}     {out.width}x{out.height}")
            continue

        webp_path = os.path.join(ASSETS, spec.out + ".webp")
        jpg_path = os.path.join(ASSETS, spec.out + ".jpg")

        webp_kb, q = encode(out, webp_path, "webp", spec.budget_kb)
        # <picture> fallback for browsers without WebP; a little more slack.
        encode(out, jpg_path, "jpeg", int(spec.budget_kb * 1.6))

        after_total += webp_kb
        saved = 100 - round(webp_kb / before_kb * 100) if before_kb else 0
        flag = "" if webp_kb <= spec.budget_kb else "  << OVER BUDGET"
        if flag:
            over_budget.append(f"{spec.out}: {webp_kb} KB > {spec.budget_kb} KB")
        dims = f"{out.width}x{out.height}"
        print(f"{spec.out:<24}{before_kb:>8} KB{webp_kb:>8} KB{q:>4}  {dims:<13} -{saved}%{flag}")

    if args.dry_run:
        return 0

    print("-" * 78)
    pct = 100 - round(after_total / before_total * 100) if before_total else 0
    print(f"{'TOTAL':<24}{before_total:>8} KB{after_total:>8} KB        "
          f"        -{pct}%  ({before_total / 1024:.1f} MB -> {after_total / 1024:.2f} MB)")

    if missing:
        print("\nMissing sources (skipped): " + ", ".join(missing))
    if over_budget:
        print("\nOver budget:")
        for line in over_budget:
            print("  - " + line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
