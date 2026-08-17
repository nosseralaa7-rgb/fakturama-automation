"""Stage 2: select the Debtor from the Order, or create it and select it back.

The Order's own address selector is the existence check - a Debtor is only
created when an exact match cannot be selected there, and the newly created
record is proven saved by selecting it from the same still-open Order.

Exactness is defined by the specification: Company, First Name, Name, ZIP and
City must all match. Anything ambiguous stops for manual review rather than
guessing, because attaching an Order to the wrong customer is not self-correcting.
"""
from __future__ import annotations

import re
import time

from ..driver.vision_driver import Region
from ..extraction.models import Debtor
from ..manual_review import stop

STAGE = "debtor"

#: Offsets from the `Addresses` label to the icon column beside it, in points.
#: Measured against the editor rather than the screen, so they survive the
#: window moving; only a Fakturama layout change would invalidate them.
ICON_DX = 46.0
UPPER_ICON_DY = 19.0

#: `First Name Last Name` and `ZIP - City` are single labels over two inputs, so
#: the inputs are offset from the label's first word.
FIRST_NAME_DX = 225.0
LAST_NAME_DX = 437.0
ZIP_DX = 84.0
CITY_DX = 319.0
#: Tab caption of the editor that New > New Term of Payment opens.
PAYMENT_EDITOR = "New Term of Payment"

ALIAS_DX = 110.0
DISCOUNT_DX = 70.0
NET_GROSS_DX = 90.0

#: Labels in these editors are right-aligned to a shared edge, so one offset
#: past the label's right edge lands inside the input on every row.
FIELD_DX = 25.0

#: The document is German; Fakturama's country list is in the UI language.
COUNTRY_NAMES = {
    "Deutschland": "Germany",
    "Österreich": "Austria",
    "Schweiz": "Switzerland",
}

#: Maps the extracted payment method onto Fakturama's payment-code dropdown.
PAYMENT_CODE = {
    "Bank Transfer": "Credit transfer",
    "Credit Card": "Credit card",
    "SEPA Direct Debit": "SEPA direct debit",
}


def resolve(app, editor: Region, debtor: Debtor, payment_method: str) -> None:
    """Ensure the open Order has the right Debtor selected."""
    if select_existing(app, editor, debtor):
        return
    create(app, debtor, payment_method)
    if not select_existing(app, editor, debtor):
        raise stop(
            "the newly saved Debtor could not be selected from the Order",
            STAGE,
            company=debtor.company,
        )


# --------------------------------------------------------------------- select


def open_address_selector(app, editor: Region):
    """Click the upper existing-contact icon beside Addresses.

    Deliberately not the lower green + icon, which starts a new Debtor instead
    of selecting one.

    Returns the located `Addresses` label so callers can derive the address
    panel from it later without a second OCR lookup - re-finding it after the
    dialog closes is unreliable, since the panel has just been repainted and
    now holds focus.
    """
    label = app.vision.find_label(r"^Addresses$", region=editor)
    # The icons are stacked below the label, about 28 points apart: the upper
    # one opens the selector, the lower green + starts a new Debtor.
    app.vision.click((label.x + ICON_DX, label.middle_y + UPPER_ICON_DY))
    time.sleep(1.2)
    return label


def select_existing(app, editor: Region, debtor: Debtor) -> bool:
    """Search the address selector; select only on an unambiguous exact match."""
    editor = app.focus_editor("New Order")
    label = open_address_selector(app, editor)
    app.wait_for_dialog(r"Select the address")
    app.log.action("opened the address selector")

    app.dialog_search(debtor.company)
    rows = _matching_rows(app, debtor)

    if len(rows) > 1:
        raise stop(
            "the address search returned more than one candidate",
            STAGE,
            company=debtor.company,
            rows=rows,
        )
    if not rows:
        app.press_dialog_button("Cancel")
        app.log.info("no exact Debtor match; will create one", company=debtor.company)
        return False

    # Double-clicking the row both selects and confirms. A single click followed
    # by OK looked like it worked - the dialog closed and OK reported success -
    # but left the Order's address empty, because the click alone did not move
    # the table's selection.
    app.choose_row(rows[0]["point"])
    app.log.verified("selected an existing Debtor", company=debtor.company)
    _confirm_addresses(app, label, debtor)
    return True


def identifying_fields(debtor: Debtor) -> dict[str, str]:
    """The values that have to match for a Debtor to count as an exact match.

    The specification names Company, First Name, Name, ZIP and City. A name part
    the source document does not supply is left out rather than matched as empty,
    which would match every row.
    """
    fields = {"company": debtor.company, "ZIP": debtor.zip, "city": debtor.city}
    if debtor.first_name:
        fields["first name"] = debtor.first_name
    if debtor.last_name:
        fields["name"] = debtor.last_name
    return fields


def _matching_rows(app, debtor: Debtor) -> list[dict]:
    """Rows whose visible text carries every identifying field.

    All of them have to appear on the same row - matching on company alone would
    happily select a different branch of the same group.

    Rows that carry the company but not the rest are logged rather than silently
    dropped. That distinction is the difference between "this customer is not in
    the database" and "this customer is there but a column this check depends on
    is not being read", and the two need opposite responses.
    """
    required = identifying_fields(debtor)
    hits, near_misses = [], []

    for band in app.rows_in_region(app.dialog_list_region()):
        text = " ".join(word.text for word in band)
        missing = [
            field for field, value in required.items() if not _field_matches(text, value)
        ]
        if missing:
            if not _field_matches(text, debtor.company):
                continue
            near_misses.append({"row": text[:120], "missing": missing})
            continue
        # Click a cell that holds data, not the row-number gutter on the far
        # left: double-clicking the gutter leaves the selector open and the
        # selection unchanged. The company cell is the most distinctive.
        anchor = next(
            (w for w in band if _field_matches(w.text, debtor.company)),
            sorted(band, key=lambda w: w.x)[min(2, len(band) - 1)],
        )
        hits.append({"text": text[:120], "point": anchor.center})

    if near_misses and not hits:
        app.log.info(
            "rows carried the company but not every identifying field",
            candidates=near_misses[:4],
        )
    return hits


#: Shortest prefix accepted when a list column has been truncated.
MIN_PREFIX = 8


def _simplify(text: str) -> str:
    """Drop case and separators, which OCR renders unreliably."""
    return "".join(character for character in text.lower() if character.isalnum())


def _contains(haystack: str, needle: str) -> bool:
    return _simplify(needle) in _simplify(haystack)


def _field_matches(row_text: str, expected: str) -> bool:
    """Whether a row shows a given value, allowing for column truncation.

    The selector truncates narrow columns, so a company saved as
    `Nordlicht Handels GmbH` is displayed as `Nordlicht H...`. The
    specification asks that the *visible* values match, so a leading fragment
    counts - but only a substantial one, and only alongside the other required
    fields, which are short enough to be shown in full.
    """
    row, value = _simplify(row_text), _simplify(expected)
    if value in row:
        return True
    for cut in range(len(value), MIN_PREFIX - 1, -1):
        if value[:cut] in row:
            return True
    return False


def _confirm_addresses(app, label, debtor: Debtor) -> None:
    """Check the address the Order now shows against the source document.

    Scoped to the address box rather than the whole editor: recognising the full
    editor in one pass drops most of this text, and a false negative here would
    stop a run that had actually succeeded.
    """
    text = app.vision.read_region(address_box(label))
    # The panel is a short scrolling area that renders only its first three
    # lines, so the ZIP/City line is not on screen to check. That pair was
    # already required to match when the row was selected, which is where the
    # exact-match rule is enforced; here the visible lines are confirmed.
    for value in (debtor.company, debtor.street):
        if not _field_matches(text, value):
            raise stop(
                "the populated address does not match the source document",
                STAGE,
                expected=value,
                on_screen=text[:300],
            )
    app.log.verified(
        "Invoice and Delivery addresses match the source document",
        company=debtor.company,
        street=debtor.street,
    )


def address_box(label) -> Region:
    """The Order's populated address panel, derived from the Addresses label.

    Kept narrow enough to exclude the Consultant column on its right and short
    enough to exclude the Items table header below it.
    """
    return Region(label.x + 60, label.middle_y - 8, label.x + 440, label.middle_y + 55)


# --------------------------------------------------------------------- create


def create(app, debtor: Debtor, payment_method: str) -> None:
    """Create the Debtor while leaving the Order tab open.

    The payment method is resolved *before* the Debtor editor is opened. The
    specification has it created from inside the open editor, but this build
    populates the Payment combo when the editor opens and never refreshes it, so
    a method created afterwards is simply not offered - the combo's popup comes
    back holding a single entry. Resolving it first is the same outcome by a
    slightly different route, and it keeps the Order tab untouched either way.
    """
    ensure_payment_method(app, payment_method)

    app.menu("New", "New Debtor")
    editor = app.open_editor_wait("New Debtor")

    # The proposed Customer ID is left unchanged, as specified.
    app.set_field(editor, r"^Company$", debtor.company)

    # One label, "First Name Last Name", spans both inputs.
    names = app.vision.find_label(r"^First$", region=editor)
    if debtor.first_name:
        app.vision.set_field((names.x + FIRST_NAME_DX, names.middle_y), debtor.first_name)
    if debtor.last_name:
        app.vision.set_field((names.x + LAST_NAME_DX, names.middle_y), debtor.last_name)
    app.log.action("set Company and contact name")
    # Salutation is left as "---" because the document supplies none.

    _fill_main_address(app, editor, debtor)
    _fill_miscellaneous(app, editor, debtor)
    _select_payment(app, editor, payment_method)

    app.save(STAGE)
    app.log.verified("created the Debtor", company=debtor.company)


def _fill_main_address(app, editor: Region, debtor: Debtor) -> None:
    """Fill Main address, then assign its roles.

    Two rows put a single label over two inputs (`First Name Last Name` and
    `ZIP - City`), so those are addressed by offset from the first word of the
    label rather than by "the field to the right of it".
    """
    app.set_field(editor, r"^Street$", debtor.street)

    zip_label = app.vision.find_label(r"^ZIP$", region=editor)
    app.vision.set_field((zip_label.x + ZIP_DX, zip_label.middle_y), debtor.zip)
    app.vision.set_field((zip_label.x + CITY_DX, zip_label.middle_y), debtor.city)
    app.log.action("set ZIP and City")

    app.select_combo_value(
        app.field_point(editor, r"^Country$", 40.0),
        COUNTRY_NAMES.get(debtor.country, debtor.country),
        editor,
        STAGE,
    )
    if debtor.email:
        app.set_field(editor, r"^E.?Mail$", debtor.email, offset=30.0)
    if debtor.phone:
        app.set_field(editor, r"^Telephone$", debtor.phone, offset=30.0)
    app.log.action("filled the Main address")
    _assign_address_roles(app, editor, debtor)


def _assign_address_roles(app, editor: Region, debtor: Debtor) -> None:
    """Tick Invoice (and Delivery) on the address-type popup.

    The roles live behind the small arrow beside `address type`. The popup it
    opens *is* exposed to accessibility, so once it is open the checkboxes are
    set structurally and their state can be read back.
    """
    arrows = [
        w for w in app.vision.words(region=editor) if w.text.strip() in (">", "▶", "»")
    ]
    if len(arrows) != 1:
        raise stop(
            "could not find the address-type control",
            STAGE,
            candidates=[(w.text, round(w.x), round(w.y)) for w in arrows],
        )
    app.vision.click(arrows[0].center)
    time.sleep(1.2)

    app.set_checkbox(editor, r"^Invoice address$", True, STAGE)
    if debtor.delivery_same_as_billing:
        # Billing and delivery are identical, so the same address takes both
        # roles rather than a second address being created.
        app.set_checkbox(editor, r"^Delivery address$", True, STAGE)
    app.vision.press_key("escape")
    time.sleep(0.6)
    app.log.verified("assigned the Main address its roles")


def _fill_miscellaneous(app, editor: Region, debtor: Debtor) -> None:
    """Alias, discount and price mode.

    There is no separate Payment tab in this build - the payment method,
    Discount and Net/Gross all live on Miscellaneous.
    """
    app.open_tab(editor, "Miscellaneous")
    if debtor.alias:
        alias = app.vision.find_label(r"^Alias$", region=editor)
        app.vision.set_field((alias.x + ALIAS_DX, alias.middle_y), debtor.alias)

    # Discount already defaults to 0%, which is what the specification wants;
    # confirm rather than retype, so a non-default default cannot slip through.
    discount = app.vision.find_label(r"^Discount$", region=editor)
    shown = app.vision.read_region(
        Region(discount.x, discount.y - 8, discount.x + 160, discount.y + 20)
    )
    if "0" not in shown:
        app.vision.set_field((discount.x + DISCOUNT_DX, discount.middle_y), "0")

    gross = app.vision.find_label(r"^Gross$", region=editor)
    app.select_combo_value((gross.x + NET_GROSS_DX, gross.middle_y), "Net", editor, STAGE)
    app.log.action("set Miscellaneous fields", alias=debtor.alias, price_mode="Net")


def _select_payment(app, editor: Region, payment_method: str) -> None:
    """Choose the payment method, which `create` has already ensured exists."""
    app.open_tab(editor, "Miscellaneous")
    app.select_combo_value(
        app.field_point(editor, r"^Payment$", 60.0), payment_method, editor, STAGE
    )


def ensure_payment_method(app, payment_method: str) -> None:
    """Reuse an exact Payment Method from Data > terms of payment, or create it."""
    app.press_nav("terms of payment")
    listing = app.vision.region_of("AXTabGroup", r"terms of payment")

    found = app.count_in_region(listing, rf"\b{re.escape(payment_method)}\b")
    if found > 1:
        raise stop(
            "several existing payment methods match the extracted one",
            STAGE,
            method=payment_method,
            found=found,
        )
    if found == 1:
        app.log.verified("reusing an existing payment method", method=payment_method)
        return

    app.menu("New", "New Term of Payment")
    editor = app.open_editor_wait(PAYMENT_EDITOR)

    app.set_field(editor, r"^Name$", payment_method, offset=FIELD_DX)
    app.set_field(editor, r"^Description$", payment_method, offset=FIELD_DX)
    # Account is deliberately left blank.

    # The payment-code label OCRs as an untranslated resource key, so the combo
    # is opened on its current value instead.
    current = app.vision.find_label(r"^Mutually$", region=editor)
    app.select_combo_value(current.center, PAYMENT_CODE[payment_method], editor, STAGE)

    # Cash discount, Discount Days and Net Days already default to 0, which is
    # what the specification asks for; the texts stay blank and "Set as
    # standard" is deliberately not pressed.
    shown = app.vision.read_region(editor)
    if shown.count("0") < 3:
        app.log.warn("payment defaults may not all be zero", on_screen=shown[:200])

    app.save(STAGE)
    app.log.verified("created the payment method", method=payment_method)
