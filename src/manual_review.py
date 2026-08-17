"""Deliberate stops for human judgement.

The task specification repeatedly requires halting rather than guessing: when a
Debtor search returns conflicting rows, when a VAT rate exists with a different
definition, when a saved Product cannot be re-selected. Guessing in an
accounting system creates silent bad data, so every such branch raises
`ManualReviewRequired`, which the entry point turns into a non-zero exit with a
screenshot and a written reason.
"""
from __future__ import annotations

from typing import Any


class ManualReviewRequired(Exception):
    """Raised when the automation must not proceed without a human decision."""

    def __init__(self, reason: str, stage: str, evidence: dict[str, Any] | None = None):
        self.reason = reason
        self.stage = stage
        self.evidence = evidence or {}
        super().__init__(f"[{stage}] {reason}")

    def report(self) -> str:
        lines = [
            "MANUAL REVIEW REQUIRED",
            f"  stage:  {self.stage}",
            f"  reason: {self.reason}",
        ]
        for key, value in self.evidence.items():
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)


def stop(reason: str, stage: str, **evidence: Any) -> "ManualReviewRequired":
    """Build the exception; the caller raises it.

    Returning rather than raising keeps `raise stop(...)` readable at the call
    site and keeps static analysis aware that the branch terminates.
    """
    return ManualReviewRequired(reason, stage, evidence)
