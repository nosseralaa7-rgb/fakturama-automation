"""Stage 3: resolve each Product, then complete its Order line.

Runs once per extracted item, in source order. As with the Debtor, the Order's
own Product selector is the existence check, and a newly created Product is
proven saved by selecting it back into the still-open Order.

Ordering matters: the required VAT rate is checked and created *before* the New
product editor is opened, so it is available in that editor's VAT dropdown.
"""
from __future__ import annotations

import re
import time
from decimal import Decimal

from ..driver.vision_driver import Region
from ..extraction.models import LineItem
from ..extraction.validate import gross_price, line_net
from ..manual_review import stop
from . import expect, items_grid

STAGE = "product"

#: Tab captions the New menu opens. The VAT editor is captioned "New TAX Rate",
#: not "New VAT" like the menu item that opens it.
VAT_EDITOR = "New TAX Rate"
PRODUCT_EDITOR = "New product"

#: Labels are right-aligned to a shared edge in these editors.
FIELD_DX = 25.0

#: Offsets from the `Items` label to the icon column beside the table. The
#: column runs: product selector, green + (adds a blank line), red x (removes a
#: line), duplicate. Only the first is wanted.
ICON_DX = 20.0
SELECT_ICON_DY = 24.0

#: Click offset into a grid cell from its column's left edge.
CELL_INSET = 30.0

#: Currency inputs are set further right of their label than text inputs are.
MONEY_DX = 60.0

#: Items-table column geometry, measured once per run - the table does not move,
#: and re-reading its header on every line is both slow and a source of flakes.
_COLUMN_CACHE: dict[str, float] = {}


def resolve_all(app, editor: Region, items: list[LineItem]) -> None:
    """Resolve every Product first, then fill in every line.

    Deliberately two passes. Adding a Product to the Items table resets the
    values already typed into the rows above it - a quantity of 24 dropped back
    to 1, and the unit price reverted to the one derived from the Product
    master. Adding all the rows before editing any of them removes that
    interference entirely.

    It also matters that the unit price is typed rather than inherited: the
    master price is a *gross* figure rounded to the cent, so deriving the net
    back from it does not reproduce the document (14.88 / 1.19 = 12.5042, which
    over 24 units is 300.10 instead of 300.00). The specification allows the
    line's unit price to be set, and setting it keeps the Order faithful to the
    source.
    """
    for index, item in enumerate(items, start=1):
        app.log.info(f"resolving product {index}/{len(items)}: {item.sku}")
        resolve(app, editor, item)

    for index, item in enumerate(items, start=1):
        app.log.info(f"completing line {index}/{len(items)}: {item.sku}")
        complete_line(app, editor, item, index)

    # Both checks happen after every line is filled in. Per-line checks cannot
    # run inside the loop in a two-pass design: all the rows exist from the
    # start, so until the last one is edited the Order's total legitimately
    # includes the others' default values.
    confirm_lines(app, editor, items)
    _confirm_net(app, editor, sum(line_net(item) for item in items))


def _confirm_net(app, editor: Region, expected: Decimal) -> None:
    from .order import totals_values

    text = totals_values(app, editor)
    if not re.search(expect.amount(expected), text):
        raise stop(
            "the Order's total net does not match the sum of the source line items",
            STAGE,
            expected=str(expected),
            on_screen=text[:200],
        )
    app.log.verified("total net matches the source line items", net=str(expected))


def resolve(app, editor: Region, item: LineItem) -> None:
    if select_existing(app, editor, item.sku):
        return
    ensure_vat(app, item.vat_percent)
    create(app, item)
    if not select_existing(app, editor, item.sku):
        raise stop(
            "the newly saved Product could not be selected from the Order",
            STAGE,
            sku=item.sku,
        )


# --------------------------------------------------------------------- select


def select_existing(app, editor: Region, sku: str) -> bool:
    """Search the Product selector for the exact SKU."""
    editor = app.focus_editor("New Order")
    _open_product_selector(app, editor)
    app.wait_for_dialog(r"Select a product")
    app.log.action("opened the Product selector")

    # The list is scanned unfiltered rather than searched. Both typing into this
    # dialog's search box and setting its value through accessibility dismiss the
    # dialog outright, so searching is not available here; the rows themselves
    # read reliably. Limitation: a catalogue long enough to need scrolling would
    # require paging the list, which this does not do.
    listing = app.vision.words_banded(app.dialog_list_region())
    rows = app.vision.find_text(expect.reference(sku), words=listing)
    if len(rows) > 1:
        raise stop(
            "the Product search returned more than one candidate",
            STAGE,
            sku=sku,
            rows=[(w.text, round(w.x), round(w.y)) for w in rows],
        )
    if not rows:
        app.press_dialog_button("Cancel")
        app.log.info("no exact Product match; will create one", sku=sku)
        return False

    # Double-click selects and confirms in one action; a single click does not
    # move the table's selection, so OK would close the dialog having done
    # nothing (the same trap as the address selector).
    app.choose_row(rows[0].center)
    app.log.verified("selected an existing Product", sku=sku)
    return True


def _open_product_selector(app, editor: Region) -> None:
    """Click the upper Product-selection icon beside the Items table.

    Not the green + control, which adds an empty line instead of selecting a
    Product. The icons are stacked to the left of the table; the upper one is
    the selector.
    """
    label = app.vision.find_label(r"^Items$", region=editor)
    point = (label.x + ICON_DX, label.middle_y + SELECT_ICON_DY)
    for attempt in range(3):
        app.vision.click(point)
        time.sleep(1.4)
        title = app.ax.snapshot()[0].title or ""
        if "Select a product" in title:
            return
        app.log.warn(f"product selector did not open (attempt {attempt + 1})")
    raise stop("the Product selector would not open", STAGE, at=[round(p) for p in point])


# ------------------------------------------------------------------------ VAT


def ensure_vat(app, percent: Decimal) -> None:
    """Reuse an exact VAT rate, or create one; anything conflicting stops.

    A rate may only be reused when its Name is `VAT <percent>%`, its Value is
    that percentage, and its E-Invoice code is the standard rate - a rate that
    merely shares a name could carry different tax semantics.
    """
    name = vat_name(percent)
    app.press_nav("VATs")
    listing = app.vision.region_of("AXTabGroup", r"^VATs$")

    matches = [
        row
        for row in app.rows_in_region(listing)
        if re.search(rf"VAT\s*{expect.percent(percent)}", _row_text(row), re.IGNORECASE)
    ]
    if len(matches) > 1:
        raise stop(
            "several VAT rates match the required one",
            STAGE,
            name=name,
            rows=[_row_text(row)[:80] for row in matches],
        )
    if len(matches) == 1:
        _confirm_reusable(app, percent, matches[0])
        return

    app.menu("New", "New VAT")
    editor = app.open_editor_wait(VAT_EDITOR)
    app.set_field(editor, r"^Name$", name, offset=FIELD_DX)
    app.set_field(editor, r"^Description$", name, offset=FIELD_DX)
    app.set_field(editor, r"^Value$", _plain(percent), offset=FIELD_DX)

    # The E-Invoice code must be the standard rate, which is already the
    # default; reuse elsewhere depends on it, so confirm rather than assume.
    # Matched against the joined region text, not word by word: OCR splits
    # "(Standard rate)" into two words, so no single word can match the phrase.
    if not re.search(r"standard\s*rate", app.vision.read_region(editor), re.IGNORECASE):
        raise stop(
            "the new VAT rate is not set to the standard E-Invoice code",
            STAGE,
            on_screen=app.vision.read_region(editor)[:200],
        )
    app.save(STAGE)
    app.log.verified("created the VAT rate", name=name)


def _row_text(row) -> str:
    return " ".join(word.text for word in row)


def _confirm_reusable(app, percent: Decimal, row) -> None:
    """Check an existing rate is really the one required before reusing it.

    The specification allows reuse only when the Name is `VAT <percent>%`, the
    Value is that percentage and the E-Invoice code is the standard rate. The
    list shows Name, Description and Value, so the first two are read from the
    row; the E-Invoice code is only on the record itself, so the record is opened
    to read it and closed again afterwards.

    Worth the extra step: a rate named `VAT 19%` whose value had been edited to
    7%, or which carried a reduced-rate code, would produce an Invoice that is
    wrong in a way no later check in this flow would catch.
    """
    name = vat_name(percent)
    text = _row_text(row)

    # The Value column is the row's last percentage, and it is compared as a
    # number: the list renders it `19.00 %` while the rate is named `VAT 19%`, so
    # a textual comparison would need to know which form it was looking at.
    shown = re.findall(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*%", text)
    if not shown or Decimal(shown[-1].replace(",", ".")) != percent:
        raise stop(
            "an existing VAT rate shares the required name but not its value",
            STAGE,
            name=name,
            expected_value=str(percent),
            row=text[:120],
        )

    anchor = next((w for w in row if "VAT" in w.text.upper()), row[0])
    app.vision.click(anchor.center, double=True)
    time.sleep(1.5)
    try:
        editor = app.open_editor_wait(name, timeout=10.0)
    except TimeoutError:
        raise stop(
            "could not open an existing VAT rate to confirm its E-Invoice code",
            STAGE,
            name=name,
        )

    on_record = app.read_all(editor)
    if not re.search(r"standard\s*rate", on_record, re.IGNORECASE):
        raise stop(
            "an existing VAT rate is not on the standard E-Invoice code",
            STAGE,
            name=name,
            on_screen=on_record[:200],
        )
    app.close_editor(name)
    app.log.verified(
        "reusing an existing VAT rate",
        name=name,
        value=str(percent),
        code="S (Standard rate)",
    )


def vat_name(percent: Decimal) -> str:
    return f"VAT {_plain(percent)}%"


def _plain(value: Decimal) -> str:
    """Render 19 rather than 19.00, matching how Fakturama names rates."""
    normalised = value.normalize()
    return format(normalised, "f")


# --------------------------------------------------------------------- create


def create(app, item: LineItem) -> None:
    """Create the Product master record.

    The master price is the *gross* unit price and deliberately excludes the
    transaction-line discount, which belongs on the Order line instead.
    """
    app.menu("New", "New product")
    editor = app.open_editor_wait(PRODUCT_EDITOR)

    # "Item Number" OCRs as two words; anchoring on "Number" is unambiguous here.
    app.set_field(editor, r"^Number$", item.sku, offset=FIELD_DX)
    app.set_field(editor, r"^Name$", item.description, offset=FIELD_DX)
    # "Description" appears twice (header and field); scope to the lower half.
    lower = Region(editor.left, editor.top + 180, editor.right, editor.bottom)
    description = app.vision.find_label(r"^Description$", region=lower)
    app.vision.set_field(
        (description.right + FIELD_DX, description.middle_y), item.description
    )

    # Currency inputs sit further right than the plain text inputs - their
    # labels are followed by a wider gap - so they need their own offset.
    price = gross_price(item.unit_net_price, item.vat_percent)
    app.set_field(editor, r"^\(gross\)$", f"{price:.2f}", offset=MONEY_DX)
    app.set_field(editor, r"^\(net\)$", "0.00", offset=MONEY_DX)
    app.set_field(editor, r"^Stock$", "0.00", offset=MONEY_DX)
    app.select_combo_value(
        app.field_point(editor, r"^VAT$", FIELD_DX),
        vat_name(item.vat_percent),
        editor,
        STAGE,
    )

    app.save(STAGE)
    app.log.verified(
        "created the Product",
        sku=item.sku,
        gross_price=str(price),
        vat=vat_name(item.vat_percent),
    )


# ------------------------------------------------------------- line completion


def complete_line(app, editor: Region, item: LineItem, index: int) -> None:
    """Set quantity, unit price and discount on the selected Product's line.

    The specification allows unit price to be set or merely confirmed; it is set
    explicitly here so the line carries the price printed on the document rather
    than depending on the Product master record being right. The line total is
    then checked against quantity x unit net x (1 - discount), which is what
    proves the row.
    """
    columns = items_grid.columns(app, editor)
    row = items_grid.row_y(app, editor, item.sku)

    items_grid.set_cell(app, (columns["qty"], row), _plain(item.quantity))
    items_grid.set_cell(app, (columns["u_price"], row), f"{item.unit_net_price:.2f}")
    if item.discount_percent:
        items_grid.set_cell(app, (columns["discount"], row), _plain(item.discount_percent))

    app.log.action(
        "completed the item line",
        sku=item.sku,
        qty=_plain(item.quantity),
        unit_net=f"{item.unit_net_price:.2f}",
        discount=_plain(item.discount_percent),
    )


def confirm_lines(app, editor: Region, items: list[LineItem], attempts: int = 2) -> None:
    """Confirm every item line shows the extracted VAT and the computed total.

    The line total is the assertion that carries the most weight: Fakturama
    computes it as quantity x unit net x (1 - discount), so agreement proves all
    three of those cells at once. VAT is asserted separately because it is not an
    input to that arithmetic - a line priced correctly under the wrong rate would
    otherwise pass.

    The whole row is logged either way, so the quantity and unit price this run
    entered are on the record next to the values Fakturama derived from them.

    A row that does not read back is re-read once with a fresh capture before the
    run is stopped: OCR recall varies between passes on an unchanged screen, and a
    transient miss here would abandon an Order that is entirely correct.
    """
    items_grid.drop_selection(app, editor)
    rows: list[str] = []
    for attempt in range(attempts):
        rows = app.list_rows(items_grid.table(app, editor))
        unread = [item.sku for item in items if _row_for(rows, item) is None]
        if not unread or attempt == attempts - 1:
            break
        app.log.warn("an item row did not read back; re-reading", skus=unread)
        time.sleep(0.8)

    for index, item in enumerate(items, start=1):
        row = _row_for(rows, item)
        if row is None:
            raise stop(
                "an item line could not be read back from the Items table",
                STAGE,
                sku=item.sku,
                rows=[r[:120] for r in rows if r.strip()][:8],
            )
        expected = {
            f"VAT {_plain(item.vat_percent)}%": expect.percent(item.vat_percent),
            f"line total {line_net(item)}": expect.amount(line_net(item)),
        }
        missing = [
            name
            for name, pattern in expected.items()
            if not re.search(pattern, row, re.IGNORECASE)
        ]
        if missing:
            raise stop(
                "an item line does not show the expected VAT rate and total",
                STAGE,
                sku=item.sku,
                missing=missing,
                row=row[:160],
            )
        app.log.verified(
            f"line {index} carries the extracted VAT and totals as the document does",
            sku=item.sku,
            row=row[:160],
        )


def _row_for(rows: list[str], item: LineItem) -> str | None:
    """The Items row carrying an item's SKU, if it was recognised."""
    pattern = expect.reference(item.sku)
    return next((row for row in rows if re.search(pattern, row, re.IGNORECASE)), None)
