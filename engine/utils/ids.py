"""Identifier generation.

Run ids sort chronologically as plain strings, so a directory listing of ``data/runs/`` is already
in order, and every random decision a run makes (the split, the control-group holdout) is derived
from the run id via :func:`seed_from`, so a run is reproducible from its id alone.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from engine.utils.time import utc_now

__all__ = ["new_model_id", "new_run_id", "new_upload_id", "seed_from"]

_SEED_MODULUS = 2**32


def new_run_id(now: datetime | None = None) -> str:
    """Return a new run id of the form ``r_<yyyymmdd>_<8 hex>``.

    The date prefix makes ids sortable; the random suffix makes them unique within a day.
    """
    moment = utc_now() if now is None else now
    return f"r_{moment.strftime('%Y%m%d')}_{secrets.token_hex(4)}"


def new_upload_id() -> str:
    """Return a new upload id of the form ``u_<12 hex>``."""
    return f"u_{secrets.token_hex(6)}"


def new_model_id(use_case_id: str, version: int) -> str:
    """Return the model id for ``version`` of ``use_case_id``: ``m_<use_case_id>_<version>``."""
    return f"m_{use_case_id}_{version}"


def seed_from(run_id: str) -> int:
    """Return a stable 32-bit seed derived from ``run_id``.

    ``hash()`` is randomised per process, so the digest is taken from sha256 instead: the same run
    id yields the same seed in every process and on every machine.
    """
    digest = hashlib.sha256(run_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % _SEED_MODULUS
