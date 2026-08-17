"""Reading list views off the screen.

Every list this automation verifies against - Documents, VATs, terms of payment,
the selector dialogs, the Items table - is a table whose rows are invisible to
accessibility, so all of them are read the same way: recognise the region in
strips, group the words into the rows they were printed on, and assert against a
whole row rather than against loose words.

Mixed into `Fakturama` rather than made a separate object: these are operations on
the same window as everything else in that class, and the split exists to keep one
concern per file, not to introduce a second thing to hold.
"""
from __future__ import annotations

import re
import time

from ..driver.vision_driver import Region, TextBox
from ..manual_review import stop

#: Vertical spread, in points, within which OCR words belong to the same row.
ROW_TOLERANCE = 8.0

#: Padding above and below a row when it is cropped to be read on its own.
ROW_PAD = 1.5

#: How far into a list the anchoring value is looked for. It is the first column -
#: a document number - and searching only there is what makes it read reliably.
ANCHOR_COLUMN = 220.0

#: Band height for reading that column. A narrow crop wants narrow bands.
ANCHOR_BAND = 40.0

#: Region width, in points, beyond which a second sliced reading pays for itself.
WIDE_REGION = 600.0

#: Width of those slices - wide enough for a column, narrow enough not to be the
#: extreme aspect ratio that loses words.
SLICE_WIDTH = 400.0


class ReadsLists:
    """Row-level reading and assertion. Requires `self.vision` and `self.log`."""

    def rows_in_region(self, region: Region) -> list[list[TextBox]]:
        """Words in a region, grouped into the rows they are printed on.

        Rows, not words, are the unit of evidence in every list this automation
        reads. Matching has to be against a row's joined text, because OCR splits
        `VAT 19%` and `Bank Transfer` into separate words and a word-wise search
        for either never matches - an existence check built on one reports
        "absent" every time and duplicates the record. And matches have to be
        counted per row, because these lists repeat the same value in their Name
        and Description columns, so one record otherwise looks like two and trips
        the ambiguity guard.

        Rows are clustered by vertical proximity rather than by rounding to a grid:
        OCR reports baselines a few points apart within one row, and a grid
        boundary falling inside a row would split it.

        Each row is then ordered left to right, which is not the same as the order
        the words are clustered in - baselines within a row differ by a point or
        two, so ordering by position alone returned `Shipping of shipping Free
        costs` for a row reading `Shipping | Free of shipping costs`.

        Good enough for *counting* records and for matching one against several
        fields, which is all it is used for. It is deliberately not what verifies a
        saved document: see `expect_row`, which anchors on the value it is looking
        for instead of trusting this segmentation.
        """
        words = self.vision.words_banded(region)
        if region.right - region.left >= WIDE_REGION:
            # A list row is around 900 points wide and 14 tall, and at that aspect
            # ratio recognition drops words out of the middle of the row. Reading
            # the same region in vertical slices recovers those and loses others;
            # the two readers' misses do not coincide, so both are used.
            merged = {(w.text, round(w.x / 8), round(w.y / 8)): w for w in words}
            for word in self.vision.words_wide(region, width=SLICE_WIDTH):
                merged.setdefault((word.text, round(word.x / 8), round(word.y / 8)), word)
            words = list(merged.values())

        rows: list[list[TextBox]] = []
        for word in sorted(words, key=lambda w: (w.y, w.x)):
            if rows and abs(word.y - rows[-1][0].y) <= ROW_TOLERANCE:
                rows[-1].append(word)
            else:
                rows.append([word])
        return [sorted(row, key=lambda word: word.x) for row in rows]

    def list_rows(self, region: Region) -> list[str]:
        """Each row of a list view as one line of text."""
        return [" ".join(word.text for word in row) for row in self.rows_in_region(region)]

    def read_all(self, region: Region) -> str:
        """All text in a region, read in strips so nothing is dropped.

        `vision.read_region` recognises a region in a single pass, which loses
        words once the region is more than a few rows tall; this is the reliable
        reader for a whole panel.
        """
        return " ".join(self.list_rows(region))

    def count_in_region(self, region: Region, pattern: str) -> int:
        """How many *rows* of a list match a pattern."""
        return sum(
            1 for row in self.list_rows(region) if re.search(pattern, row, re.IGNORECASE)
        )

    def expect_row(
        self,
        region: Region,
        anchor: str,
        expected: dict[str, str],
        stage: str,
        note: str,
        attempts: int = 3,
    ) -> str:
        """Confirm the row carrying `anchor` also carries every expected value.

        Assertions on a list have to be row-scoped. The Documents panel holds the
        Order *and* its Invoice - and after a second run, two of each - so `open`,
        `paid`, a reference and a total are all present somewhere in it no matter
        which document carries which. Only "one row shows all of these together"
        verifies anything.

        The row is found by anchoring on the one value that identifies it, the
        document number, and then reading that line on its own in single-line mode.
        This replaced segmenting the whole panel into rows first, which is not
        reliable enough to carry an assertion: a list row is around 900 points wide
        and 14 tall, its glyphs sit 16 points apart, and OCR's per-word baselines
        jitter by a similar amount - so clustering variously split one document's
        line in two and ran four documents' lines into one. Anchoring needs no
        segmentation at all: the number is found, and the crop is that word's own
        line.

        Retried with a fresh capture, because recall varies from pass to pass on an
        unchanged screen, and a missed number reads perfectly a second later.
        """
        # The anchor is looked for in the list's first column alone, read in short
        # bands. Recall on these numbers depends entirely on the crop: read across
        # the full width of the panel, Tesseract returned `INV000002`, `PO000001`
        # and `PO000002` but silently dropped `INV000001` from between them, pass
        # after pass. Read as a 220-point column in 40-point bands, all four come
        # back.
        column = Region(
            region.left, region.top, region.left + ANCHOR_COLUMN, region.bottom
        )
        seen: list[str] = []
        for attempt in range(attempts):
            seen = []
            located = self.vision.words_banded(column, height=ANCHOR_BAND)
            for hit in self.vision.find_text(anchor, words=located):
                line = Region(
                    region.left,
                    hit.y - ROW_PAD,
                    region.right,
                    hit.y + hit.height + ROW_PAD,
                )
                text = self.vision.read_line(line)
                missing = [
                    field
                    for field, pattern in expected.items()
                    if not re.search(pattern, text, re.IGNORECASE)
                ]
                if not missing:
                    self.log.verified(note, row=text[:160])
                    return text
                seen.append(f"{text[:120]} (missing {', '.join(missing)})")
            if attempt < attempts - 1:
                self.log.warn("the expected row did not read back yet; looking again",
                              rows=seen[:3])
                time.sleep(0.8)

        raise stop(
            f"{note} - no row matched",
            stage,
            anchor=anchor,
            expected=expected,
            rows=seen[:4] or ["the anchoring value was not found in the list at all"],
        )

    def expect_text(self, region: Region, pattern: str, stage: str, note: str) -> None:
        """Confirm a value is present in a region, or stop for manual review."""
        if not self.vision.find_text(pattern, region=region):
            raise stop(
                f"{note} (expected on-screen text matching {pattern!r})",
                stage,
                region=str(region),
                seen=self.vision.read_region(region)[:300],
            )
        self.log.verified(note)
