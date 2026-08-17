#!/usr/bin/env python3
"""Reset Fakturama to an empty, freshly seeded database.

Development tool. Used to prove the select-or-create branches from a known
starting point, and to re-run the flow twice in a row when checking that a
second run selects existing master data instead of duplicating it.

    python scripts/reset_fakturama.py [--workspace ~/FakturamaData]

Deletes the working directory and the Eclipse workspace metadata, relaunches
the application, and drives the first-run dialog. Everything in the working
directory is destroyed.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.driver import base
from src.driver.ax_driver import AXDriver

APP = Path.home() / "Applications" / "Fakturama2.app"
ECLIPSE_METADATA = Path.home() / ".fakturama2"
KEYRING = Path.home() / ".eclipse_keyring"


def quit_app() -> None:
    subprocess.run(["pkill", "-f", "Fakturama2.app"], capture_output=True)
    time.sleep(4)


def launch() -> None:
    subprocess.run(["open", "-a", str(APP)], check=True)


def initialise(workspace: Path, timeout: float = 480.0) -> None:
    """Drive the first-run dialogs and wait for the main window.

    Setting the working directory is only the first step: Fakturama then
    announces that it has to restart, so the sequence is a loop over whatever
    dialog is in front rather than a fixed script. The generous timeout covers
    that restart plus the database seeding that follows it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            driver = AXDriver()
            title = driver.window_title() or ""
        except base.DriverError:
            time.sleep(2)  # the app is mid-restart and has no window yet
            continue

        if title.startswith("Fakturama - "):
            print(f"ready: {title}")
            return

        if "Initialization" in title:
            fields = sorted(
                (e for e in driver.snapshot() if e.role == "AXTextField"),
                key=lambda e: e.y,
            )
            if fields:
                # The upper field is the working directory; "use default
                # database settings" is already ticked.
                driver.set_value(fields[0], str(workspace))
                time.sleep(0.5)
                _press_ok(driver, f"initialising into {workspace}")
        elif title:
            # Any other dialog on the way through (for example the notice that
            # switching workspace restarts the application) is acknowledged.
            _press_ok(driver, f"acknowledged {title!r}")
        time.sleep(2)

    raise SystemExit("timed out waiting for Fakturama to finish initialising")


def _press_ok(driver: AXDriver, note: str) -> None:
    try:
        driver.press(base.find_one(driver, role="AXButton", title="OK"))
        print(note)
    except base.DriverError:
        pass  # no OK on this window; the loop will look again


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=str(Path.home() / "FakturamaData"))
    args = parser.parse_args()
    workspace = Path(args.workspace).expanduser()

    quit_app()
    for path in (workspace, ECLIPSE_METADATA, KEYRING):
        if path.exists():
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            print(f"removed {path}")
    workspace.mkdir(parents=True, exist_ok=True)

    launch()
    initialise(workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
