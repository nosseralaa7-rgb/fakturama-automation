"""Tests for the on-screen expectations the verification steps are built from.

These are the assertions that decide whether a run is allowed to continue, and
they are matched against OCR output rather than against clean data - so both
directions matter. A pattern that is too strict stops a run that succeeded; one
that is too loose passes a document that is wrong. Every case below is a string
actually seen in Fakturama's own rendering, or a near miss that must not match.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.extraction.models import Debtor
from src.flow import expect
from src.flow.debtor import identifying_fields


def matches(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.IGNORECASE))


@pytest.mark.parametrize(
    "rendering",
    [
        "EGP559.42",              # the editor's totals block
        "559,42",                 # a comma decimal separator
        "Total EGP559.42",
        "PO000001 ... open EGP559.42",
    ],
)
def test_amount_matches_the_ways_fakturama_renders_it(rendering):
    assert matches(expect.amount(Decimal("559.42")), rendering)


@pytest.mark.parametrize("rendering", ["EGP1.234,56", "1234.56", "1 234,56"])
def test_amount_tolerates_a_thousands_separator(rendering):
    assert matches(expect.amount(Decimal("1234.56")), rendering)


@pytest.mark.parametrize("other", ["EGP559.43", "EGP5.59", "EGP559.421", "EGP1559.42"])
def test_amount_rejects_a_different_number(other):
    assert not matches(expect.amount(Decimal("559.42")), other)


@pytest.mark.parametrize("rendering", ["19%", "19 %", "19.00 %", "19,00 %", "VAT 19% (..."])
def test_percent_matches_both_forms_the_ui_uses(rendering):
    assert matches(expect.percent(Decimal("19")), rendering)


def test_percent_zero_does_not_match_a_ten_percent_discount():
    """The Items table prints a 10% line discount as `-10.00 %`."""
    assert not matches(expect.percent(0), "-10.00 %")
    assert matches(expect.percent(0), "Discount 0%")


def test_day_matches_fakturamas_date_rendering():
    assert matches(expect.day(date(2026, 3, 12)), "Mar 12, 2026")
    assert not matches(expect.day(date(2026, 3, 12)), "Mar 12, 2025")
    assert not matches(expect.day(date(2026, 3, 12)), "Aug 17, 2026")


def test_reference_tolerates_the_glyphs_ocr_confuses():
    pattern = expect.reference("PO-2026-0412")
    assert matches(pattern, "PO-2026-0412")
    assert matches(pattern, "P0-2026-0412")   # O read as zero
    assert matches(pattern, "PO–2026–0412")   # hyphens read as en dashes
    assert not matches(pattern, "PO-2026-0413")


@pytest.mark.parametrize(
    "rendering",
    [
        "PO000001",
        "POO0OOO1",     # zeros read as the letter O
        "POOO00001",    # and a run of them miscounted, which is what the list did
        "PO0001",
    ],
)
def test_document_numbers_survive_miscounted_runs_of_zeros(rendering):
    assert matches(expect.document_number("PO000001"), rendering)


@pytest.mark.parametrize("other", ["PO000002", "PO000011", "INV000001"])
def test_document_numbers_still_identify_one_document(other):
    """Tolerating the padding must not make PO000001 match PO000011."""
    assert not matches(expect.document_number("PO000001"), other)


def test_a_glued_neighbouring_column_is_tolerated():
    """Only a following digit disqualifies a row.

    Document numbers are letters then digits, so nothing else can be another
    document - whereas OCR joining the next column onto this one is a real
    possibility, and rejecting that would fail a run that had succeeded.
    """
    assert matches(expect.document_number("PO000001"), "PO000001Mar")


def build_debtor(**overrides) -> Debtor:
    fields = {
        "company": "Nordlicht Handels GmbH",
        "first_name": "Katrin",
        "last_name": "Brandt",
        "street": "Hafenstraße 47",
        "zip": "20359",
        "city": "Hamburg",
        "country": "Germany",
    }
    fields.update(overrides)
    return Debtor(**fields)


def test_an_exact_debtor_match_requires_every_field_the_spec_names():
    fields = identifying_fields(build_debtor())
    assert set(fields) == {"company", "first name", "name", "ZIP", "city"}


def test_a_name_the_document_omits_is_not_required():
    """An empty expectation would match every row, which is worse than no check."""
    fields = identifying_fields(build_debtor(first_name="", last_name=""))
    assert set(fields) == {"company", "ZIP", "city"}
    assert "" not in fields.values()
