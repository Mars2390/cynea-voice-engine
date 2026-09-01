#!/usr/bin/env python3
"""Measure text contrast over the section photography, and fail if it drops.

    python tools/check_contrast.py            # exits non-zero on a WCAG failure
    python tools/check_contrast.py --verbose  # per-sample detail

Washing a photograph behind a section darkens the ground the body copy sits
on. The effect is meant to be felt and not seen, which means it must not cost
readability — so this samples the actual rendered background inside each
section that has a `.secbg`, at points where text really sits, and checks the
palette's text colours against the darkest sample.

The bar is WCAG AA: 4.5:1 for body text, 3:1 for large text. `--faint` is
reported for information only: it sits at 2.67:1 on the bare page ground and
is used for incidental labels, so the photography is not what breaks it.
"""

from __future__ import annotations

import argparse
import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

# The palette, straight from :root in index.html.
TEXT = {"ink": (0x17, 0x1A, 0x2E), "muted": (0x6E, 0x6B, 0x7C), "faint": (0x9B, 0x98, 0xA6)}
FLOOR = {"ink": 4.5, "muted": 4.5}          # faint is reported, not enforced


def parse(value: str):
    """`#RGB`, `#RRGGBB` or `rgb(r, g, b)` -> a tuple. None if unrecognised."""
    value = (value or "").strip()
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        if len(digits) == 6:
            return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
    if value.startswith("rgb"):
        parts = value[value.find("(") + 1:value.find(")")].replace("/", " ").replace(",", " ").split()
        if len(parts) >= 3:
            return tuple(int(float(p)) for p in parts[:3])
    return None


def rgb_hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(rgb)


def channel(value: float) -> float:
    value /= 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def luminance(rgb) -> float:
    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
        from PIL import Image
    except ImportError:
        print("needs playwright and Pillow", file=sys.stderr)
        return 1

    from check_captions import serve
    base, httpd = serve()
    worst: dict[str, tuple[float, str, tuple]] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        page.goto(f"{base}/index.html", wait_until="load")
        page.wait_for_timeout(400)

        count = page.evaluate("()=>document.querySelectorAll('.secbg').length")
        for index in range(count):
            page.evaluate(f"()=>document.querySelectorAll('.secbg')[{index}]"
                          ".scrollIntoView({block:'center'})")
            page.wait_for_timeout(1200)

            # Only bare ground counts. A screenshot's darkest pixel is a glyph
            # or the navy button, neither of which says anything about the
            # background text sits on — so the page is asked which points are
            # actually unoccupied, and only those are sampled. Cards paint
            # their own opaque background and are excluded for the same reason.
            probe = page.evaluate(f"""()=>{{
              const layer=document.querySelectorAll('.secbg')[{index}];
              const sec=layer.closest('section');
              const b=sec.getBoundingClientRect();
              const top=Math.max(72,b.top), bottom=Math.min(innerHeight,b.bottom);
              const points=[];
              for(let y=top+6;y<bottom-6;y+=14){{
                for(let x=12;x<innerWidth-12;x+=24){{
                  const el=document.elementFromPoint(x,y);
                  if(el===sec||el===document.body||el===document.documentElement)
                    points.push([x,y]);
                }}
              }}
              return {{name:sec.id||'security',top:top,bottom:bottom,points:points}};
            }}""")
            if not probe["points"]:
                continue
            name = probe["name"]
            height = probe["bottom"] - probe["top"]
            shot = page.screenshot(clip={"x": 0, "y": probe["top"], "width": 1440, "height": height})
            image = Image.open(io.BytesIO(shot)).convert("RGB")

            darkest = min(
                (image.getpixel((x, int(y - probe["top"])))
                 for x, y in probe["points"]
                 if 0 <= x < image.width and 0 <= y - probe["top"] < image.height),
                key=luminance)

            # Read the colours this section actually resolves, rather than the
            # ones :root declares — a section may narrow the palette for
            # exactly this reason, and checking the global value would then be
            # measuring text that is not on the screen.
            live = page.evaluate(f"""()=>{{
              const sec=document.querySelectorAll('.secbg')[{index}].closest('section');
              const cs=getComputedStyle(sec);
              const out={{}};
              for(const name of ['--ink','--muted','--faint'])
                out[name.slice(2)]=cs.getPropertyValue(name).trim();
              return out;
            }}""")
            colours = {}
            for label, fallback in TEXT.items():
                colours[label] = parse(live.get(label, "")) or fallback

            for label, colour in colours.items():
                value = contrast(colour, darkest)
                if label not in worst or value < worst[label][0]:
                    worst[label] = (value, name, darkest)
            if args.verbose:
                print(f"  {name:10} darkest ground {darkest}  "
                      f"muted {rgb_hex(colours['muted'])} "
                      f"{contrast(colours['muted'], darkest):.2f}:1")

        browser.close()
    httpd.shutdown()

    failed = []
    print("\nworst-case text contrast over section photography")
    for label, (value, where, ground) in worst.items():
        need = FLOOR.get(label)
        state = "reported only" if need is None else ("PASS" if value >= need else "FAIL")
        if need and value < need:
            failed.append(f"{label} {value:.2f}:1 in {where} (needs {need})")
        print(f"  {label:6} {value:5.2f}:1  darkest ground {ground} in {where}   {state}")

    if failed:
        print("\nFAILED: " + "; ".join(failed))
        return 1
    print("\nall enforced thresholds met")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
