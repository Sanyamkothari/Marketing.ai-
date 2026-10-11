"""The one exception the compliance gate raises: a code, a plain message (Plan K M134).

The treat list builder turns it into its own `TreatListError` with the same code and message, so the
route that builds a treat list answers with it as it answers every other reason a list cannot be built.
"""

from __future__ import annotations

__all__ = ["ComplianceError"]


class ComplianceError(Exception):
    """The gate cannot decide; `code` is an existing configuration code (`CONFIG_INVALID`)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
