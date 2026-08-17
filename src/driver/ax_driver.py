"""macOS Accessibility binding of `UIDriver`.

This is the structural layer: controls are located by role and title in the
accessibility tree, never by fixed screen coordinates. It is the direct analogue
of Microsoft UI Automation on Windows.

Fakturama is an Eclipse RCP/SWT application, and SWT exposes only part of itself
to the accessibility layer. Measured on Fakturama 2.2.0:

* reachable  - toolbar buttons, the left Navigation View, and all dialogs
* unreachable - the document editor body (Cust.Ref, Date, Items table, address
  controls); a fully populated New Order editor still yields ~47 nodes

`VisionDriver` covers the unreachable half; `CompositeDriver` routes between them.
"""
from __future__ import annotations

import subprocess
import time
from typing import Any

import ApplicationServices as AS
import Quartz
from AppKit import NSURL, NSWorkspace

from .base import DriverError, Element

#: Attributes copied into every Element.
_TEXT_ATTRIBUTES = ("AXTitle", "AXValue", "AXHelp", "AXSubrole")

#: Roles whose AXValue is an element reference rather than text.
_OPAQUE_VALUE_ROLES = {"AXTabGroup"}


def _attr(element: Any, name: str) -> Any:
    err, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def _point_or_size(element: Any, name: str, kind: int) -> tuple[float, float]:
    raw = _attr(element, name)
    if raw is None:
        return 0.0, 0.0
    ok, unpacked = AS.AXValueGetValue(raw, kind, None)
    if not ok or unpacked is None:
        return 0.0, 0.0
    # CGPoint exposes .x/.y, CGSize exposes .width/.height.
    first = getattr(unpacked, "x", None)
    if first is not None:
        return float(unpacked.x), float(unpacked.y)
    return float(unpacked.width), float(unpacked.height)


def _main_display_width_points() -> float:
    """Width of the main display in points, used to derive the Retina scale."""
    bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return float(bounds.size.width)


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    # pyobjc renders element references like "<AXUIElement 0x...>"; not useful text.
    if text.startswith("<AXUIElement"):
        return None
    return text


class AXDriver:
    """Drives an application through its accessibility tree."""

    def __init__(self, app_name: str = "Fakturama", max_nodes: int = 4000) -> None:
        self.app_name = app_name
        self.max_nodes = max_nodes
        if not AS.AXIsProcessTrusted():
            raise DriverError(
                "This process lacks macOS Accessibility permission. Grant it to your "
                "terminal under System Settings > Privacy & Security > Accessibility."
            )

    # ---------------------------------------------------------------- process

    def _running_app(self) -> Any:
        """The running process that owns the application's window.

        Several processes can share the name - helper processes, and leftovers
        from a previous launch - so a name match alone is not enough: picking
        one of those makes `is_active()` report false and activation fail
        against a process that has no window. Candidates are therefore filtered
        to one that actually exposes a window, preferring the active one.
        """
        fragment = self.app_name.lower()
        candidates = [
            app
            for app in NSWorkspace.sharedWorkspace().runningApplications()
            # Exclude helpers such as "Open and Save Panel Service (Fakturama)".
            if fragment in (app.localizedName() or "").lower()
            and "(" not in (app.localizedName() or "")
        ]
        if not candidates:
            raise DriverError(f"{self.app_name} is not running")

        with_windows = [
            app for app in candidates if self._has_window(app.processIdentifier())
        ]
        pool = with_windows or candidates
        for app in pool:
            if app.isActive():
                return app
        return pool[0]

    @staticmethod
    def _has_window(pid: int) -> bool:
        element = AS.AXUIElementCreateApplication(pid)
        if _attr(element, "AXFocusedWindow") is not None:
            return True
        return bool(_attr(element, "AXWindows"))

    @property
    def pid(self) -> int:
        return int(self._running_app().processIdentifier())

    def is_active(self) -> bool:
        """Whether the target app currently owns the foreground."""
        try:
            return bool(self._running_app().isActive())
        except DriverError:
            return False

    def activate(self) -> None:
        """Bring the app to the foreground.

        NSRunningApplication.activate does not reliably raise this app across
        Spaces, so drive the activation through System Events instead. SWT
        toolbar actions only dispatch when the application is frontmost.
        """
        subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application "System Events" to set frontmost of first process '
                f'whose name is "{self.app_name}" to true',
            ],
            check=False,
            capture_output=True,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self._running_app().isActive():
                time.sleep(0.4)  # let the window server finish compositing
                return
            time.sleep(0.3)
        raise DriverError(f"could not bring {self.app_name} to the foreground")

    # ------------------------------------------------------------------- tree

    def _root(self) -> Any:
        """The window to search.

        SWT leaves AXWindows empty on this application, so AXFocusedWindow is the
        only reliable entry point - and it correctly follows modal dialogs as
        they open and close.
        """
        app_element = AS.AXUIElementCreateApplication(self.pid)
        windows = _attr(app_element, "AXWindows") or []
        if len(windows) == 1:
            return windows[0]
        focused = _attr(app_element, "AXFocusedWindow")
        if focused is not None:
            return focused
        if windows:
            return windows[0]
        raise DriverError(f"{self.app_name} has no reachable window")

    def _to_element(self, native: Any) -> Element:
        role = _as_text(_attr(native, "AXRole")) or "AXUnknown"
        texts = {name: _attr(native, name) for name in _TEXT_ATTRIBUTES}
        raw_value = texts["AXValue"]
        if role in _OPAQUE_VALUE_ROLES:
            raw_value = None
        x, y = _point_or_size(native, "AXPosition", AS.kAXValueCGPointType)
        width, height = _point_or_size(native, "AXSize", AS.kAXValueCGSizeType)
        return Element(
            role=role,
            title=_as_text(texts["AXTitle"]),
            value=_as_text(raw_value),
            help=_as_text(texts["AXHelp"]),
            subrole=_as_text(texts["AXSubrole"]),
            x=x,
            y=y,
            width=width,
            height=height,
            native=native,
            source="ax",
        )

    def snapshot(self) -> list[Element]:
        """Depth-first walk of the current window."""
        elements: list[Element] = []
        stack = [self._root()]
        while stack and len(elements) < self.max_nodes:
            native = stack.pop()
            elements.append(self._to_element(native))
            children = _attr(native, "AXChildren") or []
            stack.extend(reversed(list(children)))
        return elements

    def window_title(self) -> str | None:
        return _as_text(_attr(self._root(), "AXTitle"))

    # ---------------------------------------------------------------- actions

    def press(self, element: Element) -> None:
        if element.native is None:
            raise DriverError(f"{element.label()} has no accessibility handle to press")
        err = AS.AXUIElementPerformAction(element.native, "AXPress")
        if err != 0:
            raise DriverError(f"AXPress failed on {element.label()} (error {err})")

    def set_value(self, element: Element, text: str) -> None:
        if element.native is None:
            raise DriverError(f"{element.label()} has no accessibility handle to set")
        err = AS.AXUIElementSetAttributeValue(element.native, "AXValue", text)
        if err != 0:
            raise DriverError(f"setting AXValue failed on {element.label()} (error {err})")

    def menu_item(self, menu_title: str, item_title: str) -> Element:
        """Find an item in the application menu bar.

        The menu bar is fully exposed to accessibility even though most of the
        window is not, which makes it the most reliable way to open a list or
        create a record - no icon hunting, no ambiguity about which small
        control in a panel header is "add" rather than "delete".
        """
        app_element = AS.AXUIElementCreateApplication(self.pid)
        bar = _attr(app_element, "AXMenuBar")
        if bar is None:
            raise DriverError("the application exposes no menu bar")

        for menu in _attr(bar, "AXChildren") or []:
            if _as_text(_attr(menu, "AXTitle")) != menu_title:
                continue
            for submenu in _attr(menu, "AXChildren") or []:
                for entry in _attr(submenu, "AXChildren") or []:
                    if _as_text(_attr(entry, "AXTitle")) == item_title:
                        return self._to_element(entry)
            raise DriverError(f"menu {menu_title!r} has no item {item_title!r}")
        raise DriverError(f"there is no {menu_title!r} menu")

    def actions(self, element: Element) -> list[str]:
        """Action names the control advertises - useful when probing new dialogs."""
        if element.native is None:
            return []
        err, names = AS.AXUIElementCopyActionNames(element.native, None)
        return list(names) if err == 0 and names else []

    # ------------------------------------------------------------ screenshots

    def window_id(self) -> int | None:
        """CoreGraphics window id of the window the accessibility tree is reading.

        Matched by geometry against the AX window rather than by picking the
        largest one, and searched across *all* windows rather than only
        on-screen ones: the target is frequently occluded or tucked away by
        Stage Manager, and an on-screen-only search then returns some small
        unrelated panel instead.
        """
        pid = self.pid
        listing = (
            Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
            )
            or []
        )
        candidates = [w for w in listing if w.get("kCGWindowOwnerPID") == pid]

        target = self.window_bounds()
        if target is not None:
            x, y, width, height = target
            for window in candidates:
                bounds = window.get("kCGWindowBounds") or {}
                if (
                    abs(float(bounds.get("X", -1)) - x) <= 2
                    and abs(float(bounds.get("Y", -1)) - y) <= 2
                    and abs(float(bounds.get("Width", -1)) - width) <= 2
                    and abs(float(bounds.get("Height", -1)) - height) <= 2
                ):
                    return window.get("kCGWindowNumber")

        # No geometric match (a modal may have just opened); fall back to the
        # largest window belonging to the application.
        best, best_area = None, 0.0
        for window in candidates:
            bounds = window.get("kCGWindowBounds") or {}
            area = float(bounds.get("Width", 0)) * float(bounds.get("Height", 0))
            if area > best_area:
                best, best_area = window.get("kCGWindowNumber"), area
        return best

    def window_bounds(self) -> tuple[float, float, float, float] | None:
        """Window rectangle in screen points, from the accessibility tree."""
        root = self._root()
        x, y = _point_or_size(root, "AXPosition", AS.kAXValueCGPointType)
        width, height = _point_or_size(root, "AXSize", AS.kAXValueCGSizeType)
        if width <= 0 or height <= 0:
            return None
        return x, y, width, height

    def screenshot(self, path: str) -> str | None:
        """Capture the application window's own contents.

        Reads the window's backing store through CoreGraphics rather than
        photographing the screen. That distinction matters twice over:

        * `screencapture -l <window-id>` returns the Stage Manager *thumbnail* -
          small and perspective-skewed - when that feature is on.
        * cropping a full-screen grab to the window's rectangle captures
          whatever is actually on top at those coordinates. That is not
          theoretical: a browser window overlapping Fakturama produced a
          screenshot of the browser, which OCR then read as a Fakturama editor
          with all its labels mysteriously missing.

        Reading the backing store is immune to occlusion, to Stage Manager, and
        to another application holding the foreground. Clicks still require
        focus; reading no longer does.
        """
        wid = self.window_id()
        if wid is None:
            return None
        image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull,
            Quartz.kCGWindowListOptionIncludingWindow,
            wid,
            Quartz.kCGWindowImageBoundsIgnoreFraming,
        )
        if image is None or Quartz.CGImageGetWidth(image) == 0:
            return None

        url = NSURL.fileURLWithPath_(path)
        destination = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
        if destination is None:
            return None
        Quartz.CGImageDestinationAddImage(destination, image, None)
        if not Quartz.CGImageDestinationFinalize(destination):
            return None
        return path
