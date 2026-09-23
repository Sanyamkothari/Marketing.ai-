"""The M49 schedule routes, end to end through the app: create, change, fire, miss, sync, refuse.

Plan section 4's scheduler bullets, through the API a person uses: "schedules fire" (the local
scheduler, driven one tick at a time by a fake clock the app shares), "missed runs are reported"
(missed slots listed and alerted, one catch-up), and a manual "fire now" that records one firing
and exactly one audit event (DEC-781). Nothing here trains: the app's job runner records what was
submitted, and a run's end is written by the test. The one test that trains is in
`test_scheduling_api_flow.py`, marked slow.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from moto import mock_aws

from api.routes.schedules import app_request, get_scheduler
from engine.access.roles import SYSTEM_SCHEDULER
from engine.contracts import RunRecord, RunState
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.runs import update_run
from engine.scheduling.alerts import AlertKind, AlertQuery, AlertStore
from engine.scheduling.retraining import MANAGED_BY_RETRAINING
from engine.scheduling.scheduler import EventBridgeScheduler, LocalScheduler, NullScheduler
from engine.scheduling.schedules import Schedule
from engine.storage import run_key
from tests.integration.production.access_support import local_app
from tests.integration.production.schedules_support import Api, build_api, code_of
from tests.unit.production.scheduling_support import USE_CASE, register_champion
from tests.unit.production.test_firing import training_frame

pytestmark = pytest.mark.integration

THREAD_NAME = "marketing-ai-scheduler"


@pytest.fixture
def api(tmp_path: Path, config_root: Path) -> Api:
    return build_api(tmp_path, config_root)


def create(api: Api, who: str = "analyst", **body: Any) -> Any:
    return api.client.post("/schedules", json=body, headers=api.as_(who))


def score_body(api: Api, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "use_case_id": USE_CASE,
        "kind": "score",
        "cadence": "daily",
        "parameters": {"onboarding_spec_id": api.world.spec.spec_id},
    }
    body.update(changes)
    return body


def hourly_drift_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "use_case_id": USE_CASE,
        "kind": "drift_check",
        "cadence": "0 * * * *",
        "timezone": "UTC",
    }
    body.update(changes)
    return body


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def scheduler_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == THREAD_NAME and thread.is_alive()]


# ---------------------------------------------------------------------------
# create / read / change / delete
# ---------------------------------------------------------------------------
def test_a_schedule_is_created_for_the_recipes_client_with_its_first_slot(api: Api) -> None:
    response = create(api, **score_body(api))
    assert response.status_code == 201, response.text
    made = Schedule.model_validate(response.json())
    assert made.client_id == api.world.client_id, "the client defaults to the recipe's"
    assert (made.cron, made.preset, made.timezone) == ("0 2 * * *", "daily", "Asia/Kolkata")
    assert made.created_by == api.user_ids["analyst"]
    # 02:00 in Kolkata on 1 Sep 2026 is 20:30 UTC on 1 Sep (the clock stands at 00:00 UTC that day).
    assert made.next_due_at == datetime(2026, 9, 1, 20, 30, tzinfo=UTC)

    listed = api.client.get("/schedules", params={"kind": "score"}, headers=api.as_("viewer"))
    assert [item["schedule_id"] for item in listed.json()["schedules"]] == [made.schedule_id]
    read = api.client.get(f"/schedules/{made.schedule_id}", headers=api.as_("viewer"))
    assert read.status_code == 200 and read.json() == response.json()

    (event,) = api.events("schedules.create")
    assert (event.actor_id, event.object_type, event.object_id) == (
        api.user_ids["analyst"],
        "schedule",
        made.schedule_id,
    )
    assert event.details["schedule_kind"] == "score" and event.details["client_id"] == api.world.client_id


@pytest.mark.parametrize(
    ("changes", "status", "code", "path"),
    [
        ({"cadence": "every tuesday"}, 422, "CRON_INVALID", "cadence"),
        ({"cadence": "0 2 1 * 1"}, 422, "CRON_NOT_PORTABLE", "cadence"),
        ({"timezone": "Mars/Olympus"}, 422, "TIMEZONE_UNKNOWN", "timezone"),
        ({"use_case_id": "not-a-use-case"}, 404, "USE_CASE_NOT_FOUND", "use_case_id"),
        ({"parameters": {}}, 422, "SCHEDULE_INVALID", "parameters"),
        ({"client_id": "cl_nobody"}, 404, "CLIENT_NOT_FOUND", "client_id"),
        ({"parameters": {"onboarding_spec_id": "sp_nope"}}, 404, "ONBOARDING_SPEC_NOT_FOUND", None),
    ],
)
def test_a_wrong_schedule_is_refused_with_its_code_and_field(
    api: Api, changes: dict[str, Any], status: int, code: str, path: str | None
) -> None:
    response = create(api, **score_body(api, **changes))
    assert response.status_code == status, response.text
    assert code_of(response) == code
    if path is not None:
        assert response.json()["detail"]["path"] == path
    assert api.client.get("/schedules", headers=api.as_("viewer")).json()["schedules"] == []
    (event,) = api.events("schedules.create")
    assert event.outcome == "failed"


def test_an_unknown_schedule_is_404_everywhere(api: Api) -> None:
    for method, path in [
        ("GET", "/schedules/sch_missing"),
        ("PATCH", "/schedules/sch_missing"),
        ("DELETE", "/schedules/sch_missing"),
        ("POST", "/schedules/sch_missing/fire"),
        ("POST", "/schedules/sch_missing/enable"),
        ("GET", "/schedules/sch_missing/firings"),
    ]:
        kwargs: dict[str, Any] = {"json": {}} if method == "PATCH" else {}
        response = api.client.request(method, path, headers=api.as_("analyst"), **kwargs)
        assert response.status_code == 404 and code_of(response) == "SCHEDULE_NOT_FOUND", (method, path)


def test_changing_pausing_and_resuming_moves_the_next_slot(api: Api) -> None:
    made = create(api, **score_body(api)).json()
    schedule_id = made["schedule_id"]

    changed = api.client.patch(
        f"/schedules/{schedule_id}",
        json={"cadence": "30 6 * * *", "timezone": "UTC"},
        headers=api.as_("analyst"),
    )
    assert changed.status_code == 200, changed.text
    assert (changed.json()["cron"], changed.json()["preset"]) == ("30 6 * * *", None)
    assert utc(changed.json()["next_due_at"]) == datetime(2026, 9, 1, 6, 30, tzinfo=UTC)
    (event,) = api.events("schedules.update")
    assert event.before_hash is not None and event.after_hash is not None

    paused = api.client.post(f"/schedules/{schedule_id}/disable", headers=api.as_("analyst"))
    assert paused.status_code == 200
    assert paused.json()["enabled"] is False and paused.json()["next_due_at"] is None

    api.world.clock.advance(days=3)
    resumed = api.client.post(f"/schedules/{schedule_id}/enable", headers=api.as_("analyst"))
    assert resumed.json()["enabled"] is True
    assert utc(resumed.json()["next_due_at"]) == datetime(2026, 9, 4, 6, 30, tzinfo=UTC), "counted from now"
    assert [e.action for e in api.events("schedules.")][:2] == ["schedules.enable", "schedules.disable"]


def test_delete_removes_the_schedule_and_keeps_its_history(api: Api) -> None:
    made = create(api, **hourly_drift_body()).json()
    fired = api.client.post(f"/schedules/{made['schedule_id']}/fire", headers=api.as_("analyst"))
    assert fired.status_code == 201
    deleted = api.client.delete(f"/schedules/{made['schedule_id']}", headers=api.as_("analyst"))
    assert deleted.status_code == 204
    assert api.client.get(f"/schedules/{made['schedule_id']}", headers=api.as_("viewer")).status_code == 404
    assert api.world.store.list_firings(schedule_id=made["schedule_id"]), "the history is kept"
    (event,) = api.events("schedules.delete")
    assert event.object_id == made["schedule_id"] and event.before_hash is not None


# ---------------------------------------------------------------------------
# the scheduler backend is told about every change
# ---------------------------------------------------------------------------
class RecordingScheduler:
    """A `Scheduler` that remembers what it was told, and can be made to fail."""

    backend = "recording"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, bool | None]] = []
        self.fail = fail

    def sync(self, schedule: Schedule) -> None:
        if self.fail:
            raise ConnectionError("scheduler unreachable")
        self.calls.append(("sync", schedule.schedule_id, schedule.enabled))

    def remove(self, schedule_id: str) -> None:
        if self.fail:
            raise ConnectionError("scheduler unreachable")
        self.calls.append(("remove", schedule_id, None))

    def start(self) -> None:
        """Nothing to start."""

    def stop(self) -> None:
        """Nothing to stop."""


def test_create_change_pause_and_delete_are_pushed_to_the_scheduler(api: Api) -> None:
    recording = RecordingScheduler()
    api.app.state.scheduler = recording
    schedule_id = create(api, **hourly_drift_body()).json()["schedule_id"]
    api.client.patch(f"/schedules/{schedule_id}", json={"cadence": "weekly"}, headers=api.as_("analyst"))
    api.client.post(f"/schedules/{schedule_id}/disable", headers=api.as_("analyst"))
    api.client.delete(f"/schedules/{schedule_id}", headers=api.as_("analyst"))
    assert recording.calls == [
        ("sync", schedule_id, True),
        ("sync", schedule_id, True),
        ("sync", schedule_id, False),
        ("remove", schedule_id, None),
    ]


def test_a_scheduler_that_refuses_a_new_schedule_leaves_nothing_behind(api: Api) -> None:
    api.app.state.scheduler = RecordingScheduler(fail=True)
    response = create(api, **hourly_drift_body())
    assert response.status_code == 502 and code_of(response) == "SCHEDULER_SYNC_FAILED"
    assert api.world.store.list() == ()


def test_eventbridge_receives_the_schedule_the_api_created(api: Api) -> None:
    """The real `EventBridgeScheduler` against moto: create, pause and delete reach AWS."""
    with mock_aws():
        aws = boto3.client("scheduler", region_name="ap-south-1")
        aws.create_schedule_group(Name="marketing-ai-test")
        api.app.state.scheduler = EventBridgeScheduler(
            group_name="marketing-ai-test",
            target_arn="arn:aws:lambda:ap-south-1:123456789012:function:fire-schedule",
            role_arn="arn:aws:iam::123456789012:role/scheduler",
            region_name="ap-south-1",
            client=aws,
        )
        schedule_id = create(api, **score_body(api)).json()["schedule_id"]
        stored = aws.get_schedule(Name=schedule_id, GroupName="marketing-ai-test")
        assert stored["ScheduleExpression"] == "cron(0 2 * * ? *)"
        assert stored["ScheduleExpressionTimezone"] == "Asia/Kolkata"
        api.client.post(f"/schedules/{schedule_id}/disable", headers=api.as_("analyst"))
        assert aws.get_schedule(Name=schedule_id, GroupName="marketing-ai-test")["State"] == "DISABLED"
        assert api.client.delete(f"/schedules/{schedule_id}", headers=api.as_("analyst")).status_code == 204
        with pytest.raises(aws.exceptions.ResourceNotFoundException):
            aws.get_schedule(Name=schedule_id, GroupName="marketing-ai-test")


# ---------------------------------------------------------------------------
# firing: by the local scheduler, missed slots, and by hand
# ---------------------------------------------------------------------------
def test_the_local_scheduler_fires_a_due_schedule_through_the_app(tmp_path: Path, config_root: Path) -> None:
    api = build_api(tmp_path, config_root, scheduler_backend="local", scheduler_tick_seconds=3600)
    register_champion(api.world, training_frame(api.world))
    with TestClient(api.app, raise_server_exceptions=False) as client:
        scheduler = api.app.state.scheduler
        assert isinstance(scheduler, LocalScheduler)
        assert scheduler.running
        made = client.post("/schedules", json=score_body(api), headers=api.as_("analyst")).json()
        due = utc(made["next_due_at"])

        api.world.clock.now = due + timedelta(minutes=1)
        scheduler.tick()

        (firing,) = client.get(f"/schedules/{made['schedule_id']}/firings", headers=api.as_("viewer")).json()[
            "firings"
        ]
        assert (firing["trigger"], firing["status"], firing["result_code"]) == (
            "scheduled",
            "running",
            "SCORING_STARTED",
        )
        assert utc(firing["scheduled_for"]) == due
        run = api.world.storage.read_model(run_key(firing["run_id"], "run.json"), RunRecord)
        assert run.mode.value == "score" and run.dataset_id == firing["dataset_id"]
        assert api.world.jobs.submitted, "the run went to the app's job runner"

        # The run ends; reading the history settles the firing from the run's own record.
        update_run(api.world.storage, firing["run_id"], state=RunState.DONE, finished_at=api.world.clock())
        (settled,) = client.get(
            f"/schedules/{made['schedule_id']}/firings", headers=api.as_("viewer")
        ).json()["firings"]
        assert (settled["status"], settled["result_code"]) == ("succeeded", "SCORED")
        after = client.get(f"/schedules/{made['schedule_id']}", headers=api.as_("viewer")).json()
        assert utc(after["next_due_at"]) == due + timedelta(days=1)

    assert scheduler_threads() == [], "shutdown stops the thread"
    (event,) = api.events("schedules.fire")
    assert (event.actor_id, event.object_id) == (SYSTEM_SCHEDULER.user_id, made["schedule_id"])
    assert event.details["firing_id"] == firing["firing_id"] and event.details["trigger"] == "scheduled"


def test_missed_slots_are_listed_alerted_and_caught_up_once(api: Api) -> None:
    api.app.state.settings = api.app.state.settings.model_copy(update={"scheduler_backend": "local"})
    made = create(api, **hourly_drift_body()).json()
    scheduler = get_scheduler(app_request(api.app))
    assert isinstance(scheduler, LocalScheduler) and not scheduler.running

    api.world.clock.advance(hours=5, minutes=30)  # slots at 01:00 ... 05:00 passed with nobody ticking
    scheduler.tick()

    missed = api.client.get("/monitoring/missed-firings", headers=api.as_("viewer")).json()["firings"]
    assert len(missed) == 5
    assert {firing["schedule_id"] for firing in missed} == {made["schedule_id"]}
    assert sorted(utc(firing["scheduled_for"]).hour for firing in missed) == [1, 2, 3, 4, 5]
    assert all(firing["error_code"] == "SCHEDULE_MISSED" for firing in missed)
    own = api.client.get(
        f"/schedules/{made['schedule_id']}/firings", params={"status": "missed"}, headers=api.as_("viewer")
    ).json()["firings"]
    assert len(own) == 5
    everything = api.client.get(
        f"/schedules/{made['schedule_id']}/firings", headers=api.as_("viewer")
    ).json()["firings"]
    catch_up = [firing for firing in everything if firing["trigger"] == "catch_up"]
    assert len(catch_up) == 1 and catch_up[0]["result_code"] == "NO_CHAMPION", "one catch-up, not five"
    filtered = api.client.get(
        "/monitoring/missed-firings", params={"client_id": "cl_other"}, headers=api.as_("viewer")
    ).json()["firings"]
    assert filtered == []

    alerts = api.client.get(
        "/monitoring/alerts", params={"kind": "schedule_missed"}, headers=api.as_("viewer")
    ).json()["alerts"]
    assert len(alerts) == 1 and alerts[0]["schedule_id"] == made["schedule_id"]
    assert "missed 5 run(s)" in alerts[0]["message"]
    (event,) = api.events("schedules.missed")
    assert event.actor_id == SYSTEM_SCHEDULER.user_id and event.details["count"] == 5


def test_fire_now_records_a_manual_firing_and_exactly_one_audit_event(api: Api) -> None:
    made = create(api, **hourly_drift_body()).json()
    before = len(api.events())
    response = api.client.post(f"/schedules/{made['schedule_id']}/fire", headers=api.as_("analyst"))
    assert response.status_code == 201, response.text
    firing = response.json()
    assert (firing["trigger"], firing["status"], firing["result_code"]) == (
        "manual",
        "succeeded",
        "NO_CHAMPION",
    )
    assert firing["scheduled_for"] is None

    events = api.events()
    assert len(events) == before + 1, "the firer wrote no event of its own"
    (event,) = events[:1]
    assert (event.action, event.actor_id, event.object_type, event.object_id, event.outcome) == (
        "schedules.fire",
        api.user_ids["analyst"],
        "schedule",
        made["schedule_id"],
        "success",
    )
    assert event.details["firing_id"] == firing["firing_id"]
    assert (event.details["trigger"], event.details["schedule_kind"]) == ("manual", "drift_check")
    assert event.request_id == response.headers["x-request-id"]

    history = api.client.get(f"/schedules/{made['schedule_id']}/firings", headers=api.as_("viewer")).json()
    assert [item["firing_id"] for item in history["firings"]] == [firing["firing_id"]]
    assert api.client.get(f"/schedules/{made['schedule_id']}", headers=api.as_("viewer")).json()[
        "last_fired_at"
    ]


def test_a_paused_schedule_can_still_be_fired_by_hand(api: Api) -> None:
    made = create(api, **hourly_drift_body(enabled=False)).json()
    assert made["next_due_at"] is None
    response = api.client.post(f"/schedules/{made['schedule_id']}/fire", headers=api.as_("analyst"))
    assert response.status_code == 201 and response.json()["trigger"] == "manual"


def test_a_failed_manual_firing_is_answered_recorded_and_alerted(api: Api) -> None:
    """No champion to score with: the firing fails, the request does not."""
    made = create(api, **score_body(api)).json()
    response = api.client.post(f"/schedules/{made['schedule_id']}/fire", headers=api.as_("analyst"))
    assert response.status_code == 201, response.text
    firing = response.json()
    assert firing["status"] == "failed" and firing["error_code"]
    alerts = AlertStore(api.world.store.engine).query(AlertQuery(kind=AlertKind.SCHEDULED_JOB_FAILED))
    assert len(alerts) == 1 and alerts[0].schedule_id == made["schedule_id"]
    (event,) = api.events("schedules.fire")
    assert event.details["outcome"] == "failed" and event.details["reason_code"] == firing["error_code"]


def test_a_manual_retrain_starts_a_training_run_and_never_touches_the_champion(api: Api) -> None:
    champion = register_champion(api.world, training_frame(api.world))
    made = create(
        api, use_case_id=USE_CASE, kind="retrain", cadence="monthly", client_id=api.world.client_id
    ).json()
    firing = api.client.post(f"/schedules/{made['schedule_id']}/fire", headers=api.as_("analyst")).json()
    assert (firing["status"], firing["result_code"]) == ("running", "TRAINING_STARTED"), firing
    run = api.world.storage.read_model(run_key(firing["run_id"], "run.json"), RunRecord)
    assert run.mode.value == "train"
    current = api.world.registry.get_champion(USE_CASE)
    assert current is not None and current.model_id == champion.model_id


# ---------------------------------------------------------------------------
# managed retraining schedules
# ---------------------------------------------------------------------------
def test_retraining_sync_creates_the_managed_schedule_which_can_be_paused_not_edited(api: Api) -> None:
    first = api.client.post("/schedules/retraining/sync", headers=api.as_("analyst"))
    assert first.status_code == 200, first.text
    assert first.json()["targets"] == 1 and len(first.json()["created"]) == 1
    again = api.client.post("/schedules/retraining/sync", headers=api.as_("analyst")).json()
    assert (again["created"], again["updated"], again["removed"]) == ([], [], []), "idempotent"

    (managed,) = api.client.get("/schedules", headers=api.as_("viewer")).json()["schedules"]
    assert managed["managed_by"] == MANAGED_BY_RETRAINING and managed["kind"] == "drift_check"
    edited = api.client.patch(
        f"/schedules/{managed['schedule_id']}", json={"cadence": "daily"}, headers=api.as_("analyst")
    )
    assert edited.status_code == 409 and code_of(edited) == "SCHEDULE_MANAGED"
    assert (
        api.client.delete(f"/schedules/{managed['schedule_id']}", headers=api.as_("analyst")).status_code
        == 409
    )
    paused = api.client.post(f"/schedules/{managed['schedule_id']}/disable", headers=api.as_("analyst"))
    assert paused.status_code == 200 and paused.json()["enabled"] is False
    (event,) = api.events("schedules.retraining_sync")[-1:]
    assert event.details["count"] == 1


# ---------------------------------------------------------------------------
# the scheduler's life with the app
# ---------------------------------------------------------------------------
def test_backend_none_starts_nothing_and_touches_no_file(tmp_path: Path) -> None:
    app = local_app(tmp_path, auth_mode="off")
    assert app.state.settings.scheduler_backend == "none"
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert scheduler_threads() == []
        assert getattr(app.state, "scheduler", None) is None
    tables = set(sqlalchemy.inspect(sqlite_engine(tmp_path / PLATFORM_DB_FILENAME)).get_table_names())
    assert not tables & {"schedule", "schedule_firing", "alert"}, "startup created no scheduling table"
    # The routes still work, with a scheduler that pushes nowhere.
    assert isinstance(get_scheduler(app_request(app)), NullScheduler)


def test_backend_local_starts_the_thread_with_the_app_and_stops_it_with_the_app(
    tmp_path: Path, config_root: Path
) -> None:
    api = build_api(tmp_path, config_root, scheduler_backend="local", scheduler_tick_seconds=3600)
    with TestClient(api.app):
        assert len(scheduler_threads()) == 1
        managed = api.world.store.list(managed_by=MANAGED_BY_RETRAINING)
        assert len(managed) == 1, "startup synced monitoring.retraining"
    assert scheduler_threads() == []


def test_a_scheduler_that_cannot_be_built_does_not_stop_the_api(tmp_path: Path) -> None:
    app = local_app(tmp_path, auth_mode="off")
    # eventbridge without its ARNs: the settings object is built by hand, so nothing refused it earlier.
    app.state.settings = app.state.settings.model_copy(update={"scheduler_backend": "eventbridge"})
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert getattr(app.state, "scheduler", None) is None


# ---------------------------------------------------------------------------
# who may do what (DEC-780)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("who", ["viewer", "approver", "admin"])
def test_only_an_analyst_changes_or_fires_a_schedule(api: Api, who: str) -> None:
    made = create(api, **hourly_drift_body()).json()
    schedule_id = made["schedule_id"]
    for method, path, body in [
        ("POST", "/schedules", hourly_drift_body()),
        ("PATCH", f"/schedules/{schedule_id}", {"enabled": False}),
        ("POST", f"/schedules/{schedule_id}/disable", None),
        ("POST", f"/schedules/{schedule_id}/fire", None),
        ("DELETE", f"/schedules/{schedule_id}", None),
        ("POST", "/schedules/retraining/sync", None),
    ]:
        response = api.client.request(method, path, json=body, headers=api.as_(who))
        assert response.status_code == 403, (method, path, response.text)
        assert code_of(response) == "ROLE_REQUIRED"
        assert response.json()["detail"]["message"].startswith("Only an Analyst can ")
    assert api.client.get("/schedules", headers=api.as_(who)).status_code == 200, "reading is Viewer"
    assert api.world.store.list_firings(schedule_id=schedule_id) == ()


def test_the_refusal_names_the_action(api: Api) -> None:
    response = api.client.post("/schedules/sch_x/fire", headers=api.as_("viewer"))
    assert response.json()["detail"]["message"] == "Only an Analyst can run a schedule now."
    denied = api.events("schedules.fire")[0]
    assert denied.outcome == "denied" and denied.actor_id == api.user_ids["viewer"]


def test_without_a_sign_in_nothing_answers(api: Api) -> None:
    for method, path in [("GET", "/schedules"), ("POST", "/schedules"), ("GET", "/monitoring/alerts")]:
        response = api.client.request(method, path, json={} if method == "POST" else None)
        assert response.status_code == 401, (method, path)
