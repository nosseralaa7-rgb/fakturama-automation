#!/usr/bin/env python3
"""Run individual flow stages against an already-open Order.

Development harness. `run.py` starts from the image every time, which makes
iterating on one UI stage slow; this loads previously extracted data from JSON
and runs only the stages named.

    python scripts/dev_stage.py order.json --stages header debtor items save invoice

Stages are run in the order given, so a stage can be repeated or skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.extraction.models import OrderData
from src.flow import debtor as debtor_stage
from src.flow import invoice as invoice_stage
from src.flow import order as order_stage
from src.flow import product as product_stage
from src.flow.app import Fakturama
from src.manual_review import ManualReviewRequired
from src.runlog import RunLog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("order_json")
    parser.add_argument("--stages", nargs="+", default=["header", "debtor", "items", "save", "invoice"])
    parser.add_argument("--new-order", action="store_true", help="open a fresh Order first")
    parser.add_argument("--document", help="saved Order tab to use for the invoice stage")
    args = parser.parse_args()

    order = OrderData.model_validate(json.loads(Path(args.order_json).read_text()))
    log = RunLog(name=f"dev-{'-'.join(args.stages)}")
    app = Fakturama(log)
    app.activate()
    app.dismiss_modals()

    editor = (
        order_stage.open_new_order(app)
        if args.new_order
        else app.focus_editor("New Order")
    )
    invoice = None
    document = None

    try:
        for stage in args.stages:
            log.info(f"=== stage: {stage} ===")
            if stage == "header":
                order_stage.fill_header(app, editor, order)
            elif stage == "debtor":
                debtor_stage.resolve(app, editor, order.debtor, order.payment.method)
            elif stage == "items":
                product_stage.resolve_all(app, editor, order.items)
            elif stage == "save":
                order_stage.confirm_order_level(app, editor)
                order_stage.confirm_totals(app, editor, order)
                document = order_stage.save_order(app)
                order_stage.verify_in_documents(app, order, document)
            elif stage == "invoice":
                document = document or args.document
                editor = app.focus_editor(document)
                invoice = invoice_stage.create_from_order(app, editor)
                invoice_stage.confirm_copied(app, invoice, order)
                invoice_stage.apply_payment(app, invoice, order)
                invoice_document = invoice_stage.save_invoice(app)
                invoice_stage.verify_final(app, order, document, invoice_document)
            else:
                raise SystemExit(f"unknown stage {stage!r}")
            log.step(app.ax, f"after {stage}")
        print(f"\nOK. Artifacts: {log.directory}")
        return 0
    except ManualReviewRequired as needs_review:
        log.error("manual review", stage=needs_review.stage, reason=needs_review.reason)
        log.step(app.ax, "state at failure")
        print("\n" + needs_review.report(), file=sys.stderr)
        return 2
    except Exception as failure:  # noqa: BLE001
        log.error("failed", error=f"{type(failure).__name__}: {failure}")
        log.step(app.ax, "state at failure")
        print(f"\nFailed: {type(failure).__name__}: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
