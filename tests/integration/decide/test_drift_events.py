"""Plan J M109 acceptance: an event a person noted appears on the drift view of the run it explains.

The run is a scoring run whose customers really moved: the training and scoring runs are written by
`tests.fixtures.make_run.write_run`, and the drift verdict is measured by the engine's own
`compute_drift` against a baseline built by `register.drift_baseline` (`tests/fixtures/decide/drift_runs.py`).
Everything else goes through the real routes, with sign-in on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.audit.events import AuditQuery
from engine.decide.drift_annotations import DRIFT_ANNOTATIONS_FILENAME
from engine.pilot.plain import jargon_in
from engine.storage import LocalStorage, run_key
from tests.fixtures.decide.drift_runs import MOVED, DriftedRun, write_drifted_run
from tests.fixtures.make_run import RunSpec, write_run
from tests.integration.production.access_support import audit_log_at, bearer, local_app, make_user

pytestmark = pytest.mark.integration


class Setup:
    def __init__(self, tmp_path: Path, *, move: bool = True) -> None:
        self.storage = LocalStorage(tmp_path)
        self.run: DriftedRun = write_drifted_run(self.storage, move=move)
        self.app = local_app(tmp_path)
        self.client = TestClient(self.app)
        self.analyst = bearer(self.app, make_user(self.app, "analyst_1", roles=(Role.ANALYST,)))
        self.viewer = bearer(self.app, make_user(self.app, "viewer_1", roles=(Role.VIEWER,)))
        self.url = f"/runs/{self.run.run_id}/drift-events"

    def note(self, **body: Any) -> Any:
        payload = {"event_date": "2026-08-25", "kind": "price_change", "note": "Prices went up 8%."} | body
        return self.client.post(self.url, json=payload, headers=self.analyst)

    def view(self) -> dict[str, Any]:
        response = self.client.get(self.url, headers=self.viewer)
        assert response.status_code == 200, response.text
        return dict(response.json())


def test_the_fixture_run_really_drifted(tmp_path: Path) -> None:
    """The premise: the engine itself measured a change, and it names the measures that moved."""
    setup = Setup(tmp_path)
    assert setup.run.report.status.value == "drifted"
    assert set(MOVED) <= set(setup.run.report.drifted_features)


def test_a_noted_event_appears_on_the_drift_view(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    before = setup.view()
    assert before["events"] == [] and before["drift_status"] == "drifted"
    assert before["headline"] == "No event has been noted for the period since the model was trained."

    drift_bytes = setup.storage.read_bytes(run_key(setup.run.run_id, "drift.json"))
    response = setup.note(measures=["visits_last_7d"])
    assert response.status_code == 201, response.text

    view = setup.view()
    (event,) = view["events"]
    assert event["kind_label"] == "A price change" and event["note"] == "Prices went up 8%."
    assert event["counted"] is True and event["reason"] is None
    assert event["explains"] == ["visits_last_7d"]
    assert view["headline"].startswith("Possibly explained by a price change on 25 Aug 2026")
    assert "visits_last_7d" in view["explained_measures"]
    assert "tenure_months" in view["unexplained_measures"]
    # The verdict is the run's own and is never softened by a note.
    assert view["drift_status"] == "drifted"
    assert setup.storage.read_bytes(run_key(setup.run.run_id, "drift.json")) == drift_bytes
    assert setup.storage.exists(run_key(setup.run.run_id, DRIFT_ANNOTATIONS_FILENAME))
    assert "does not change the measured change" in view["note"]


def test_an_event_outside_the_period_is_kept_but_not_counted(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    assert setup.note(event_date="2026-07-01", note="Spring sale").status_code == 201  # before training
    assert setup.note(event_date="2026-10-15", note="Rival opens").status_code == 201  # after the scoring run
    view = setup.view()
    assert [e["counted"] for e in view["events"]] == [False, False]
    assert "before the model was trained" in view["events"][0]["reason"]
    assert "after the day this run measured" in view["events"][1]["reason"]
    assert view["headline"] == "No event has been noted for the period since the model was trained."
    assert view["explained_measures"] == []


def test_a_general_event_is_a_reason_for_the_change_as_a_whole(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    assert setup.note(kind="competitor_launch", note="New rival site").status_code == 201
    view = setup.view()
    assert view["events"][0]["measures"] == [] and view["events"][0]["explains"] == []
    assert view["headline"].startswith("Possibly explained by a competitor's launch")
    assert view["explained_measures"] == []  # no measure is claimed for an event that names none


def test_a_measure_the_report_did_not_compare_is_refused(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    response = setup.note(measures=["not_a_measure"])
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "DRIFT_ANNOTATION_INVALID"
    assert "not_a_measure" in response.json()["detail"]["message"]
    assert setup.view()["events"] == []
    assert not setup.storage.exists(run_key(setup.run.run_id, DRIFT_ANNOTATIONS_FILENAME))


def test_a_stable_run_lists_its_notes_and_has_nothing_to_explain(tmp_path: Path) -> None:
    setup = Setup(tmp_path, move=False)
    assert setup.run.report.status.value == "stable"
    assert setup.note().status_code == 201
    view = setup.view()
    assert view["headline"] is None and view["drift_status"] == "stable"
    assert len(view["events"]) == 1


def test_a_run_with_no_drift_report_refuses_a_note_and_says_so(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    training = setup.run.training_run_id  # a training run writes no drift report
    url = f"/runs/{training}/drift-events"
    refused = setup.client.post(
        url, json={"event_date": "2026-08-25", "kind": "other", "note": "x"}, headers=setup.analyst
    )
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "DRIFT_NOT_MEASURED"
    view = setup.client.get(url, headers=setup.viewer).json()
    assert view["drift_measured"] is False and view["drift_status"] is None and view["headline"] is None
    assert setup.client.get("/runs/r_20260101_00000000/drift-events", headers=setup.viewer).status_code == 404


def test_a_note_can_be_removed_and_an_unknown_id_is_refused(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.note()
    (event,) = setup.view()["events"]
    gone = setup.client.delete(f"{setup.url}/{event['annotation_id']}", headers=setup.analyst)
    assert gone.status_code == 200 and gone.json()["events"] == []
    again = setup.client.delete(f"{setup.url}/{event['annotation_id']}", headers=setup.analyst)
    assert again.status_code == 404 and again.json()["detail"]["code"] == "DRIFT_ANNOTATION_NOT_FOUND"


def test_only_an_analyst_may_note_an_event_and_it_is_audited_without_the_text(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    refused = setup.client.post(
        setup.url,
        json={"event_date": "2026-08-25", "kind": "other", "note": "secret plan"},
        headers=setup.viewer,
    )
    assert refused.status_code == 403
    assert setup.client.get(setup.url).status_code in (401, 403)  # sign-in is on
    assert setup.note(note="Rival sale, call Asha on 98xxxxxx").status_code == 201

    events = audit_log_at(tmp_path).query(AuditQuery(action="drift_events.add"))
    assert [e.outcome for e in events].count("denied") == 1  # the Viewer's attempt is on record too
    (done,) = [e for e in events if e.outcome != "denied"]
    stored = json.dumps([e.model_dump(mode="json") for e in events], default=str)
    assert done.details["run_id"] == setup.run.run_id
    assert setup.run.run_id in stored
    assert "Asha" not in stored and "98xxxxxx" not in stored  # the typed text is never audited


def test_every_sentence_the_view_writes_is_plain_language(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    for body in (
        {"event_date": "2026-07-01"},
        {"measures": ["visits_last_7d", "tenure_months"]},
        {"event_date": "2026-10-15", "kind": "seasonal", "note": "Diwali"},
    ):
        setup.note(**body)
    view = setup.view()
    texts = [
        view["headline"],
        view["note"],
        *(e["reason"] or "" for e in view["events"]),
        *(label for _, label in view["kinds"]),
    ]
    for text in texts:
        assert not jargon_in(text or ""), (text, jargon_in(text or ""))


def test_a_scoring_run_with_no_notes_is_unchanged_by_the_feature(tmp_path: Path) -> None:
    """Default behaviour: nothing is written to a run until someone notes an event."""
    storage = LocalStorage(tmp_path)
    run_id = write_run(storage, RunSpec("targeted-advertisement", rows=200))
    app = local_app(tmp_path)
    client = TestClient(app)
    viewer = bearer(app, make_user(app, "viewer_2", roles=(Role.VIEWER,)))
    assert client.get(f"/runs/{run_id}/drift-events", headers=viewer).status_code == 200
    assert not storage.exists(run_key(run_id, DRIFT_ANNOTATIONS_FILENAME))
