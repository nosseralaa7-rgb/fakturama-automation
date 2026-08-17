"""Driving Fakturama's selector dialogs.

The selector dialogs (`Select the address`, `Select a product`) are where the two
grounding layers meet most closely: the dialog, its buttons and its search field
are exposed to accessibility, but the rows inside its table are not. So the
structure comes from the accessibility tree and the row contents from OCR, and
confirmation always comes from watching the dialog close rather than from a click
returning successfully.

Mixed into `Fakturama` for the same reason as `ReadsLists`: one concern per file,
one object to drive the application with.
"""
from __future__ import annotations

import re
import time

from ..driver import base
from ..driver.base import Element, wait_until
from ..driver.vision_driver import Region
from ..manual_review import stop


class DrivesDialogs:
    """Modal dialogs and their result lists. Requires `self.ax` and `self.vision`."""


    def wait_for_main_window(self, timeout: float = 15.0) -> None:
        """Block until the main window is frontmost again.

        Needed after a selector dialog closes. Everything downstream measures
        regions against the *focused* window, so acting while a dialog is still
        in front computes editor coordinates against the dialog's much smaller
        frame - which lands outside it and fails as an empty crop. A fixed sleep
        is not enough; the dialog sometimes lingers.
        """
        wait_until(
            lambda: (self.window_title() or "").startswith("Fakturama - "),
            timeout=timeout,
            description="the selector dialog to close",
        )

    def choose_row(self, point: tuple[float, float], attempts: int = 3) -> None:
        """Double-click a list row and wait for the selector to close.

        Retried, because the double-click intermittently fails to register on
        these SWT tables and a single miss would otherwise abandon a run that
        was otherwise fine. Confirmation is the dialog closing, not the click
        returning.
        """
        for attempt in range(attempts):
            self.vision.click(point, double=True)
            try:
                self.wait_for_main_window(timeout=8.0)
                return
            except TimeoutError:
                self.log.warn(f"row selection did not confirm (attempt {attempt + 1})")
        raise stop(
            "could not confirm the selected row",
            "app",
            at=[round(coordinate) for coordinate in point],
        )

    def dismiss_modals(self, attempts: int = 4) -> None:
        """Close any modal left in front, so a run starts from the main window.

        A previous run that stopped inside a selector leaves that dialog open,
        and the editor tabs are then unreachable - the accessibility tree only
        exposes the frontmost window. Cancelling is preferred over confirming so
        nothing is committed by accident.
        """
        for _ in range(attempts):
            root = self.ax.snapshot()[0]
            title = root.title or ""
            if root.role != "AXWindow" or title.startswith("Fakturama - "):
                return
            for label in ("Cancel", "Close", "OK"):
                try:
                    self.press_dialog_button(label)
                    break
                except base.DriverError:
                    continue
            else:
                self.vision.press_key("escape")
            self.log.info(f"dismissed a leftover dialog: {title!r}")
            time.sleep(1.0)

    def wait_for_dialog(self, title_pattern: str, timeout: float = 20.0) -> Element:
        """Wait for a modal window whose title matches, and return its root."""
        regex = re.compile(title_pattern, re.IGNORECASE)

        def find_dialog():
            root = self.ax.snapshot()[0]
            if root.role == "AXWindow" and root.title and regex.search(root.title):
                return root
            return None

        return wait_until(
            find_dialog, timeout=timeout, description=f"dialog ~{title_pattern!r}"
        )

    def dialog_button(self, title: str) -> Element:
        return base.find_one(self.ax, role="AXButton", title=title)

    def dialog_search(self, term: str, settle: float = 1.8) -> None:
        """Type into a selector dialog's search box.

        The box is a real `AXSearchField`, so it can be located structurally
        rather than by pixel. It is clicked rather than written via AXValue
        because the filter runs off keystrokes - setting the value silently
        leaves the list unfiltered.
        """
        field = base.find_one(self.ax, role="AXTextField", subrole="AXSearchField")
        self.vision.click(field.center)
        self.vision.press_key("a", ["command"])
        self.vision.type_text(term)
        time.sleep(settle)
        self.log.action(f"searched for {term!r}")

    def dialog_list_region(self) -> Region:
        """Bounds of a selector dialog's result list.

        The rows themselves are not exposed to accessibility, but the scroll
        area that holds them is - so AX supplies the bounds and OCR reads the
        rows inside, keeping the dialog's chrome out of the match.
        """
        # The product selector has two scroll areas (the row list and a preview
        # pane); the list is the larger one.
        areas = base.find(self.ax, role="AXScrollArea")
        if not areas:
            raise stop("the selector dialog exposes no result list", "app")
        area = max(areas, key=lambda a: a.width * a.height)
        return Region(area.x, area.y, area.x + area.width, area.y + area.height)

    def press_dialog_button(self, title: str) -> None:
        self.ax.press(self.dialog_button(title))
        self.log.action(f"pressed dialog button {title!r}")
