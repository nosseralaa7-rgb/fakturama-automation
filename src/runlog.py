"""Per-run artifacts: a step log plus a screenshot of every state change.

Every run writes to `runs/<timestamp>/`, producing the annotated screenshots
required as a deliverable and the evidence trail used to debug a failed step.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


class RunLog:
    """Records what the automation did, and what the screen looked like."""

    def __init__(self, root: str | Path = "runs", name: str | None = None) -> None:
        stamp = name or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.directory = Path(root) / stamp
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []
        self._step = 0

    # ------------------------------------------------------------------ log

    def event(self, kind: str, message: str, **fields: Any) -> None:
        entry = {
            "step": self._step,
            "at": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "message": message,
            **fields,
        }
        self.events.append(entry)
        print(f"[{kind:8}] {message}" + (f"  {fields}" if fields else ""))
        self._flush()

    def info(self, message: str, **fields: Any) -> None:
        self.event("info", message, **fields)

    def action(self, message: str, **fields: Any) -> None:
        self.event("action", message, **fields)

    def verified(self, message: str, **fields: Any) -> None:
        self.event("verified", message, **fields)

    def warn(self, message: str, **fields: Any) -> None:
        self.event("warn", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self.event("error", message, **fields)

    # ---------------------------------------------------------------- steps

    def step(self, driver, title: str) -> str | None:
        """Advance to a named step and capture the screen as it now stands."""
        self._step += 1
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
        path = self.directory / f"{self._step:02d}-{slug}.png"
        captured = driver.screenshot(str(path))
        self.event("step", title, screenshot=Path(captured).name if captured else None)
        return captured

    def _flush(self) -> None:
        (self.directory / "run.json").write_text(json.dumps(self.events, indent=2))

    def summary(self) -> str:
        kinds: dict[str, int] = {}
        for event in self.events:
            kinds[event["kind"]] = kinds.get(event["kind"], 0) + 1
        counts = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
        return f"{self.directory}: {counts}"
