"""Pixel-grounded binding of `UIDriver`, for the parts SWT hides from accessibility.

Fakturama's document editor (Cust.Ref, Date, price mode, the Items table, the
address selector icons) is invisible to the accessibility tree, so those controls
are located from a screenshot instead.

Grounding is layered, cheapest and most precise first:

1. **OCR anchoring** - Tesseract returns exact pixel boxes for every visible word.
   Labels in this UI sit immediately left of the control they name, so a control
   is addressed as "the field to the right of `Cust.Ref.`". Nothing is guessed.
2. **LLM fallback** - for controls with no nearby text (icons), a vision model is
   asked to locate the control in the screenshot.

Both compute coordinates per frame from the current window position, so the
window may be moved or resized between steps. No coordinate is ever hardcoded.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Sequence

from .base import DriverError, Element, ElementNotFound
from .imaging import collapse, crop_and_upscale, png_width


#: Height of an editor's tab strip, in points; excluded from its content region.
TAB_STRIP_HEIGHT = 24.0


@dataclass(frozen=True)
class Region:
    """An axis-aligned area of the screen, in points."""

    left: float
    top: float
    right: float
    bottom: float

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x <= self.right and self.top <= y <= self.bottom

    def __str__(self) -> str:
        return f"({self.left:.0f},{self.top:.0f})-({self.right:.0f},{self.bottom:.0f})"


@dataclass(frozen=True)
class TextBox:
    """A word recognised on screen, in *screen point* coordinates."""

    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def middle_y(self) -> float:
        return self.y + self.height / 2


class VisionDriver:
    """Locates and operates controls from screenshots of the target window."""

    def __init__(self, ax_driver, llm_locate=None) -> None:
        #: Used for window geometry and capture; vision never re-implements those.
        self.ax = ax_driver
        #: Optional callable(image_path, description) -> (px, py) in image pixels.
        self.llm_locate = llm_locate
        for tool in ("tesseract", "cliclick"):
            if shutil.which(tool) is None:
                raise DriverError(f"required tool {tool!r} is not installed")

    # -------------------------------------------------------------- geometry

    def window_frame(self) -> tuple[float, float, float, float]:
        """Window origin and size in screen points, read from the AX tree."""
        root = self.ax.snapshot()[0]
        if root.width <= 0 or root.height <= 0:
            raise DriverError("could not determine window geometry")
        return root.x, root.y, root.width, root.height

    def capture(self, path: str | None = None) -> tuple[str, float, float, float]:
        """Capture the window.

        Returns the image path plus the mapping back to screen points:
        (path, origin_x, origin_y, scale) where scale is image pixels per point
        (2.0 on a Retina display).
        """
        if path is None:
            handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            handle.close()
            path = handle.name
        # Reading no longer needs the foreground: the capture comes from the
        # window's backing store, not from the screen.
        origin_x, origin_y, width_pt, _ = self.window_frame()
        if self.ax.screenshot(path) is None:
            raise DriverError(
                "window capture failed - the window must be on screen. "
                "Call driver.activate() first."
            )
        width_px = png_width(path)
        scale = width_px / width_pt if width_pt else 1.0
        return path, origin_x, origin_y, scale

    # ------------------------------------------------------------------- OCR

    def words(
        self,
        region: Region | None = None,
        min_confidence: float = 40.0,
        upscale: int = 2,
        psm: str | Sequence[str] = ("4", "3"),
    ) -> list[TextBox]:
        """Every word Tesseract finds, in screen points.

        Accuracy on this UI depends heavily on preprocessing. Cropping to the
        panel of interest and upscaling before recognition is what makes values
        inside input fields legible; recognising the whole 3000x1800 window in
        one pass silently drops entire bands of the form. PSM 4 (single column
        of variable-size text) measured best on Fakturama's editors.

        Recall also varies between page-segmentation modes on the same image -
        one pass finds `Cust.Ref.` but drops `Date`, the next does the reverse -
        so several modes are run and their results merged. Missing a label means
        a control cannot be found at all, which matters far more here than the
        cost of an extra pass.

        Treat the result as reliable for *locating* labels but not for reading
        back exact values - Tesseract misreads characters in this UI's field
        font (`PO-2026-0412` came back as `(PO-2026-0419`). Value verification
        goes through the vision model instead.
        """
        modes = (psm,) if isinstance(psm, str) else tuple(psm)
        image, origin_x, origin_y, scale = self.capture()
        crop_left = crop_top = 0.0
        if region is not None:
            crop_left = max(0.0, (region.left - origin_x) * scale)
            crop_top = max(0.0, (region.top - origin_y) * scale)
            image = crop_and_upscale(
                image,
                (crop_left, crop_top,
                 (region.right - origin_x) * scale, (region.bottom - origin_y) * scale),
                upscale,
            )
        elif upscale > 1:
            image = crop_and_upscale(image, None, upscale)

        merged: dict[tuple[str, int, int], TextBox] = {}
        for mode in modes:
            result = subprocess.run(
                ["tesseract", image, "stdout", "--psm", mode, "tsv"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise DriverError(f"tesseract failed: {result.stderr[:200]}")

            for line in result.stdout.splitlines()[1:]:
                parts = line.split("\t")
                if len(parts) < 12:
                    continue
                text = parts[11].strip()
                if not text:
                    continue
                try:
                    left, top, width, height = (float(parts[i]) for i in (6, 7, 8, 9))
                    confidence = float(parts[10])
                except ValueError:
                    continue
                if confidence < min_confidence:
                    continue
                # OCR pixels -> full-window pixels -> screen points.
                box = TextBox(
                    text=text,
                    x=origin_x + (crop_left + left / upscale) / scale,
                    y=origin_y + (crop_top + top / upscale) / scale,
                    width=width / upscale / scale,
                    height=height / upscale / scale,
                    confidence=confidence,
                )
                # Same word found by two modes lands within a few points; bucket
                # on a coarse grid so it is stored once, keeping the better read.
                key = (box.text, round(box.x / 8), round(box.y / 8))
                if key not in merged or merged[key].confidence < confidence:
                    merged[key] = box
        return list(merged.values())

    def words_banded(self, region: Region, height: float = 60.0) -> list[TextBox]:
        """Recognise a region in overlapping strips and merge the results.

        Recall degrades badly with crop height: a selector row that reads
        perfectly in a 22-point band is dropped entirely when the whole 470-point
        list is recognised in one pass. Strips half-overlap so a line straddling
        a boundary still falls wholly inside some strip, and duplicates from the
        overlap are collapsed on a coarse grid.

        Use this for anything list-shaped or taller than a few rows.
        """
        merged: dict[tuple[str, int, int], TextBox] = {}
        top = region.top
        while top < region.bottom:
            strip = Region(
                region.left, top, region.right, min(top + height, region.bottom)
            )
            for word in self.words(region=strip):
                merged[(word.text, round(word.x / 8), round(word.y / 8))] = word
            top += height / 2
        return list(merged.values())

    def words_wide(self, region: Region, width: float = 320.0) -> list[TextBox]:
        """Recognise a wide, short region in overlapping vertical slices.

        A table header is ~1200 points wide and ~20 tall. At that aspect ratio
        recognition is unreliable - some captions come back, others silently do
        not, and which ones varies between passes. Splitting into slices gives
        each one a sane shape.
        """
        merged: dict[tuple[str, int, int], TextBox] = {}
        left = region.left
        while left < region.right:
            slice_ = Region(
                left, region.top, min(left + width, region.right), region.bottom
            )
            for word in self.words(region=slice_):
                merged[(word.text, round(word.x / 8), round(word.y / 8))] = word
            left += width / 2
        return list(merged.values())

    def read_region(self, region: Region) -> str:
        """All text in a region as one string - used to verify saved values."""
        return " ".join(word.text for word in self.words(region=region))

    #: Page-segmentation modes for a crop known to hold exactly one line of text.
    LINE_MODES = ("7", "6", "4")

    def read_line(self, region: Region) -> str:
        """Read a crop that is known to hold a single line of text.

        The default modes assume a column of text, and on a wide, short crop they
        lose words out of the middle of it: the Invoice's `Order Date` field came
        back as `Date 2026`, having dropped `Mar 12,` from between them. PSM 7 says
        "this is one line" and recovers the whole field, so field-level reads use
        this and panel-level reads use `read_region`.
        """
        # Collapsed because the modes disagree slightly on where a word is, and
        # without it a line comes back as `Mar 12, 2026 2026 | Nordlicht Handels
        # Handels` - which matches everything it should, but is unreadable as
        # evidence in a log.
        found = collapse(self.words(region=region, psm=self.LINE_MODES))
        return " ".join(word.text for word in sorted(found, key=lambda word: word.x))

    def find_text(
        self,
        pattern: str,
        words: Sequence[TextBox] | None = None,
        region: Region | None = None,
    ) -> list[TextBox]:
        """Words matching a regex (case-insensitive), optionally within a region."""
        regex = re.compile(pattern, re.IGNORECASE)
        if words is None:
            candidates = self.words(region=region)
        else:
            candidates = words
            if region is not None:
                candidates = [w for w in candidates if region.contains(*w.center)]
        return [w for w in candidates if regex.search(w.text)]

    def find_label(
        self,
        pattern: str,
        words: Sequence[TextBox] | None = None,
        region: Region | None = None,
        attempts: int = 3,
    ) -> TextBox:
        """Exactly one word matching, else raise - ambiguity is never guessed away.

        Labels repeat across panels in this UI (`Cust.Ref.` appears both in the
        editor and as a Documents column header), so callers scope by region.

        A *miss* is retried with a fresh capture, because Tesseract's recall on
        this UI varies between passes on an unchanged screen - the same label is
        found on one pass and dropped on the next. A retry costs a second; a
        spurious miss costs the run. Ambiguity is not retried: two matches is a
        real condition, not a flake.
        """
        regex = re.compile(pattern, re.IGNORECASE)

        def unique(candidates: Sequence[TextBox]) -> TextBox | None:
            hits = collapse(w for w in candidates if regex.search(w.text))
            if len(hits) > 1:
                raise DriverError(
                    f"{len(hits)} on-screen texts matched {pattern!r}: "
                    f"{[(h.text, round(h.x), round(h.y)) for h in hits]}"
                )
            return hits[0] if hits else None

        if words is not None:
            hit = unique(words)
            if hit is not None:
                return hit
            raise ElementNotFound(f"no on-screen text matching {pattern!r}")

        # Recall depends strongly on crop size, so try progressively smaller
        # crops rather than repeating the same large one. A tall region read in
        # a single pass drops individual words unpredictably; the same region
        # read in strips does not. Ambiguity propagates immediately - two
        # matches is a real condition, not something a retry should paper over.
        for attempt in range(attempts):
            strategies = (
                lambda: self.words_banded(region, height=120) if region else self.words(),
                lambda: self.words_banded(region, height=50) if region else self.words(),
                lambda: self.words(region=region),
            )
            for strategy in strategies:
                hit = unique(strategy())
                if hit is not None:
                    return hit
            if attempt < attempts - 1:
                time.sleep(0.4)

        where = f" within {region}" if region else ""
        raise ElementNotFound(f"no on-screen text matching {pattern!r}{where}")

    def point_right_of(
        self,
        label_pattern: str,
        offset: float = 40.0,
        words: Sequence[TextBox] | None = None,
        region: Region | None = None,
    ) -> tuple[float, float]:
        """A click point inside the control sitting to the right of a label."""
        label = self.find_label(label_pattern, words, region)
        return label.right + offset, label.middle_y

    def region_of(self, role: str, title_pattern: str) -> Region:
        """Region of an accessibility element, matched by regex on its title.

        This is the hybrid seam: the accessibility tree still knows the layout
        containers even where it cannot see the controls inside them, so AX
        supplies the bounds and OCR reads what is inside them.
        """
        regex = re.compile(title_pattern)
        hits = [
            element
            for element in self.ax.snapshot()
            if element.role == role and element.title and regex.search(element.title)
        ]
        if not hits:
            raise ElementNotFound(f"no {role} titled ~{title_pattern!r} to scope to")
        if len(hits) > 1:
            raise DriverError(
                f"{len(hits)} {role} elements matched ~{title_pattern!r}: "
                f"{[h.title for h in hits]}"
            )
        element = hits[0]
        return Region(
            element.x, element.y, element.x + element.width, element.y + element.height
        )

    def editor_region(self, name: str = "New Order") -> Region:
        """Bounds of the open document editor's *content*.

        The tab strip is excluded. Tab captions are named after the records they
        hold, so an open `VAT 19%` tab otherwise collides with the `VAT` field
        label inside the editor and makes the lookup ambiguous.
        """
        region = self.region_of("AXTabGroup", rf"^\*?{re.escape(name)}$")
        return Region(
            region.left, region.top + TAB_STRIP_HEIGHT, region.right, region.bottom
        )

    def locate_by_description(self, description: str) -> tuple[float, float]:
        """LLM fallback for controls with no adjacent text, such as icons."""
        if self.llm_locate is None:
            raise DriverError(
                f"cannot locate {description!r}: no LLM grounding callable configured"
            )
        image, origin_x, origin_y, scale = self.capture()
        pixel_x, pixel_y = self.llm_locate(image, description)
        return origin_x + pixel_x / scale, origin_y + pixel_y / scale

    # --------------------------------------------------------------- actions

    def _ensure_frontmost(self) -> None:
        """Guarantee the click or keystroke reaches the target application.

        Synthetic events go to whichever application owns the foreground, so any
        focus loss between locating a control and operating it would deliver the
        event to an unrelated window. This is not theoretical: with macOS Stage
        Manager enabled the target window is tucked away as soon as another app
        takes focus, and an unguarded click lands in that other app.
        """
        if not self.ax.is_active():
            self.ax.activate()

    def click(self, point: tuple[float, float], double: bool = False) -> None:
        self._ensure_frontmost()
        verb = "dc" if double else "c"
        _cliclick(f"{verb}:{round(point[0])},{round(point[1])}")

    def type_text(self, text: str) -> None:
        """Type into whatever currently has keyboard focus.

        Routed through System Events rather than cliclick so non-ASCII characters
        in German addresses (umlauts, sharp s) are delivered correctly.
        """
        self._ensure_frontmost()
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e", f'tell application "System Events" to keystroke "{escaped}"'],
            check=False,
            capture_output=True,
        )

    def press_key(self, key: str, modifiers: Sequence[str] = ()) -> None:
        """Press a named key, e.g. press_key('a', ['command']) to select all."""
        self._ensure_frontmost()
        using = ""
        if modifiers:
            joined = ", ".join(f"{m} down" for m in modifiers)
            using = f" using {{{joined}}}"
        named = {
            "tab": 48,
            "return": 36,
            "escape": 53,
            "delete": 51,
            "left": 123,
            "right": 124,
            "down": 125,
            "up": 126,
        }
        if key in named:
            script = f'tell application "System Events" to key code {named[key]}{using}'
        else:
            script = f'tell application "System Events" to keystroke "{key}"{using}'
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)

    def set_field(self, point: tuple[float, float], text: str) -> None:
        """Focus a text control, clear it, and type a new value."""
        self.click(point)
        self.press_key("a", ["command"])
        self.press_key("delete")
        self.type_text(text)

    # ------------------------------------------------- UIDriver conformance

    def activate(self) -> None:
        self.ax.activate()

    def snapshot(self) -> list[Element]:
        """Recognised words presented as elements, so the flow can log them."""
        return [
            Element(
                role="OCRText",
                value=word.text,
                x=word.x,
                y=word.y,
                width=word.width,
                height=word.height,
                source="vision",
            )
            for word in self.words()
        ]

    def press(self, element: Element) -> None:
        self.click(element.center)

    def set_value(self, element: Element, text: str) -> None:
        self.set_field(element.center, text)

    def screenshot(self, path: str) -> str | None:
        return self.ax.screenshot(path)


def _cliclick(*commands: str) -> None:
    result = subprocess.run(["cliclick", *commands], capture_output=True, text=True)
    if result.returncode != 0:
        raise DriverError(f"cliclick {commands} failed: {result.stderr[:200]}")
