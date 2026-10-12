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
from engine.doctor import registry as registry_module
from engine.doctor.diagnose import LoadedFile
from engine.doctor.memory import remember
from engine.doctor.registry import DataToolContext, data_registry
from engine.stages import ingest
from engine.storage import LocalStorage
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
ANALYST = Principal(user_id="u-analyst", username="analyst", roles=frozenset({Role.ANALYST}), kind="user")

Calls = Callable[[Sequence[str]], list[dict[str, Any]]]


def _look(name: str) -> Calls:
    return lambda columns: arg_sets(TOOLS[name], columns)


def _fixed(*calls: dict[str, Any]) -> Calls:
    return lambda _columns: list(calls)


def _edits(columns: Sequence[str]) -> list[dict[str, Any]]:
    """Hide each column by hand, and a merge of spellings that names planted values (what a person may type)."""
    return [{"kind": "drop_column", "column": column} for column in columns] + [
        {"kind": "map_values", "column": "notes", "params": {"mapping": {f"{NAME} {EMAIL}": PHONE}}},
        {"kind": "map_values", "column": "region_contact", "params": {"mapping": {"North": "N"}}},
    ]


CHAT_ONLY = frozenset({"propose_setting", "propose_step"})
"""Made inside a chat turn: their canary is a whole chat session (`test_the_chat_suggestions_pass_the_canary`)."""

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
}


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


def _case(ctx: AgentContext, root: Path) -> DataToolContext:
    events = _events(ctx)
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
    )
    storage = LocalStorage(root)
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
        config_root=ctx.config_root,
    )


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
                result: dict[str, Any] = data_registry().call(tool.name, given, case).output
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
