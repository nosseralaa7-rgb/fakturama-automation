"""Semantic operations on Fakturama, built on the two grounding layers.

The flow stages speak in Fakturama's own vocabulary ("open a New Order", "set
Cust.Ref"), and this module decides for each operation whether the accessibility
tree can serve it or whether it has to be grounded from pixels:

* toolbar, navigation list, dialogs, menus -> `AXDriver`
* document editor body                     -> `VisionDriver`

Keeping that decision here means the stage modules never mention either
mechanism, and a Windows UIA binding would replace the two drivers without
touching the stages.
"""
from __future__ import annotations

import re
import time
from datetime import date

from ..driver import base
from ..driver.ax_driver import AXDriver
from ..driver.base import Element, wait_until
from ..driver.vision_driver import Region, VisionDriver
from ..manual_review import stop
from .dialogs import DrivesDialogs
from .reading import ReadsLists

#: Left navigation entries are exposed as static text, not buttons.
NAV_ROLE = "AXStaticText"


class Fakturama(ReadsLists, DrivesDialogs):
    """Everything the flow is allowed to ask of the application.

    The two mixins carry the parts that grew their own vocabulary - reading list
    views, and driving selector dialogs - so that this file stays the place where
    the grounding decision is made rather than a catalogue of everything.
    """

    def __init__(self, log, app_name: str = "Fakturama") -> None:
        self.log = log
        self.ax = AXDriver(app_name)
        self.vision = VisionDriver(self.ax)

    # -------------------------------------------------------------- session

    def activate(self) -> None:
        self.ax.activate()

    def window_title(self) -> str | None:
        return self.ax.window_title()

    # -------------------------------------------------------------- toolbar

    def toolbar(self, title: str) -> Element:
        return base.find_one(self.ax, role="AXButton", title=title)

    def press_toolbar(self, title: str) -> None:
        """Press a top toolbar button. Requires the app to be frontmost."""
        self.activate()
        self.ax.press(self.toolbar(title))
        self.log.action(f"pressed toolbar {title!r}")

    def menu(self, menu_title: str, item_title: str) -> None:
        """Invoke a menu-bar item.

        The whole menu bar is exposed to accessibility, so this is the most
        reliable control path in the application - preferred over clicking
        navigation entries or panel-header icons wherever a menu item exists.
        """
        self.activate()
        self.ax.press(self.ax.menu_item(menu_title, item_title))
        time.sleep(1.4)
        self.log.action(f"selected menu {menu_title} > {item_title}")

    def press_nav(self, title: str) -> None:
        """Open one of the Data lists (Documents, VATs, terms of payment...).

        Uses the Data menu; the left Navigation View carries the same entries
        but exposes them as static text with no actions, so it would have to be
        clicked by position.
        """
        try:
            self.menu("Data", title)
            return
        except base.DriverError:
            pass
        self.activate()
        entry = base.find_one(self.ax, role=NAV_ROLE, value=title)
        try:
            self.ax.press(entry)
        except base.DriverError:
            self.vision.click(entry.center)
        time.sleep(1.2)
        self.log.action(f"opened navigation entry {title!r}")

    # --------------------------------------------------------------- editors

    def open_tabs(self) -> list[Element]:
        """Editor tabs, in screen order. The active one has value 'True'."""
        tabs = [
            e
            for e in self.ax.snapshot()
            if e.role == "AXRadioButton" and e.y < 200 and e.title
        ]
        return sorted(tabs, key=lambda e: e.x)

    def active_editor(self) -> str | None:
        """Title of the focused editor tab, including any dirty marker."""
        for tab in self.open_tabs():
            if tab.value == "True":
                return tab.title
        return None

    def is_dirty(self) -> bool:
        """Whether the active editor has unsaved changes.

        Eclipse prefixes a dirty editor's tab with `*`, which gives a structural
        way to confirm that a save actually committed.
        """
        title = self.active_editor()
        return bool(title and title.startswith("*"))

    def editor_region(self, name: str) -> Region:
        return self.vision.editor_region(name)

    def open_editor(self, toolbar_title: str, editor_name: str, timeout: float = 25.0) -> Region:
        """Press a toolbar button and wait for its editor to become active."""
        self.press_toolbar(toolbar_title)
        expected = re.compile(rf"^\*?{re.escape(editor_name)}$")

        def ready():
            active = self.active_editor()
            return bool(active and expected.match(active))

        wait_until(ready, timeout=timeout, description=f"{editor_name} editor to open")
        region = self.editor_region(editor_name)
        self.log.verified(f"{editor_name} editor is open", region=str(region))
        return region

    def open_editor_wait(self, editor_name: str, timeout: float = 25.0) -> Region:
        """Wait for an editor opened by something other than a toolbar button."""
        expected = re.compile(rf"^\*?{re.escape(editor_name)}$")
        wait_until(
            lambda: bool(self.active_editor() and expected.match(self.active_editor())),
            timeout=timeout,
            description=f"{editor_name} editor to open",
        )
        region = self.editor_region(editor_name)
        self.log.verified(f"{editor_name} editor is open", region=str(region))
        return region

    def focus_editor(self, name: str, timeout: float = 10.0) -> Region:
        """Bring an already-open editor tab to the front.

        Used to return to the Order after a detour into a master-data editor -
        the Order tab is never closed, so this is a switch, not a reopen.
        """
        expected = re.compile(rf"^\*?{re.escape(name)}$")
        if expected.match(self.active_editor() or ""):
            return self.editor_region(name)

        for tab in self.open_tabs():
            if tab.title and expected.match(tab.title):
                self.vision.click(tab.center)
                wait_until(
                    lambda: bool(expected.match(self.active_editor() or "")),
                    timeout=timeout,
                    description=f"{name} tab to become active",
                )
                self.log.action(f"switched back to the {name!r} tab")
                return self.editor_region(name)
        raise stop(f"the {name!r} editor is no longer open", "app",
                   tabs=[t.title for t in self.open_tabs()])

    def close_editor(self, name: str) -> None:
        """Close an editor tab that was opened only to inspect a record.

        Worth doing rather than leaving open: the tab strip scrolls once it fills,
        and a scrolled-away Order tab can no longer be switched back to - which
        would break the one invariant of the whole flow. A dirty editor is left
        alone, because closing it would raise a save prompt.
        """
        if not re.match(rf"^{re.escape(name)}$", self.active_editor() or ""):
            return
        self.vision.press_key("w", ["command"])
        time.sleep(0.8)
        self.log.action(f"closed the {name!r} tab")

    def open_tab(self, region: Region, name: str) -> None:
        """Switch to a tab inside an editor (Addresses, Miscellaneous, Payment)."""
        tab = self.vision.find_label(rf"^{re.escape(name)}$", region=region)
        self.vision.click(tab.center)
        time.sleep(0.8)
        self.log.action(f"opened the {name!r} tab")

    def set_checkbox(
        self, region: Region, label_pattern: str, checked: bool, stage: str
    ) -> None:
        """Tick or untick the checkbox immediately left of a label.

        Prefers the accessibility tree, which exposes checkbox state directly;
        only dialogs expose it, so editors fall back to clicking the box.
        """
        for element in self.ax.snapshot():
            if element.role == "AXCheckBox" and element.title:
                if re.search(label_pattern, element.title, re.IGNORECASE):
                    if (element.value == "1") != checked:
                        self.ax.press(element)
                    self.log.action(f"set checkbox {element.title!r} = {checked}")
                    return

        label = self.vision.find_label(label_pattern, region=region)
        self.vision.click((label.x - 12, label.middle_y))
        self.log.action(f"clicked the checkbox beside ~{label_pattern!r}")

    def press_list_add(self, region: Region) -> None:
        """Press the green + control at the upper right of a list view."""
        button = base.find(self.ax, role="AXButton", help__contains="new")
        inside = [
            b for b in button if region.contains(*b.center)
        ]
        if len(inside) == 1:
            self.ax.press(inside[0])
        else:
            # Fall back to the control's fixed position within the panel header.
            self.vision.click((region.right - 60, region.top + 12))
        self.log.action("pressed the list's add control")
        time.sleep(1.0)

    # ------------------------------------------------------- editor fields

    def field_point(
        self, region: Region, label_pattern: str, offset: float = 40.0
    ) -> tuple[float, float]:
        """Locate the input to the right of a label inside an editor.

        OCR renders punctuation inconsistently (`Cust.Ref.` is often read as
        `Cust-Ref.`), so callers pass patterns with `.` as a wildcard.
        """
        return self.vision.point_right_of(label_pattern, offset, region=region)

    def set_field(self, region: Region, label_pattern: str, value: str, offset: float = 40.0) -> None:
        point = self.field_point(region, label_pattern, offset)
        self.vision.set_field(point, value)
        self.log.action(f"set field ~{label_pattern!r} = {value!r}", at=[round(p) for p in point])

    def set_date(self, region: Region, label_pattern: str, value: date) -> None:
        """Fill a segmented date control.

        The control is a native stepper showing `MMM D, YYYY`, not a free-text
        field: it consumes digits segment by segment in the display order, and
        ignores separators. Typing a German `12.03.2026` therefore yields
        `Dec 3, 2026`, so the digits are emitted month-first to match the
        control's own order rather than the source document's.

        Clicking also focuses whichever segment sits under the cursor - landing
        on the day segment made `03122026` read as `Aug 3, 1220`. Left arrows
        walk focus back to the first segment so the digits always start at the
        month, wherever the click happened to land.
        """
        # A small offset lands on the leftmost (month) segment; the arrow keys
        # then guarantee it regardless of where the click actually fell. The
        # pause matters - keystrokes sent before the click's focus settles are
        # swallowed, which is what silently produced `Aug 3, 1220`.
        label = self.vision.find_label(label_pattern, region=region)
        point = (label.right + 20.0, label.middle_y)
        self.vision.click(point)
        time.sleep(0.5)
        for _ in range(3):
            self.vision.press_key("left")
            time.sleep(0.15)
        self.vision.type_text(f"{value.month:02d}{value.day:02d}{value.year:04d}")

        # Move focus off the control before reading it back: a focused segment
        # is drawn highlighted (white on blue) and OCR frequently misses it.
        self.vision.press_key("tab")
        time.sleep(0.6)

        band = Region(label.x - 10, label.y - 12, label.right + 260, label.y + 26)
        shown = self.vision.read_region(band)
        if str(value.year) not in shown:
            raise stop(
                "the date control did not accept the extracted date",
                "order",
                expected=value.isoformat(),
                on_screen=shown[:200],
            )
        self.log.action(
            f"set date ~{label_pattern!r} = {value.isoformat()}",
            at=[round(p) for p in point],
        )

    def read_editor_text(self, region: Region) -> str:
        return self.vision.read_region(region)

    # ---------------------------------------------------------------- menus

    def choose_menu_item(self, title: str, timeout: float = 8.0) -> None:
        """Select an item from a popup menu that is already open.

        Native popup menus are exposed to accessibility even though the SWT
        control that opened them is not.
        """
        item = wait_until(
            lambda: base.find_one(self.ax, role="AXMenuItem", title=title),
            timeout=timeout,
            description=f"menu item {title!r}",
        )
        self.ax.press(item)
        self.log.action(f"chose menu item {title!r}")

    def open_dropdown(self, point: tuple[float, float]) -> None:
        self.vision.click(point)
        time.sleep(0.6)

    def select_combo_value(
        self, point: tuple[float, float], value: str, region: Region, stage: str
    ) -> None:
        """Open a combo box and choose an entry by its visible text.

        Native popup menus are reachable through accessibility even when the
        combo that opened them is not, so that path is tried first; when the
        popup is drawn by SWT itself the entry is located by OCR instead.
        """
        row = Region(point[0] - 70, point[1] - 14, point[0] + 380, point[1] + 20)

        # Type-ahead first: it selects the entry by *name*, so it is immune to
        # the two things that make clicking popup entries unreliable here - the
        # popup positions the selected entry over the combo (so screen position
        # says nothing about which entry is which), and a long list is clipped
        # at the top of the screen, hiding entries entirely from OCR.
        before = {
            (w.text, round(w.x / 6), round(w.y / 6)) for w in self.vision.words()
        }

        # Some combos in this UI open from a click anywhere in the field; others
        # only from the small arrow at their right-hand end, and the field can be
        # several hundred points wide. Rather than model which is which, try the
        # field position and then the right edge of its row, checking the value
        # after each attempt.
        for candidate in (point, (region.right - 45, point[1]), (region.right - 20, point[1])):
            self.open_dropdown(candidate)
            self.vision.type_text(value)
            time.sleep(0.5)
            self.vision.press_key("return")
            time.sleep(0.8)
            if self._combo_shows(row, value):
                self.log.action(f"selected {value!r} from a dropdown")
                return
            self.vision.press_key("escape")
            time.sleep(0.4)

        # Fall back to picking the entry out of the popup: the wanted one is
        # whatever text appeared that was not on screen beforehand.
        self.open_dropdown(point)
        try:
            self.choose_menu_item(value, timeout=3.0)
        except (TimeoutError, base.DriverError):
            appeared = [
                w
                for w in self.vision.find_text(rf"^{re.escape(value)}$")
                if (w.text, round(w.x / 6), round(w.y / 6)) not in before
            ]
            if len(appeared) != 1:
                self.vision.press_key("escape")
                raise stop(
                    f"could not select {value!r} in the dropdown",
                    stage,
                    candidates=[(w.text, round(w.x), round(w.y)) for w in appeared],
                    row_reads=self.vision.read_region(row)[:120],
                )
            self.vision.click(appeared[0].center)
        time.sleep(0.8)
        if not self._combo_shows(row, value):
            raise stop(
                f"the dropdown did not settle on {value!r}",
                stage,
                row_reads=self.vision.read_region(row)[:120],
            )
        self.log.action(f"selected {value!r} from a dropdown")

    def _combo_shows(self, row: Region, value: str) -> bool:
        """Whether a closed combo displays the wanted value.

        Compared without separators or case: OCR renders the combo's own font
        inconsistently, and a hyphen or space difference is not a mismatch.
        """
        simplify = lambda text: "".join(  # noqa: E731
            character for character in text.lower() if character.isalnum()
        )
        return simplify(value) in simplify(self.vision.read_region(row))
    # ----------------------------------------------------------------- save

    def save(self, stage: str, timeout: float = 25.0) -> None:
        """Press Save once and confirm the editor stopped being dirty.

        The specification says to click Save exactly once; the dirty marker is
        the confirmation that the click was actually accepted, so no second
        click is ever needed or attempted.
        """
        if not self.is_dirty():
            self.log.warn("save requested but the editor reports no changes")
        self.press_toolbar("Save")
        try:
            wait_until(
                lambda: not self.is_dirty(),
                timeout=timeout,
                description="editor to leave the dirty state",
            )
        except TimeoutError as exc:
            raise stop(
                "Save did not clear the editor's unsaved-changes marker",
                stage,
                active_editor=self.active_editor(),
                detail=str(exc),
            )
        self.log.verified("save committed", editor=self.active_editor())
