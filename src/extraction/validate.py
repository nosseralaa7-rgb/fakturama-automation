"""Arithmetic self-check on extracted data.

The automation writes into an accounting system, so a misread digit is worse
than a failed run. Every extraction is reconciled against the totals printed on
the source document *before* the UI is touched: if the numbers do not add up,
the source was misread and the run stops while nothing has been created yet.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .models import LineItem, OrderData

CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    """Round to two decimal places, half away from zero (the invoicing convention)."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def line_net(item: LineItem) -> Decimal:
    """quantity x unit net price x (1 - discount%), to the cent."""
    gross_of_discount = item.quantity * item.unit_net_price
    factor = Decimal("1") - (item.discount_percent / Decimal("100"))
    return money(gross_of_discount * factor)


def line_vat(item: LineItem) -> Decimal:
    return money(line_net(item) * item.vat_percent / Decimal("100"))


def gross_price(unit_net: Decimal, vat_percent: Decimal) -> Decimal:
    """Product master price for Fakturama: unit net x (1 + VAT%/100).

    The task specification is explicit that the transaction-line discount is
    *not* applied here - the discount belongs on the Order line, not on the
    Product record.
    """
    return money(unit_net * (Decimal("1") + vat_percent / Decimal("100")))


def reconcile(order: OrderData) -> list[str]:
    """Return human-readable discrepancies; an empty list means the data is sound."""
    problems: list[str] = []

    if not order.items:
        problems.append("no line items were extracted")

    computed_net = Decimal("0")
    computed_vat = Decimal("0")
    for index, item in enumerate(order.items, start=1):
        expected = line_net(item)
        if expected != money(item.source_line_total):
            problems.append(
                f"line {index} ({item.sku}): computed net {expected} "
                f"but the document shows {money(item.source_line_total)}"
            )
        computed_net += expected
        computed_vat += line_vat(item)

    computed_net = money(computed_net)
    computed_vat = money(computed_vat)

    if computed_net != money(order.totals.total_net):
        problems.append(
            f"sum of line nets {computed_net} does not match "
            f"the document total net {money(order.totals.total_net)}"
        )
    if computed_vat != money(order.totals.total_vat):
        problems.append(
            f"sum of line VAT {computed_vat} does not match "
            f"the document VAT {money(order.totals.total_vat)}"
        )

    stated_gross = money(order.totals.total_gross)
    if money(order.totals.total_net + order.totals.total_vat) != stated_gross:
        problems.append(
            f"net {money(order.totals.total_net)} + VAT {money(order.totals.total_vat)} "
            f"does not equal the document gross {stated_gross}"
        )

    if order.payment.paid and order.payment.payment_date is None:
        problems.append("payment is marked PAID but no payment date was extracted")
    if not order.payment.paid and order.payment.payment_date is not None:
        problems.append("a payment date was extracted although the order is not marked PAID")

    return problems
