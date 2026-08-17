#!/usr/bin/env python3
"""Record the Fakturama window while a run drives it.

    python scripts/record.py --out docs/run.mp4 --stop-file /tmp/stop-recording

Captures the window's own backing store through the same path the vision driver
uses, rather than recording the screen. That has three consequences worth having:
the recording contains the application and nothing else that happens to be on the
desktop, it stays correct when another window overlaps Fakturama, and it needs no
screen-recording session of its own.

Frames are captured slowly and played back quickly, because a UI automation run
is mostly waiting - eight times speed turns eight minutes of waiting into a clip
someone will actually watch.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.driver.ax_driver import AXDriver

#: Encoded width, in pixels. The window is captured at Retina resolution; a clip
#: this wide stays readable and small enough to live in a git repository.
WIDTH = 1400


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/run.mp4")
    parser.add_argument("--fps", type=float, default=1.0, help="captured frames per second")
    parser.add_argument("--speed", type=float, default=8.0, help="playback speed multiplier")
    parser.add_argument("--seconds", type=float, default=1800.0, help="hard stop")
    parser.add_argument("--stop-file", help="stop as soon as this file exists")
    parser.add_argument("--frames-dir", default=None)
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        print("ffmpeg is required to encode the recording", file=sys.stderr)
        return 1

    stop_file = Path(args.stop_file) if args.stop_file else None
    if stop_file is not None and stop_file.exists():
        stop_file.unlink()

    frames = Path(args.frames_dir or Path(args.out).with_suffix("").as_posix() + ".frames")
    frames.mkdir(parents=True, exist_ok=True)
    for stale in frames.glob("*.jpg"):
        stale.unlink()

    driver = AXDriver()
    deadline = time.monotonic() + args.seconds
    interval = 1.0 / args.fps
    count = 0

    print(f"recording the Fakturama window at {args.fps}fps -> {args.out}")
    while time.monotonic() < deadline:
        if stop_file is not None and stop_file.exists():
            break
        started = time.monotonic()
        if _grab(driver, frames / f"{count:05d}.jpg"):
            count += 1
        time.sleep(max(0.0, interval - (time.monotonic() - started)))

    if not count:
        print("no frames were captured - is Fakturama running?", file=sys.stderr)
        return 1

    _encode(frames, Path(args.out), args.fps * args.speed)
    shutil.rmtree(frames, ignore_errors=True)
    size = Path(args.out).stat().st_size / 1e6
    print(f"wrote {args.out} ({count} frames, {size:.1f} MB)")
    return 0


def _grab(driver: AXDriver, destination: Path) -> bool:
    """Capture one frame, downscaled. Returns False if the window was unavailable."""
    from PIL import Image

    raw = destination.with_suffix(".png")
    try:
        if driver.screenshot(str(raw)) is None:
            return False
        image = Image.open(raw)
        height = round(image.height * WIDTH / image.width)
        image.convert("RGB").resize((WIDTH, height), Image.LANCZOS).save(
            destination, quality=80
        )
        return True
    except Exception:  # noqa: BLE001 - a dropped frame must not end the recording
        return False
    finally:
        raw.unlink(missing_ok=True)


def _encode(frames: Path, out: Path, framerate: float) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-framerate", f"{framerate:g}",
            "-i", str(frames / "%05d.jpg"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26",
            # yuv420p needs even dimensions; -2 keeps the aspect ratio.
            "-vf", f"scale={WIDTH}:-2",
            str(out),
        ],
        check=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
