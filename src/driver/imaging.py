"""Image plumbing for the OCR layer: preparing crops and deduplicating words.

Separated from the driver because it is the part with no knowledge of Fakturama or
of accessibility - given pixels and boxes it prepares one and cleans up the other.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import DriverError

if TYPE_CHECKING:  # imported for typing only; vision_driver imports this module
    from .vision_driver import TextBox


def collapse(boxes, tolerance: float = 8.0) -> list[TextBox]:
    """Merge boxes describing the same on-screen word.

    Duplicates arise two ways, and only the second is obvious. Overlapping strips
    report the same word twice with slightly different baselines. Less obviously,
    the same word read by different page-segmentation modes comes back *spelled*
    differently: one screen returned `CustRef.`, `Cust.Ref.` and `Cust-Ref.` for
    a single label, all at the same coordinates. Comparing text would count that
    label three times and report an ambiguity that does not exist on screen.

    So position identifies a word, not its text - nothing else can be printed in
    the same place - and the highest-confidence reading is the one kept.
    """
    kept: list[TextBox] = []
    for box in sorted(boxes, key=lambda box: -box.confidence):
        for existing in kept:
            if (
                abs(existing.x - box.x) <= tolerance
                and abs(existing.y - box.y) <= tolerance
            ):
                break
        else:
            kept.append(box)
    return kept


def crop_and_upscale(
    path: str, box: tuple[float, float, float, float] | None, factor: int
) -> str:
    """Write a cropped, upscaled copy of an image and return its path."""
    from PIL import Image  # imported lazily; only the vision path needs it

    image = Image.open(path)
    if box is not None:
        left, top, right, bottom = (int(round(v)) for v in box)
        left, top = max(0, left), max(0, top)
        right, bottom = min(image.width, right), min(image.height, bottom)
        if right <= left or bottom <= top:
            raise DriverError(f"empty crop box {box} for image {image.size}")
        image = image.crop((left, top, right, bottom))
    if factor > 1:
        image = image.resize((image.width * factor, image.height * factor), Image.LANCZOS)
    output = path.replace(".png", "") + f".ocr{factor}.png"
    image.save(output)
    return output


def png_width(path: str) -> float:
    """Pixel width from the PNG header - avoids a Pillow dependency here."""
    with open(path, "rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise DriverError(f"{path} is not a PNG")
    return float(int.from_bytes(header[16:20], "big"))
