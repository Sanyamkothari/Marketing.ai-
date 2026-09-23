"""The one exception M48 raises, with a code and a business-language message (never a data value)."""

from __future__ import annotations

from typing import Final

__all__ = ["MAX_PRINCIPAL_ID_CHARS", "PrivacyError", "require_principal_id"]

MAX_PRINCIPAL_ID_CHARS: Final[int] = 256


class PrivacyError(Exception):
    """A privacy operation failed. `code` is machine-readable; `message` never quotes a principal id.

    Codes: `PRINCIPAL_ID_INVALID`, `ERASURE_INCOMPLETE`, `ERASURE_FAILED`, `LIFECYCLE_READ_FAILED`.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def require_principal_id(principal_id: str) -> str:
    """The stripped id, or `PrivacyError('PRINCIPAL_ID_INVALID')` when empty or over-long."""
    cleaned = principal_id.strip() if isinstance(principal_id, str) else ""
    if not cleaned or len(cleaned) > MAX_PRINCIPAL_ID_CHARS:
        raise PrivacyError(
            "PRINCIPAL_ID_INVALID",
            f"A data principal id must be between 1 and {MAX_PRINCIPAL_ID_CHARS} characters.",
        )
    return cleaned
