"""Next month for a client with two files (customers + orders): the comparison and the missing-column check use the
same kind of table as last month (raw with raw, joined with joined), so the columns the approved join builds are not
doubted or called missing when the new month's raw files are read."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from engine.agent.contracts import ProposalState
from engine.doctor import visit
from engine.doctor.carry import last_month
from engine.doctor.case import Case, CaseStore, VisitState
from engine.doctor.cleaning import cleaning_detector
from engine.doctor.combining import combining_detector
from engine.doctor.fingerprint import FingerprintStore, fingerprint_detector
from engine.doctor.protocol import approve_class_of
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.test_combining import _customers, _Day, _one, _orders, _tick_and_approve


def _two_months(tmp_path: Path) -> tuple[_Day, Case]:
    day = _Day(
        tmp_path, {"up-c": ("customers.csv", _customers()), "up-o": ("orders.csv", _orders(strangers=10))}
    )
    day.deps = vs.deps_for(
        day.storage,
        day.world,
        detectors=(
            cleaning_detector(day.world.offers),
            combining_detector(),
            fingerprint_detector(day.storage),
        ),
        combine=day._combine,
    )
    case = day.open()
    return day, _tick_and_approve(day, case, _one(case, "build_dataset"))


def _new_case(day: _Day, customers: pd.DataFrame, orders: pd.DataFrame) -> Case:
    day.world.files["up-c2"] = vs.loaded("up-c2", "customers_m2.csv", customers, day.config)
    day.world.files["up-o2"] = vs.loaded("up-o2", "orders_m2.csv", orders, day.config)
    return CaseStore(day.storage, clock=day.world.clock).create(
        client_id=vs.CLIENT,
        created_by=vs.ANALYST,
        files=[vs.case_file(u, day.world) for u in ("up-c2", "up-o2")],
    )


def _month_two(day: _Day, customers: pd.DataFrame, orders: pd.DataFrame) -> Case:
    case = _new_case(day, customers, orders)
    return visit.resume(vs.CLIENT, case.case_id, day.deps, actor=vs.ANALYST)


def test_the_same_two_files_next_month_carry_the_join_and_ask_nothing(tmp_path: Path) -> None:
    day, first = _two_months(tmp_path)
    case = _month_two(day, _customers(), _orders(strangers=10))
    # the raw customers file is not compared with last month's joined table: no doubt about the columns it builds
    assert not case.doubts and case.visit is not None
    assert case.state is VisitState.FOLLOW_UP and case.approvals[0].text_shown.startswith(
        "Carried on from last month"
    )
    join = next(i for i in case.items if i.kind == "build_dataset")
    assert (
        join.state is ProposalState.ACCEPTED
    )  # "Same as last month": ticked by the rules, not by anyone's click
    (only,) = case.files
    assert only.original_upload_id == "up-c2" and only.upload_id != first.files[0].upload_id
    prints = FingerprintStore(day.storage).prints(vs.CLIENT)
    assert [p.derived for p in prints] == [True, True]  # joined with joined


def test_a_changed_join_still_needs_a_yes(tmp_path: Path) -> None:
    day, _first = _two_months(tmp_path)
    # most orders now belong to nobody on the list: the join leaves out more rows than the protocol allows
    case = _month_two(day, _customers(), _orders(strangers=200))
    join = next(i for i in case.items if i.kind == "build_dataset")
    assert join.state is ProposalState.PENDING and approve_class_of(join.kind, join.payload) is not None
    assert case.state is not VisitState.FOLLOW_UP and not case.approvals

    # another shape of customer file (a column less): not the same join either
    other = _month_two(day, _customers().drop(columns=["age"]), _orders(strangers=10))
    assert next(i for i in other.items if i.kind == "build_dataset").state is ProposalState.PENDING


def _with_steps(day: _Day, case: Case, steps: dict[str, str]) -> None:
    """Last month's plan also held steps on these columns (column to the upload it was on)."""
    template = next(i for i in case.items if i.kind == "key")
    added = tuple(
        template.model_copy(
            update={
                "item_id": f"recipe_step.base.{column}",
                "kind": "recipe_step",
                "upload_id": upload_id,
                "payload": {"step": {"kind": "trim", "column": column}},
            }
        )
        for column, upload_id in steps.items()
    )
    CaseStore(day.storage, clock=day.world.clock).update(
        vs.CLIENT, case.case_id, expected_version=case.version, actor=vs.ANALYST,
        change=lambda c: (c.model_copy(update={"items": (*c.items, *added)}), ()),
    )  # fmt: skip


def test_a_step_on_a_column_the_join_built_is_not_called_missing_but_a_raw_one_is(tmp_path: Path) -> None:
    day, first = _two_months(tmp_path)
    _with_steps(day, first, {"orders_90d": first.files[0].upload_id, "region": "up-c"})

    def missing(customers: pd.DataFrame) -> tuple[str, ...]:
        case = _new_case(day, customers, _orders(strangers=10))
        found = last_month(day.storage, case, [day.world.files["up-c2"], day.world.files["up-o2"]])
        assert found is not None
        return found[1].missing_columns

    assert missing(_customers()) == ()  # `orders_90d` is built by the join: it is not in the raw files
    assert missing(_customers().drop(columns=["region"])) == ("region",)
