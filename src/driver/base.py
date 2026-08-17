"""Driver-agnostic UI vocabulary.

The flow layer is written against `UIDriver` only. Two bindings implement it:

* `ax_driver.AXDriver`      - macOS Accessibility tree (structural, no coordinates)
* `vision_driver.VisionDriver` - screenshot + LLM grounding (per-frame coordinates)

On Windows the same interface would be bound to Microsoft UI Automation: `press`
maps to the Invoke pattern, `set_value` to the Value pattern, and `find` to a
property-condition tree search. Nothing above this module changes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol


class DriverError(RuntimeError):
    """Base class for grounding failures."""


class ElementNotFound(DriverError):
    pass


class AmbiguousMatch(DriverError):
    """More than one element matched a query that must resolve to exactly one.

    The flow treats this as a manual-review trigger rather than guessing.
    """


@dataclass(frozen=True)
class Element:
    """A located control, independent of how it was found."""

    role: str
    title: str | None = None
    value: str | None = None
    help: str | None = None
    subrole: str | None = None
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    #: Backend handle (an AXUIElement for the AX driver, None for vision hits).
    native: Any = field(default=None, repr=False, compare=False)
    #: Which layer grounded this element - recorded in the run log.
    source: str = "ax"

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2

    def label(self) -> str:
        """Human-readable identity used in logs and manual-review reports."""
        for candidate in (self.title, self.value, self.help):
            if candidate:
                text = str(candidate).strip().replace("\n", " ")
                if text:
                    return f"{self.role}:{text[:40]!r}"
        return self.role


def matches(element: Element, **criteria: Any) -> bool:
    """Match an element against field criteria.

    String criteria are exact by default. Suffix a key with `__contains` for a
    case-insensitive substring test, e.g. `find(role="AXButton", title__contains="Save")`.
    """
    for key, expected in criteria.items():
        if expected is None:
            continue
        if key.endswith("__contains"):
            actual = getattr(element, key[: -len("__contains")], None)
            if actual is None or str(expected).lower() not in str(actual).lower():
                return False
        else:
            if getattr(element, key, None) != expected:
                return False
    return True


class UIDriver(Protocol):
    """Everything the flow layer is allowed to do to the application."""

    def activate(self) -> None:
        """Bring the target application to the foreground."""

    def snapshot(self) -> list[Element]:
        """Flattened list of currently reachable elements."""

    def press(self, element: Element) -> None:
        """Activate a control (button, menu item, list row)."""

    def set_value(self, element: Element, text: str) -> None:
        """Replace the text content of an editable control."""

    def screenshot(self, path: str) -> str | None:
        """Capture the target window; returns the path, or None if unavailable."""


def find(driver: UIDriver, **criteria: Any) -> list[Element]:
    """All elements in the current snapshot matching the criteria."""
    return [e for e in driver.snapshot() if matches(e, **criteria)]


def find_one(driver: UIDriver, **criteria: Any) -> Element:
    """Exactly one match, or raise.

    Ambiguity is an error rather than a "pick the first" heuristic: the task
    specification requires stopping for manual review whenever a selection is
    not unambiguous.
    """
    hits = find(driver, **criteria)
    if not hits:
        raise ElementNotFound(f"no element matching {criteria}")
    if len(hits) > 1:
        raise AmbiguousMatch(
            f"{len(hits)} elements matched {criteria}: {[h.label() for h in hits]}"
        )
    return hits[0]


def wait_until(
    predicate: Callable[[], Any],
    timeout: float = 15.0,
    poll: float = 0.4,
    description: str = "condition",
) -> Any:
    """Poll until `predicate` returns something truthy, or raise on timeout.

    Every state change in the flow is confirmed through this rather than a fixed
    sleep, so the automation stays correct on a slow machine.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except DriverError as exc:  # transient during a repaint
            last_error = exc
        time.sleep(poll)
    suffix = f" (last error: {last_error})" if last_error else ""
    raise TimeoutError(f"timed out after {timeout}s waiting for {description}{suffix}")


def wait_for_element(driver: UIDriver, timeout: float = 15.0, **criteria: Any) -> Element:
    """Wait until exactly one element matches, then return it."""
    return wait_until(
        lambda: find_one(driver, **criteria),
        timeout=timeout,
        description=f"element {criteria}",
    )


def summarize(elements: Iterable[Element], limit: int = 40) -> str:
    """Compact multi-line description of a snapshot, for logs and error reports."""
    lines = [f"  {e.label()} @({e.x:.0f},{e.y:.0f})" for e in list(elements)[:limit]]
    return "\n".join(lines)
