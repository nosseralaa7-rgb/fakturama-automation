#!/usr/bin/env python3
"""Dump the macOS Accessibility (AX) tree of a running app.

Development/diagnostic tool: used to discover how Fakturama's SWT widgets are
exposed to the accessibility layer, which determines what the AX driver can
ground on and where the vision fallback is required.

Usage:
    python scripts/ax_dump.py [app_name] [--depth N] [--all-windows]
"""
from __future__ import annotations

import argparse
import sys

import ApplicationServices as AS
from AppKit import NSWorkspace

# Attributes worth printing for each node, when present.
INTERESTING = (
    "AXRole",
    "AXSubrole",
    "AXTitle",
    "AXValue",
    "AXDescription",
    "AXHelp",
    "AXPlaceholderValue",
    "AXIdentifier",
    "AXRoleDescription",
)


def attr(element, name):
    """Read one AX attribute, returning None when unavailable."""
    err, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def find_app(name_fragment: str):
    """Return the pid of the first running app whose name contains the fragment."""
    fragment = name_fragment.lower()
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        name = app.localizedName() or ""
        # Skip macOS helper processes such as "Open and Save Panel Service (X)".
        if fragment in name.lower() and "(" not in name:
            return app.processIdentifier(), name
    return None, None


def describe(element) -> str:
    """One-line summary of a node: role plus whatever identifying text it exposes."""
    parts = []
    for name in INTERESTING:
        value = attr(element, name)
        if value in (None, ""):
            continue
        text = str(value).replace("\n", " ")
        if len(text) > 60:
            text = text[:57] + "..."
        parts.append(f"{name[2:]}={text!r}")

    position, size = attr(element, "AXPosition"), attr(element, "AXSize")
    if position is not None and size is not None:
        # pyobjc renders these as opaque AXValue refs; the repr carries the numbers.
        parts.append(f"pos/size={position}/{size}")
    return " ".join(parts)


def walk(element, depth: int, max_depth: int, counter: dict) -> None:
    print("  " * depth + describe(element))
    counter["nodes"] += 1
    role = attr(element, "AXRole")
    if role:
        counter.setdefault("roles", {})
        counter["roles"][str(role)] = counter["roles"].get(str(role), 0) + 1
    if depth >= max_depth:
        return
    for child in attr(element, "AXChildren") or []:
        walk(child, depth + 1, max_depth, counter)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("app", nargs="?", default="Fakturama")
    parser.add_argument("--depth", type=int, default=12)
    parser.add_argument("--all-windows", action="store_true")
    args = parser.parse_args()

    if not AS.AXIsProcessTrusted():
        print("ERROR: this process lacks Accessibility permission.", file=sys.stderr)
        return 2

    pid, name = find_app(args.app)
    if pid is None:
        print(f"ERROR: no running app matching {args.app!r}", file=sys.stderr)
        return 1
    print(f"=== {name} (pid {pid}) ===")

    app_element = AS.AXUIElementCreateApplication(pid)

    # SWT/Eclipse leaves AXWindows empty, so fall back to AXFocusedWindow.
    roots = list(attr(app_element, "AXWindows") or [])
    if not roots or not args.all_windows:
        focused = attr(app_element, "AXFocusedWindow")
        if focused is not None:
            roots = [focused]
    if not roots:
        print("no reachable windows")
        return 1

    counter = {"nodes": 0}
    for root in roots:
        walk(root, 0, args.depth, counter)

    print(f"\n--- {counter['nodes']} nodes ---")
    for role, count in sorted(counter.get("roles", {}).items(), key=lambda kv: -kv[1]):
        print(f"{count:5d}  {role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
