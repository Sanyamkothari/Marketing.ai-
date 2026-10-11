"""Plan M, M178 (DEC-1608): Reports, `#/reports[/<tab>]`, in the fast suite.

Two node suites in `tests/integration/production/ui/reports/` (sharing that directory's jsdom install, as the other
screens' tests do) draw the page as `ui/index.html` loads it, against the API answers recorded from the validated
Hillstrom demo (`tests/fixtures/ui_demo/api.json`):

* `tabs.test.mjs` - the four tabs; that every report reachable today from `#/pilot` (and its Build data page) is
  reachable from Reports; that the programme summary is the block Results draws, from the same source; and that no tab shows an engine word
  outside technical details;
* `audit_equal.test.mjs` - the audit form regrouped into three steps sends the same request as the old `#/audit`
  form for the same files and choices, once per click.

The last claim of the acceptance ("the audit form's results are unchanged") is proven here against the REAL API:
the files are uploaded to a real app, both forms are driven in jsdom with those real upload answers, and the two
bodies they send are posted to `POST /campaigns/audit`; the two answers must be the same.

Node and jsdom are a development dependency of that one directory: without them the tests skip with the install
command, or fail under `REQUIRE_JSDOM=1` as CI sets it (`tests/fixtures/node.py`).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.measurement.simulate import population
from tests.fixtures.node import skip_without_jsdom
from tests.integration.measurement.support import USE_CASE, ok

pytestmark = pytest.mark.integration

NODE_DIR: Final[Path] = Path(__file__).resolve().parent / "production" / "ui"
REPORTS_DIR: Final[Path] = NODE_DIR / "reports"

# Keys that differ between two otherwise identical answers: the moment each record was made.
_MOMENT: Final[tuple[str, ...]] = ("_at", "as_of")


def _node(test_file: str, env: dict[str, str] | None = None) -> str:
    node = skip_without_jsdom(NODE_DIR)
    result = subprocess.run(
        [node, "--test", str(REPORTS_DIR / test_file)],
        cwd=NODE_DIR,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-3000:]
    assert "# fail 0" in result.stdout
    return result.stdout


def test_the_reports_tabs_in_jsdom() -> None:
    _node("tabs.test.mjs")


def test_the_check_form_sends_the_old_forms_request_in_jsdom() -> None:
    _node("audit_equal.test.mjs")


def _upload_answer(client: TestClient, frame: pd.DataFrame, name: str) -> dict[str, Any]:
    payload = frame.to_csv(index=False, lineterminator="\n").encode()
    response = client.post(
        "/uploads", files={"file": (name, payload, "text/csv")}, data={"use_case": USE_CASE, "mode": "score"}
    )
    answer: dict[str, Any] = ok(response, 201)
    return answer


def _stripped(value: Any, campaign_id: str) -> Any:
    """An answer without what belongs to one record: its id (also inside other ids) and the moment it was made."""
    if isinstance(value, dict):
        return {
            k: _stripped(v, campaign_id)
            for k, v in value.items()
            if not k.endswith(_MOMENT) and k != "campaign_id"
        }
    if isinstance(value, list):
        return [_stripped(v, campaign_id) for v in value]
    if isinstance(value, str):
        return value.replace(campaign_id, "<campaign>")
    return value


def _same_answer(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return bool(_stripped(a, a["campaign"]["campaign_id"]) == _stripped(b, b["campaign"]["campaign_id"]))


def test_the_audit_result_through_the_new_form_equals_the_old_forms(
    config_root: Path, tmp_path: Path
) -> None:
    """The same files through either form, to a real app: the same answer."""
    skip_without_jsdom(NODE_DIR)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sim = population(10_000, 0.10, 0.04, seed=10321, control_share=0.2)
    rng = np.random.default_rng(1)
    scores = sim.scores
    assignment = pd.DataFrame(
        {
            "customer_id": scores["customer_id"],
            "group": np.where(scores["control_group"], 0, 1),
            "age": rng.integers(18, 80, len(scores)),
            "region": rng.choice(list("ABCD"), len(scores)),
        }
    )
    reached = rng.random(len(scores)) < 0.6
    contacts = pd.DataFrame(
        {
            "customer_id": scores["customer_id"],
            "contacted": np.where(scores["control_group"], 0, reached.astype(int)),
        }
    )
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        answers = {
            "assignment": _upload_answer(client, assignment, "assignment.csv"),
            "outcomes": _upload_answer(client, sim.outcomes, "outcomes.csv"),
            "contact": _upload_answer(client, contacts, "contacts.csv"),
        }
        answers["programme"] = answers[
            "outcomes"
        ]  # the programme form is only drawn against, not posted, here
        uploads = tmp_path / "uploads.json"
        uploads.write_text(json.dumps(answers), encoding="utf-8")
        out = tmp_path / "bodies"
        _node("audit_equal.test.mjs", {"RP_OUT": str(out), "RP_UPLOADS": str(uploads)})

        for name in ("audit", "audit_with_contact"):
            old = json.loads((out / f"{name}_old.json").read_text(encoding="utf-8"))
            new = json.loads((out / f"{name}_new.json").read_text(encoding="utf-8"))
            assert new == old, "the same request"
            answer_old = ok(client.post("/campaigns/audit", json=old), 201)
            answer_new = ok(client.post("/campaigns/audit", json=new), 201)
            assert (
                answer_new["campaign"]["campaign_id"] != answer_old["campaign"]["campaign_id"]
            ), "two records"
            assert _same_answer(answer_new, answer_old), f"{name}: the same result"
            assert answer_new["audit"], "an audit was made"
        # The contact file changed the answer, so the comparison above is not vacuous.
        plain = ok(
            client.post("/campaigns/audit", json=json.loads((out / "audit_new.json").read_text())), 201
        )
        with_contact = ok(
            client.post(
                "/campaigns/audit", json=json.loads((out / "audit_with_contact_new.json").read_text())
            ),
            201,
        )
        assert plain.get("contacts") is None and with_contact.get("contacts") is not None
