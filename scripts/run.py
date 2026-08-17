#!/usr/bin/env python3
"""Image-to-cash entry point: one order image -> saved Order + linked Invoice.

    python scripts/run.py assets/sample_order.png
    python scripts/run.py assets/sample_order.png --extract-only

Every step writes a screenshot and a log line to `runs/<timestamp>/`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.extraction.extract import extract_order
from src.flow import debtor as debtor_stage
from src.flow import invoice as invoice_stage
from src.flow import order as order_stage
from src.flow import product as product_stage
from src.flow.app import Fakturama
from src.manual_review import ManualReviewRequired
from src.runlog import RunLog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="path to the order image")
    parser.add_argument(
        "--extract-only",
        action="store_true",
        help="extract and validate the image without touching Fakturama",
    )
    parser.add_argument(
        "--expect-selection-only",
        action="store_true",
        help="fail if any master record had to be created (idempotency check)",
    )
    parser.add_argument("--runs-dir", default="runs")
    args = parser.parse_args()

    log = RunLog(args.runs_dir)
    log.info("run started", image=args.image)

    try:
        order = extract_order(args.image, log=log)
        log.info(
            "extracted order",
            reference=order.external_reference,
            debtor=order.debtor.company,
            items=len(order.items),
            gross=str(order.totals.total_gross),
        )
        if args.extract_only:
            print(order.model_dump_json(indent=2))
            return 0

        app = Fakturama(log)
        app.activate()
        app.dismiss_modals()

        # Stage 1 - open the Order and fill its header.
        editor = order_stage.open_new_order(app)
        log.step(app.ax, "new order opened")
        order_stage.fill_header(app, editor, order)
        log.step(app.ax, "order header filled")

        # Stage 2 - Debtor, created only if it cannot be selected.
        debtor_stage.resolve(app, editor, order.debtor, order.payment.method)
        log.step(app.ax, "debtor resolved")

        # Stage 3 - one pass per item, in source order.
        product_stage.resolve_all(app, editor, order.items)
        log.step(app.ax, "all item lines complete")

        # Stage 4 - confirm what Fakturama computed, then save.
        order_stage.confirm_order_level(app, editor)
        order_stage.confirm_totals(app, editor, order)
        document = order_stage.save_order(app)
        log.step(app.ax, "order saved")
        order_stage.verify_in_documents(app, order, document)

        # Stage 5 - linked Invoice via the Order's follow-up area. The Order's
        # tab is now named after its document number, not "New Order".
        editor = app.focus_editor(document)
        invoice = invoice_stage.create_from_order(app, editor)
        log.step(app.ax, "invoice created from order")
        invoice_stage.confirm_copied(app, invoice, order)
        invoice_stage.apply_payment(app, invoice, order)
        invoice_document = invoice_stage.save_invoice(app)
        log.step(app.ax, "invoice saved")
        invoice_stage.verify_final(app, order, document, invoice_document)

        created = _creations(log)
        if args.expect_selection_only and created:
            log.error("master data was created although only selection was expected",
                      created=created)
            print("\nIdempotency check failed - these were created rather than "
                  "selected:\n  " + "\n  ".join(created), file=sys.stderr)
            return 3

        log.info("run complete", artifacts=str(log.directory), created=created)
        print(f"\nDone. Artifacts: {log.directory}")
        return 0

    except ManualReviewRequired as needs_review:
        log.error("stopped for manual review", stage=needs_review.stage,
                  reason=needs_review.reason, **needs_review.evidence)
        _capture_failure(log)
        print("\n" + needs_review.report(), file=sys.stderr)
        print(f"Artifacts: {log.directory}", file=sys.stderr)
        return 2

    except Exception as failure:  # noqa: BLE001 - surface anything with evidence
        log.error("run failed", error=f"{type(failure).__name__}: {failure}")
        _capture_failure(log)
        print(f"\nFailed: {type(failure).__name__}: {failure}", file=sys.stderr)
        print(f"Artifacts: {log.directory}", file=sys.stderr)
        return 1


def _creations(log: RunLog) -> list[str]:
    """Master records this run had to create rather than select.

    Every creation branch logs `created the ...`, so the run's own log answers the
    question the select-or-create design exists to answer. A second run over the
    same image should create nothing, which `--expect-selection-only` turns into
    an assertion - the cheapest regression test there is for that design.
    """
    return [
        event["message"]
        for event in log.events
        if event["message"].startswith("created the ")
    ]


def _capture_failure(log: RunLog) -> None:
    """Best-effort screenshot of whatever was on screen when the run stopped."""
    try:
        from src.driver.ax_driver import AXDriver

        log.step(AXDriver(), "state at failure")
    except Exception:  # noqa: BLE001 - the app may not even be running
        log.warn("could not capture a screenshot of the failure state")


if __name__ == "__main__":
    raise SystemExit(main())
