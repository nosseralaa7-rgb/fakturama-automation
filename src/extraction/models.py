"""Typed representation of an order image.

Money is `Decimal` throughout, never `float`. The flow computes a Product's
gross master price from these values and compares totals to the cent, and binary
floating point would make those comparisons unreliable.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

PaymentMethod = Literal["Bank Transfer", "Credit Card", "SEPA Direct Debit"]


def _parse_date(value: object) -> object:
    """Accept the German `DD.MM.YYYY` used on the source documents, plus ISO."""
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return value


def _parse_decimal(value: object) -> object:
    """Accept `1.234,56` (German), `1,234.56` (English), and bare numbers."""
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip().replace("€", "").replace("EUR", "").replace("%", "").strip()
        if "," in text and "." in text:
            # Whichever separator comes last is the decimal point.
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        return Decimal(text) if text else value
    return value


def _normalise_key(value: object) -> object:
    """Fold typographic dashes in machine identifiers to plain hyphens.

    Documents are typeset, so an article number printed as `NL-4021` may carry
    an en or em dash. The model reports what it sees - correctly - but the value
    is a lookup key, and searching for a hyphen would then never match a record
    saved with a dash. Normalising here keeps every later comparison exact.
    """
    if isinstance(value, str):
        for dash in ("‐", "‑", "‒", "–", "—", "−"):
            value = value.replace(dash, "-")
        return value.strip()
    return value


OrderDate = Annotated[date, BeforeValidator(_parse_date)]
Money = Annotated[Decimal, BeforeValidator(_parse_decimal)]
#: Identifier fields that must compare exactly against records in Fakturama.
Key = Annotated[str, BeforeValidator(_normalise_key)]


class Debtor(BaseModel):
    """The customer, as the Fakturama Debtor editor expects it."""

    model_config = ConfigDict(extra="forbid")

    company: str
    first_name: str = ""
    last_name: str = ""
    salutation: str | None = None
    street: str
    zip: str
    city: str
    country: str = "Germany"
    email: str | None = None
    phone: str | None = None
    alias: str | None = None
    #: Drives whether the Main address also takes the Delivery address role.
    delivery_same_as_billing: bool = True


class PaymentInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: PaymentMethod
    paid: bool = False
    payment_date: OrderDate | None = None


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: Key
    description: str
    quantity: Money
    unit_net_price: Money
    vat_percent: Money
    discount_percent: Money = Decimal("0")
    #: The line total printed on the source document, used to cross-check.
    source_line_total: Money


class Totals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_net: Money
    total_vat: Money
    total_gross: Money


class OrderData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_date: OrderDate
    external_reference: Key = Field(description="Goes into the Order's Cust.Ref field")
    debtor: Debtor
    payment: PaymentInfo
    items: list[LineItem]
    totals: Totals


#: JSON Schema handed to the vision model. Written by hand rather than generated
#: from the model so the field descriptions can carry extraction instructions.
EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["order_date", "external_reference", "debtor", "payment", "items", "totals"],
    "properties": {
        "order_date": {"type": "string", "description": "Order date as DD.MM.YYYY"},
        "external_reference": {
            "type": "string",
            "description": "The customer's own order/reference number, e.g. PO-2026-0412",
        },
        "debtor": {
            "type": "object",
            "additionalProperties": False,
            "required": ["company", "street", "zip", "city"],
            "properties": {
                "company": {"type": "string"},
                "first_name": {"type": "string"},
                "last_name": {"type": "string"},
                "salutation": {"type": ["string", "null"]},
                "street": {"type": "string"},
                "zip": {"type": "string"},
                "city": {"type": "string"},
                "country": {"type": "string"},
                "email": {"type": ["string", "null"]},
                "phone": {"type": ["string", "null"]},
                "alias": {"type": ["string", "null"]},
                "delivery_same_as_billing": {"type": "boolean"},
            },
        },
        "payment": {
            "type": "object",
            "additionalProperties": False,
            "required": ["method", "paid"],
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["Bank Transfer", "Credit Card", "SEPA Direct Debit"],
                    "description": "Normalise: Überweisung -> Bank Transfer, "
                    "Lastschrift -> SEPA Direct Debit, Kreditkarte -> Credit Card",
                },
                "paid": {"type": "boolean"},
                "payment_date": {"type": ["string", "null"]},
            },
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "sku",
                    "description",
                    "quantity",
                    "unit_net_price",
                    "vat_percent",
                    "source_line_total",
                ],
                "properties": {
                    "sku": {"type": "string", "description": "Article number"},
                    "description": {"type": "string", "description": "Main item name only"},
                    "quantity": {"type": "string"},
                    "unit_net_price": {"type": "string", "description": "Net unit price"},
                    "vat_percent": {"type": "string", "description": "e.g. 19"},
                    "discount_percent": {"type": "string", "description": "0 when absent"},
                    "source_line_total": {
                        "type": "string",
                        "description": "The net line amount printed on the document",
                    },
                },
            },
        },
        "totals": {
            "type": "object",
            "additionalProperties": False,
            "required": ["total_net", "total_vat", "total_gross"],
            "properties": {
                "total_net": {"type": "string"},
                "total_vat": {"type": "string"},
                "total_gross": {"type": "string"},
            },
        },
    },
}
