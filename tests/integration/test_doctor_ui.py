"""Data Doctor's page (`ui/modules/doctor/index.js`) in jsdom, against REAL API responses (Plan L M155, M157, M161).

The node test (`production/ui/doctor/doctor.test.mjs`) draws the page on a diagnosis the app below made of
five files - a customer list, a recharge log, a ticket log whose IDs carry a prefix the list's do not, and
two months of one bills report, the second with a renamed column - and agrees to RCA's outcome:

* **diagnosis** - `POST /doctor/diagnosis` on the five uploads (links, stacks, and RCA's outcome offer);
* **agreement** - `POST /doctor/outcome` for the offer's default window, its sentence word for word;
* **uploads** - what the page holds for each file.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from tests.fixtures.node import skip_without_jsdom
from tests.integration.test_api_doctor_outcome import customers_csv, recharges_csv

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
TESTS_DIR: Final[Path] = NODE_DIR / "doctor"


def tickets_csv() -> bytes:
    lines = ["ticket_id,customer_id,opened_on,topic"]
    lines += [
        f"T{index:05d},c{index % 600:05d},2025-0{1 + index % 6}-1{index % 9},network" for index in range(900)
    ]
    return ("\n".join(lines) + "\n").encode()


def bills_csv(month: int, amount: str) -> bytes:
    lines = [f"customer_id,bill_date,{amount},plan"]
    lines += [f"C{index:05d},2025-{month:02d}-05,{100 + index % 50},prepaid" for index in range(600)]
    return ("\n".join(lines) + "\n").encode()


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def write_fixtures(root: Path, config_root: Path) -> Path:
    out = root / "fixtures"
    out.mkdir(parents=True)
    files = {
        "customers.csv": customers_csv(),
        "recharges.csv": recharges_csv(),
        "tickets.csv": tickets_csv(),
        "bills_2025_04.csv": bills_csv(4, "amount"),
        "bills_2025_05.csv": bills_csv(5, "bill_amount"),
    }
    bodies: dict[str, Any] = {}
    with TestClient(create_app(config_root=config_root, data_dir=root / "data")) as client:
        uploads = []
        for name, data in files.items():
            answer = _ok(
                client.post(
                    "/uploads",
                    files={"file": (name, data, "text/csv")},
                    data={"use_case": "payment-propensity", "mode": "train"},
                ),
                201,
            )
            uploads.append({"upload_id": answer["upload_id"], "name": name, "intake": answer["intake"]})
        bodies["uploads"] = uploads
        diagnosis = bodies["diagnosis"] = _ok(
            client.post("/doctor/diagnosis", json={"upload_ids": [u["upload_id"] for u in uploads]})
        )
        rca = next(item for item in diagnosis["use_cases"] if item["use_case_id"] == "rca")
        offer = rca["outcome_offer"]
        choice = next(item for item in offer["choices"] if item["default"])
        bodies["agreement"] = _ok(
            client.post(
                "/doctor/outcome",
                json={
                    "use_case": "rca",
                    "entity_upload_id": offer["entity_upload_id"],
                    "event_upload_id": offer["event_upload_id"],
                    "date_column": offer["date_column"],
                    "days": choice["days"],
                    "sentence": choice["sentence"],
                    "agreed": True,
                },
            ),
            201,
        )
    for name, body in bodies.items():
        (out / f"{name}.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    return out


def test_the_fixtures_are_the_world_the_screen_is_tested_in(tmp_path: Path, config_root: Path) -> None:
    """Runs without node: what the node test relies on is really what the API answered."""
    out = write_fixtures(tmp_path, config_root)
    diagnosis = json.loads((out / "diagnosis.json").read_text(encoding="utf-8"))
    links = {link["file_name"]: link for link in diagnosis["links"]}
    assert links["tickets.csv"]["coverage"] < 0.8 and links["tickets.csv"]["fix"]
    assert links["tickets.csv"]["fixed_coverage"] >= 0.8
    assert any(
        set(stack["file_names"]) == {"bills_2025_04.csv", "bills_2025_05.csv"}
        for stack in diagnosis["stacks"]
    )
    rca = next(item for item in diagnosis["use_cases"] if item["use_case_id"] == "rca")
    assert rca["outcome_offer"] and len(rca["outcome_offer"]["choices"]) > 1


def test_data_doctor_page_in_jsdom(tmp_path: Path, config_root: Path) -> None:
    node = skip_without_jsdom(NODE_DIR)  # REQUIRE_JSDOM=1 (CI) turns a skip into a failure
    out = write_fixtures(tmp_path, config_root)
    tests = sorted(str(p) for p in TESTS_DIR.glob("*.test.mjs"))
    assert tests, "no jsdom test was found"
    result = subprocess.run(
        [node, "--test", *tests],
        cwd=NODE_DIR,
        env={**os.environ, "PB_FIXTURES": str(out)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
