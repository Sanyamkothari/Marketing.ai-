"""The plan's tables are kept by what makes them (materialize `_made_key`): an answer about a setting does not run
the plan over the whole file again; a step, the ID, the outcome or the file's cells do."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd

from engine.doctor.case import ProposalState
from engine.doctor.materialize import ALIAS_KIND, Full, Sample, _made_key


def _item(item_id: str, kind: str, payload: dict[str, Any], **changes: Any) -> Any:
    fields: dict[str, Any] = {
        "item_id": item_id,
        "kind": kind,
        "state": ProposalState.ACCEPTED,
        "upload_id": "u1",
        "goal_id": None,
        "payload": payload,
        "payload_digest": repr(sorted(payload.items())),
        "decided_by": "person",
        "agreed_rows": None,
        "standing_answer_id": None,
    }
    return SimpleNamespace(**{**fields, **changes})


def _file(frame: pd.DataFrame) -> Any:
    return SimpleNamespace(upload_id="u1", file_name="orders.csv", file_rows=None, frame=frame)


STEP = _item("i_step", "recipe_step", {"step": {"kind": "drop_duplicates"}})
SETTING = _item("i_setting", "setting", {"name": "time_limit_minutes", "value": 20})
FRAME = pd.DataFrame({"id": ["a", "b", "c"], "amount": ["1", "2", "3"]})


def _key(items: list[Any], frame: pd.DataFrame = FRAME, **changes: Any) -> str | None:
    args: dict[str, Any] = {
        "scope": Full(),
        "as_of": None,
        "training_as_of": None,
        "candidate": None,
        "twice": False,
    }
    return _made_key(items, [_file(frame)], **{**args, **changes})


def test_an_answer_about_a_setting_does_not_change_what_the_plan_makes() -> None:
    assert _key([STEP]) == _key([STEP, SETTING])
    other = _item("i_setting", "setting", {"name": "time_limit_minutes", "value": 45})
    assert _key([STEP, SETTING]) == _key([STEP, other])


def test_a_step_an_alias_the_id_the_outcome_or_a_build_change_it() -> None:
    base = _key([STEP])
    more = [
        _item("i_two", "recipe_step", {"step": {"kind": "drop_columns"}}),
        _item("i_alias", ALIAS_KIND, {"column": "ID", "as": "id"}),
        _item("i_key", "key", {"column": "id"}),
        _item("i_target", "outcome_column", {"column": "amount"}),
        _item("i_build", "recipe_step", {"buildable": {"outcome_column": "amount"}}),
    ]
    keys = {_key([STEP, extra]) for extra in more}
    assert base not in keys and len(keys) == len(more)


def test_a_withdrawn_step_is_not_part_of_the_plan() -> None:
    gone = _item("i_two", "recipe_step", {"step": {"kind": "drop_columns"}}, state=ProposalState.REJECTED)
    assert _key([STEP, gone]) == _key([STEP])


def test_the_cells_the_scope_the_dates_and_a_step_being_tried_change_it() -> None:
    base = _key([STEP])
    changed = pd.DataFrame({"id": ["a", "b", "c"], "amount": ["1", "2", "4"]})
    assert _key([STEP], changed) != base
    assert _key([STEP], scope=Sample(n=2, seed=1)) != base
    assert _key([STEP], as_of=pd.Timestamp("2011-11-30").date()) != base
    assert _key([STEP], twice=True) != base
    assert _key([STEP], FRAME.copy()) == base  # the same cells in another object
