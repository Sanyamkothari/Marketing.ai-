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


# --- review fixes --------------------------------------------------------------------------------------
def _capped_unpriced_root(config_root: Path, target: Path) -> Path:
    """A copy of `configs/` with a run cost cap and no price list: every billed run needs a person's yes."""
    import shutil

    from engine.aws.prices import PRICES_FILENAME

    shutil.copytree(config_root, target)
    path = target / "engine.yaml"
    text = path.read_text(encoding="utf-8")
    assert "max_run_cost_usd: null" in text
    path.write_text(text.replace("max_run_cost_usd: null", "max_run_cost_usd: 5.0", 1), encoding="utf-8")
    (target / PRICES_FILENAME).unlink()
    return target


def test_a_capped_deployment_with_nothing_to_learn_is_a_quiet_success(
    tmp_path: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix: the cost gate is met only by a run about to start, not by every learn firing.

    On a capped SageMaker deployment whose runs cannot be priced, a learn firing with nothing measured used to
    fail with RUN_COST_NEEDS_CONFIRMATION and a critical alert every day although no run would have started.
    (The gate refusing a campaign that would really be learned from is in
    `tests/integration/measurement/test_connection_pulls_api.py`.)
    """
    from tests.fixtures.settings import sagemaker_settings

    monkeypatch.delenv("MARKETING_AI_CONFIG_DIR", raising=False)
    world = make_world(tmp_path / "w", _capped_unpriced_root(config_root, tmp_path / "configs"))
    firer = ScheduleFirer(
        world.services(settings=sagemaker_settings(), cycle=CycleServices(campaigns=InMemoryCampaignStore()))
    )
    learn = firer.fire(world.schedule(ScheduleKind.LEARN)) or pytest.fail("not fired")
    assert (learn.status, learn.result_code, learn.error_code) == (
        FiringStatus.SUCCEEDED,
        "NOTHING_TO_LEARN",
        None,
    )
    assert world.alerts.store.query(AlertQuery()) == ()  # type: ignore[attr-defined]


@pytest.mark.parametrize("kind", [ScheduleKind.LEARN, ScheduleKind.RETRAIN])
def test_a_learning_run_that_registers_no_model_says_so(world: World, kind: ScheduleKind) -> None:
    """Review fix: a learning run that ends without a model raises an alert; a retrain is as before (none)."""
    from engine.contracts import RunState
    from engine.runs import update_run

    firer = ScheduleFirer(world.services())
    started = firer.fire(world.schedule(ScheduleKind.RETRAIN)) or pytest.fail("not fired")
    assert started.run_id is not None
    world.store.save_firing(started.model_copy(update={"kind": kind}))  # as the step that started it
    update_run(world.storage, started.run_id, state=RunState.DONE)  # finished, and nothing registered
    (settled,) = firer.settle()
    assert (settled.status, settled.result_code) == (FiringStatus.SUCCEEDED, "MODEL_NOT_REGISTERED")
    alerts = world.alerts.store.query(AlertQuery())  # type: ignore[attr-defined]
    if kind is ScheduleKind.RETRAIN:
        assert alerts == ()
        return
    (alert,) = alerts
    assert alert.kind is AlertKind.SCHEDULED_JOB_FAILED and alert.run_id == started.run_id
    assert "no new model was registered" in alert.message and jargon_in(alert.message) == ()


def test_the_resolved_connection_never_prints_its_secrets(world: World) -> None:
    """Review fix: `Resolved` carries the decrypted keys to the connector only; its repr leaves them out."""
    from engine.measurement.pull import resolve

    connections = ConnectionStore(world.storage, Settings())
    record = connections.create(
        kind="s3",
        name="Client exports",
        config={"bucket": "client-exports", "region": "us-east-1", "prefix": "exports/"},
        secrets={"access_key_id": "AKIA-REPR-CHECK", "secret_access_key": "very-secret-value-0123"},
    )
    resolved = resolve(connections, record.connection_id, PullSelection(path="exports/a.csv"))
    assert resolved.secrets["secret_access_key"] == "very-secret-value-0123"
    shown = repr(resolved)
    assert "very-secret-value-0123" not in shown and "AKIA-REPR-CHECK" not in shown
