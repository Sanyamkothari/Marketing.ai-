"""When the join key is itself personal data (phone, PAN, e-mail, Aadhaar): the working key is a salted hash (G24).

Two files are matched on a phone number or a PAN, but the model, the AI and the intermediate tables have no need to
carry the number itself. So the key is written as a **salted hash** while the files are joined, and the real value is
kept in one place only: a small lookup (`key_vault`) used when the contact list is exported, where the campaign
needs the real number.

* The hash is HMAC-SHA256 of the key as :func:`clean_contact_key` writes it (so `98765 43210` and `+919876543210`
  are one customer), with a random salt kept for the case; the same salt gives the same hash in every file of the
  case, a different case gives a different hash.
* **Aadhaar is never stored**, not even hashed into a lookup: a join on an Aadhaar number is refused and the person is
  asked for another key.
* Nothing here logs or returns a real value except :func:`key_vault` and :func:`attach_real_keys`, which are the vault
  and the export.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any, Final

import pandas as pd

__all__ = [
    "KEY_VAULT_FILENAME",
    "PII_KEY_KINDS",
    "WorkingKeyError",
    "attach_real_keys",
    "clean_contact_key",
    "key_vault",
    "new_salt",
    "pii_key_kind",
    "refuse_aadhaar",
    "working_key",
]

KEY_VAULT_FILENAME: Final[str] = "key_vault.parquet"
PII_KEY_KINDS: Final[tuple[str, ...]] = ("aadhaar", "pan", "phone", "email")
"""The personal-data kinds a key can be, in the order they are looked for."""
_HASH_CHARS: Final[int] = 32


class WorkingKeyError(Exception):
    """A key that cannot be used; `code` is a stable code and `message` says what to do, in plain words."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def clean_contact_key(value: object) -> str | None:
    """A phone, e-mail, PAN or ID written one way, so two files (or a suppression list) meet.

    An Indian mobile number is written `+91` and ten digits (the one phone rule, `engine.agent.key_steps`); anything
    else is lower-cased with its spaces removed. A blank is None.
    """
    from engine.agent.key_steps import KeyRule, key_text, normalise_key_value

    text = key_text(value)
    if text is None:
        return None
    written, converted = normalise_key_value(text, KeyRule(phone=True))
    if converted and written is not None and written.startswith("+"):
        return written
    plain, _ = normalise_key_value(text, KeyRule(spaces=True, case=True))
    return plain


def pii_key_kind(series: pd.Series[Any], name: str) -> str | None:
    """The personal-data kind a key column is made of (see :data:`PII_KEY_KINDS`), or None for an ordinary ID."""
    from engine.pii import detect_pii
    from engine.stages.ingest import infer_column_type

    kinds = set(detect_pii(series, name, infer_column_type(series)))
    return next((kind for kind in PII_KEY_KINDS if kind in kinds), None)


def refuse_aadhaar(kind: str | None) -> None:
    """Stop a join on an Aadhaar number: it is never stored, so another key must be used."""
    if kind == "aadhaar":
        raise WorkingKeyError(
            "KEY_AADHAAR_NOT_STORED",
            "This ID column holds Aadhaar numbers, which are never stored. Use another ID column "
            "(a customer number, or a phone number) to match the files.",
        )


def new_salt() -> str:
    """A random salt for one case."""
    return secrets.token_hex(16)


def working_key(series: pd.Series[Any], salt: str) -> pd.Series[Any]:
    """The key column as salted hashes (a blank stays blank)."""
    key = salt.encode("utf-8")

    def one(value: object) -> str | None:
        text = clean_contact_key(value)
        if text is None:
            return None
        return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()[:_HASH_CHARS]

    return series.map(one).astype(object)


def key_vault(real: pd.Series[Any], hashed: pd.Series[Any]) -> pd.DataFrame:
    """The lookup from working key to the real value, one row for each customer with a key."""
    frame = pd.DataFrame({"working_key": hashed.to_numpy(), "real_key": real.astype("string").to_numpy()})
    return frame.dropna(subset=["working_key"]).drop_duplicates("working_key").reset_index(drop=True)


_SAFE_NAME: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_]+")


def attach_real_keys(
    frame: pd.DataFrame, vault: pd.DataFrame, key_column: str, *, name: str = "contact_key"
) -> pd.DataFrame:
    """`frame` with the real key beside the working one: for the contact-list export, and nothing else."""
    lookup = dict(zip(vault["working_key"].tolist(), vault["real_key"].tolist(), strict=True))
    out = frame.copy()
    out[_SAFE_NAME.sub("_", name)] = out[key_column].map(lookup)
    return out
