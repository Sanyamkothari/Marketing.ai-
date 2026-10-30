"""An approved case is reopened: it shows what was approved and asks nothing that was already answered.

The Data Doctor screen reads the case again every time it opens (`GET /cases/{id}/screen`, after `POST /cases/{id}/
visit` resumes a case resting mid-step). For a case past Approve - the hand-off done, waiting for next month's file -
both must leave every answer where it was: no question comes back unless something it depends on changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.doctor import visit
from engine.doctor.case import CaseStore
from engine.doctor.conversation import FirstLookCard, QuestionCard, ReadyCard
from engine.doctor.screen import build_screen
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.screen_support import scenario
from tests.unit.doctor.test_protocol_visit import _deps, _found, _remove_rows_ask
from tests.unit.doctor.visit_support import ANALYST, CLIENT, World


@pytest.fixture
def world() -> World:
    return vs.world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def _answers(screen_cards: tuple[object, ...]) -> dict[str, str | None]:
    return {c.question_id: c.answer for c in screen_cards if isinstance(c, QuestionCard)}


def test_reopening_an_approved_case_asks_nothing_that_was_answered(
    storage: LocalStorage, world: World
) -> None:
    approved = scenario("ready", storage, world)
    case_id = approved.case.case_id
    before = _answers(approved.view.conversation)
    assert before and all(a is not None for a in before.values()), "the walk answered every question"
    assert approved.case.state.value in {"hand_off", "follow_up"}

    # The screen opens: the visit resumes the case where it rests (as `POST /cases/{id}/visit`), then it is read.
    detector = _found(_remove_rows_ask())
    visit.resume(CLIENT, case_id, _deps(storage, world, detector), actor=ANALYST)
    again = build_screen(storage, CLIENT, case_id, config_root=None)

    assert _answers(again.view.conversation) == before, "every answer is still there, none is asked again"
    assert not [q for q in again.case.questions if q.answer is None and q.blocking]
    first = next(c for c in again.view.conversation if isinstance(c, FirstLookCard))
    assert first.questions_count == 0
    assert again.view.verdict.key in {"ready", "ready_with_warnings"}
    ready = again.view.conversation[-1]
    assert isinstance(ready, ReadyCard) and ready.stage == "start"
    was = approved.view.conversation[-1]
    assert (
        isinstance(was, ReadyCard) and ready.sentence == was.sentence
    ), "what was approved is shown, word for word"
    assert again.case.version == approved.case.version, "reopening changes nothing in the case"


def test_a_screen_read_twice_is_the_same_screen(storage: LocalStorage, world: World) -> None:
    approved = scenario("ready", storage, world)
    store = CaseStore(storage)
    first = build_screen(storage, CLIENT, approved.case.case_id, config_root=None)
    second = build_screen(storage, CLIENT, approved.case.case_id, config_root=None)
    assert first.view == second.view
    assert store.get(CLIENT, approved.case.case_id).version == approved.case.version


def test_the_first_look_tells_the_needed_questions_from_the_optional_ones(
    storage: LocalStorage, world: World
) -> None:
    """The header counts the questions that stop the check; the first look counts all of them. It says which are needed."""
    from engine.doctor.conversation import _first_look

    case = scenario("ready", storage, world).case
    mixed = _first_look(case, 1, 3, (), needed=2).sentence
    assert "3 questions are only yours to answer. 2 are needed to go on; the other is optional." in mixed
    one = _first_look(case, 1, 2, (), needed=1).sentence
    assert "1 is needed to go on; the other is optional." in one
    all_needed = _first_look(case, 1, 2, (), needed=2).sentence
    assert (
        "needed to go on" not in all_needed
    ), "when every question is needed the count is the same everywhere"


def test_a_step_title_with_a_full_stop_does_not_double_it() -> None:
    from engine.doctor.protocol import AskOption, _stated
    from engine.doctor.visit_contracts import OptionEffect

    option = AskOption(
        label="Leave them out",
        effect=OptionEffect(option_id="x", kind="remove_rows", title="Use 2011-11-30 and leave them out."),
    )
    assert _stated(option) == "Adds the step: Use 2011-11-30 and leave them out."
