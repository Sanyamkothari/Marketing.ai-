"""The plan's tables are kept by what makes them (materialize `_made_key`): the same plan asked for again in one round
is not run again, and any change to an item, the file's cells, the scope or the dates is a new key."""

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
        "replaces": None,
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


def test_the_same_plan_asked_for_again_is_the_same_key() -> None:
    assert _key([STEP, SETTING]) == _key([STEP, SETTING])
    same = _item("i_setting", "setting", {"name": "time_limit_minutes", "value": 20})
    assert _key([STEP, SETTING]) == _key([STEP, same])


def test_any_change_to_an_item_is_a_new_key_even_with_a_stale_digest() -> None:
    other = _item("i_setting", "setting", {"name": "time_limit_minutes", "value": 45})
    assert _key([STEP, SETTING]) != _key([STEP, other])
    stale = _item(
        "i_setting",
        "setting",
        {"name": "time_limit_minutes", "value": 45},
        payload_digest=SETTING.payload_digest,
    )
    assert _key([STEP, SETTING]) != _key([STEP, stale]), "the payload, not its stored digest, makes the key"


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


def test_a_withdrawn_step_is_a_new_key() -> None:
    kept = _item("i_two", "recipe_step", {"step": {"kind": "drop_columns"}})
    gone = _item("i_two", "recipe_step", {"step": {"kind": "drop_columns"}}, state=ProposalState.REJECTED)
    assert _key([STEP, gone]) != _key([STEP, kept])


def test_the_cells_the_scope_the_dates_and_a_step_being_tried_change_it() -> None:
    base = _key([STEP])
    changed = pd.DataFrame({"id": ["a", "b", "c"], "amount": ["1", "2", "4"]})
    assert _key([STEP], changed) != base
    assert _key([STEP], scope=Sample(n=2, seed=1)) != base
    assert _key([STEP], as_of=pd.Timestamp("2011-11-30").date()) != base
    assert _key([STEP], twice=True) != base
    assert _key([STEP], FRAME.copy()) == base  # the same cells in another object
