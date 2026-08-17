"""Stage 1 and 4: open the Order, fill its header, complete and save it.

The Order editor stays open for the whole run. Missing master data (Debtor,
Payment Method, VAT, Product) is created in other editors and then selected
back into *this* tab, exactly as the task specification requires - the
selection succeeding from the Order is what proves the record was saved.
"""
from __future__ import annotations

import re

from ..driver.vision_driver import Region
from ..extraction.models import OrderData
from ..manual_review import stop
from . import expect

STAGE = "order"

def header_band(app, editor: Region) -> Region:
    """The document header row (No., Date, price mode).

    Anchored on the `Date` label rather than a fixed offset from the editor's
    top edge, so it survives changes to how the editor region is measured.
    Scoping matters because `Net` and `Gross` also appear further down, in the
    totals block.
    """
    date = app.vision.find_label(r"^Date$", region=editor)
    return Region(editor.left, date.y - 14, editor.right, date.y + 18)


def open_new_order(app) -> Region:
    """Press Order in the toolbar and wait for the New Order editor."""
    return app.open_editor("Order", "New Order")


def fill_header(app, editor: Region, order: OrderData) -> None:
    """Set Date, Cust.Ref and the price mode.

    The automatically proposed document number is deliberately left untouched.
    """
    app.set_date(editor, r"^Date$", order.order_date)
    app.set_field(editor, r"^Cust.?Ref", order.external_reference)
    band = header_band(app, editor)

    # The editor opens in Gross; the specification requires Net with VAT.
    mode = app.vision.find_text(r"^(Gross|Net)$", region=band)
    if len(mode) != 1:
        raise stop(
            "could not identify the document price-mode control",
            STAGE,
            candidates=[(w.text, round(w.x), round(w.y)) for w in mode],
        )
    if mode[0].text.strip() != "Net":
        app.select_combo_value(
            (mode[0].x + 10, mode[0].middle_y), "Net", editor, STAGE
        )

    app.expect_text(band, r"^Net$", STAGE, "document price mode is Net")
    confirm_vat_mode(app, editor)


def common_data_band(app, editor: Region) -> Region:
    """The `common data` group: reference, addresses, VAT mode, follow-up area.

    Bounded by the two labels that open and close it rather than by offsets, so
    it holds whichever rows this document type happens to show - an Invoice adds
    Service date and Order Date to the same group.
    """
    top = app.vision.find_label(r"^Cust.?Ref", region=editor)
    items = app.vision.find_label(r"^Items$", region=editor)
    return Region(editor.left, top.y - 22, editor.right, items.y - 6)


def confirm_vat_mode(app, editor: Region, band: Region | None = None) -> None:
    """Confirm the document still carries VAT, which the flow never changes.

    Asserted rather than assumed: `With VAT` is the default, but a document with
    VAT switched off would produce an Invoice whose totals silently exclude tax,
    and nothing further downstream would notice.

    Decided on the single word `With`, not on the phrase `With VAT`. The combo
    reads either `With VAT` or `Without VAT`, so one word settles it - and it has
    to be one word, because in the Invoice editor the address panel's lines
    interleave with this column's, which splits `With` and `VAT` into different
    rows and defeats any phrase match. `Without` is checked for explicitly rather
    than inferred from `With` being absent, so a failed reading and a genuinely
    disabled VAT are not reported as the same thing.
    """
    band = common_data_band(app, editor) if band is None else band
    words = {word.text.strip().lower() for word in app.vision.words_banded(band)}
    if "without" in words:
        raise stop("the document has VAT switched off", STAGE)
    if "with" not in words:
        raise stop(
            "could not confirm the document's VAT mode",
            STAGE,
            on_screen=sorted(words)[:40],
        )
    app.log.verified("document VAT mode is `With VAT`")


def totals_band(app, editor: Region) -> Region:
    """The totals block at the bottom right, including Discount and Shipping.

    Anchored on the `Remarks` label, which shares the block's top edge and is the
    only caption down here that appears exactly once.
    """
    remarks = app.vision.find_label(r"^Remarks$", region=editor)
    left = max(editor.left, editor.right - 700)
    return Region(left, remarks.y - 12, editor.right, editor.bottom - 5)


def totals_values(app, editor: Region) -> str:
    """Text of the totals column at the bottom right of the editor.

    This is the reliable place to read computed amounts. The alternative -
    reading a cell in the Items table - fails whenever that row is selected,
    because the selection draws it white-on-blue and OCR returns nothing.
    """
    band = totals_band(app, editor)
    return app.read_all(Region(editor.right - 145, band.top, editor.right, band.bottom))


def confirm_order_level(app, editor: Region) -> None:
    """Confirm the order-level Discount and Shipping were left at their defaults.

    The source document supplies neither, so the specification requires 0% and
    free shipping. Both are defaults, and both are confirmed anyway - an
    order-level discount would quietly contradict the totals this run has just
    checked against the document.
    """
    text = app.read_all(totals_band(app, editor))
    missing = [
        name
        for name, pattern in (
            ("Discount 0%", expect.percent(0)),
            ("Free of shipping costs", r"free\s*of\s*shipping"),
        )
        if not re.search(pattern, text, re.IGNORECASE)
    ]
    if missing:
        raise stop(
            "the Order's discount and shipping are not at the values the document implies",
            STAGE,
            missing=missing,
            on_screen=text[:300],
        )
    app.log.verified("order-level Discount is 0% and shipping is free")


def confirm_totals(app, editor: Region, order: OrderData) -> None:
    """Check the Order's own totals against the source document.

    Fakturama recomputes these from the line items, so agreement means every
    quantity, price, discount and VAT rate was entered correctly - it verifies
    the whole item stage in one step.
    """
    text = totals_values(app, editor)
    expected = {
        "total net": order.totals.total_net,
        "VAT": order.totals.total_vat,
        "total gross": order.totals.total_gross,
    }
    missing = [
        f"{name} {amount}"
        for name, amount in expected.items()
        if not _amount_present(text, amount)
    ]
    if missing:
        raise stop(
            "the Order's totals do not match the source document",
            STAGE,
            missing=missing,
            on_screen=text[:400],
        )
    app.log.verified("Order totals match the source document", **{
        k: str(v) for k, v in expected.items()
    })


def _amount_present(text: str, amount) -> bool:
    """Look for an amount in either decimal convention, ignoring separators."""
    plain = f"{amount:.2f}"
    return plain in text or plain.replace(".", ",") in text


def save_order(app) -> str:
    """Save the Order and return the title its tab now carries.

    Saving renames the tab from `New Order` to the document number it was
    allocated, so callers must follow that rename - looking for `New Order`
    afterwards can silently find a different, unsaved Order tab and operate on
    that instead, which is how the Invoice step ended up clicking a disabled
    follow-up button.
    """
    app.save(STAGE)
    title = (app.active_editor() or "").lstrip("*")
    app.log.verified("Order saved", document=title)
    return title


def documents_table(app) -> Region:
    """Open Data > Documents and return the region holding its rows.

    The panel puts a document-type tree to the left of the table; the table's
    first column heading marks where the rows themselves start, so the tree's
    `Invoices` and `Orders` entries cannot be mistaken for row content.
    """
    app.press_nav("Documents")
    listing = app.vision.region_of("AXTabGroup", r"^Documents$")
    try:
        heading = app.vision.find_label(r"^Document$", region=listing)
    except Exception:  # noqa: BLE001 - the heading is an optimisation, not a requirement
        app.log.warn("could not locate the Documents column heading; reading the whole panel")
        return listing
    return Region(heading.x - 40, heading.y - 6, listing.right, listing.bottom)


def verify_in_documents(app, order: OrderData, document: str) -> None:
    """Confirm the saved Order's row in Data > Documents, field by field.

    Everything the specification names for this row is checked together, on one
    row: the generated number, the document date, the customer reference, the
    `open` state and the total. Checking them together is the point - the list
    also holds the Invoice, so any one of these values is present in the panel
    regardless of which document actually carries it.
    """
    app.expect_row(
        documents_table(app),
        expect.document_number(document),
        {
            "date": expect.day(order.order_date),
            "reference": expect.reference(order.external_reference),
            "state": r"\bopen\b",
            "total": expect.amount(order.totals.total_gross),
        },
        STAGE,
        f"Documents lists {document} dated {order.order_date.isoformat()} "
        f"with reference {order.external_reference}, state open and total "
        f"{order.totals.total_gross}",
    )
