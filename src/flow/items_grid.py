"""Geometry of the Order's Items table.

The table is the one control in this application that has to be addressed by
grid position: its cells expose nothing to accessibility and hold no text until
they are filled, so there is no label to anchor on. Everything here exists to
derive that grid from what *is* legible - the column headings and the SKU printed
in each row - so no cell coordinate is ever hardcoded.

Split out of the Product stage because it is a different kind of code: the stage
decides what the Order should say, this decides where on screen to say it.
"""
from __future__ import annotations

import time

from ..driver.vision_driver import Region
from ..manual_review import stop
from . import expect

STAGE = "product"

#: Click offset into a grid cell from its column's left edge.
CELL_INSET = 30.0

#: Items-table column geometry, measured once per run - the table does not move,
#: and re-reading its header on every line is both slow and a source of flakes.
_COLUMN_CACHE: dict[str, float] = {}


#: Column order of the Items table, left to right after the picture flag.
COLUMNS = (
    "qty",
    "item_no",
    "picture",
    "name",
    "description",
    "vat",
    "u_price",
    "discount",
    "price",
)


def table(app, editor: Region) -> Region:
    """The Items table, from its header row down to the Remarks block.

    Deliberately starts at the *top* of the header band rather than below it. The
    header band is sized for recognising the header, so its lower edge falls a few
    points inside the first row - and a crop boundary through a row's glyphs does
    not lose the row, it corrupts it: `24.00 NL-4021 Edelstahl ...` came back as
    `Lt )`, which reads as a row that exists and says something else. Including
    the header costs one extra row of text that no assertion can match, which is
    the cheaper mistake by a wide margin.
    """
    header = header_band(app, editor)
    remarks = app.vision.find_label(r"^Remarks$", region=editor)
    return Region(editor.left, header.top, editor.right, remarks.y - 8)


def drop_selection(app, editor: Region) -> None:
    """Move keyboard focus out of the Items table before reading it back.

    A cell left in edit mode draws a text control over the cell, so the value read
    back is the editor's rather than the row's. Remarks is the nearest harmless
    place to put focus: clicking it types nothing and leaves the document
    unchanged. The row last worked on stays *highlighted* - that is selection, not
    focus, and Fakturama's selected rows do still read - so this is about closing
    the editor, not about clearing the highlight.
    """
    remarks = app.vision.find_label(r"^Remarks$", region=editor)
    app.vision.click((remarks.right + 150, remarks.middle_y + 40))
    time.sleep(0.6)


#: Any one of these identifies the Items header row.
HEADER_ANCHORS = ("Items", "Picture", "Description", "U.Price", "Discount", "Price", "Qty.")


def header_band(app, editor: Region) -> Region:
    """The Items table's header row.

    Located from whichever of its captions happens to be recognised. Depending
    on a single anchor made this fail intermittently - any given word is dropped
    by some passes - whereas needing only one of seven is dependable.
    """
    words = app.vision.words_banded(editor)
    wanted = {_simplify(anchor) for anchor in HEADER_ANCHORS}
    hits = [w for w in words if _simplify(w.text) in wanted]
    if not hits:
        raise stop(
            "could not find the Items table header",
            STAGE,
            seen=[(w.text, round(w.x), round(w.y)) for w in words][:20],
        )
    # Several captions share the row; the modal y is the row's baseline.
    rows: dict[int, int] = {}
    for hit in hits:
        rows[round(hit.y / 6)] = rows.get(round(hit.y / 6), 0) + 1
    band_y = max(rows, key=lambda key: rows[key]) * 6
    return Region(editor.left, band_y - 10, editor.right, band_y + 14)


def columns(app, editor: Region) -> dict[str, float]:
    """Click x for each Items column, derived from two header captions.

    The `Qty.` caption is short and abuts a column separator, and OCR drops it
    from every pass - so the columns are located from `U.Price` and `Discount`,
    which read reliably, using the table's fixed column pitch.
    """
    if _COLUMN_CACHE:
        return _COLUMN_CACHE

    # Scoped to the header row: `Discount` also captions the order-level total
    # further down. Read in vertical slices because the row's aspect ratio
    # defeats one-pass recognition.
    header = header_band(app, editor)
    captions = app.vision.words_wide(header)
    origin, pitch = _fit_grid(captions)
    _COLUMN_CACHE.update(
        {name: origin + pitch * i + CELL_INSET for i, name in enumerate(COLUMNS)}
    )
    app.log.info(
        "located Items columns",
        pitch=round(pitch, 1),
        **{k: round(v) for k, v in _COLUMN_CACHE.items() if k in ("qty", "u_price", "discount")},
    )
    return _COLUMN_CACHE


#: Column headings, mapped to their index in `COLUMNS`.
CAPTION_INDEX = {
    "qty": 0,
    "itemno": 1,
    "picture": 2,
    "name": 3,
    "description": 4,
    "vat": 5,
    "uprice": 6,
    "discount": 7,
    "price": 8,
}


def _fit_grid(words) -> tuple[float, float]:
    """Least-squares fit of the table's column origin and pitch.

    Individual headings are read unreliably - one pass loses `Qty.`, another
    truncates `Discount` to `Discou` - so no single caption can be depended on.
    The columns are evenly pitched, though, so any two recognised headings
    determine the whole grid, and fitting across all of them averages out the
    few-point jitter in each.
    """
    points: dict[int, float] = {}
    for word in words:
        simplified = _simplify(word.text)
        if len(simplified) < 3:
            continue  # too short to attribute to a column
        matches = [
            index
            for caption, index in CAPTION_INDEX.items()
            if caption.startswith(simplified)
        ]
        if len(matches) == 1:
            points.setdefault(matches[0], word.x)

    if len(points) < 2:
        raise stop(
            "could not identify enough Items column headings to place the grid",
            STAGE,
            recognised=[(w.text, round(w.x)) for w in words][:20],
        )

    indices = sorted(points)
    n = len(indices)
    mean_i = sum(indices) / n
    mean_x = sum(points[i] for i in indices) / n
    variance = sum((i - mean_i) ** 2 for i in indices)
    pitch = (
        sum((i - mean_i) * (points[i] - mean_x) for i in indices) / variance
        if variance
        else 75.0
    )
    if not 40 < pitch < 140:
        raise stop(
            "the Items column pitch came out implausible",
            STAGE,
            pitch=round(pitch, 1),
            points={i: round(points[i]) for i in indices},
        )
    return mean_x - pitch * mean_i, pitch


def _simplify(text: str) -> str:
    return "".join(character for character in text.lower() if character.isalnum())


def row_y(app, editor: Region, sku: str) -> float:
    """Vertical centre of the row holding a SKU.

    Anchoring on the row's own contents avoids having to know the row height or
    count rows - the line just added is wherever its SKU appears.
    """
    table = Region(editor.left, editor.top + 200, editor.right, editor.bottom)
    hits = app.vision.find_text(expect.reference(sku), region=table)
    if len(hits) != 1:
        raise stop(
            "could not locate the item row for this SKU",
            STAGE,
            sku=sku,
            candidates=[(w.text, round(w.x), round(w.y)) for w in hits],
        )
    return hits[0].middle_y


def set_cell(app, point: tuple[float, float], value: str) -> None:
    """Open a grid cell for editing and replace its contents."""
    app.vision.click(point, double=True)
    time.sleep(0.6)
    app.vision.press_key("a", ["command"])
    app.vision.type_text(value)
    app.vision.press_key("return")
    time.sleep(0.6)
