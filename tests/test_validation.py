"""Tests for the extraction self-check and the Product gross-price rule.

No LLM calls: fixtures are built in code so the suite runs without API keys.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.extraction.models import Debtor, LineItem, OrderData, PaymentInfo, Totals
from src.extraction.validate import gross_price, line_net, reconcile


def build_order(**overrides) -> OrderData:
    """The sample purchase order, which reconciles exactly."""
    data = {
        "order_date": "12.03.2026",
        "external_reference": "PO-2026-0412",
        "debtor": Debtor(
            company="Nordlicht Handels GmbH",
            first_name="Katrin",
            last_name="Brandt",
            street="Hafenstraße 47",
            zip="20359",
            city="Hamburg",
            country="Germany",
            email="k.brandt@nordlicht-handels.de",
            phone="+49 40 5512 8890",
        ),
        "payment": PaymentInfo(method="Bank Transfer", paid=True, payment_date="20.03.2026"),
        "items": [
            LineItem(
                sku="NL-4021",
                description="Edelstahl Thermobecher 500ml",
                quantity="24",
                unit_net_price="12.50",
                vat_percent="19",
                discount_percent="0",
                source_line_total="300.00",
            ),
            LineItem(
                sku="NL-7788",
                description="Filterkaffee Bio 1kg",
                quantity="10",
                unit_net_price="18.90",
                vat_percent="19",
                discount_percent="10",
                source_line_total="170.10",
            ),
        ],
        "totals": Totals(total_net="470.10", total_vat="89.32", total_gross="559.42"),
    }
    data.update(overrides)
    return OrderData.model_validate(data)


def test_sample_order_reconciles():
    assert reconcile(build_order()) == []


def test_german_decimal_and_date_formats_parse():
    order = build_order(
        totals=Totals(total_net="470,10", total_vat="89,32", total_gross="559,42")
    )
    assert order.totals.total_net == Decimal("470.10")
    assert order.order_date.isoformat() == "2026-03-12"


def test_discount_is_applied_to_the_line_net():
    # 10 x 18.90 = 189.00, less 10% = 170.10
    assert line_net(build_order().items[1]) == Decimal("170.10")


def test_tampered_line_total_is_caught():
    order = build_order()
    order.items[0].source_line_total = Decimal("299.00")
    problems = reconcile(order)
    assert any("line 1" in p and "NL-4021" in p for p in problems)


def test_wrong_grand_total_is_caught():
    order = build_order(totals=Totals(total_net="470.10", total_vat="89.32", total_gross="999.99"))
    assert any("gross" in p for p in reconcile(order))


def test_wrong_total_net_is_caught():
    order = build_order(totals=Totals(total_net="400.00", total_vat="89.32", total_gross="489.32"))
    assert any("sum of line nets" in p for p in reconcile(order))


def test_paid_without_a_date_is_caught():
    order = build_order(payment=PaymentInfo(method="Bank Transfer", paid=True))
    assert any("no payment date" in p for p in reconcile(order))


def test_payment_date_without_paid_is_caught():
    order = build_order(
        payment=PaymentInfo(method="Bank Transfer", paid=False, payment_date="20.03.2026")
    )
    assert any("not marked PAID" in p for p in reconcile(order))


@pytest.mark.parametrize(
    ("net", "vat", "expected"),
    [
        ("12.50", "19", "14.88"),  # 14.875 rounds half up
        ("18.90", "19", "22.49"),  # 22.491 rounds down
        ("10.00", "19", "11.90"),
        ("0.01", "19", "0.01"),
        ("100.00", "7", "107.00"),
    ],
)
def test_gross_price_rounding(net, vat, expected):
    assert gross_price(Decimal(net), Decimal(vat)) == Decimal(expected)


def test_gross_price_ignores_the_line_discount():
    """The Product master price must not carry the transaction-line discount."""
    discounted = build_order().items[1]
    assert gross_price(discounted.unit_net_price, discounted.vat_percent) == Decimal("22.49")
