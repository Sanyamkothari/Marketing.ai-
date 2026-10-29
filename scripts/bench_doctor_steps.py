"""Time each step of a Data Doctor visit on a big file, through the API with the fake AI (no network).

Usage: .venv/bin/python scripts/bench_doctor_steps.py CSV [--goal retail-win-back] [--dump DIR] [--no-pool]

It uploads the file, opens a case, gives AI consent and then times: visit, goal, and the answer to the first open
question. With --dump each step's case JSON is written to DIR (one file per step) so two runs can be compared
byte for byte (`diff -r`); clocks are blanked and random ids are made a counter first. No cell value is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

os.environ.setdefault("MARKETING_AI_ALLOW_FAKE_AI", "true")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_CLOCK = re.compile(r'"\d{4}-\d\d-\d\dT[0-9:.]+(?:Z|[+-]\d\d:\d\d)"')


def _stable(case: dict[str, object]) -> str:
    """The case as sorted JSON with every clock blanked, so two runs of the same file can be diffed."""
    return _CLOCK.sub('""', json.dumps(case, indent=1, sort_keys=True))


def _fixed_ids() -> None:
    """Make the random ids (upload, case, event) a counter, so a question id built from an upload id matches."""
    import hashlib
    import itertools
    import secrets

    counter = itertools.count()

    def token_hex(nbytes: int | None = None) -> str:
        size = 32 if nbytes is None else nbytes
        return hashlib.sha256(str(next(counter)).encode()).hexdigest()[: 2 * size]

    secrets.token_hex = token_hex


def main() -> None:
    from fastapi.testclient import TestClient

    from api.main import create_app

    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--goal", default="retail-win-back")
    parser.add_argument("--dump", type=Path, default=None)
    parser.add_argument(
        "--no-pool",
        action="store_true",
        help="hash rows in this process instead of a pool of worker processes (for a machine short of memory)",
    )
    args = parser.parse_args()
    if args.dump:
        _fixed_ids()
    if args.no_pool:
        from engine.stages import ingest

        ingest._pool_disabled = True
    data = Path(tempfile.mkdtemp(prefix="bench-doctor-"))
    if args.dump:
        shutil.rmtree(args.dump, ignore_errors=True)
        args.dump.mkdir(parents=True)
    rows: list[tuple[str, float]] = []

    def step(name: str, call: Callable[[], Any]) -> Any:
        start, cpu_start = time.perf_counter(), time.process_time()
        response = call()
        took, cpu = time.perf_counter() - start, time.process_time() - cpu_start
        rows.append((name, took))
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
        print(
            f"{name:<10} {response.status_code} {took:6.1f}s wall {cpu:6.1f}s cpu   peak memory {peak} MB",
            flush=True,
        )
        if args.dump and response.headers.get("content-type", "").startswith("application/json"):
            (args.dump / f"{len(rows)}-{name}.json").write_text(_stable(response.json()))
        return response

    with TestClient(create_app(config_root=REPO / "configs", data_dir=data)) as client:
        up = step(
            "upload",
            lambda: client.post(
                "/uploads",
                files={"file": (args.csv.name, args.csv.read_bytes(), "text/csv")},
                data={"use_case": args.goal, "mode": "train"},
            ),
        )
        case_id = client.post("/cases", json={"upload_ids": [up.json()["upload_id"]]}).json()["case_id"]
        client.post(f"/cases/{case_id}/ai-consent", json={})
        case = step("visit", lambda: client.post(f"/cases/{case_id}/visit", json={})).json()
        case = step(
            "goal",
            lambda: client.post(
                f"/cases/{case_id}/goal",
                json={"version": case["version"], "goal_id": args.goal, "op_id": "g1"},
            ),
        ).json()
        open_q = [q for q in case.get("questions", []) if not q.get("answer") and q.get("options")]
        if open_q:
            q = open_q[0]
            step(
                "answer",
                lambda: client.post(
                    f"/cases/{case_id}/answers",
                    json={
                        "version": case["version"],
                        "question_id": q["question_id"],
                        "option_id": q["options"][0]["option_id"],
                        "op_id": "a1",
                    },
                ),
            )
        else:
            print("no open question to answer")
    print("total", round(sum(t for _, t in rows), 1))
    shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    main()
