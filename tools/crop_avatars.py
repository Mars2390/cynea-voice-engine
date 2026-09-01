#!/usr/bin/env python3
"""Crop the agent photographs to square avatars and encode them for the web.

    python tools/crop_avatars.py            # write assets/avatar-<name>.{webp,jpg}
    python tools/crop_avatars.py --preview  # 480px contact sheet, writes nothing else
    python tools/crop_avatars.py --dry-run

Why not optimize_images.py
--------------------------
That script centre-crops, which is right for the scenery but wrong here. These
are tightly framed portraits and a centre crop lands on a chin or, for Maya,
on the background behind her. Each photo therefore gets a hand-placed window,
recorded below as fractions of the source so it survives a re-export at a
different resolution.

It also reads its masters from assets/_originals/. That stash held the *old*
avatars, so running it against these photographs would have overwritten them
with the images they replaced. This script stashes the current masters first
and always crops from the stash, which makes re-runs idempotent.

Output: 240x240, which is 2x the largest box any avatar renders into
(.agent-figure is 120px; the dashboard and manager cards are 80px and the
hero caller is 68px). WebP is what browsers actually load — the JPEG is the
<picture> fallback.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("Pillow is required:  pip install Pillow")

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ORIGINALS = ASSETS / "_originals"

SIZE = 240          # 2x the 120px .agent-figure, the largest box in play
WEBP_Q = 82
JPEG_Q = 82

# Crop windows as fractions of the source, (centre_x, centre_y, side).
# `side` is a fraction of the shorter edge. Placed by eye against a thirds
# grid; the note says what the window is holding on to.
CROPS: dict[str, tuple[float, float, float, str]] = {
    "kwame": (0.65, 0.30, 0.93, "pulled back to the whole profile, brow to jaw"),
    "amina": (0.56, 0.45, 0.92, "as wide as the source allows: nose to shoulder"),
    "kofi":  (0.65, 0.30, 0.62, "smile and beard; the source itself crops above the eyes"),
    "maya":  (0.38, 0.30, 0.58, "her head at the left third, not the room behind her"),
}


def master(name: str) -> pathlib.Path:
    """The full-resolution source, stashing it on first sight.

    The stash is the master copy. If a new photograph is dropped into
    assets/, delete the stashed one and re-run to adopt it.
    """
    stashed = ORIGINALS / f"avatar-{name}.jpg"
    live = ASSETS / f"avatar-{name}.jpg"

    if not stashed.exists():
        if not live.exists():
            sys.exit(f"no photo for {name}: expected {live}")
        ORIGINALS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live, stashed)
        print(f"  stashed master  {stashed.relative_to(ROOT)}")
    return stashed


def window(size: tuple[int, int], spec: tuple[float, float, float, str]) -> tuple[int, int, int, int]:
    """Pixel crop box, clamped so it never runs off the source."""
    width, height = size
    cx, cy, side, _ = spec
    length = int(min(width, height) * side)
    left = int(cx * width) - length // 2
    top = int(cy * height) - length // 2
    left = max(0, min(left, width - length))
    top = max(0, min(top, height - length))
    return left, top, left + length, top + length


def render(name: str, spec, dry: bool) -> Image.Image:
    source = Image.open(master(name))
    source = ImageOps.exif_transpose(source).convert("RGB")
    box = window(source.size, spec)
    face = source.crop(box).resize((SIZE, SIZE), Image.LANCZOS)

    if not dry:
        webp = ASSETS / f"avatar-{name}.webp"
        jpeg = ASSETS / f"avatar-{name}.jpg"
        face.save(webp, "WEBP", quality=WEBP_Q, method=6)
        face.save(jpeg, "JPEG", quality=JPEG_Q, optimize=True, progressive=True)
        print(f"  {name:6} {source.size[0]}x{source.size[1]} -> {SIZE}x{SIZE}  "
              f"webp {webp.stat().st_size/1024:5.1f} KB   "
              f"jpg {jpeg.stat().st_size/1024:5.1f} KB   {spec[3]}")
    else:
        print(f"  {name:6} would crop {box} of {source.size}")
    return face


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--preview", action="store_true",
                    help="write a contact sheet of the crops and stop")
    args = ap.parse_args()

    faces = {}
    for name, spec in CROPS.items():
        faces[name] = render(name, spec, args.dry_run or args.preview)

    if args.preview:
        sheet = Image.new("RGB", (SIZE * len(faces), SIZE), (251, 248, 245))
        for i, face in enumerate(faces.values()):
            sheet.paste(face, (i * SIZE, 0))
        out = ROOT / "avatar-preview.png"
        sheet.save(out)
        print(f"\ncontact sheet: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
