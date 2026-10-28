"""Shared helpers for the family-D (other domains, messy files) real-data runners.

Not a runner. It re-uses family B's timing and JSON helpers (`b_common`) and writes results to
`/home/user/datasets/_results/d/<stage>/<name>.json`, outside the repository: they are derived
from the data, and the data is never committed.

Python 3.11 compatible; numpy and pandas only, plus the repository's own packages.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _p in (str(HERE), str(HERE.parents[1])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from b_common import (  # noqa: E402  (re-exported for the d_* runners, see __all__)
    DATASETS,
    REPO,
    Clock,
    command_line,
    env_info,
    jsonable,
    peak_rss_mb,
)

__all__ = [
    "DATASETS",
    "REPO",
    "RESULTS",
    "Clock",
    "command_line",
    "doctor_client",
    "env_info",
    "jsonable",
    "peak_rss_mb",
    "post_file",
    "write_result",
]

RESULTS = DATASETS / "_results" / "d"


def write_result(stage: str, name: str, payload: dict[str, Any]) -> Path:
    """Write `<RESULTS>/<stage>/<name>.json` and return its path."""
    out = RESULTS / stage / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(jsonable(payload), indent=2) + "\n", encoding="utf-8")
    return out


def doctor_client() -> Any:
    """A FastAPI TestClient on just the Data Doctor router, so the real HTTP route (multipart
    parsing, the 20 MB cap, the 422 messages) is what answers, without starting a server."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.v2.engines.doctor import router

    app = FastAPI()
    app.include_router(router, prefix="/v2/engines")
    return TestClient(app)


def post_file(
    client: Any, route: str, data: bytes, filename: str, fields: dict[str, str] | None = None
) -> dict[str, Any]:
    """POST a file to a Doctor route; return status and a short view of the answer (never cells)."""
    resp = client.post(
        f"/v2/engines/doctor/{route}",
        files={"file": (filename, data, "application/octet-stream")},
        data=fields or {},
    )
    out: dict[str, Any] = {"status": resp.status_code, "bytes": len(data)}
    try:
        body = resp.json()
    except ValueError:
        out["body"] = resp.text[:300]
        return out
    if resp.status_code != 200:
        out["detail"] = str(body.get("detail"))[:600]
        return out
    if route == "profile":
        out["readiness_score"] = body["readiness_score"]
        out["rows"] = body["rows"]
        out["columns"] = body["columns"]
        out["problems"] = [
            f"{p['severity']}:{p['code']}:{p.get('column') or ''}:{p['message'][:90]}"
            for p in body["problems"]
        ]
    else:
        out["rows"] = body["rows"]
        out["mapped"] = body["mapped"]
        out["problem_counts"] = body["problem_counts"]
    return out
