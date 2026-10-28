"""Waiting for the background check of the whole file (`view.full_check`) in the API tests.

Approve is refused (`FULL_CHECK_RUNNING`) until the count of the whole file is in the case, and the job that makes it
runs beside the request. A test that approves waits here first, the way the screen's poll does: each tick asks the
visit to continue (which takes a finished count into the case) and reads the screen.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

__all__ = ["settled"]


def settled(client: TestClient, case_id: str, *, seconds: float = 60.0) -> dict[str, Any]:
    """The case once its whole-file check is done (or none is waiting); fails the test when it does not finish."""
    deadline = time.monotonic() + seconds
    while True:
        response = client.post(f"/cases/{case_id}/visit", json={})
        assert response.status_code == 200, response.text
        case: dict[str, Any] = response.json()
        shown = client.get(f"/cases/{case_id}/screen").json()["view"]["full_check"]
        if shown is None or shown["state"] in {"done", "failed"}:
            return case
        assert time.monotonic() < deadline, f"the whole-file check did not finish: {shown}"
        time.sleep(0.05)
