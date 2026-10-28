"""`propose_unpivot`: a wide monthly sheet turned into rows by the AI, with the year asked when the names have none."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.agent.contracts import ProposalSource
from engine.agent.unpivot import UnpivotError, UnpivotSpec, find_wide_columns, unpivot
from engine.doctor import visit
from engine.doctor.actions import ACTIONS, action_lines
from engine.doctor.case import Case, CaseQuestion
from engine.doctor.questions import QuestionError, build_ask
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.test_agent_review import Script, _answer_key, _deps, _guard, _opened, _review
from tests.unit.doctor.visit_support import FIRST, World

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _wide(world: World, *, year: str = "") -> World:
    frame = vs.customers(seed=1)
    rng = np.random.default_rng(5)
    for month in MONTHS:
        frame[f"spend_{year}{month}" if not year else f"spend_{year}_{month}"] = np.round(
            rng.gamma(2.0, 100.0, len(frame)), 2
        )
    return World(
        files={FIRST: vs.loaded(FIRST, "customers_oct.csv", frame, world.offers[0].config)},
        offers=world.offers,
    )


def _unpivot(**args: object) -> dict[str, object]:
    return {"action": "propose_unpivot", "file": "F1", "args": args}


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def test_the_action_is_offered_to_the_review_prompt() -> None:
    assert "propose_unpivot" in {a.name for a in ACTIONS}
    assert any(line.startswith('`{"action": "propose_unpivot"') for line in action_lines("review"))


def _asked(case: Case, code: str) -> CaseQuestion:
    return vs.question(case, code)


def test_months_with_no_year_are_offered_through_a_question_per_year(storage: LocalStorage) -> None:
    world = _wide(vs.world())
    client = Script([_unpivot()])
    port = _review(_guard(client))
    case = _answer_key(storage, world, _opened(storage, world, port), port)
    assert not [i for i in case.items if i.source is ProposalSource.AI], "no step before the person answers"
    q = _asked(case, "Q26_UNPIVOT_YEAR")
    assert q.blocking and q.answer is None
    labels = [o.label for o in q.options]
    year = world.clock().year
    assert f"They cover {year}" in labels and any(label.startswith("None of these") for label in labels)
    assert "become" in q.text, "the trial's counts are in the question"
    deps = _deps(storage, world, port)
    case = visit.answer(
        vs.CLIENT, case.case_id, q.question_id, vs.option(q, f"They cover {year}"), deps,
        expected_version=case.version, actor=vs.ANALYST,
    )  # fmt: skip
    (item,) = [i for i in case.items if i.item_id.startswith(q.question_id)]
    step = item.payload["step"]
    assert isinstance(step, dict) and step["kind"] == "unpivot" and step["column"] == "customer_id"
    assert step["params"]["year"] == year and len(step["params"]["value_columns"]) == 12
    assert str(year) in item.title, "the person sees which year was assumed"
    assert item.decided_by == vs.ANALYST, "the step comes from the person's answer"


def test_keeping_the_columns_adds_nothing(storage: LocalStorage) -> None:
    world = _wide(vs.world())
    port = _review(_guard(Script([_unpivot()])))
    case = _answer_key(storage, world, _opened(storage, world, port), port)
    q = _asked(case, "Q26_UNPIVOT_YEAR")
    case = visit.answer(
        vs.CLIENT, case.case_id, q.question_id, vs.option(q, "None of these: keep them as columns"),
        _deps(storage, world, port), expected_version=case.version, actor=vs.ANALYST,
    )  # fmt: skip
    assert not [i for i in case.items if "unpivot" in str(i.payload.get("step"))]


def test_a_year_in_the_names_is_offered_as_one_yes_or_no_question(storage: LocalStorage) -> None:
    world = _wide(vs.world(), year="2025")
    port = _review(_guard(Script([_unpivot()])))
    case = _answer_key(storage, world, _opened(storage, world, port), port)
    q = _asked(case, "Q25_UNPIVOT")
    assert q.blocking and not [i for i in case.items if i.source is ProposalSource.AI]
    case = visit.answer(
        vs.CLIENT, case.case_id, q.question_id, vs.option(q, "Yes, turn them into rows"),
        _deps(storage, world, port), expected_version=case.version, actor=vs.ANALYST,
    )  # fmt: skip
    (item,) = [i for i in case.items if i.item_id.startswith(q.question_id)]
    step = item.payload["step"]
    assert isinstance(step, dict) and "year" not in step["params"]


def test_the_data_policy_lets_the_question_through(storage: LocalStorage) -> None:
    world = _wide(vs.world(), year="2025")
    port = _review(_guard(Script([_unpivot()])))
    case = _answer_key(storage, world, _opened(storage, world, port), port)
    assert _asked(case, "Q25_UNPIVOT").blocking


def test_the_model_cannot_ask_the_unpivot_questions_itself() -> None:
    with pytest.raises(QuestionError):
        build_ask("Q26", {}, rows=10)


def test_the_unpivot_has_a_rule_set_in_the_data_policy() -> None:
    from engine.doctor.policy import load_data_policy

    assert "data.unpivot" in load_data_policy(None).rule_sets


def test_the_given_year_dates_the_months_in_the_rows() -> None:
    frame = pd.DataFrame({"cid": ["a", "b"], **{f"spend_{m}": [1.0, 2.0] for m in MONTHS[:3]}})
    group = find_wide_columns(list(frame.columns))
    assert group is not None and not group.dated
    params = group.params(key="cid", id_columns=["cid"], outcome=None, taken=frame.columns, year=2025)
    rows = unpivot(frame, key="cid", params=params, max_failure_pct=1.0).frame
    assert list(rows["month"].dt.strftime("%Y-%m")) == ["2025-01", "2025-02", "2025-03"] * 2
    with pytest.raises(UnpivotError):
        UnpivotSpec.from_params("cid", {**params, "year": 25})
