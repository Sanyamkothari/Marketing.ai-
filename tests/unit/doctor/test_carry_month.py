"""A new month, the same kind of file (`engine.doctor.carry.last_month`): the steps approved last month are carried
again without asking, in the name of who approved; only what is new or changed is open, and a step that cannot apply,
a step a person left out, consent and outcome, and anything with a stop or flag are asked."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from engine.agent.contracts import ProposalState
from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.carry import carries_on
from engine.doctor.case import Case, CaseStore, VisitState
from engine.doctor.cleaning import cleaning_detector
from engine.doctor.diagnose import Offer
from engine.doctor.fingerprint import fingerprint_detector
from engine.doctor.screen import screen_view
from engine.doctor.visit import active_section, confirmed
from engine.doctor.visit_contracts import PlanCheck
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs

ROWS = 400


def _month(month: int, *, rename: bool = False) -> pd.DataFrame:
    rng = np.random.default_rng(month)
    frame = pd.DataFrame(
        {
            "customer_id": [f"C{1000 + i}" for i in range(ROWS)],
            "spend": [f"₹{1200 + 3 * i:,}" for i in range(ROWS)],
            "joined": [f"{1 + i % 28:02d}/{month:02d}/2026" for i in range(ROWS)],
            "region": rng.choice(["North", "South", "East"], ROWS),
            "age": rng.integers(18, 80, ROWS),
        }
    )
    return frame.rename(columns={"spend": "total_spend"}) if rename else frame


class _Months:
    def __init__(self, tmp_path: Path) -> None:
        self.config = load_use_case(vs.GOAL_A)
        self.world = vs.World(files={}, offers=(Offer(config=self.config, stage="Acquire"),))
        self.storage = LocalStorage(tmp_path)
        self.deps = vs.deps_for(
            self.storage,
            self.world,
            detectors=(cleaning_detector(self.world.offers), fingerprint_detector(self.storage)),
        )

    def open(self, month: int, **kwargs: bool) -> Case:
        name = f"up-{month}"
        self.world.files[name] = vs.loaded(name, f"month{month}.csv", _month(month, **kwargs), self.config)
        case = CaseStore(self.storage, clock=self.world.clock).create(
            client_id=vs.CLIENT, created_by=vs.ANALYST, files=[vs.case_file(name, self.world)]
        )
        return visit.resume(vs.CLIENT, case.case_id, self.deps, actor=vs.ANALYST)

    def month_one(self, *, leave_out: str | None = None) -> Case:
        case = self.open(1)
        if leave_out is not None:
            item = next(i for i in case.items if i.payload.get("step", {}).get("column") == leave_out)
            case = visit.decide(
                vs.CLIENT, case.case_id, item.item_id, False, self.deps,
                expected_version=case.version, actor=vs.ANALYST,
            )  # fmt: skip
        case = visit.approve(
            vs.CLIENT, case.case_id, self.deps, expected_version=case.version, actor=vs.ANALYST
        )
        assert case.state is VisitState.FOLLOW_UP
        return case


def test_the_same_kind_of_file_next_month_asks_nothing_and_says_whose_approval_it_goes_on_from(
    tmp_path: Path,
) -> None:
    months = _Months(tmp_path)
    first = months.month_one()
    steps = [i for i in first.items if i.kind == "recipe_step"]
    assert steps  # money as text, day/month dates: the rules' sure fixes, approved once

    case = months.open(2)
    assert case.case_id != first.case_id and case.state is VisitState.FOLLOW_UP
    assert case.visit is not None and case.visit.carry_source is not None
    assert case.visit.carry_source.case_id == first.case_id
    (approval,) = case.approvals
    assert approval.approved_by == vs.ANALYST and approval.text_shown.startswith("Carried on from")
    events = CaseStore(months.storage).timeline(vs.CLIENT, case.case_id)
    (carried,) = [e for e in events if e.kind.value == "approval"]
    assert carried.details["carried"] is True and carried.details["last_month"] is True
    assert carried.details["carried_from"] == f"{first.approvals[-1].approval_id}@{first.case_id}"


def test_a_renamed_column_is_not_carried_and_the_rest_is_folded_as_the_same_as_last_month(
    tmp_path: Path,
) -> None:
    months = _Months(tmp_path)
    months.month_one()
    case = months.open(2, rename=True)
    assert case.state is not VisitState.FOLLOW_UP and not case.approvals  # a person looks
    assert case.visit is not None and case.visit.carry_source is not None
    assert case.visit.carry_source.missing_columns == ("spend",)

    view = screen_view(months.storage, case, config_root=None)
    assert view.carried is not None and view.carried.last_month
    by_id = {i.item_id: i for i in case.items}
    folded = {by_id[i].payload["step"]["column"] for i in view.carried.approved if "step" in by_id[i].payload}
    assert "joined" in folded and "total_spend" not in folded and "spend" not in folded
    assert view.carried.line is not None and "‘spend’" in view.carried.line
    assert "total_spend" in {  # the renamed column's step is open, with the Doctor's own "may be renamed"
        i.payload["step"]["column"]
        for i in case.items
        if i.item_id not in view.carried.approved and "step" in i.payload
    }
    assert any("renamed" in d.because for d in case.doubts)


def test_consent_a_step_a_person_left_out_and_a_stop_are_asked_again_next_month(tmp_path: Path) -> None:
    months = _Months(tmp_path)
    months.month_one(leave_out="joined")
    case = months.open(2)
    assert case.state is not VisitState.FOLLOW_UP and not case.approvals  # the date fix was left out: asked
    items, questions = active_section(case)
    check = PlanCheck(digest="x")
    again = next(i for i in items if i.payload.get("step", {}).get("column") == "joined")
    assert again.state is ProposalState.ACCEPTED  # the rules would tick it again, but nobody carries it on
    assert carries_on(case, items, questions, check, confirmed=confirmed) is None

    consent = again.model_copy(
        update={"item_id": "consent_column.g.x", "kind": "consent_column", "payload": {"column": "opt_in"}}
    )
    others = tuple(i for i in items if i.item_id != again.item_id)
    assert carries_on(case, (*others, consent), questions, check, confirmed=confirmed) is None


def test_another_kind_of_table_does_not_start_from_last_months_approval(tmp_path: Path) -> None:
    months = _Months(tmp_path)
    months.month_one()
    name = "up-other"
    other = pd.DataFrame({"sku": [f"S{i}" for i in range(ROWS)], "price": range(ROWS), "stock": range(ROWS)})
    months.world.files[name] = vs.loaded(name, "stock.csv", other, months.config)
    case = CaseStore(months.storage).create(
        client_id=vs.CLIENT, created_by=vs.ANALYST, files=[vs.case_file(name, months.world)]
    )
    case = visit.resume(vs.CLIENT, case.case_id, months.deps, actor=vs.ANALYST)
    assert case.visit is not None and case.visit.carry_source is None and not case.visit.approved_before
    assert not case.approvals
