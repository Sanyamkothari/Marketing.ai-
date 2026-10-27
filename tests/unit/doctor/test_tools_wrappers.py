"""The remaining wrapper tools (Plan O M236, DEC-1806; the plan's §M236, the design §5.5).

Acceptance, as the plan lists it:

* each wrapper's output equals the wrapped function's on fixtures;
* `propose_role` and `propose_mapping` land pending, `check`, never ticked - and the policy refuses either offered
  ticked (`POLICY_NEVER_TICKED`);
* the injection canary passes for every new tool (`tests/unit/doctor/test_registry_canary.py`, extended) and no
  planted value reaches the AI service's prompt (`second_opinion`, `explain`);
* `explain` is `tests/unit/doctor/test_explain.py`.

And the wave-1 requests to M236: `plan_build` reads each file's role from `propose_role`, and M240's
`read_workbook_sheets` and `find_excel_serial_dates` have catalogue rows and are registered.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.advisor import advise
from engine.agent.assist import assist_session
from engine.agent.contracts import SessionStatus
from engine.agent.egress import cap_sent
from engine.agent.session import start_session
from engine.agent.tools import AgentContext
from engine.connections import limits
from engine.connections import registry as connectors
from engine.connections.base import Selection, frame_preview
from engine.connections.store import ConnectionStore
from engine.doctor import registry as registry_module
from engine.doctor.build_driver import DiagnosisRoles, plan_build
from engine.doctor.catalogue import CATALOGUE, row
from engine.doctor.contracts import Diagnosis
from engine.doctor.diagnose import LoadedFile, diagnose_file
from engine.doctor.joins import how_files_fit
from engine.doctor.registry import DataToolContext, DataToolError, DataToolKind, Exposure, data_registry
from engine.doctor.tools_wave_one import UNCATALOGUED
from engine.doctor.tools_wrappers import (
    M236_TOOLS,
    PREFLIGHT_FOLDER,
    ProposedRoles,
    preflight_file_name,
    whole_marker,
)
from engine.intake.config import IntakeConfig
from engine.intake.run import intake_file
from engine.onboarding.mapping import suggested_mapping_spec
from engine.onboarding.sources import profile_source
from engine.pii import detect_pii, free_text_pii
from engine.pilot.preflight import run_preflight
from engine.run_checks import things_checked
from engine.settings import Settings
from engine.stages import ingest
from engine.stages.validate import check_leakage_suspected, params_from_config
from engine.storage import LocalStorage, upload_key
from engine.uplift.checks import run_uplift_checks
from engine.utils.ids import seed_from
from tests.unit.agent.assist_client import AssistClient
from tests.unit.agent.egress_canary import guardrails, meter_for
from tests.unit.agent.helpers import context_for, header_help_root

ANALYST = Principal(user_id="u-asha", username="asha", roles=frozenset({Role.ANALYST}), kind="user")
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def _customers(rows: int = 400) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"CUST-{i:05d}" for i in range(rows)],
            "signup_date": [f"2024-{1 + i % 12:02d}-{1 + i % 27:02d}" for i in range(rows)],
            "plan": [("Gold", "Silver", "Bronze")[i % 3] for i in range(rows)],
            "monthly_spend": [float(100 + (i * 7) % 900) for i in range(rows)],
            "preferred_channel": [
                ("sms", "none", "email")[i % 3] if i % 4 == 0 else "none" for i in range(rows)
            ],
            "converted_30d": [1 if i % 4 == 0 else 0 for i in range(rows)],
        }
    )


def _bills(rows: int = 900) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"CUST-{(i * 7) % 400:05d}" for i in range(rows)],
            "bill_date": [f"2025-{1 + i % 12:02d}-{1 + i % 27:02d}" for i in range(rows)],
            "amount": [round(100 + (i * 13) % 700 + 0.5, 2) for i in range(rows)],
        }
    )


def _loaded(ctx: AgentContext, frame: pd.DataFrame, upload_id: str, file_name: str) -> LoadedFile:
    profile = ingest.profile_dataset(
        frame,
        ctx.config,
        upload_id=upload_id,
        file_name=file_name,
        file_format="csv",
        file_size_bytes=1,
        delimiter=",",
        encoding="utf-8",
    )
    return LoadedFile(upload_id=upload_id, file_name=file_name, frame=frame, profile=profile)


@pytest.fixture
def agent() -> AgentContext:
    return context_for(_customers())


@pytest.fixture
def case(agent: AgentContext, tmp_path: Path) -> DataToolContext:
    files = (
        _loaded(agent, agent.frame, "up-customers", "customers.csv"),
        _loaded(agent, _bills(), "up-bills", "bills.csv"),
    )
    return DataToolContext(
        agent=agent,
        principal=ANALYST,
        caller="person",
        storage=LocalStorage(tmp_path / "store"),
        files=files,
        clock=lambda: NOW,
    )


def _call(name: str, given: dict[str, Any], ctx: DataToolContext) -> dict[str, Any]:
    output: dict[str, Any] = data_registry().call(name, given, ctx).output
    return output


def _dumped(models: Any) -> list[dict[str, Any]]:
    return [model.model_dump(mode="json") for model in models]


# ---------------------------------------------------------------------------
# The registry rows
# ---------------------------------------------------------------------------
def test_every_m236_tool_is_registered_by_its_catalogue_row() -> None:
    registry = data_registry()
    for name in M236_TOOLS:
        tool = registry.get(name)
        entry = row(name)
        assert entry is not None and entry.number == tool.number, name
        assert entry.kind == tool.kind.value and entry.exposure == tool.exposure.value, name
        assert tool.callers == frozenset({"rules", "person"}), name
        assert tool.route_policy.role is Role.ANALYST, name
        if tool.kind is DataToolKind.PROPOSE:
            assert tool.policy_kind == f"data.{name}", name
    wrappers = {entry.name for entry in CATALOGUE if entry.status == "wrapper"}
    # The plan's §M236 names fourteen wrappers; four catalogue wrappers are in no milestone's list yet.
    assert wrappers - set(registry.names()) == {
        "read_file",
        "list_sheets",
        "read_headers",
        "propose_renamed_copy",
    }


def test_every_proposing_m236_tool_has_a_rule_set_and_an_output_reader() -> None:
    from engine.doctor.policy import OUTPUT_READERS, load_data_policy

    rule_sets = load_data_policy(None).rule_sets
    for name in M236_TOOLS:
        kind = data_registry().get(name).proposal_kind
        if kind is not None:
            assert kind in rule_sets and kind in OUTPUT_READERS, name


def test_m240s_two_tools_have_catalogue_rows_and_are_registered() -> None:
    assert not UNCATALOGUED
    for name, number in (("read_workbook_sheets", 82), ("find_excel_serial_dates", 83)):
        entry = row(name)
        assert entry is not None and entry.number == number and entry.kind == "read", name
        tool = data_registry().get(name)
        assert tool.number == number and tool.exposure is Exposure.SESSION_ONLY
    serial = data_registry().get("find_excel_serial_dates")
    assert (
        serial.proposal_kind == "data.excel_serial_dates"
    ), "its parse_date steps are decided by M233's rules"
    assert data_registry().get("read_workbook_sheets").proposal_kind is None


# ---------------------------------------------------------------------------
# #41 propose_role, and the build plan reading it
# ---------------------------------------------------------------------------
def test_propose_role_equals_detect_roles_and_the_diagnosis(case: DataToolContext) -> None:
    for loaded in case.files:
        out = _call("propose_role", {"upload_id": loaded.upload_id}, case)
        source = profile_source(
            loaded.frame,
            loaded_config(case),
            source_id=loaded.upload_id,
            client_id="data-doctor",
            file_name=loaded.file_name,
            row_count=loaded.profile.row_count,
        )
        assert out["candidates"] == _dumped(source.role_candidates)
        from engine.config import get_roles

        diagnosed = diagnose_file(loaded, loaded_config(case), get_roles())
        assert out["role"] == diagnosed.kind
        assert out["reason"] == diagnosed.kind_reason


def loaded_config(case: DataToolContext) -> Any:
    assert case.agent is not None
    return case.agent.config


def test_propose_role_and_propose_mapping_land_pending_check_never_ticked(case: DataToolContext) -> None:
    for name, given in (
        ("propose_role", {"upload_id": "up-bills"}),
        ("propose_mapping", {"upload_id": "up-bills"}),
    ):
        out = _call(name, given, case)
        assert (out["state"], out["confidence"], out["ticked"], out["source"]) == (
            "pending",
            "check",
            False,
            "rules",
        ), name


@pytest.mark.parametrize("name", ["propose_role", "propose_mapping"])
def test_the_policy_refuses_a_role_or_mapping_offered_ticked(name: str, case: DataToolContext) -> None:
    """A rules proposal that is not `sure` never starts ticked (`data.never_ticked`, tick_only_sure)."""
    tool = data_registry().get(name)
    original = tool.run

    def ticked(parsed: Any, ctx: DataToolContext) -> Any:
        return original(parsed, ctx).model_copy(update={"ticked": True})

    registry = registry_module.DataToolRegistry()
    registry.register(dataclasses.replace(tool, run=ticked))
    with pytest.raises(DataToolError) as caught:
        registry.call(name, {"upload_id": "up-bills"}, case)
    assert caught.value.code == "POLICY_NEVER_TICKED"


def test_the_build_plan_reads_each_files_role_from_propose_role(case: DataToolContext) -> None:
    """The wave-1 request: `plan_build` takes M236's `propose_role` (through the registry), and the plan is the
    one the diagnosis's roles give, because `propose_role` is the diagnosis's rule."""
    from engine.config import get_roles

    roles = get_roles()
    files = list(case.files)
    diagnosed = tuple(diagnose_file(f, loaded_config(case), roles) for f in files)
    diagnosis = how_files_fit(Diagnosis(files=diagnosed, use_cases=()), files, roles)
    frames = {f.upload_id: f.frame for f in files}
    asked: list[str] = []

    class Spy(ProposedRoles):
        def propose_role(self, upload_id: str) -> Any:
            asked.append(upload_id)
            return super().propose_role(upload_id)

    through_tool = plan_build(
        diagnosis, frames, use_case="targeted-advertisement", label_spec=None, roles=Spy(case)
    )
    by_diagnosis = plan_build(
        diagnosis, frames, use_case="targeted-advertisement", label_spec=None, roles=DiagnosisRoles(diagnosis)
    )
    assert sorted(asked) == ["up-bills", "up-customers"]
    assert through_tool.model_dump(mode="json") == by_diagnosis.model_dump(mode="json")


def test_build_dataset_asks_propose_role_through_the_registry(
    case: DataToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    original = ProposedRoles.propose_role

    def spy(self: ProposedRoles, upload_id: str) -> Any:
        asked.append(upload_id)
        return original(self, upload_id)

    monkeypatch.setattr(ProposedRoles, "propose_role", spy)
    out = _call(
        "build_dataset",
        {"use_case": "targeted-advertisement", "upload_ids": ["up-customers", "up-bills"]},
        case,
    )
    assert sorted(asked) == ["up-bills", "up-customers"]
    assert out["entity"]["upload_ids"] == ["up-customers"]


# ---------------------------------------------------------------------------
# #42 propose_key, #43 propose_mapping
# ---------------------------------------------------------------------------
def test_propose_key_is_the_rules_advisors_id_column(case: DataToolContext, agent: AgentContext) -> None:
    out = _call("propose_key", {}, case)
    advice = advise(agent)
    assert out["column"] == advice.primary_key == "customer_id"
    assert out["stop_reason"] is None
    assert out["unique_columns"] == list(agent.profile.primary_key_candidates[:10])
    assert out["state"] == "pending" and (out["ticked"] is False or out["confidence"] == "sure")


def test_propose_key_says_why_when_no_column_can_be_the_id(tmp_path: Path) -> None:
    frame = pd.DataFrame({"city": ["Pune", "Pune", "Delhi"] * 50, "spend": [1.0, 2.0, 3.0] * 50})
    agent = context_for(frame)
    ctx = DataToolContext(agent=agent, principal=ANALYST, caller="person")
    out = _call("propose_key", {}, ctx)
    advice = advise(agent)
    assert out["column"] is None and out["stop_reason"] == advice.stop_reason and out["stop_reason"]


def test_propose_mapping_equals_the_suggested_mapping(case: DataToolContext) -> None:
    loaded = case.files[1]
    out = _call("propose_mapping", {"upload_id": "up-bills"}, case)
    spec = suggested_mapping_spec(
        profile_source(
            loaded.frame,
            loaded_config(case),
            source_id=loaded.upload_id,
            client_id="data-doctor",
            file_name=loaded.file_name,
            row_count=loaded.profile.row_count,
        ),
        loaded_config(case),
        role=out["role"],
        use_case="targeted-advertisement",
        mapping_id="any",
    )
    assert out["columns"] == _dumped(spec.columns)
    assert out["unmapped_source"] == list(spec.unmapped_source)
    assert out["missing_required"] == list(spec.missing_required)
    assert out["role"] == _call("propose_role", {"upload_id": "up-bills"}, case)["role"]


def test_propose_mapping_refuses_a_role_that_does_not_exist(case: DataToolContext) -> None:
    with pytest.raises(DataToolError) as caught:
        _call("propose_mapping", {"upload_id": "up-bills", "role": "spaceships"}, case)
    assert caught.value.code == "TOOL_INPUT_INVALID"


# ---------------------------------------------------------------------------
# #3 propose_intake
# ---------------------------------------------------------------------------
MESSY = b"Customer report,,\nid,plan,spend\nC1,Gold,10\nC2,Silver,-\nC3,Gold,30\nGrand Total,,40\n"


def test_propose_intake_freezes_what_intake_decided(case: DataToolContext) -> None:
    storage = case.storage
    assert storage is not None
    storage.write_bytes(upload_key("up-customers", "source.csv"), MESSY)
    result = intake_file(
        storage,
        received_key=upload_key("up-customers", "source.csv"),
        received_format="csv",
        csv_key=upload_key("up-customers", "source.csv"),
        original_key=upload_key("up-customers", "original.csv"),
        config=IntakeConfig(),
    )
    assert result.worth_keeping
    storage.write_model(upload_key("up-customers", "intake.json"), result.report)
    out = _call("propose_intake", {"upload_id": "up-customers"}, case)
    report = result.report
    assert out["rewritten"] is True and out["decisions"] is not None
    for name in ("header_row", "rows_above_header", "footer_rows", "delimiter", "encoding", "sheet"):
        assert out["decisions"][name] == getattr(report, name), name
    assert out["state"] == "pending" and out["ticked"] is False
    untouched = _call("propose_intake", {"upload_id": "up-bills"}, case)
    assert untouched["rewritten"] is False and untouched["decisions"] is None


# ---------------------------------------------------------------------------
# #6 preview_connection, #7 propose_import
# ---------------------------------------------------------------------------
class _FakeConnector:
    """A saved connection's connector that reads a frame instead of a server."""

    kind = "postgres"

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def available(self) -> bool:
        return True

    def info(self) -> Any:
        return connectors.CONNECTORS["postgres"].info()

    def preview(self, config: Any, secrets: Any, selection: Selection) -> Any:
        return frame_preview(self.frame)

    def file_format(self, selection: Selection) -> str:
        return "csv"


@pytest.fixture
def connected(
    case: DataToolContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DataToolContext, str, Any]:
    store = ConnectionStore(LocalStorage(tmp_path / "conn"), Settings())
    record = store.create(
        kind="postgres",
        name="CRM",
        config={"host": "db.example.com", "database": "crm", "user": "r"},
        secrets={},
    )
    fake = _FakeConnector(pd.DataFrame({"id": ["C1", "C2"], "email": ["a@example.test", "b@example.test"]}))
    monkeypatch.setattr(connectors, "connector", lambda kind: fake)
    return dataclasses.replace(case, connections=store), record.connection_id, fake


def test_preview_connection_is_the_connectors_masked_preview(
    connected: tuple[DataToolContext, str, Any],
) -> None:
    ctx, connection_id, fake = connected
    out = _call(
        "preview_connection", {"connection_id": connection_id, "schema_name": "public", "table": "t"}, ctx
    )
    expected = fake.preview({}, {}, Selection(schema_name="public", table="t")).model_dump(mode="json")
    assert {k: out[k] for k in ("columns", "rows", "note")} == expected
    assert out["rows_total"] == len(expected["rows"])
    assert "a@example.test" not in json.dumps(out)


def test_a_preview_cell_with_masked_personal_data_is_shown_as_its_marker_whole() -> None:
    assert whole_marker("27[REDACTED:pan]1ZV") == "[REDACTED:pan]"
    assert whole_marker("call [REDACTED:phone] or [REDACTED:email]") == "[REDACTED:phone] [REDACTED:email]"
    assert whole_marker("Gold") == "Gold"


def test_propose_import_is_pending_and_reads_only_the_preview(
    connected: tuple[DataToolContext, str, Any],
) -> None:
    ctx, connection_id, fake = connected
    out = _call("propose_import", {"connection_id": connection_id, "path": "exports/may.csv"}, ctx)
    assert out["columns"] == ["id", "email"] and out["file_format"] == fake.file_format(Selection())
    assert (out["state"], out["confidence"], out["ticked"]) == ("pending", "check", False)
    assert "exports/may.csv" in out["summary"]


def test_an_unknown_connection_is_the_routes_refusal(connected: tuple[DataToolContext, str, Any]) -> None:
    ctx, _, _ = connected
    with pytest.raises(DataToolError) as caught:
        _call("preview_connection", {"connection_id": "c_000000000000"}, ctx)
    assert caught.value.code == "CONNECTION_NOT_FOUND" and caught.value.status == 404


@pytest.mark.parametrize("name", ["preview_connection", "propose_import"])
def test_the_connection_tools_share_the_routes_cap_on_calls_at_once(
    connected: tuple[DataToolContext, str, Any], monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """At most four calls to clients' systems at once, whoever asks: with the connection routes' gate full,
    the tool is `429 CONNECTION_BUSY` straight away and the connector is never called."""
    ctx, connection_id, fake = connected
    asked: list[Selection] = []
    monkeypatch.setattr(fake, "preview", lambda _c, _s, selection: asked.append(selection))
    taken = [limits.CALLS.acquire(blocking=False) for _ in range(limits.MAX_CONCURRENT_CALLS)]
    try:
        assert all(taken)
        with pytest.raises(DataToolError) as caught:
            _call(name, {"connection_id": connection_id, "path": "exports/may.csv"}, ctx)
    finally:
        for _ in taken:
            limits.CALLS.release()
    assert (caught.value.code, caught.value.status) == ("CONNECTION_BUSY", 429)
    assert asked == []


def test_the_connection_routes_and_the_tools_hold_one_gate() -> None:
    from api.routes import connections as routes

    assert routes._CALLS is limits.CALLS and routes._IMPORTS is limits.IMPORTS


def test_the_connection_tools_need_the_saved_connections(case: DataToolContext) -> None:
    assert data_registry().find("preview_connection", case) is None
    assert data_registry().find("propose_import", case) is None


# ---------------------------------------------------------------------------
# #8 check_refresh
# ---------------------------------------------------------------------------
def test_check_refresh_is_latest_recipe_inputs(tmp_path: Path, config_root: Path) -> None:
    from engine.scheduling.firing import latest_recipe_inputs
    from tests.unit.production.scheduling_support import make_world, tables

    world = make_world(tmp_path, config_root)
    later = datetime(2026, 7, 2, tzinfo=UTC)
    world.add_source("activity_july", tables(seed=7)["activity"], role="activity", created_at=later)
    ctx = DataToolContext(principal=ANALYST, caller="person", client_store=world.client_store)
    out = _call("check_refresh", {"spec_id": world.spec.spec_id}, ctx)
    inputs = latest_recipe_inputs(world.client_store, world.spec)
    assert [t["source_id"] for t in out["tables"]] == [s.source_id for s in inputs.sources]
    assert out["newer_total"] == len(inputs.rebound) == 1
    assert [t["newer"] for t in out["tables"]] == [False, True]
    assert out["bound_total"] == 0, "uploaded files: a refresh reads no connection"


def test_check_refresh_refuses_an_unknown_recipe(tmp_path: Path, config_root: Path) -> None:
    from tests.unit.production.scheduling_support import make_world

    world = make_world(tmp_path, config_root)
    ctx = DataToolContext(principal=ANALYST, caller="person", client_store=world.client_store)
    with pytest.raises(DataToolError) as caught:
        _call("check_refresh", {"spec_id": "nope"}, ctx)
    assert caught.value.status == 404


# ---------------------------------------------------------------------------
# #9 preflight
# ---------------------------------------------------------------------------
def test_preflight_is_the_preflight_checker_on_the_case_files(case: DataToolContext, tmp_path: Path) -> None:
    out = _call("preflight", {}, case)
    folder = tmp_path / "pf"
    folder.mkdir()
    for loaded, name in zip(case.files, ("customers.csv", "bills.csv"), strict=True):
        loaded.frame.to_csv(folder / name, index=False)
    expected = run_preflight([folder], None, now=NOW).model_dump(mode="json")
    expected["folder"] = PREFLIGHT_FOLDER
    assert out["result"] == expected
    assert out["verdict"] == run_preflight([folder], None, now=NOW).verdict
    assert str(tmp_path) not in json.dumps(out), "no server path"


def test_preflight_names_each_file_by_the_words_that_name_a_table() -> None:
    vocabulary = frozenset({"customers", "customer", "bills", "bill"})
    taken: set[str] = set()
    assert (
        preflight_file_name("customers of Jane Roe jane.roe@example.test.csv", vocabulary, taken)
        == "customers.csv"
    )
    assert preflight_file_name("Bills May 2025.xlsx", vocabulary, taken) == "bills.csv"
    assert preflight_file_name("bills june.csv", vocabulary, taken) == "bills-2.csv"
    assert preflight_file_name("Ravi's export.csv", vocabulary, taken) == "file.csv"


# ---------------------------------------------------------------------------
# #31 second_opinion
# ---------------------------------------------------------------------------
def test_second_opinion_is_assist_session_and_ticks_nothing() -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(600)],
            "churn_flg": [1 if i % 4 == 0 else 0 for i in range(600)],
            "final_status": [("open", "closed", "pending")[i % 3] for i in range(600)],
            "monthly_spend": [float(10 + i % 90) for i in range(600)],
        }
    )
    agent = context_for(frame, target="churn_flg", config_root=header_help_root())
    session = start_session(agent, session_id="s-1")
    scripted = [{"column": "final_status", "why": "It may be set when the customer leaves."}]
    saved: list[Any] = []
    ctx = DataToolContext(
        agent=agent,
        principal=ANALYST,
        caller="person",
        session=session,
        save_session=saved.append,
        meter=meter_for(AssistClient(columns=scripted)),
        guardrails=guardrails(),
        clock=lambda: NOW,
    )
    out = _call("second_opinion", {}, ctx)
    direct = assist_session(
        agent,
        session,
        meter=meter_for(AssistClient(columns=scripted)),
        guardrails=guardrails(),
        clock=lambda: NOW,
    )
    assert out["llm_calls"] == direct.llm_calls and out["leak_questions"] == direct.leak_questions
    assert tuple(out["reordered"]) == direct.reordered and out["blocked_by"] == direct.blocked_by
    assert (out["source"], out["ticked"], out["state"]) == ("ai", False, "pending")
    assert len(saved) == 1 and saved[0].questions == direct.session.questions
    assert saved[0].transcript == cap_sent(direct.session.transcript), "capped as the route caps it"
    added = [p for q in saved[0].questions for o in q.options if (p := o.proposal) is not None]
    assert added and all(p.state.value == "pending" for p in added)


def _assist_case(
    config_root: Path | None, **session_update: Any
) -> tuple[DataToolContext, AssistClient, list[Any]]:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(600)],
            "churn_flg": [1 if i % 4 == 0 else 0 for i in range(600)],
            "final_status": [("open", "closed", "pending")[i % 3] for i in range(600)],
        }
    )
    agent = context_for(frame, target="churn_flg", config_root=config_root)
    session = start_session(agent, session_id="s-2").model_copy(update=session_update)
    client = AssistClient(columns=[{"column": "final_status", "why": "Set when the customer leaves."}])
    saved: list[Any] = []
    ctx = DataToolContext(
        agent=agent,
        principal=ANALYST,
        caller="person",
        session=session,
        save_session=saved.append,
        meter=meter_for(client),
        guardrails=guardrails(),
        clock=lambda: NOW,
    )
    return ctx, client, saved


def test_second_opinion_refuses_an_applied_session_as_the_assist_route() -> None:
    ctx, client, saved = _assist_case(header_help_root(), status=SessionStatus.APPLIED)
    with pytest.raises(DataToolError) as caught:
        _call("second_opinion", {}, ctx)
    assert (caught.value.code, caught.value.status) == ("AGENT_SESSION_APPLIED", 409)
    assert not client.calls and not saved, "no AI call, the approved session is not changed"


def test_second_opinion_needs_an_ai_service(case: DataToolContext) -> None:
    ctx = dataclasses.replace(case, session=start_session(case.agent, session_id="s"), save_session=lambda _s: None)  # type: ignore[arg-type]
    assert data_registry().find("second_opinion", ctx) is None


# ---------------------------------------------------------------------------
# #65 find_personal_data, #66 check_leaks, #68 check_uplift_data
# ---------------------------------------------------------------------------
def test_find_personal_data_is_detect_pii_per_column() -> None:
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(200)],
            "contact_email": [f"user{i}@example.test" for i in range(200)],
            "notes": [f"call me on 98765 4{i:04d}" for i in range(200)],
            "spend": [float(i) for i in range(200)],
        }
    )
    agent = context_for(frame)
    out = _call("find_personal_data", {}, DataToolContext(agent=agent, principal=ANALYST, caller="person"))
    types = {c.name: c.inferred_type for c in agent.profile.columns}
    expected = {
        name: (
            list(detect_pii(frame[name], name, types[name])),
            list(free_text_pii(frame[name], types[name])),
        )
        for name in frame.columns
    }
    listed = {c["column"]: (c["kinds"], c["free_text_kinds"]) for c in out["columns"]}
    assert listed == {name: kinds for name, kinds in expected.items() if kinds[0] or kinds[1]}
    assert "contact_email" in listed and out["checked"] == 4


def test_check_leaks_reports_the_runs_own_check_and_the_first_look(agent: AgentContext) -> None:
    frame = _customers()
    frame["refund_after_churn"] = frame["converted_30d"]  # a copy of the answer
    leaky = context_for(frame)
    ctx = DataToolContext(agent=leaky, principal=ANALYST, caller="person")
    out = _call("check_leaks", {"outcome": "converted_30d", "primary_key": "customer_id"}, ctx)
    params = params_from_config(
        leaky.config,
        primary_key="customer_id",
        target="converted_30d",
        seed=seed_from(leaky.upload_id),
        row_count=leaky.profile.row_count,
    )
    direct = check_leakage_suspected(frame, params)
    assert out["run_check"] == _dumped(direct.findings)
    assert out["verdict"] == "leak_suspected"
    assert any(c["column"] == "refund_after_churn" for c in out["columns"])
    assert [s["source"] for s in out["sources"]] == [
        "run_checks",
        "first_look",
        "build_report",
        "builder",
        "ai_reviewer",
    ]
    assert not next(s for s in out["sources"] if s["source"] == "ai_reviewer")["ran"]


def test_check_leaks_on_a_clean_file_finds_none(case: DataToolContext) -> None:
    out = _call("check_leaks", {"outcome": "converted_30d", "primary_key": "customer_id"}, case)
    assert out["verdict"] == "none_found" and out["columns"] == []


def test_check_uplift_data_is_the_campaign_effect_checks(case: DataToolContext, agent: AgentContext) -> None:
    out = _call("check_uplift_data", {"primary_key": "customer_id", "target": "converted_30d"}, case)
    direct = run_uplift_checks(
        agent.frame,
        agent.config,
        primary_key="customer_id",
        target="converted_30d",
        upload_id=agent.upload_id,
        now=NOW,
    )
    assert out["report"] == direct.report.model_dump(mode="json")
    assert out["checks_total"] == len(direct.report.checks)


# ---------------------------------------------------------------------------
# #73 read_run_checks
# ---------------------------------------------------------------------------
def test_read_run_checks_is_things_checked_with_the_too_good_alarm(tmp_path: Path, config_root: Path) -> None:
    from tests.unit.doctor.test_result_alarm import RUN_ID, record, write_training_run

    storage = LocalStorage(tmp_path)
    write_training_run(storage, config_root)
    ctx = DataToolContext(principal=ANALYST, caller="person", storage=storage, config_root=config_root)
    out = _call("read_run_checks", {"run_id": RUN_ID}, ctx)
    assert out == things_checked(storage, record(), config_root).model_dump(mode="json")
    assert out["checks"][0]["code"] == "RESULT_SUSPICIOUSLY_GOOD"
    with pytest.raises(DataToolError) as caught:
        _call("read_run_checks", {"run_id": "run-nope"}, ctx)
    assert caught.value.code == "RUN_NOT_FOUND"


# ---------------------------------------------------------------------------
# #76 request_approval
# ---------------------------------------------------------------------------
def _scores(unknown: int) -> pd.DataFrame:
    """A per-customer file (one row per customer); its first `unknown` rows name nobody on the list."""
    ids = [f"CUST-{i:05d}" if i >= unknown else f"GONE-{i:05d}" for i in range(400)]
    return pd.DataFrame({"customer_id": ids, "tier": [("A", "B")[i % 2] for i in range(400)]})


def _joined(case: DataToolContext, unknown: int) -> tuple[DataToolContext, Any]:
    assert case.agent is not None
    files = (case.files[0], _loaded(case.agent, _scores(unknown), "up-scores", "scores.csv"))
    ctx = dataclasses.replace(case, files=files)
    joined = data_registry().call(
        "join_files",
        {"base_upload_id": "up-customers", "base_key_column": "customer_id", "upload_ids": ["up-scores"]},
        ctx,
    )
    return dataclasses.replace(ctx, evidence=(joined,)), joined


def test_request_approval_names_the_sentence_role_and_expiry_of_an_approve_class_item(
    case: DataToolContext,
) -> None:
    ctx, joined = _joined(case, unknown=40)  # a tenth of the file meets no customer: over 2%
    out = _call("request_approval", {"evidence_id": joined.evidence_id}, ctx)
    assert out["approve_class"] is True and out["kind"] == "data.join_files"
    assert out["sentence"] == joined.output["summary"]
    assert out["sentence_digest"] == hashlib.sha256(out["sentence"].encode()).hexdigest()
    assert out["role"] == "analyst" and out["requested_by"] == "u-asha"
    assert out["another_person"] is bool(loaded_config(case).governance.approval_required)
    assert datetime.fromisoformat(out["expires_at"]) == NOW + timedelta(days=14)


def test_request_approval_says_when_ticking_is_enough(case: DataToolContext) -> None:
    ctx, joined = _joined(case, unknown=0)
    out = _call("request_approval", {"evidence_id": joined.evidence_id}, ctx)
    assert out["approve_class"] is False and out["sentence"] is None and out["role"] is None


def test_request_approval_refuses_what_is_not_a_proposal_of_this_case(case: DataToolContext) -> None:
    looked = data_registry().call("describe_missing", {}, case)
    ctx = dataclasses.replace(case, evidence=(looked,))
    for given in ({"evidence_id": looked.evidence_id}, {"evidence_id": "ev_" + "a" * 16}):
        with pytest.raises(DataToolError) as caught:
            _call("request_approval", given, ctx)
        assert caught.value.code == "TOOL_INPUT_INVALID"


def test_request_approval_is_an_ask_no_viewer_may_call(case: DataToolContext) -> None:
    tool = data_registry().get("request_approval")
    assert tool.kind is DataToolKind.ASK
    viewer = Principal(user_id="v", username="v", roles=frozenset({Role.VIEWER}), kind="user")
    ctx, joined = _joined(case, unknown=0)
    with pytest.raises(DataToolError) as caught:
        _call(
            "request_approval",
            {"evidence_id": joined.evidence_id},
            dataclasses.replace(ctx, principal=viewer),
        )
    assert caught.value.code == "TOOL_ROLE_REQUIRED"
