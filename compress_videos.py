#!/usr/bin/env python3
"""Compress every .mp4 in assets/ for fast web loading.

Targets 720p / CRF 28 (no audio — these are muted background videos), which
typically lands each clip in the ~1-2 MB range. Each original is kept as a
`.mp4.bak` backup; files that already have a backup are skipped so the script
is safe to re-run.

Usage:
    python compress_videos.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"
CRF = "28"
TARGET_HEIGHT = 720


def human(num_bytes: float) -> str:
    """Human-readable file size."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def compress(src: Path, backup: Path) -> bool:
    """Compress `backup` (the moved original) back into `src`. Returns success."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(backup),
        # Downscale to 720p height, keep aspect, force even width; never upscale.
        "-vf", f"scale=-2:min({TARGET_HEIGHT}\\,ih)",
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", CRF,
        "-pix_fmt", "yuv420p",
        "-an",                       # drop audio (videos play muted)
        "-movflags", "+faststart",   # web-friendly: moov atom up front
        str(src),
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode != 0 or not src.exists():
        last_line = ""
        if result.stderr:
            lines = result.stderr.decode(errors="replace").strip().splitlines()
            last_line = lines[-1] if lines else ""
        print(f"   x ffmpeg failed{(' - ' + last_line) if last_line else ''}")
        return False
    return True


def main() -> None:
    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found. Install with: winget install ffmpeg")
        sys.exit(1)

    if not ASSETS.is_dir():
        print(f"assets/ folder not found at {ASSETS}")
        sys.exit(1)

    mp4s = sorted(p for p in ASSETS.glob("*.mp4") if p.is_file())
    if not mp4s:
        print("No .mp4 files found in assets/.")
        return

    total_before = 0
    total_after = 0
    processed = 0
    skipped = 0

    for src in mp4s:
        # "world.mp4" -> "world.mp4.bak"  (handles spaces in names too)
        backup = src.parent / (src.name + ".bak")

        if backup.exists():
            print(f"-> Skipping {src.name} (already compressed - backup exists)")
            skipped += 1
            continue

        before = src.stat().st_size
        print(f"\n>> {src.name}  ({human(before)})")

        # Move the original aside, then compress it back to the real name.
        src.rename(backup)
        if not compress(src, backup):
            # Restore the original on any failure so nothing is lost.
            if src.exists():
                src.unlink()
            backup.rename(src)
            print("   original restored.")
            continue

        after = src.stat().st_size
        total_before += before
        total_after += after
        processed += 1
        pct = (1 - after / before) * 100 if before else 0
        print(f"   {human(before)} -> {human(after)}  ({pct:.0f}% smaller)")

    print("\n" + "=" * 50)
    print(f"Processed: {processed}    Skipped: {skipped}")
    if processed:
        saved = total_before - total_after
        pct = (saved / total_before) * 100 if total_before else 0
        print(f"Total:  {human(total_before)} -> {human(total_after)}")
        print(f"Saved:  {human(saved)}  ({pct:.0f}% smaller)")
    print("Originals kept as *.mp4.bak - delete them once you're happy with the results.")


if __name__ == "__main__":
    main()
