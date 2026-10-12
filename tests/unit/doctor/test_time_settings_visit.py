"""Plan O M244 (DEC-1814) inside the visit: the time settings asked through M246's protocol and remembered per
client (the design §3.5, §7.2).

A Fault Prediction case whose newest month's outcomes are not known yet: the visit's time-settings detector asks
"Leave it out of testing?" (blocking: the readiness probe would fail), offers the training budget as three labelled
choices, and decides the cut points itself (they are feasible). The person's answer about the newest month is
remembered for the client; next month's case applies it, pre-ticked and saying so, and asks nothing - with next
month's own cut-off. Each answer's item carries the change itself, and the prepared copy built from the case passes
the readiness probe the question was about.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine.agent.contracts import ProposalState
from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.case import Case, CaseFile, CaseStore
from engine.doctor.diagnose import Offer
from engine.doctor.standing import StandingAnswerStore
from engine.doctor.time_settings import (
    apply_time_settings,
    read_offer,
    time_setting_changes,
    time_settings_detector,
)
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.readiness_fixtures import report
from tests.unit.doctor.test_time_settings import _loaded, _monthly

GOAL = "fault-prediction"
CLIENT = vs.CLIENT
ANALYST = vs.ANALYST
SEP, OCT = "up-assets-sep", "up-assets-oct"
LABEL = re.compile(r"^(Quick|Balanced|Thorough) \(about \d+ min(, recommended)?\)$")


@pytest.fixture
def world() -> vs.World:
    config = load_use_case(GOAL)
    return vs.World(
        files={
            SEP: _loaded(_monthly(seed=1), config, SEP),
            # Next month's file: May to October, so its newest month (and the cut-off) is a month later.
            OCT: _loaded(_monthly(seed=2, first="2026-05-31"), config, OCT),
        },
        offers=(Offer(config=config, stage="Retain"),),
    )


def _deps(storage: LocalStorage, w: vs.World) -> visit.VisitDeps:
    return vs.deps_for(storage, w, detectors=(time_settings_detector(storage),))


def _open(storage: LocalStorage, w: vs.World, upload_id: str) -> Case:
    created = CaseStore(storage, clock=w.clock).create(
        client_id=CLIENT,
        created_by=ANALYST,
        files=[CaseFile(upload_id=upload_id, file_name="assets.csv", added_at=vs.T0, added_by=ANALYST)],
    )
    deps = _deps(storage, w)
    case = visit.resume(CLIENT, created.case_id, deps, actor=ANALYST)
    for code in ("KEY_AMBIGUOUS",):
        open_q = [q for q in case.questions if q.code == code and q.answer is None]
        if open_q:
            case = visit.answer(
                CLIENT, case.case_id, open_q[0].question_id, vs.option(open_q[0], "asset_id"), deps,
                expected_version=case.version, actor=ANALYST, op_id=f"{upload_id}-key",
            )  # fmt: skip
    return visit.set_goal(
        CLIENT,
        case.case_id,
        GOAL,
        deps,
        expected_version=case.version,
        actor=ANALYST,
        op_id=f"{upload_id}-goal",
    )


def _open_questions(case: Case, code: str) -> list[object]:
    return [q for q in case.questions if q.code == code and q.answer is None]


def test_the_time_questions_go_through_the_protocol_and_the_answer_stands_for_the_client(
    tmp_path: Path, world: vs.World
) -> None:
    storage = LocalStorage(tmp_path / "data")
    case = _open(storage, world, SEP)

    maturity = [q for q in case.questions if q.code == "LABEL_MATURITY"]
    assert len(maturity) == 1 and maturity[0].answer is None
    asked = maturity[0]
    assert asked.text == "Your newest month's outcomes are not final yet. Leave it out of testing?"
    assert asked.blocking and asked.recommended == "leave_out"
    assert [o.label for o in asked.options] == ["Leave it out", "Keep it"]

    # The budget: three labelled choices (no history here, so `check`: one question, Balanced recommended).
    budget = [q for q in case.questions if q.code == "TRAINING_BUDGET"]
    assert len(budget) == 1 and not budget[0].blocking and budget[0].recommended == "balanced"
    assert [o.option_id for o in budget[0].options] == ["quick", "balanced", "thorough"]
    assert all(LABEL.match(o.label) for o in budget[0].options)
    assert read_offer(storage, "upload", SEP, GOAL) is not None, "the offer is kept beside the data"

    # The cut points are feasible: decided by the rules (AUTO), shown, ticked - as the split's own setting.
    cuts = [i for i in case.items if i.payload.get("setting") == "cuts"]
    assert len(cuts) == 1 and cuts[0].state is ProposalState.ACCEPTED and cuts[0].kind == "setting"
    assert cuts[0].payload["path"] == "split.type" and cuts[0].payload["value"] == "time_based"

    deps = _deps(storage, world)
    case = visit.answer(
        CLIENT, case.case_id, asked.question_id, "leave_out", deps,
        expected_version=case.version, actor=ANALYST, op_id="sep-maturity",
    )  # fmt: skip
    left_out = [i for i in case.items if i.payload.get("setting") == "label_maturity"]
    assert len(left_out) == 1 and left_out[0].state is ProposalState.ACCEPTED
    # The item carries the change: the cut-off and the recipe step that leaves the newest month out.
    assert left_out[0].payload["after"] == "2026-08-31" and left_out[0].upload_id == SEP
    assert left_out[0].payload["step"]["kind"] == "remove_rows"
    assert left_out[0].payload["step"]["params"] == {"operator": "after", "values": ["2026-08-31"]}
    # The prepared copy built from the case: X_LABEL_MATURITY, which failed on the file, passes.
    config = load_use_case(GOAL)
    changes = time_setting_changes(case, GOAL)
    assert changes.settings == {"split.type": "time_based"}
    sep = world.files[SEP].frame
    assert report(sep, config).probe("X_LABEL_MATURITY").status == "fail"
    prepared = apply_time_settings(sep, changes, upload_id=SEP, config=config).reset_index(drop=True)
    assert len(prepared) == len(sep) - 300
    assert report(prepared, config).probe("X_LABEL_MATURITY").status == "pass"
    remembered = StandingAnswerStore(storage).list(CLIENT, include_revoked=False)
    assert [a.detector for a in remembered] == ["LABEL_MATURITY"]
    assert remembered[0].option_label == "Leave it out" and remembered[0].answered_by == ANALYST

    # Next month's case of the same client: the answer is applied, pre-ticked, saying so; nothing is asked.
    later = _open(storage, world, OCT)
    assert not _open_questions(later, "LABEL_MATURITY")
    applied = [i for i in later.items if i.payload.get("setting") == "label_maturity"]
    assert len(applied) == 1 and applied[0].state is ProposalState.ACCEPTED
    assert applied[0].standing_answer_id == remembered[0].answer_id
    assert applied[0].title.startswith("Using your answer from ")
    # Next month's own cut-off: the newest month of this file is left out, not September's.
    assert applied[0].payload["after"] == "2026-09-30" and applied[0].upload_id == OCT
    oct_frame = world.files[OCT].frame
    oct_prepared = apply_time_settings(
        oct_frame, time_setting_changes(later, GOAL), upload_id=OCT, config=config
    ).reset_index(drop=True)
    assert len(oct_prepared) == len(oct_frame) - 300
    assert report(oct_prepared, config).probe("X_LABEL_MATURITY").status == "pass"
