"""Render assets/sample_order.html to assets/sample_order.png.

Tries Playwright first, then falls back to headless Google Chrome, then to
wkhtmltoimage. Run with the repo venv:

    .venv/bin/python scripts/render_sample_order.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HTML = REPO / "assets" / "sample_order.html"
PNG = REPO / "assets" / "sample_order.png"
WIDTH = 794  # A4 at 96 dpi

CHROME_BINARIES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def render_playwright() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": WIDTH, "height": 1123},
                                    device_scale_factor=2)
            page.goto(HTML.as_uri())
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(PNG), full_page=True)
            browser.close()
        return True
    except Exception as exc:  # browser missing / version mismatch
        print(f"playwright unavailable: {exc}", file=sys.stderr)
        return False


def render_chrome() -> bool:
    binary = next((b for b in CHROME_BINARIES if Path(b).exists()), None)
    if binary is None:
        return False
    with tempfile.TemporaryDirectory() as profile:
        cmd = [
            binary,
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=2",
            f"--user-data-dir={profile}",
            f"--window-size={WIDTH},1123",
            f"--screenshot={PNG}",
            HTML.as_uri(),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(result.stderr[-2000:], file=sys.stderr)
    return PNG.exists()


def render_wkhtmltoimage() -> bool:
    binary = shutil.which("wkhtmltoimage")
    if binary is None:
        return False
    result = subprocess.run(
        [binary, "--width", str(WIDTH), "--zoom", "2", str(HTML), str(PNG)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(result.stderr[-2000:], file=sys.stderr)
    return PNG.exists()


def main() -> int:
    if not HTML.exists():
        print(f"missing {HTML}", file=sys.stderr)
        return 1
    PNG.unlink(missing_ok=True)
    for name, fn in (("playwright", render_playwright),
                     ("chrome", render_chrome),
                     ("wkhtmltoimage", render_wkhtmltoimage)):
        if fn():
            print(f"rendered via {name}: {PNG} ({PNG.stat().st_size} bytes)")
            return 0
    print("no renderer succeeded", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
