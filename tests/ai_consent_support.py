"""Test helper: a data directory in which the workspace client has already let the AI read its data."""

from __future__ import annotations

from pathlib import Path

from engine.ai_consent import WORKSPACE_CLIENT, AiConsentStore
from engine.storage import LocalStorage
from engine.utils.time import utc_now


def consented(data_dir: Path, client_id: str = WORKSPACE_CLIENT) -> Path:
    """Record the client's yes in `data_dir` (as `POST /cases/{id}/ai-consent` does) and return the directory."""
    AiConsentStore(LocalStorage(data_dir)).give(client_id, actor="test", at=utc_now())
    return data_dir
