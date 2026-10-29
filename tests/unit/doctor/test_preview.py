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
from engine.doctor.preview import MAX_COLUMNS, PERSONAL_CELL, PREVIEW_ROWS, cached_preview, preview_rows
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

    first = cached_preview("c1", "case-cache", case_files, items, build)
    again = cached_preview("c1", "case-cache", case_files, items, build)
    other = cached_preview("c1", "case-cache", case_files, [*items, _item(DROP, "i-drop")], build)
    assert first is again and len(calls) == 2 and other is not first
    cached_preview("c2", "case-cache", case_files, items, build)
    assert len(calls) == 3, "another client's case is never served this client's rows"


def _sql(text: str, order: int = 1, inputs: tuple[str, ...] = ("this", "people")) -> RecipeStep:
    return _step(
        RecipeStepKind.SQL_QUERY,
        "out",
        order,
        sql=text,
        inputs=list(inputs),
        output_name="out",
        description="Test query.",
    )


def _people_case() -> tuple[LoadedFile, LoadedFile]:
    """customers.csv holds no personal column; people.csv (a second file of the case) holds names and numbers."""
    customers = pd.DataFrame(
        {"customer_id": [f"C{i}" for i in range(30)], "city": ["Pune", "Delhi"] * 15, "amount": range(30)}
    )
    people = pd.DataFrame(
        {
            "customer_id": [f"C{i}" for i in range(30)],
            "full_name": [f"Asha Rao {chr(65 + i % 26)}" for i in range(30)],
            "mobile": [f"98765{i:05d}" for i in range(30)],
        }
    )
    return _file(customers), _file(people, "up-people", "people.csv")


def _clear_cells(file: Any) -> str:
    return " ".join(cell for row in file.before + file.after for cell in row)


def test_a_join_that_brings_names_in_from_another_file_shows_none_of_them() -> None:
    first, second = _people_case()
    join = "SELECT c.*, p.full_name AS who, p.mobile AS m FROM t_this c JOIN t_people p USING (customer_id)"
    view = preview_rows([first, second], [_item(_sql(join), "i-join")], only=FIRST)
    (shown,) = view.files
    assert shown.note is None
    flags = {c.name: c.personal for c in shown.columns}
    assert flags["who"] and flags["m"] and not flags["city"]
    assert "Asha" not in _clear_cells(shown) and "98765" not in _clear_cells(shown)
    assert PERSONAL_CELL in _clear_cells(shown)


def test_a_query_that_puts_names_under_another_columns_name_shows_none_of_them() -> None:
    first, second = _people_case()
    replace = (
        "SELECT * REPLACE (p.full_name AS customer_id) FROM t_this c JOIN t_people p USING (customer_id)"
    )
    view = preview_rows([first, second], [_item(_sql(replace), "i-replace")], only=FIRST)
    (shown,) = view.files
    assert (
        "Asha" not in _clear_cells(shown)
        and next(c for c in shown.columns if c.name == "customer_id").personal
    )
    own = "SELECT * REPLACE (full_name AS customer_id) FROM t_this"
    alone = preview_rows(
        [second], [_item(_sql(own, inputs=("this",)), "i-own", upload_id="up-people")]
    ).files[0]
    assert alone.note is None and "Asha" not in _clear_cells(alone)
    assert next(c for c in alone.columns if c.name == "customer_id").personal


def test_personal_values_found_in_a_column_by_value_alone_are_withheld() -> None:
    first, second = _people_case()
    copy = _step(RecipeStepKind.SPLIT_COLUMN, "city", new_column="town", separator="~", part=0)
    # a column whose values are another file's personal values, whatever put them there, is personal
    first = LoadedFile(**{**first.__dict__, "frame": first.frame.assign(city=second.frame["full_name"])})
    view = preview_rows([first, second], [_item(copy, "i-copy")], only=FIRST)
    town = next(c for c in view.files[0].columns if c.name == "town")
    index = [c.name for c in view.files[0].columns].index("town")
    assert town.personal and {row[index] for row in view.files[0].after} == {PERSONAL_CELL}


def test_the_tool_does_not_hand_the_ai_names_a_query_joined_in() -> None:
    first, second = _people_case()
    join = "SELECT c.*, p.full_name AS who, p.mobile AS m FROM t_this c JOIN t_people p USING (customer_id)"
    ctx = _ctx([first, second], [_item(_sql(join), "i-join")])
    out = data_registry().call("preview_working_copy", {"rows": 5}, ctx).output
    text = str(out)
    assert "Asha" not in text and "98765" not in text and PERSONAL_CELL in text


def test_a_plain_row_filter_after_a_query_marks_no_column_changed() -> None:
    first, _ = _people_case()
    keep = "SELECT * FROM t_this WHERE amount > 5"
    shown = preview_rows([first], [_item(_sql(keep, inputs=("this",)), "i-keep")]).files[0]
    assert shown.note is None and len(shown.after) == PREVIEW_ROWS
    assert not any(c.changed for c in shown.columns)
    assert all(int(row[2]) > 5 for row in shown.after)


def test_a_query_that_rewrites_a_cell_marks_that_column_changed_by_the_id_column() -> None:
    first, _ = _people_case()
    rewrite = "SELECT customer_id, city, amount * 2 AS amount FROM t_this WHERE amount > 5"
    shown = preview_rows([first], [_item(_sql(rewrite, inputs=("this",)), "i-rw")]).files[0]
    assert shown.note is None and {c.name for c in shown.columns if c.changed} == {"amount"}


def test_a_wide_file_serves_the_changed_columns_first_and_says_how_many_it_left_out() -> None:
    wide = pd.DataFrame({f"c{i:03d}": [f"v{i}"] * 12 for i in range(300)})
    wide["c299"] = ["  x "] * 12
    strip = _step(RecipeStepKind.NORMALISE_TEXT, "c299", strip=True)
    shown = preview_rows([_file(wide)], [_item(strip, "i-strip")]).files[0]
    names = [c.name for c in shown.columns]
    assert len(names) == MAX_COLUMNS == 60 and shown.left_out_columns == 240
    assert "c299" in names and next(c for c in shown.columns if c.name == "c299").changed
    assert all(len(row) == 60 for row in shown.before + shown.after)


def test_a_step_that_breaks_in_an_unexpected_way_is_a_note_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import engine.doctor.preview as preview

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(preview, "materialize_plan", boom)
    shown = preview_rows([_file(_frame())], [_item(STRIP, "i-strip")]).files[0]
    assert shown.note and shown.before == shown.after


def test_the_tool_refuses_instead_of_ending_the_review_when_the_preview_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import engine.doctor.tools_preview as tool

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(tool, "preview_rows", boom)
    with pytest.raises(DataToolError) as refused:
        data_registry().call("preview_working_copy", {}, _ctx([_file(_frame())], []))
    assert refused.value.code == "PREVIEW_UNAVAILABLE"


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
        "upload_id", "file_name", "total_rows", "columns", "before", "after", "left_out_columns", "note", "after_table", "after_rows"
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


def test_a_date_column_is_shown_as_dates_not_as_nanoseconds() -> None:
    from engine.doctor.preview import masked_rows

    frame = pd.DataFrame({"when": pd.to_datetime(["2026-04-14", "2026-07-21", None])})
    rows = masked_rows(frame, ("when",), (), 5)
    assert rows == (("2026-04-14",), ("2026-07-21",), ("",))


def test_a_length_of_time_is_shown_as_a_length_not_as_nanoseconds() -> None:
    from engine.doctor.preview import masked_rows

    frame = pd.DataFrame({"gap": pd.to_timedelta(["1 days", "90 minutes", None])})
    rows = masked_rows(frame, ("gap",), (), 5)
    assert rows == (("1 days 00:00:00",), ("0 days 01:30:00",), ("",))


def test_a_plan_that_builds_a_table_says_which_table_the_after_rows_are() -> None:
    """After a build the rows shown are the new table's, not the file's: the preview names it."""
    first, _ = _people_case()
    per_customer = "SELECT customer_id, count(*) AS n FROM t_this GROUP BY customer_id"
    built = preview_rows([first], [_item(_sql(per_customer, inputs=("this",)), "i-build")])
    (shown,) = built.files
    assert shown.after_table == "out"
    plain = preview_rows([first], [_item(STRIP, "i-strip")])
    assert plain.files[0].after_table is None
