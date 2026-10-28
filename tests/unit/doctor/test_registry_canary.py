"""Every Data Doctor tool is in the egress canary and passes in both modes (Plan O M232, DEC-1802; design §5.4).

The canary is Plan G's (`tests/unit/agent/egress_canary.py`): fake personal data planted in cells, in headers, in
the ID column - and here also in a second file, in file names and in intake notes. Each registered tool is called
through the registry with arguments that reach every column and every file, and its result is put through the
egress gate exactly as a prompt would take it (`Egress.prepare`, `for_prompt`, the last check `assert_clean`).
No planted value a pattern can recognise survives, no personal-data column yields a value, the last check has
nothing left to do, and in `summaries_only` no word that only lives in a cell survives either.

The chat's two suggestions run inside a chat turn: they are called by whole chat sessions through the registry
(`data_doctor.unified` on), and every prompt and everything stored is searched.

A tool registered without a canary entry fails `test_every_registered_tool_has_a_canary_entry`: adding a tool
means adding its canary calls (the design §5.4).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.egress import Egress, assert_clean
from engine.agent.loop import chat_turn
from engine.agent.session import start_session
from engine.agent.tools import TOOLS, AgentContext, AgentToolError
from engine.agent.untrusted import for_prompt
from engine.contracts import RunMode
from engine.doctor import registry as registry_module
from engine.doctor.case import CaseFile, CaseStore
from engine.doctor.case_files import CaseFiles
from engine.doctor.diagnose import LoadedFile
from engine.doctor.memory import remember
from engine.doctor.prescription import slot_name
from engine.doctor.readiness import CheckReadinessArgs, ReadinessInputs
from engine.doctor.registry import DataToolContext, data_registry
from engine.stages import ingest
from engine.storage import LocalStorage, upload_key
from engine.utils.time import utc_now
from tests.unit.agent.egress_canary import (
    ADDRESS,
    CELL_WORDS,
    EMAIL,
    HARD_SECRETS,
    ID_BASE,
    NAME,
    PHONE,
    ToolingClient,
    after,
    arg_sets,
    canary_context,
    guardrails,
    leaks,
    meter_for,
    stored_text,
)
from tests.unit.doctor.test_memory import recipe, upload_record

PERSONAL_VALUES = (
    "Joe Bloggs",
    "Ann Lee",
    "PQRST5678U",
    "LMNOP4321Z",
    "ann.lee",
    "91234 56789",
    "99887 76655",
)
"""Values of the columns the profile marks as personal data: never shown, in any mode."""
MODES = ("masked_data", "summaries_only")
CUSTOMERS = "up-customers"
EVENTS = "up-events"
LATER = "up-events-later"
"""The next export of the events report, overlapping the first: what M237's `stack_files` reads."""
ANALYST = Principal(user_id="u-analyst", username="analyst", roles=frozenset({Role.ANALYST}), kind="user")

Calls = Callable[[Sequence[str]], list[dict[str, Any]]]


def _look(name: str) -> Calls:
    return lambda columns: arg_sets(TOOLS[name], columns)


def _fixed(*calls: dict[str, Any]) -> Calls:
    return lambda _columns: list(calls)


def _each(argument: str) -> Calls:
    """One call per column of the file, naming it as `argument`."""
    return lambda columns: [{argument: column} for column in columns]


def _edits(columns: Sequence[str]) -> list[dict[str, Any]]:
    """Hide each column by hand, and a merge of spellings that names planted values (what a person may type)."""
    return [{"kind": "drop_column", "column": column} for column in columns] + [
        {"kind": "map_values", "column": "notes", "params": {"mapping": {f"{NAME} {EMAIL}": PHONE}}},
        {"kind": "map_values", "column": "region_contact", "params": {"mapping": {"North": "N"}}},
    ]


CHAT_ONLY = frozenset({"propose_setting", "propose_step"})
"""Made inside a chat turn: their canary is a whole chat session (`test_the_chat_suggestions_pass_the_canary`)."""

_SQL_CUSTOMERS = slot_name(Path(f"customers of {NAME} {EMAIL}.csv").stem)
_SQL_EVENTS = slot_name(Path(f"bills {PHONE}.csv").stem)
"""The table names the SQL step gives the canary's two files (`engine.doctor.prescription.slot_name`)."""

SPECS: dict[str, Calls | None] = {
    **{name: _look(name) for name in TOOLS},
    "propose_setting": None,
    "propose_step": None,
    "edit_by_hand": _edits,
    "diagnose_files": _fixed({}, {"upload_ids": [CUSTOMERS]}, {"upload_ids": [EVENTS, CUSTOMERS]}),
    "find_file_role": _fixed({}, {"upload_id": CUSTOMERS}, {"upload_id": EVENTS}),
    "advise": _fixed({}, {"primary_key": "customer_id", "target": "converted_30d"}),
    "first_look": _fixed(
        {"outcome": "converted_30d"}, {"outcome": "converted_30d", "primary_key": "customer_id"}
    ),
    "how_files_fit": _fixed({}),
    "what_can_this_power": _fixed({}),
    "enough_data": _fixed({"outcome": "converted_30d"}),
    "data_request": _fixed({}),
    "find_time_columns": _fixed({}, {"upload_id": CUSTOMERS}, {"upload_id": EVENTS}),
    "recall_treatment": _fixed({}),
    # Plan O wave 1 (DEC-1802 (t)): M240's, M238's, M237's, M241's and M234's tools.
    "read_other_format": _fixed({"file_id": CUSTOMERS}),
    "check_parser_loss": _fixed({"file_id": CUSTOMERS}),
    "synonym_clusters": _each("column"),
    "impossible_values": _fixed({}),
    "detect_units": _each("column"),
    "keep_latest_per_id": _fixed({"time_column": "signup_date"}),
    "split_file": _fixed(
        {"rule": {"column": "signup_date", "by": "date", "cut": "2024-06-30"}},
        {"rule": {"column": "region_contact", "by": "values", "scoring_values": ["North"]}},
    ),
    "normalise_key": _fixed(
        {
            "upload_id": EVENTS,
            "column": "customer_id",
            "other_upload_id": CUSTOMERS,
            "other_column": "customer_id",
        }
    ),
    "join_files": _fixed(
        {
            "base_upload_id": CUSTOMERS,
            "base_key_column": "customer_id",
            "upload_ids": [EVENTS],
            "kinds": {EVENTS: "outcome_list"},
        },
        {"base_upload_id": CUSTOMERS, "base_key_column": "customer_id", "upload_ids": [EVENTS, LATER]},
    ),
    "stack_files": _fixed(
        {"upload_ids": [EVENTS, LATER]},
        {"upload_ids": [EVENTS, LATER], "key_column": "customer_id", "date_column": "bill_date"},
    ),
    "build_dataset": _fixed({"use_case": "targeted-advertisement", "upload_ids": [CUSTOMERS, EVENTS, LATER]}),
    "detect_locale": _fixed({}),
    "parse_indian_numbers": _fixed({}),
    "indian_booleans": _fixed({}),
    "detect_india_ids": _fixed({}),
    "check_readiness": _fixed({}, {"mode": "train"}),
    # Plan O M236 (DEC-1806): M240's two tools, catalogued as #82 and #83, and M236's wrappers and `explain`.
    "read_workbook_sheets": _fixed({"file_id": CUSTOMERS}),
    "find_excel_serial_dates": _fixed({"file_id": CUSTOMERS}),
    "propose_intake": _fixed({}, {"upload_id": CUSTOMERS}),
    "preview_connection": lambda _columns: [{"connection_id": _IDS["connection"], "table": "customers"}],
    "propose_import": lambda _columns: [
        {"connection_id": _IDS["connection"], "path": "exports/customers.csv"}
    ],
    "check_refresh": lambda _columns: [{"spec_id": _IDS["spec"]}],
    "preflight": _fixed({}),
    "second_opinion": _fixed({}),
    "explain": lambda _columns: [{"evidence_ids": list(_IDS["evidence"])}],
    "propose_role": _fixed({}, {"upload_id": CUSTOMERS}, {"upload_id": EVENTS}),
    "propose_key": _fixed({}, {"target": "converted_30d"}),
    "propose_mapping": _fixed({}, {"upload_id": EVENTS}),
    "find_personal_data": _fixed({}),
    "check_leaks": _fixed({"outcome": "converted_30d", "primary_key": "customer_id"}),
    "check_uplift_data": _fixed({"primary_key": "customer_id", "target": "converted_30d"}),
    "read_run_checks": lambda _columns: [{"run_id": _IDS["run"]}],
    "request_approval": lambda _columns: [{"evidence_id": evidence} for evidence in _IDS["evidence"]],
    # Plan O wave 2 integration: M242's conversion on every column (a column that holds no money is refused in
    # the output, which must be clean too).
    "convert_currency": lambda columns: [
        {
            "column": column,
            "from_currency": "USD",
            "to_currency": "INR",
            "rate": 83.1,
            "rate_date": "2026-10-01",
            "source": "RBI reference rate, 1 Oct 2026",
        }
        for column in columns
    ],
    # Plan O M239 (DEC-1809): the outcome tools, on every column and on the case's event files.
    "define_outcome": _fixed(
        {"kind": "event_presence"},
        {"kind": "event_presence", "event_upload_id": EVENTS, "date_column": "bill_date", "days": 30},
        {"kind": "event_absence", "event_upload_id": EVENTS, "days": 60, "grace_days": 7},
        {
            "kind": "value_threshold",
            "event_upload_id": EVENTS,
            "days": 30,
            "column": "amount",
            "operator": ">",
            "threshold": 500,
        },
    ),
    "map_outcome_values": lambda columns: [{"column": column} for column in columns]
    + [{"column": "region_contact", "map": {"North": "yes", "South": "no"}}],
    "check_target_type": _each("column"),
    # Plan O M244 (DEC-1814): the time-settings tools, with every column named as the date each row describes
    # (a column that is not a date is refused, and the refusal must be clean too) and as the outcome.
    "estimate_training_time": _fixed({}, {"minutes": 5}, {"minutes": 240}),
    "propose_training_budget": _fixed({}),
    "propose_time_settings": lambda columns: [{}, {"window_days": 30}]
    + [{"as_of_column": column} for column in columns]
    + [{"outcome_column": column} for column in columns],
    # The agent's look at its own work: the first rows, personal columns withheld, in every data-access mode.
    "preview_working_copy": _fixed({}, {"rows": 3}, {"rows": 10}),
    # The SQL step reads tables named after their files, and the canary's file names carry planted secrets.
    "propose_features": lambda _columns: [
        {
            "upload_id": EVENTS,
            "entity_key": "customer_id",
            "time_column": "bill_date",
            "as_of": "2025-09-01",
            "features": [{"name": "total_spend", "agg": "sum", "column": "amount"}],
        },
        {
            "upload_id": EVENTS,
            "entity_key": "customer_id",
            "time_column": "bill_date",
            "as_of_rule": "latest_date_in_file",
            "preset": "rfm",
            "amount_column": "amount",
        },
    ],
    "propose_amount_outcome": lambda _columns: [
        {
            "upload_id": EVENTS,
            "entity_key": "customer_id",
            "time_column": "bill_date",
            "amount_column": "amount",
            "window_days": 30,
            "as_of": "2025-09-01",
        },
    ],
    "propose_sql_step": lambda _columns: [
        {
            "description": "Every customer row",
            "sql": f"SELECT * FROM t_{_SQL_CUSTOMERS}",
            "output_name": "customers_copy",
            "upload_ids": [CUSTOMERS],
        },
        {
            "description": "Bills per customer",
            "sql": f"SELECT customer_id, COUNT(*) AS bills FROM t_{_SQL_EVENTS} GROUP BY customer_id",
            "output_name": "bill_counts",
            "upload_ids": [EVENTS],
        },
    ],
}

_IDS: dict[str, Any] = {}
"""What M236's canary calls name (a connection, a recipe, a run, earlier results), set by `_case`."""


class _CanaryConnector:
    """A saved connection's connector serving the canary file, masked as every connector masks its preview."""

    kind = "postgres"

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def available(self) -> bool:
        return True

    def info(self) -> Any:
        from engine.connections.registry import CONNECTORS

        return CONNECTORS["postgres"].info()

    def preview(self, config: Any, secrets: Any, selection: Any) -> Any:
        from engine.connections.base import frame_preview

        return frame_preview(self.frame)

    def file_format(self, selection: Any) -> str:
        return "csv"


def test_every_registered_tool_has_a_canary_entry() -> None:
    assert set(SPECS) == set(data_registry().names())


# ---------------------------------------------------------------------------
# The case: the canary file, an event file with its own secrets, a remembered treatment
# ---------------------------------------------------------------------------
def _events(ctx: AgentContext, rows: int = 900) -> pd.DataFrame:
    ids = [str(ID_BASE + 7 * (i % 300)) for i in range(rows)]
    return pd.DataFrame(
        {
            "customer_id": ids,
            "bill_date": [f"2025-{1 + i % 12:02d}-{1 + i % 27:02d}" for i in range(rows)],
            "amount": [round(100 + (i * 13) % 700 + 0.5, 2) for i in range(rows)],
            "delivery_note": [[f"{NAME} at {ADDRESS}", f"call {PHONE}", "ok"][i % 3] for i in range(rows)],
            EMAIL: ["x", "y", "z"] * (rows // 3),
        }
    )


def _profile(ctx: AgentContext, frame: pd.DataFrame, upload_id: str, file_name: str) -> Any:
    return ingest.profile_dataset(
        frame,
        ctx.config,
        upload_id=upload_id,
        file_name=file_name,
        file_format="csv",
        file_size_bytes=1,
        delimiter=",",
        encoding="utf-8",
    )


def _stored_case(storage: LocalStorage, ctx: AgentContext, file_name: str) -> CaseFiles:
    """The canary file as a stored upload in an M231 case, for M240's tools (which read the case's files)."""
    source = upload_key(CUSTOMERS, "source.csv")
    storage.write_text(source, ctx.frame.to_csv(index=False))
    storage.write_text(
        upload_key(CUSTOMERS, "upload.json"),
        json.dumps({"upload_id": CUSTOMERS, "file_name": file_name, "source_key": source}),
    )
    store = CaseStore(storage)
    case = store.create(
        client_id="default",
        created_by=ANALYST.user_id,
        files=[
            CaseFile(upload_id=CUSTOMERS, file_name=file_name, added_at=utc_now(), added_by=ANALYST.user_id)
        ],
    )
    return CaseFiles(store=store, client_id="default", case_id=case.case_id, actor=ANALYST.user_id)


def _readiness(ctx: AgentContext) -> Callable[[CheckReadinessArgs], ReadinessInputs]:
    """M234's inputs for the canary file, as a case would build them for its prepared copy."""

    def inputs(args: CheckReadinessArgs) -> ReadinessInputs:
        return ReadinessInputs(
            frame=ctx.frame,
            config=ctx.config,
            mode=RunMode(args.mode),
            primary_key="customer_id",
            target="converted_30d",
            upload_id=CUSTOMERS,
            row_count=len(ctx.frame),
        )

    return inputs


def _case(ctx: AgentContext, root: Path) -> DataToolContext:
    events = _events(ctx)
    later = pd.concat([events.iloc[600:], _events(ctx, 1_200).iloc[900:]], ignore_index=True)
    files = (
        LoadedFile(
            upload_id=CUSTOMERS,
            file_name=f"customers of {NAME} {EMAIL}.csv",
            frame=ctx.frame,
            profile=ctx.profile,
            notes=(f"Read the sheet {NAME} {PHONE} sent.",),
        ),
        LoadedFile(
            upload_id=EVENTS,
            file_name=f"bills {PHONE}.csv",
            frame=events,
            profile=_profile(ctx, events, EVENTS, "bills.csv"),
        ),
        LoadedFile(
            upload_id=LATER,
            file_name=f"bills later {EMAIL}.csv",
            frame=later,
            profile=_profile(ctx, later, LATER, "bills later.csv"),
        ),
    )
    storage = LocalStorage(root)
    case_files = _stored_case(storage, ctx, files[0].file_name)
    _m236(storage, ctx, root)
    upload_record(storage, "u_month_one", f"{NAME} {EMAIL}.csv")
    remember(
        storage,
        use_case_id=ctx.use_case_id,
        columns=[str(c) for c in ctx.frame.columns],
        sheet=None,
        intake_enabled=True,
        recipe=recipe(),
        recipe_upload_id="u_month_one",
        source_upload_id="u_month_one",
    )
    return DataToolContext(
        agent=ctx,
        principal=ANALYST,
        caller="person",
        storage=storage,
        files=files,
        session=start_session(ctx, session_id="canary"),
        save_session=lambda _session: None,
        config_root=ctx.config_root,
        primary_key="customer_id",
        case_files=case_files,
        readiness=_readiness(ctx),
        case_items=(),  # the plan so far, for `preview_working_copy`
        **_m236_context(ctx, root, storage, files),
    )


def _m236(storage: LocalStorage, ctx: AgentContext, root: Path) -> None:
    """What M236's tools read: the canary file's intake report (planted text in its notes and footer labels), a
    training run's artefacts."""
    from engine.intake.contracts import IntakeReport
    from tests.unit.doctor.test_result_alarm import RUN_ID, write_training_run

    report = IntakeReport(
        source_format="csv",
        applied=True,
        header_row=2,
        rows_above_header=1,
        footer_rows=1,
        footer_labels=(f"Total for {NAME} {EMAIL}",),
        notes=(f"Left out the title row '{NAME} {PHONE}'.",),
    )
    storage.write_model(upload_key(CUSTOMERS, "intake.json"), report)
    write_training_run(storage, ctx.config_root or Path("configs"))
    _IDS["run"] = RUN_ID


def _m236_context(
    ctx: AgentContext, root: Path, storage: LocalStorage, files: Sequence[LoadedFile]
) -> dict[str, Any]:
    """M236's context fields: saved connections (a fake connector serving the canary file), an onboarding recipe,
    an AI service whose second opinion names planted text, and earlier results to explain and approve."""
    from engine.connections import registry as connectors
    from engine.connections.store import ConnectionStore
    from engine.settings import Settings
    from tests.unit.agent.assist_client import AssistClient
    from tests.unit.production.scheduling_support import make_world

    store = ConnectionStore(LocalStorage(root / "connections"), Settings())
    record = store.create(
        kind="postgres", name=f"CRM of {NAME}", config={"host": "db.example.test"}, secrets={}
    )
    _IDS["connection"] = record.connection_id
    fake = _CanaryConnector(ctx.frame)
    _PATCHES.append((connectors, "connector", connectors.connector))
    connectors.connector = lambda _kind: fake  # type: ignore[assignment,return-value]
    world = make_world(root / "world", ctx.config_root or Path("configs"))
    _IDS["spec"] = world.spec.spec_id
    first = DataToolContext(
        agent=ctx,
        principal=ANALYST,
        caller="person",
        storage=storage,
        files=tuple(files),
        primary_key="customer_id",
    )
    evidence = (
        data_registry().call("describe_missing", {}, first),
        data_registry().call(
            "rate_by", {"column": "region_contact", "outcome_column": "converted_30d"}, first
        ),
        data_registry().call(
            "join_files",
            {"base_upload_id": CUSTOMERS, "base_key_column": "customer_id", "upload_ids": [EVENTS, LATER]},
            first,
        ),
        data_registry().call("stack_files", {"upload_ids": [EVENTS, LATER]}, first),
    )
    _IDS["evidence"] = tuple(result.evidence_id for result in evidence)
    planted = [{"column": column, "why": f"{NAME} wrote {EMAIL} here."} for column in ctx.frame.columns]
    return {
        "connections": store,
        "client_store": world.client_store,
        "meter": meter_for(AssistClient(columns=planted)),
        "guardrails": guardrails(),
        "evidence": evidence,
    }


_PATCHES: list[tuple[Any, str, Any]] = []


@pytest.fixture(autouse=True)
def _restore_patches() -> Any:
    yield
    while _PATCHES:
        module, name, original = _PATCHES.pop()
        setattr(module, name, original)


def _gate(case: DataToolContext, mode: str) -> Egress:
    agent = case.agent
    assert agent is not None
    columns = list(dict.fromkeys(str(c) for f in case.files for c in f.frame.columns))
    personal = {c.name for f in case.files for c in f.profile.columns if c.pii_kinds}
    return Egress.build(
        columns,
        personal_columns=sorted(personal),
        always_hide=agent.config.agent.always_hide_columns,
        mode=mode,  # type: ignore[arg-type]
    )


def _sent(gate: Egress, value: dict[str, Any]) -> str:
    """What a prompt would hold of `value`, after the gate and the untrusted-text pass, as JSON."""
    names = {column: gate.label(column) for column in gate.columns}
    return json.dumps(for_prompt(gate.prepare(value), names), ensure_ascii=False)


@pytest.mark.parametrize("mode", MODES)
def test_every_registered_tool_passes_the_canary(mode: str, tmp_path: Path) -> None:
    ctx = canary_context(mode=mode, rows=900)
    case = _case(ctx, tmp_path)
    gate = _gate(case, mode)
    columns = list(gate.columns)
    answered: dict[str, int] = {}
    for tool in data_registry().list():
        calls = SPECS[tool.name]
        if calls is None:
            assert tool.name in CHAT_ONLY
            continue
        for given in calls([c for c in columns if c in ctx.frame.columns]):
            try:
                # A tool only the rules and the AI may call (the SQL step) runs as the rules do.
                caller = case if case.caller in tool.callers else replace(case, caller="rules")
                result: dict[str, Any] = data_registry().call(tool.name, given, caller).output
                answered[tool.name] = answered.get(tool.name, 0) + 1
            except AgentToolError as exc:  # a refusal is fed back to the model; it must be clean too
                result = {"error": exc.code, "message": exc.message}
            payload = _sent(gate, result)
            where = f"{tool.name}({json.dumps(given)[:80]})"
            assert leaks(payload, HARD_SECRETS) == [], where
            assert leaks(payload, PERSONAL_VALUES) == [], where
            assert assert_clean(payload)[1] == 0, f"{where}: the last check had to mask something"
            if mode == "summaries_only":
                assert leaks(payload, CELL_WORDS, exact=True) == [], where
    missing = {t.name for t in data_registry().list()} - CHAT_ONLY - set(answered)
    assert not missing, f"tools that never answered on the canary: {sorted(missing)}"


def test_the_planted_values_really_reach_the_wrapped_results(tmp_path: Path) -> None:
    """Without the gate the results do hold planted values, so the canary above is not vacuous."""
    ctx = canary_context(rows=900)
    case = _case(ctx, tmp_path)
    raw = json.dumps(
        [
            data_registry().call("diagnose_files", {}, case).output,
            data_registry().call("recall_treatment", {}, case).output,
            data_registry().call("sample_rows", {"columns": ["notes", "feedback"]}, case).output,
        ]
    )
    assert EMAIL in raw and PHONE in raw


# ---------------------------------------------------------------------------
# The chat's suggestions, through the registry
# ---------------------------------------------------------------------------
def _suggestions(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Per turn, a look at one column and one suggestion citing it: a merge of its first value into planted
    text, and a setting whose reason holds planted text. At most one refusal per turn, so the ids stay in step.
    """
    steps: list[dict[str, Any]] = []
    turn = 0
    for column in frame.columns:
        turn += 1
        first = str(frame[column].iloc[0])
        steps.extend(
            [
                {"action": "value_counts", "args": {"column": str(column)}},
                {
                    "action": "propose_step",
                    "args": {
                        "kind": "map_values",
                        "column": str(column),
                        "params": {"mapping": {first: f"{NAME} {EMAIL}"}},
                        "evidence_ids": [f"c{turn}-e1"],
                        "reason": f"Merge {EMAIL} and {PHONE}.",
                    },
                },
            ]
        )
    turn += 1
    steps.extend(
        [
            {"action": "describe_missing", "args": {}},
            {
                "action": "propose_setting",
                "args": {"path": "split.validation_fraction", "value": 0.2, "reason": f"Ring {PHONE}."},
            },
        ]
    )
    return steps


@pytest.mark.parametrize("mode", MODES)
def test_the_chat_suggestions_pass_the_canary(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "unified_on", lambda _config: True)
    ctx = canary_context(mode=mode, rows=600)
    client = ToolingClient(_suggestions(ctx.frame), per_turn=2)
    session = start_session(ctx, session_id="canary")
    meter = meter_for(client)
    message = "Please fix every column."
    for _ in range(200):  # a turn that is refused twice ends early; the rest of its calls go to the next
        if not client.queue:
            break
        turn = chat_turn(ctx, session, message, meter=meter, guardrails=guardrails(), principal=ANALYST)
        session = after(session, message, turn)
    assert not client.queue
    made = {p.kind.value for p in session.proposals if p.source is not None}
    assert made == {"recipe_step", "setting"}, "both suggestions went through the registry"
    for text in client.sent_text:
        assert leaks(text, HARD_SECRETS) == []
        assert leaks(text, PERSONAL_VALUES) == []
        if mode == "summaries_only":
            assert leaks(text, CELL_WORDS, exact=True) == []
    stored = stored_text(session)
    assert leaks(stored, HARD_SECRETS) == [] and leaks(stored, PERSONAL_VALUES) == []
    for message_ in session.transcript:
        if message_.turn is not None:
            assert "EGRESS_LATE_MASK" not in message_.turn.error_codes
