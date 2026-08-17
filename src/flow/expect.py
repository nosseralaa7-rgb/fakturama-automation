"""Regex builders for verifying values that were read back off the screen.

Every value this automation confirms comes out of OCR, so assertions are written
as tolerant patterns rather than string equality. Tesseract renders the decimal
separator, the currency prefix and punctuation inconsistently in Fakturama's
fonts, and an assertion that rejects `EGP559,42` when the document says `559.42`
stops a run that actually succeeded - a false failure is as costly here as a
missed one, because both end with a human re-doing the work.

Pure and separate from the stages on purpose: the tolerances are the part most
likely to be wrong, so they are unit-tested rather than only exercised against a
live window.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal


def amount(value: Decimal | int | float) -> str:
    """Match a money amount however the UI renders it.

    Fakturama prints `EGP559.42`, its lists sometimes print `559,42`, and a
    four-figure total may carry a thousands separator - all of which mean the
    same number, so all of them match.

    Bounded on both sides, which matters more than it looks: without a left guard
    an expectation of `559.42` is satisfied by a total of `1559.42`, so a run
    would confirm an Order it had got wrong by a factor of three.
    """
    whole, _, cents = f"{Decimal(str(value)):.2f}".partition(".")
    groups = [whole[max(0, i - 3):i] for i in range(len(whole), 0, -3)][::-1]
    return r"(?<![\d.,])" + r"[.,\s]?".join(groups) + rf"[.,]{cents}(?!\d)"


def percent(value: Decimal | int) -> str:
    """Match a percentage as `19%`, `19 %`, or the `19.00 %` form lists use.

    Anchored against a preceding digit so that asking for 0% does not match the
    `0.00 %` inside a line's `-10.00 %` discount.
    """
    plain = re.escape(format(Decimal(str(value)).normalize(), "f")).replace(r"\.", "[.,]")
    return rf"(?<![\d.,]){plain}(?:[.,]\d+)?\s*%"


def day(value: date) -> str:
    """Match Fakturama's own date rendering, `Mar 12, 2026`."""
    return rf"{value.strftime('%b')}\s*0?{value.day},?\s*{value.year}"


def document_number(text: str) -> str:
    """Match a number the application generated and then rendered, e.g. `PO000001`.

    Fakturama zero-pads these, and OCR miscounts runs of identical glyphs -
    `PO000001` was read back as `POOO00001`, nine characters for eight. So a run of
    zeros (or of the `O` this font makes them look like) matches any run of the
    same, while every other character still has to be exactly right.

    The trailing guard keeps that from becoming a licence: `PO000011` does not
    match `PO000001`, because the digit after the run of zeros must be the last
    one.
    """
    pattern, index = [], 0
    while index < len(text):
        if text[index] in "O0":
            while index < len(text) and text[index] in "O0":
                index += 1
            pattern.append("[O0]+")
        else:
            pattern.append(re.escape(text[index]))
            index += 1
    return "".join(pattern) + r"(?!\d)"


def reference(text: str) -> str:
    """Match a document reference tolerantly.

    OCR confuses `O` with `0` in this UI's field font and misreads the hyphens in
    a reference like `PO-2026-0412`, so letters and digits are matched exactly,
    the two easily-confused glyphs interchangeably, and punctuation as any single
    character.
    """
    return "".join(
        "." if not character.isalnum() else "[O0]" if character in "O0" else character
        for character in text
    )
