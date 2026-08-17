"""Stage 5: create the linked Invoice from the saved Order and verify it.

The Invoice is created from the Order's own "Create a follow-up document" area,
never from the top toolbar - only the follow-up action preserves the link back
to the source Order, which is the relationship the whole flow exists to produce.
"""
from __future__ import annotations

import re
import time

from ..driver.vision_driver import Region
from ..extraction.models import OrderData
from ..manual_review import stop
from . import expect
from . import order as order_stage
from . import product as product_stage

STAGE = "invoice"


def create_from_order(app, editor: Region) -> Region:
    """Press Invoice inside the follow-up area of the saved Order."""
    anchor = app.vision.find_label(r"follow.?up", region=editor)
    candidates = app.vision.find_text(r"^Invoice$", region=editor)
    below = [w for w in candidates if w.y > anchor.middle_y]
    if len(below) != 1:
        raise stop(
            "could not identify the follow-up Invoice control",
            STAGE,
            candidates=[(w.text, round(w.x), round(w.y)) for w in candidates],
        )

    app.vision.click(below[0].center)
    app.log.action("pressed Invoice in the Order's follow-up area")
    time.sleep(1.5)
    return app.open_editor_wait("New Invoice")


def confirm_copied(app, invoice: Region, order: OrderData) -> None:
    """Check the fields Fakturama copied across from the Order.

    The proposed Invoice No., Invoice Date and Service date are left untouched, so
    what is checked is what should have carried over: the customer reference, the
    Order Date, the VAT mode, the item lines and the total. The lines are held to
    exactly the same expectations the Order's were, which is the point - the
    Invoice is the document that will be sent, so it is the one that has to be
    right, and it is only right here because Fakturama copied it.
    """
    band = order_stage.common_data_band(app, invoice)
    common = app.read_all(band)

    if not re.search(expect.reference(order.external_reference), common, re.IGNORECASE):
        raise stop(
            "the Invoice did not inherit the Order's customer reference",
            STAGE,
            expected=order.external_reference,
            on_screen=common[:400],
        )

    _confirm_order_date(app, invoice, order)
    order_stage.confirm_vat_mode(app, invoice, band=band)

    totals = order_stage.totals_values(app, invoice)
    if not re.search(expect.amount(order.totals.total_gross), totals):
        raise stop(
            "the Invoice did not inherit the Order's total",
            STAGE,
            expected=str(order.totals.total_gross),
            on_screen=totals[:200],
        )
    product_stage.confirm_lines(app, invoice, order.items)
    app.log.verified(
        "Invoice inherited the Order's reference, date, VAT mode, lines and total"
    )


def _confirm_order_date(app, invoice: Region, order: OrderData) -> None:
    """Confirm the Order Date carried over, read from beside its own label.

    Deliberately not from the whole group's text: this editor shows an Invoice
    Date and a Service date as well, both proposed as today, so a group-wide
    search would pass on the wrong field. And deliberately not from the row the
    label sits on either - the address panel to its left has lines of its own, and
    the two columns' baselines interleave closely enough that the label and its
    value cluster into different rows.

    So it is read the same way every other field in these editors is: crop to the
    label's own line, immediately right of it, and read that.
    """
    label = app.vision.find_label(r"^Order$", region=invoice)
    field = Region(label.right, label.y - 10, label.right + 320, label.y + 26)
    shown = app.vision.read_line(field)
    if not re.search(expect.day(order.order_date), shown):
        raise stop(
            "the Invoice did not inherit the Order's Order Date",
            STAGE,
            expected=order.order_date.isoformat(),
            on_screen=shown[:120],
        )
    app.log.verified("Invoice carries the Order Date", date=order.order_date.isoformat())


def payment_row(app, invoice: Region) -> tuple[Region, object]:
    """The Invoice's payment row, and its `paid` checkbox label.

    The row holds, left to right: the `paid` checkbox, the payment method, the
    payment date, and the amount. There is no `Payment` caption - the method is
    simply displayed - so the row is anchored on `paid`.
    """
    label = app.vision.find_label(r"^paid$", region=invoice)
    row = Region(invoice.left, label.y - 18, invoice.right, label.y + 22)
    return row, label


def apply_payment(app, invoice: Region, order: OrderData) -> None:
    """Confirm the payment method and, when the source says PAID, record payment.

    The method is inherited from the Debtor, so it is confirmed rather than
    chosen. When the document is not marked paid the box is left clear and no
    date or amount is invented, as the specification requires.
    """
    row, label = payment_row(app, invoice)
    shown = app.vision.read_region(row)
    if not _field_prefix(shown, order.payment.method):
        raise stop(
            "the Invoice does not carry the payment method from the source document",
            STAGE,
            expected=order.payment.method,
            on_screen=shown[:200],
        )
    app.log.verified("Invoice payment method is correct", method=order.payment.method)

    if not order.payment.paid:
        app.log.info("source document is not marked PAID; leaving the Invoice open")
        return

    if order.payment.payment_date is None:  # pragma: no cover - reconcile rejects this
        raise stop("the order is PAID but carries no payment date", STAGE)

    # Ticking `paid` reveals the payment date and amount, which default to today
    # and to the full invoice total respectively.
    app.vision.click((label.x - 12, label.middle_y))
    time.sleep(1.2)
    app.log.action("ticked the Invoice's paid box")

    row, _ = payment_row(app, invoice)
    app.set_date(row, r"^at$", order.payment.payment_date)

    # Move focus off the amount before reading it. Setting the date leaves focus
    # on the field after it, and a focused field is drawn highlighted with a caret
    # sitting in the text - which is what came back as `59.42` for a field
    # displaying `559.42`. Read twice before believing a mismatch.
    app.vision.press_key("tab")
    time.sleep(0.8)
    for attempt in range(2):
        settled = _payment_value(app, row)
        if re.search(expect.amount(order.totals.total_gross), settled):
            break
        app.log.warn("the payment amount did not read back; re-reading", read=settled[:60])
        time.sleep(0.8)
    else:
        raise stop(
            "the payment amount is not the full invoice total",
            STAGE,
            expected=str(order.totals.total_gross),
            on_screen=settled[:200],
        )
    app.log.verified(
        "Invoice marked paid",
        date=order.payment.payment_date.isoformat(),
        value=str(order.totals.total_gross),
    )


def _payment_value(app, row: Region) -> str:
    """The amount in the payment row's own Value field.

    Read from beside the `Value` caption rather than from the row as a whole. The
    row spans the width of the editor, so the totals block sits inside it - and a
    row-wide search for the invoice total is therefore satisfied by the `Total`
    line whether the payment amount is right, wrong or empty.
    """
    label = app.vision.find_label(r"^Value$", region=row)
    field = Region(label.right, label.y - 10, label.right + 200, label.y + 26)
    return app.vision.read_line(field)


def _field_prefix(text: str, expected: str) -> bool:
    """Whether a value appears, allowing for the column truncating it."""
    simplify = lambda s: "".join(c for c in s.lower() if c.isalnum())  # noqa: E731
    haystack, value = simplify(text), simplify(expected)
    return any(value[:cut] in haystack for cut in range(len(value), 3, -1))


def save_invoice(app) -> str:
    """Save the Invoice and return the number its tab now carries.

    Saving renames the tab from `New Invoice` to the allocated document number,
    which is both the confirmation that it saved and the value the final
    verification needs to identify its row.
    """
    app.save(STAGE)
    document = (app.active_editor() or "").lstrip("*")
    app.log.verified("Invoice saved", document=document)
    return document


def verify_final(app, order: OrderData, order_document: str, invoice_document: str) -> None:
    """Confirm both rows in Data > Documents: Invoice paid, source Order still open.

    Each document is checked as a whole row. The panel holds both of them, so
    `paid`, `open` and the total are all present in it regardless of which
    document carries which - only a row-scoped assertion distinguishes "the
    Invoice is paid and its Order is still open" from "one of these two things is
    true of one of these two documents".
    """
    # Each assertion reads the list itself rather than sharing one reading: that
    # is what lets a row that came back incomplete be read again, and these two
    # checks are the last thing standing between a run and claiming success.
    table = order_stage.documents_table(app)
    expected_state = r"\bpaid\b" if order.payment.paid else r"\bopen\b"

    app.expect_row(
        table,
        expect.document_number(invoice_document),
        {
            "reference": expect.reference(order.external_reference),
            "state": expected_state,
            "total": expect.amount(order.totals.total_gross),
        },
        STAGE,
        f"Documents lists Invoice {invoice_document} with reference "
        f"{order.external_reference}, the extracted payment state and total "
        f"{order.totals.total_gross}",
    )
    app.expect_row(
        table,
        expect.document_number(order_document),
        {
            "reference": expect.reference(order.external_reference),
            "state": r"\bopen\b",
            "total": expect.amount(order.totals.total_gross),
        },
        STAGE,
        f"the source Order {order_document} is still listed as open with the same "
        f"reference and total",
    )
