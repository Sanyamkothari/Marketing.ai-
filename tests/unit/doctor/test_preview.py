"""The data panel: the first rows of each file before and after the ticked steps, masked (`engine.doctor.preview`),
the agent's `preview_working_copy` tool, and the screen's `view.preview` / `ledger.steps[].samples`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import JsonValue

from engine.access.roles import Principal, Role
from engine.agent.config import DataAccess
from engine.agent.contracts import (
    AgentConfidence,
    DecidedBy,
    ProposalSource,
    ProposalState,
    RecipeStep,
    RecipeStepKind,
)
from engine.config import load_use_case
from engine.doctor.case import CaseItem, CaseStore, payload_digest
from engine.doctor.diagnose import LoadedFile
from engine.doctor.preview import PERSONAL_CELL, PREVIEW_ROWS, cached_preview, preview_rows
from engine.doctor.registry import DataToolContext, DataToolError, data_registry
from engine.doctor.screen import build_screen
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.visit_support import ANALYST, CLIENT, FIRST, GOAL_A, T0

ANALYST_P = Principal(user_id="u-analyst", username="asha", roles=frozenset({Role.ANALYST}), kind="user")


def _frame(rows: int = 40) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"C{1000 + i}" for i in range(rows)],
            "city": [" Pune " if i % 2 else "Delhi" for i in range(rows)],
            "email": [f"person{i}@example.com" for i in range(rows)],
            "spend": [float(i) for i in range(rows)],
            "opted_in": [i % 3 for i in range(rows)],
        }
    )


def _file(frame: pd.DataFrame, upload_id: str = FIRST, name: str = "customers.csv") -> LoadedFile:
    return vs.loaded(upload_id, name, frame, load_use_case(GOAL_A))


def _step(
    kind: RecipeStepKind, column: str, order: int = 1, new_column: str | None = None, **params: JsonValue
) -> RecipeStep:
    return RecipeStep(
        order=order,
        kind=kind,
        column=column,
        new_column=new_column,
        params=params,
        reason="test",
        decided_by=DecidedBy.USER,
    )


def _item(
    step: RecipeStep, item_id: str, *, state: ProposalState = ProposalState.ACCEPTED, upload_id: str = FIRST
) -> CaseItem:
    payload: dict[str, JsonValue] = {"upload_id": upload_id, "step": step.model_dump(mode="json")}
    return CaseItem(
        item_id=item_id,
        kind="recipe_step",
        title=step.reason,
        source=ProposalSource.AI,
        confidence=AgentConfidence.SURE,
        state=state,
        upload_id=upload_id,
        payload=payload,
        payload_digest=payload_digest(payload),
        proposed_at=T0,
    )


STRIP = _step(RecipeStepKind.NORMALISE_TEXT, "city", strip=True)
DROP = _step(RecipeStepKind.REMOVE_ROWS, "opted_in", operator="equals", values=[0])


def test_no_steps_shows_the_same_first_rows_twice_and_nothing_changed() -> None:
    shown = preview_rows([_file(_frame())], []).files[0]
    assert shown.total_rows == 40 and len(shown.before) == PREVIEW_ROWS == len(shown.after)
    assert shown.before == shown.after
    assert [c.name for c in shown.columns] == ["customer_id", "city", "email", "spend", "opted_in"]
    assert not any(c.changed for c in shown.columns)
    assert shown.before[0][1] == " Pune " or shown.before[0][1] == "Delhi"


def test_a_ticked_step_changes_its_column_and_only_its_column() -> None:
    shown = preview_rows([_file(_frame())], [_item(STRIP, "i-strip")]).files[0]
    changed = {c.name for c in shown.columns if c.changed}
    assert changed == {"city"}
    city = [c.name for c in shown.columns].index("city")
    assert shown.before[1][city] == " Pune " and shown.after[1][city] == "Pune"


def test_a_step_that_is_not_ticked_changes_nothing() -> None:
    items = [
        _item(STRIP, "i-strip", state=ProposalState.PENDING),
        _item(DROP, "i-drop", state=ProposalState.REJECTED),
    ]
    shown = preview_rows([_file(_frame())], items).files[0]
    assert shown.before == shown.after and not any(c.changed for c in shown.columns)


def test_rows_a_step_leaves_out_are_not_in_the_rows_after_and_the_other_columns_are_not_marked() -> None:
    shown = preview_rows([_file(_frame())], [_item(DROP, "i-drop")]).files[0]
    opted = [c.name for c in shown.columns].index("opted_in")
    assert all(row[opted] != "0" for row in shown.after) and any(row[opted] == "0" for row in shown.before)
    assert {c.name for c in shown.columns if c.changed} == set()  # no cell of a kept row was rewritten


def test_a_column_the_plan_removes_is_still_listed_and_marked() -> None:
    drop = _step(RecipeStepKind.DROP_COLUMN, "spend")
    shown = preview_rows([_file(_frame())], [_item(drop, "i-drop-col")]).files[0]
    spend = next(c for c in shown.columns if c.name == "spend")
    assert spend.changed and spend.name == shown.columns[-1].name  # after the columns that stay
    index = [c.name for c in shown.columns].index("spend")
    assert shown.before[0][index] == "0" and shown.after[0][index] == ""


def test_personal_columns_show_no_value_and_other_cells_are_masked() -> None:
    frame = _frame()
    frame["notes"] = [f"call 9876543{i:03d} or mail x{i}@corp.in" for i in range(len(frame))]
    shown = preview_rows([_file(frame)], []).files[0]
    flags = {c.name: c.personal for c in shown.columns}
    assert flags["email"] is True
    email = [c.name for c in shown.columns].index("email")
    assert {row[email] for row in shown.before + shown.after} == {PERSONAL_CELL}
    notes = [c.name for c in shown.columns].index("notes")
    text = " ".join(row[notes] for row in shown.before)
    assert "9876543" not in text and "@corp.in" not in text


def test_a_column_made_out_of_a_personal_one_is_personal_too() -> None:
    split = _step(RecipeStepKind.SPLIT_COLUMN, "email", new_column="mailbox", separator="@", part=0)
    shown = preview_rows([_file(_frame())], [_item(split, "i-split")]).files[0]
    mailbox = next(c for c in shown.columns if c.name == "mailbox")
    assert mailbox.personal and mailbox.changed
    index = [c.name for c in shown.columns].index("mailbox")
    assert {row[index] for row in shown.after} == {PERSONAL_CELL}


def test_a_step_that_cannot_run_on_the_sample_is_a_note_not_an_error() -> None:
    broken = _step(RecipeStepKind.NORMALISE_TEXT, "no_such_column", strip=True)
    shown = preview_rows([_file(_frame())], [_item(broken, "i-broken")]).files[0]
    assert shown.note and "could not run" in shown.note
    assert shown.before == shown.after


def test_each_step_has_a_few_changed_rows_for_the_plan_screen() -> None:
    view = preview_rows([_file(_frame())], [_item(STRIP, "i-strip"), _item(DROP, "i-drop")])
    strip = view.step_samples["i-strip"]
    assert strip.before == ({"city": " Pune "},) * 3 and strip.after == ({"city": "Pune"},) * 3
    dropped = view.step_samples["i-drop"]
    assert len(dropped.before) == 3 and all(row == {"opted_in": "0"} for row in dropped.before)
    assert dropped.after == ()


def test_the_samples_are_not_part_of_the_served_preview() -> None:
    view = preview_rows([_file(_frame())], [_item(STRIP, "i-strip")])
    assert view.step_samples and "step_samples" not in view.model_dump(mode="json")


def test_several_files_each_have_their_own_rows_and_steps() -> None:
    other = _file(_frame(12).rename(columns={"city": "town"}), "up-other", "other.csv")
    step = _item(STRIP, "i-strip")  # about the first file
    view = preview_rows([_file(_frame()), other], [step])
    first, second = view.files
    assert first.upload_id == FIRST and second.upload_id == "up-other"
    assert any(c.changed for c in first.columns) and not any(c.changed for c in second.columns)
    assert second.total_rows == 12 and len(second.before) == 10


def test_a_sampled_file_reports_the_rows_of_the_whole_file() -> None:
    loaded = _file(_frame())
    sampled = LoadedFile(**{**loaded.__dict__, "file_rows": 250_000})
    assert preview_rows([sampled], []).files[0].total_rows == 250_000


def test_the_preview_is_counted_once_per_plan() -> None:
    calls: list[int] = []
    case_files = [vs.case_file(FIRST, vs.world())]
    items = [_item(STRIP, "i-strip")]

    def build() -> Any:
        calls.append(1)
        return preview_rows([_file(_frame())], items)

    first = cached_preview("case-cache", case_files, items, build)
    again = cached_preview("case-cache", case_files, items, build)
    other = cached_preview("case-cache", case_files, [*items, _item(DROP, "i-drop")], build)
    assert first is again and len(calls) == 2 and other is not first


# ---------------------------------------------------------------------------
# The agent's tool
# ---------------------------------------------------------------------------
def _ctx(
    files: list[LoadedFile], items: list[CaseItem] | None, *, access: DataAccess = DataAccess.MASKED_DATA
) -> DataToolContext:
    from engine.agent.tools import AgentContext
    from engine.config import RunMode

    config = load_use_case(GOAL_A)
    config = config.model_copy(update={"agent": config.agent.model_copy(update={"ai_data_access": access})})
    agent = AgentContext(
        use_case_id=config.id,
        config=config,
        config_root=None,
        upload_id=files[0].upload_id,
        mode=RunMode.TRAIN,
        profile=files[0].profile,
        frame=files[0].frame,
    )
    return DataToolContext(
        agent=agent,
        principal=ANALYST_P,
        caller="ai",
        files=tuple(files),
        case_items=None if items is None else tuple(items),
    )


def test_the_tool_is_for_the_ai_and_the_screen_and_needs_the_plan() -> None:
    registry = data_registry()
    files = [_file(_frame())]
    assert registry.find("preview_working_copy", _ctx(files, [])) is not None
    assert (
        registry.find("preview_working_copy", _ctx(files, None)) is None
    )  # the caller did not hand over the plan
    person = DataToolContext(caller="person", files=tuple(files), case_items=())
    rules = DataToolContext(caller="rules", files=tuple(files), case_items=())
    assert registry.find("preview_working_copy", person) is not None
    assert registry.find("preview_working_copy", rules) is None


def test_the_tool_shows_the_agent_its_fix_and_withholds_personal_data() -> None:
    result = data_registry().call(
        "preview_working_copy", {"rows": 3}, _ctx([_file(_frame())], [_item(STRIP, "i-strip")])
    )
    out = result.output
    (shown,) = out["files"]
    assert shown["file"] == "F1" and out["shows_values"] is True
    assert len(shown["before"]["rows"]) == 3 == len(shown["after"]["rows"])
    assert shown["changed_columns"] == ["city"]
    names = shown["before"]["columns"]
    assert names == shown["after"]["columns"]
    city, email = names.index("city"), names.index("email")
    assert shown["before"]["rows"][1][city] == " Pune " and shown["after"]["rows"][1][city] == "Pune"
    cells = shown["before"]["rows"] + shown["after"]["rows"]
    assert {row[email] for row in cells} == {PERSONAL_CELL}
    assert result.exposure.value == "session-only"


def test_the_result_has_the_columns_and_rows_shape_the_egress_gate_reads() -> None:
    """A column the gate hides is hidden in `rows` by position, whatever the tool did."""
    from engine.agent.egress import Egress

    ctx = _ctx([_file(_frame())], [_item(STRIP, "i-strip")])
    out = data_registry().call("preview_working_copy", {"rows": 2}, ctx).output
    gate = Egress.build(_frame().columns, always_hide=["spend"])
    prepared = gate.prepare(out["files"][0]["after"])
    spend = prepared["columns"].index("spend")
    assert all(row[spend] != "1.0" and row[spend] != "1" for row in prepared["rows"])
    assert "Pune" in {cell for row in prepared["rows"] for cell in row}, "the other columns stay readable"


def test_the_tool_shows_no_cell_when_the_settings_let_the_ai_see_summaries_only() -> None:
    result = data_registry().call(
        "preview_working_copy",
        {},
        _ctx([_file(_frame())], [_item(STRIP, "i-strip")], access=DataAccess.SUMMARIES_ONLY),
    )
    (shown,) = result.output["files"]
    assert result.output["shows_values"] is False
    assert shown["before"]["rows"] == [] and shown["after"]["rows"] == []
    assert shown["changed_columns"] == ["city"]


def test_the_tool_stays_under_the_row_level_budget_on_a_wide_file() -> None:
    wide = pd.DataFrame({f"column_{i:02d}": [f"value number {i} {j}" for j in range(30)] for i in range(60)})
    result = data_registry().call("preview_working_copy", {"rows": 10}, _ctx([_file(wide)], []))
    (shown,) = result.output["files"]
    columns = shown["before"]["columns"]
    assert shown["left_out_columns"] > 0 and len(columns) + shown["left_out_columns"] == 60
    assert all(len(row) == len(columns) for row in shown["before"]["rows"])
    named = data_registry().call(
        "preview_working_copy", {"columns": ["column_59"], "rows": 2}, _ctx([_file(wide)], [])
    )
    assert named.output["files"][0]["before"]["columns"] == ["column_59"]


def test_the_tool_refuses_a_column_the_file_does_not_have() -> None:
    with pytest.raises(DataToolError) as refused:
        data_registry().call("preview_working_copy", {"columns": ["nope"]}, _ctx([_file(_frame())], []))
    assert refused.value.code == "TOOL_INPUT_INVALID"


def test_the_tool_hides_a_column_the_settings_hide() -> None:
    ctx = _ctx([_file(_frame())], [])
    assert ctx.agent is not None
    hiding = ctx.agent.config.model_copy(
        update={"agent": ctx.agent.config.agent.model_copy(update={"always_hide_columns": ("city",)})}
    )
    agent = ctx.agent.__class__(**{**ctx.agent.__dict__, "config": hiding})
    hidden = DataToolContext(**{**ctx.__dict__, "agent": agent})
    (shown,) = data_registry().call("preview_working_copy", {}, hidden).output["files"]
    city = shown["before"]["columns"].index("city")
    assert {row[city] for row in shown["before"]["rows"]} == {PERSONAL_CELL}


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------
def _case_with_step(tmp_path: Path) -> tuple[LocalStorage, vs.World, Any]:
    storage = LocalStorage(tmp_path)
    w = vs.world()
    case = vs.open_case(storage, w)
    step = _step(RecipeStepKind.REMOVE_ROWS, "marketing_opt_in", operator="equals", values=[0])
    item = _item(step, "i-optin")
    case = CaseStore(storage).update(
        CLIENT,
        case.case_id,
        expected_version=case.version,
        actor=ANALYST,
        change=lambda c: (c.model_copy(update={"items": (*c.items, item)}), []),
    )
    return storage, w, case


def test_the_screen_serves_the_preview_and_fills_the_steps_samples(tmp_path: Path) -> None:
    storage, w, case = _case_with_step(tmp_path)
    view = build_screen(storage, CLIENT, case.case_id, config_root=None, load_files=w.load).view
    assert view.preview is not None
    (shown,) = view.preview.files
    assert shown.upload_id == FIRST and len(shown.before) == PREVIEW_ROWS
    assert [c.name for c in shown.columns][:2] == ["customer_id", "account_no"]
    opted = [c.name for c in shown.columns].index("marketing_opt_in")
    assert all(row[opted] == "1" for row in shown.after)
    served = view.model_dump(mode="json")
    assert set(served["preview"]["files"][0]) == {
        "upload_id", "file_name", "total_rows", "columns", "before", "after", "note"
    }  # fmt: skip
    assert set(served["preview"]["files"][0]["columns"][0]) == {"name", "changed", "personal"}


def test_the_screen_has_no_preview_when_it_was_given_no_files(tmp_path: Path) -> None:
    storage, _, case = _case_with_step(tmp_path)
    assert build_screen(storage, CLIENT, case.case_id, config_root=None).view.preview is None


def test_a_file_that_cannot_be_read_never_breaks_the_screen(tmp_path: Path) -> None:
    storage, _, case = _case_with_step(tmp_path)

    def unreadable(_: Any) -> Any:
        raise RuntimeError("storage is down")

    screen = build_screen(storage, CLIENT, case.case_id, config_root=None, load_files=unreadable)
    assert screen.view.preview is None and screen.case.case_id == case.case_id


def test_the_ledgers_steps_carry_their_samples_once_the_plan_is_counted(tmp_path: Path) -> None:
    from tests.unit.doctor.test_plan_guards import LOSES, _add_losing_step, _at_approve

    storage, deps, case = _at_approve(tmp_path)
    case = _add_losing_step(storage, deps, case)
    w = vs.world()
    view = build_screen(storage, CLIENT, case.case_id, config_root=None, load_files=w.load).view
    assert view.ledger is not None
    (step,) = [s for s in view.ledger["steps"] if s["item_id"] == LOSES]
    samples = step["samples"]
    assert samples["before"] and all(row == {"marketing_opt_in": "0"} for row in samples["before"])
    assert samples["after"] == []
    assert view.preview is not None and len(view.preview.files[0].after) == PREVIEW_ROWS
