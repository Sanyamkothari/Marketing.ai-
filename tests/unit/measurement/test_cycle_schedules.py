"""Plan J M107 (DEC-1317): the loop's schedule kinds, their parameters and their alerts; defaults unchanged.

Three kinds join `score`, `drift_check` and `retrain`: `treat_list`, `measure` and `learn`. A `measure`
schedule names where the outcomes are read from (a saved connection, a table or file, and the column that
dates each row); the other two need nothing. Every existing schedule, source and alert is stored exactly
as before: the new fields are left out of a document while unset.

The firings here run on `tests/unit/production/scheduling_support.py`'s world (a real client store, a fake
clock, nothing trained); the whole loop, for real, is `tests/integration/measurement/test_monthly_loop.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.connections.store import ConnectionStore
from engine.measurement.campaign import InMemoryCampaignStore
from engine.measurement.cycle import CYCLE_CODES, CYCLE_SERVICES_MISSING, LEARN_NOT_READY, CycleServices
from engine.measurement.pull import PULL_CODES, OutcomePullSpec, PullSelection
from engine.onboarding.specs import SourceSpec
from engine.pilot.plain import jargon_in
from engine.scheduling.alerts import SEVERITY_FOR, AlertKind, AlertQuery, new_alert, sns_subject
from engine.scheduling.firing import ScheduleFirer
from engine.scheduling.schedules import FiringStatus, Schedule, ScheduleKind, ScheduleParameters
from engine.settings import Settings
from tests.unit.production.scheduling_support import World, make_world

NOW = datetime(2026, 9, 1, tzinfo=UTC)
PULL = OutcomePullSpec(
    connection_id="c_0123456789ab",
    selection=PullSelection(schema_name="crm", table="outcomes"),
    date_column="recorded_on",
)


def _schedule(kind: ScheduleKind, **parameters: object) -> Schedule:
    return Schedule(
        schedule_id=f"sch_{kind.value}",
        client_id="cl_1",
        use_case_id="telco-churn",
        kind=kind,
        cron="0 7 * * *",
        created_by="u_test",
        created_at=NOW,
        updated_at=NOW,
        parameters=ScheduleParameters(**parameters),  # type: ignore[arg-type]
    )


def test_the_three_new_kinds_exist_beside_the_old_ones() -> None:
    assert [kind.value for kind in ScheduleKind] == [
        "score",
        "drift_check",
        "retrain",
        "treat_list",
        "measure",
        "learn",
    ]


def test_a_measure_schedule_names_where_its_outcomes_come_from() -> None:
    assert _schedule(ScheduleKind.MEASURE, outcomes=PULL).parameters.outcomes == PULL
    with pytest.raises(ValidationError, match="outcomes"):
        _schedule(ScheduleKind.MEASURE)
    for kind in (ScheduleKind.SCORE, ScheduleKind.TREAT_LIST, ScheduleKind.LEARN, ScheduleKind.RETRAIN):
        with pytest.raises(ValidationError):
            _schedule(kind, outcomes=PULL, onboarding_spec_id="sp_1")
    for kind in (ScheduleKind.TREAT_LIST, ScheduleKind.MEASURE, ScheduleKind.LEARN):
        with pytest.raises(ValidationError):
            _schedule(kind, onboarding_spec_id="sp_1", outcomes=PULL)
        with pytest.raises(ValidationError):
            _schedule(kind, dataset_id="ds_1", outcomes=PULL)
    assert _schedule(ScheduleKind.TREAT_LIST).parameters == ScheduleParameters()
    assert _schedule(ScheduleKind.LEARN).parameters == ScheduleParameters()


def test_an_outcomes_pull_is_a_table_or_a_file_never_free_text() -> None:
    with pytest.raises(ValidationError):
        OutcomePullSpec.model_validate({**PULL.model_dump(), "query": "SELECT * FROM crm.outcomes"})
    with pytest.raises(ValidationError):
        OutcomePullSpec.model_validate({**PULL.model_dump(), "date_column": ""})


def test_default_documents_are_byte_identical() -> None:
    """A schedule, a source and a firing written today are stored exactly as before M107."""
    assert ScheduleParameters().model_dump_json() == (
        '{"onboarding_spec_id":null,"dataset_id":null,"model_version_id":null}'
    )
    assert ScheduleParameters(onboarding_spec_id="sp_1").model_dump_json() == (
        '{"onboarding_spec_id":"sp_1","dataset_id":null,"model_version_id":null}'
    )
    source = SourceSpec.model_validate(
        {
            "source_id": "s_1",
            "client_id": "cl_1",
            "file_name": "customers.csv",
            "storage_key": "clients/cl_1/sources/s_1/raw.csv",
            "file_format": "csv",
            "rows": 3,
            "columns": ["a"],
            "fingerprint": {"hash": "0" * 64, "algorithm": "sha256", "n_rows": 3, "columns": ["a"]},
            "created_at": "2026-09-01T00:00:00Z",
        }
    )
    assert "binding" not in source.model_dump_json()


def test_the_loop_alerts_are_information_and_say_what_happened() -> None:
    for kind in (AlertKind.TREAT_LIST_READY, AlertKind.CAMPAIGN_MEASURED, AlertKind.CHALLENGER_WAITING):
        assert SEVERITY_FOR[kind] == "info"
        alert = new_alert(kind, use_case_id="telco-churn", message="Something happened.", now=NOW)
        assert sns_subject(alert).startswith("[info] ")
    assert {kind.value for kind in AlertKind} >= {
        "drift_above_threshold",
        "performance_drop",
        "scheduled_job_failed",
        "schedule_missed",
    }


def test_the_codes_are_named_once() -> None:
    assert {CYCLE_SERVICES_MISSING, LEARN_NOT_READY} <= CYCLE_CODES
    assert not CYCLE_CODES & PULL_CODES


# --- firings of the new kinds ---------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path: Path, config_root: Path) -> World:
    return make_world(tmp_path, config_root)


def test_a_loop_kind_without_its_services_fails_with_a_plain_reason(world: World) -> None:
    firer = ScheduleFirer(world.services())
    for kind in (ScheduleKind.TREAT_LIST, ScheduleKind.LEARN):
        firing = firer.fire(world.schedule(kind)) or pytest.fail("not fired")
        assert (firing.status, firing.error_code) == (FiringStatus.FAILED, CYCLE_SERVICES_MISSING)
    failed = world.alerts.store.query(AlertQuery(kind=AlertKind.SCHEDULED_JOB_FAILED))  # type: ignore[attr-defined]
    assert len(failed) == 2
    assert all(jargon_in(alert.message) == () for alert in failed)


def test_nothing_new_is_a_quiet_success(world: World) -> None:
    connections = ConnectionStore(world.storage, Settings())
    firer = ScheduleFirer(
        world.services(cycle=CycleServices(campaigns=InMemoryCampaignStore(), connections=connections))
    )
    treat = firer.fire(world.schedule(ScheduleKind.TREAT_LIST)) or pytest.fail("not fired")
    assert (treat.status, treat.result_code) == (FiringStatus.SUCCEEDED, "NOTHING_NEW")
    learn = firer.fire(world.schedule(ScheduleKind.LEARN)) or pytest.fail("not fired")
    assert (learn.status, learn.result_code) == (FiringStatus.SUCCEEDED, "NOTHING_TO_LEARN")
    measure = firer.fire(world.schedule(ScheduleKind.MEASURE, parameters=ScheduleParameters(outcomes=PULL)))
    assert measure is not None
    assert (measure.status, measure.result_code) == (FiringStatus.SUCCEEDED, "NOTHING_TO_MEASURE")
    store = world.alerts.store  # type: ignore[attr-defined]
    assert store.query(AlertQuery()) == ()


def test_a_loop_schedule_needs_a_use_case_that_contacts_and_holds_back(world: World) -> None:
    from engine.scheduling.scheduler import NullScheduler
    from engine.scheduling.schedules import ScheduleError
    from engine.scheduling.service import create_schedule

    for kind in (ScheduleKind.TREAT_LIST, ScheduleKind.LEARN):
        with pytest.raises(ScheduleError) as caught:
            create_schedule(
                world.store,
                NullScheduler(),
                config_root=world.config_root,
                client_id=world.client_id,
                use_case_id="fault-prediction",
                kind=kind,
                cadence="daily",
                created_by="u_test",
            )
        assert caught.value.code == "SCHEDULE_INVALID" and jargon_in(caught.value.message) == ()
    made = create_schedule(
        world.store,
        NullScheduler(),
        config_root=world.config_root,
        client_id=world.client_id,
        use_case_id="telco-churn",
        kind=ScheduleKind.TREAT_LIST,
        cadence="daily",
        created_by="u_test",
    )
    assert made.kind is ScheduleKind.TREAT_LIST and made.parameters == ScheduleParameters()
