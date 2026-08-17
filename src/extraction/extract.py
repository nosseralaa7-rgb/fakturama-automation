"""Image -> validated `OrderData`."""
from __future__ import annotations

from ..llm import vision_json
from ..manual_review import stop
from .models import EXTRACTION_SCHEMA, OrderData
from .validate import reconcile

SYSTEM = (
    "You transcribe purchase orders into structured data for an accounting system. "
    "Report only what is printed on the document. Never infer, correct, or complete "
    "a value that is not visible - a missing field is recoverable, an invented one "
    "is not. Copy every number exactly as printed, including its decimals."
)

USER = (
    "Extract this purchase order as JSON matching the provided schema.\n"
    "- Dates as DD.MM.YYYY exactly as printed.\n"
    "- The external reference is the customer's own order number (Bestellnummer / "
    "Ihre Referenz), not any internal document number.\n"
    "- Prices are NET unit prices. Do not convert to gross.\n"
    "- source_line_total is the net line amount printed in the rightmost column.\n"
    "- discount_percent is 0 when no discount is shown for that line.\n"
    "- Set paid true only if the document states the order is paid."
)


def extract_order(image_path: str, log=None) -> OrderData:
    """Read an order image and return data that reconciles with its own totals.

    Raises `ManualReviewRequired` when the arithmetic does not check out, so a
    misread document is caught before any record is created in Fakturama.
    """
    raw = vision_json(image_path, SYSTEM, USER, EXTRACTION_SCHEMA, log=log)
    order = OrderData.model_validate(raw)

    problems = reconcile(order)
    if problems:
        raise stop(
            "extracted values do not reconcile with the totals on the document",
            "extraction",
            image=image_path,
            discrepancies=problems,
        )

    if log:
        log.verified(
            "extraction reconciles with the document totals",
            reference=order.external_reference,
            items=len(order.items),
            total_gross=str(order.totals.total_gross),
        )
    return order
