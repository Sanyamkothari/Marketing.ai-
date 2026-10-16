"""The Data Doctor combines several files with no AI: a customer list and its orders become one change with plain
counts that needs its own yes, monthly exports that repeat rows become one stack that counts each row once, and a
log that barely meets the list is a question instead of a proposal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.contracts import ProposalState
from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.case import Case, CaseFile, CaseItem, CaseStore, VisitState
from engine.doctor.combine_run import combine_item, upload_ids_of
from engine.doctor.combining import combining_detector
from engine.doctor.contracts import Diagnosis
from engine.doctor.diagnose import Offer
from engine.doctor.protocol import approve_class_of
from engine.storage import LocalStorage
from tests.unit.doctor import visit_support as vs

ASHA = Principal(user_id=vs.ANALYST, username="asha", roles=frozenset({Role.ANALYST}), kind="user")
CUSTOMERS = 300
START = pd.Timestamp("2026-01-01")


def _customers() -> pd.DataFrame:
    rng = np.random.default_rng(1)
    return pd.DataFrame(
        {
            "customer_id": [f"C-{n:05d}" for n in range(1, CUSTOMERS + 1)],
            "region": rng.choice(["North", "South", "East"], CUSTOMERS),
            "age": rng.integers(18, 80, CUSTOMERS),
        }
    )


def _orders() -> pd.DataFrame:
    """The customers C-00001 to C-00270 order, written `1` to `270`; 30 orders are by people not on the list."""
    who = [(n % 270) + 1 for n in range(1200)] + [CUSTOMERS + 1 + n % 10 for n in range(30)]
    days = [(n * 7) % 250 for n in range(len(who))]
    return pd.DataFrame(
        {
            "order_id": [f"O{n}" for n in range(len(who))],
            "customer_id": who,
            "order_date": [(START + pd.Timedelta(days=d)).strftime("%Y-%m-%d") for d in days],
            "amount": [100 + n % 900 for n in range(len(who))],
        }
    )


def _export(first: int, last: int) -> pd.DataFrame:
    rows = range(first, last)
    return pd.DataFrame(
        {
            "order_id": [f"O{n}" for n in rows],
            "customer_id": [1 + n % 50 for n in rows],
            "order_date": [(START + pd.Timedelta(days=n % 28)).strftime("%Y-%m-%d") for n in rows],
            "amount": [100 + n % 7 for n in rows],
        }
    )


class _Day:
    def __init__(self, tmp_path: Path, files: dict[str, tuple[str, pd.DataFrame]]) -> None:
        self.config = load_use_case(vs.GOAL_A)
        self.world = vs.World(
            files={u: vs.loaded(u, name, frame, self.config) for u, (name, frame) in files.items()},
            offers=(Offer(config=self.config, stage="Acquire"),),
        )
        self.storage = LocalStorage(tmp_path)
        self.deps = vs.deps_for(
            self.storage, self.world, detectors=(combining_detector(),), combine=self._combine
        )

    def _combine(self, case: Case, item: CaseItem, actor: str) -> CaseFile:
        """The port the API gives the visit, over the in-memory files: the same builder, the table kept as a file."""
        ids = upload_ids_of(item)
        files = self.world.files
        made = combine_item(
            item,
            Diagnosis.model_validate(case.diagnosis),
            {u: files[u].frame for u in ids},
            {u: files[u].file_name for u in ids},
        )
        new = f"up-combined-{len(files)}"
        files[new] = vs.loaded(new, made.file_name, made.frame, self.config)
        return CaseFile(
            upload_id=new, file_name=made.file_name, original_upload_id=ids[0], added_at=vs.T0, added_by=actor
        )

    def open(self) -> Case:
        store = CaseStore(self.storage, clock=self.world.clock)
        case = store.create(
            client_id=vs.CLIENT,
            created_by=vs.ANALYST,
            files=[vs.case_file(u, self.world) for u in self.world.files],
        )
        return visit.resume(vs.CLIENT, case.case_id, self.deps, actor=vs.ANALYST)

    def agree_and_approve(self, case: Case, item: CaseItem) -> Case:
        case = visit.confirm(
            vs.CLIENT, case.case_id, item.item_id, self.deps, principal=ASHA, sentence=item.title,
            expected_version=case.version,
        )  # fmt: skip
        return visit.approve(
            vs.CLIENT, case.case_id, self.deps, expected_version=case.version, actor=vs.ANALYST
        )


def _one(case: Case, kind: str) -> CaseItem:
    (item,) = [i for i in case.items if i.kind == kind]
    return item


def test_a_customer_list_and_its_orders_are_one_change_with_counts_that_needs_its_own_yes(
    tmp_path: Path,
) -> None:
    day = _Day(tmp_path, {"up-c": ("customers.csv", _customers()), "up-o": ("orders.csv", _orders())})
    case = day.open()
    item = _one(case, "build_dataset")

    # One item, in plain words, with the counts; it is the person's to tick, and needs its own yes.
    assert item.state is ProposalState.PENDING
    assert "270 of 300 customers matched" in item.title
    assert "30 of 1,230 rows in orders.csv belong to someone who is not in customers.csv" in item.title
    assert "customers.csv keeps its own IDs" in item.title
    assert approve_class_of(item.kind, item.payload) is not None
    assert (item.payload["customers"], item.payload["customers_matched"]) == (300, 270)

    case = day.agree_and_approve(case, item)

    # The table that goes on is the new one: one row per customer, the list's own IDs, the orders counted per customer.
    (only,) = case.files
    table = day.world.files[only.upload_id].frame
    assert len(table) == CUSTOMERS and table["customer_id"].is_unique
    assert table["customer_id"].iloc[0] == "C-00001"
    orders = _orders()
    today = pd.to_datetime(orders["order_date"]).max()
    mine = orders[orders["customer_id"] == 1]
    recent = (pd.to_datetime(mine["order_date"]) > today - pd.Timedelta(days=90)).sum()
    assert table["orders_90d"].iloc[0] == recent
    assert table["orders_90d"].iloc[CUSTOMERS - 1] == 0 and pd.isna(table["days_since_last_orders"].iloc[-1])
    assert case.visit is not None and case.visit.combined == (item.item_id,)

    # The Doctor reads the new table from the start; approving it hands that table on.
    assert case.state is VisitState.APPROVE
    assert not [i for i in case.items if i.kind == "build_dataset" and i.item_id != item.item_id]
    case = visit.approve(vs.CLIENT, case.case_id, day.deps, expected_version=case.version, actor=vs.ANALYST)
    assert case.visit is not None and case.visit.hand_off is not None
    assert case.visit.hand_off.upload_ids == (only.upload_id,)


def test_monthly_exports_that_repeat_rows_are_one_stack_that_counts_each_row_once(tmp_path: Path) -> None:
    day = _Day(
        tmp_path,
        {"up-jan": ("orders_jan.csv", _export(0, 200)), "up-feb": ("orders_feb.csv", _export(150, 350))},
    )
    case = day.open()
    item = _one(case, "stack_files")

    assert not [i for i in case.items if i.kind == "build_dataset"], "no customer list: nothing to join"
    assert item.state is ProposalState.ACCEPTED, "same columns: stacked is the recommended default"
    assert "50 rows that two exports both hold are counted once" in item.title
    assert (item.payload["rows_in"], item.payload["rows_out"]) == (400, 350)
    assert (
        approve_class_of(item.kind, item.payload) is not None
    ), "12% of the rows is more than a small change"

    case = day.agree_and_approve(case, item)
    (only,) = case.files
    table = day.world.files[only.upload_id].frame
    assert len(table) == 350 and table["order_id"].is_unique


def test_a_log_that_barely_meets_the_list_is_a_question_not_a_proposal(tmp_path: Path) -> None:
    strangers = _orders().assign(customer_id=lambda f: [f"X{n}" for n in f["customer_id"]])
    day = _Day(tmp_path, {"up-c": ("customers.csv", _customers()), "up-o": ("orders.csv", strangers)})
    case = day.open()

    assert not [i for i in case.items if i.kind in {"build_dataset", "stack_files"}]
    asked = next(q for q in case.questions if q.code == "FILES_DO_NOT_MEET")
    assert "orders.csv is left out" in asked.text and "near match" in asked.text
    assert not asked.blocking and any(o.safe for o in asked.options)


def test_one_customer_on_two_rows_is_a_question_not_a_proposal(tmp_path: Path) -> None:
    twice = pd.concat([_customers(), _customers().head(5)], ignore_index=True)
    day = _Day(tmp_path, {"up-c": ("customers.csv", twice), "up-o": ("orders.csv", _orders())})
    case = day.open()

    assert not [i for i in case.items if i.kind == "build_dataset"]
    assert any(q.code == "CUSTOMER_REPEATS" for q in case.questions)


def test_the_approved_join_is_refused_when_the_files_no_longer_fit_as_approved(tmp_path: Path) -> None:
    day = _Day(tmp_path, {"up-c": ("customers.csv", _customers()), "up-o": ("orders.csv", _orders())})
    case = day.open()
    item = _one(case, "build_dataset")
    changed = item.model_copy(update={"payload": {**item.payload, "rules": "0" * 64}})
    files = day.world.files
    with pytest.raises(Exception, match="COMBINE_CHANGED"):
        combine_item(
            changed,
            Diagnosis.model_validate(case.diagnosis),
            {u: f.frame for u, f in files.items()},
            {u: f.file_name for u, f in files.items()},
        )


def test_stacked_exports_in_different_units_stop_with_a_blocking_question(tmp_path: Path) -> None:
    paise = _export(200, 400).assign(amount=lambda f: f["amount"] * 100)
    day = _Day(tmp_path, {"up-jan": ("orders_jan.csv", _export(0, 200)), "up-feb": ("orders_feb.csv", paise)})
    case = day.open()
    (ask,) = [q for q in case.questions if q.code == "Q06_UNIT_SCALE"]
    assert ask.blocking and ask.column == "amount"
    assert "times larger in orders_feb.csv than in orders_jan.csv" in ask.text
    assert _one(case, "stack_files"), "the stack is still offered; the answer comes before the approval"
    assert _one(case, "stack_files").state is ProposalState.PENDING, "different units: never pre-ticked"


def test_files_with_the_same_columns_are_stacked_and_pre_ticked(tmp_path: Path) -> None:
    files = {"up-jan": ("orders_jan.csv", _export(0, 200)), "up-feb": ("orders_feb.csv", _export(200, 400))}
    item = _one(_Day(tmp_path, files).open(), "stack_files")
    assert item.state is ProposalState.ACCEPTED
    assert item.payload.get("needs_yes") is False


def test_a_stack_the_guard_stops_on_or_cannot_check_is_not_pre_ticked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engine.doctor import combining
    from engine.doctor.guards import Finding
    from engine.doctor.stack_files import StackError

    files = {"up-jan": ("orders_jan.csv", _export(0, 200)), "up-feb": ("orders_feb.csv", _export(200, 400))}
    real = combining.guard_stack

    def stops(*args: Any, **kwargs: Any) -> Any:
        receipt, findings = real(*args, **kwargs)
        return receipt, (*findings, Finding(level="stop", code="STEP_ZERO_ROWS", message="rows were lost"))

    def cannot_run(*args: Any, **kwargs: Any) -> Any:
        raise StackError("STACK_FAILED", "cannot be built")

    monkeypatch.setattr(combining, "guard_stack", stops)
    stopped = _one(_Day(tmp_path / "a", files).open(), "stack_files")
    assert stopped.payload["needs_yes"] is True and stopped.state is ProposalState.PENDING

    monkeypatch.setattr(combining, "guard_stack", cannot_run)
    unchecked = _one(_Day(tmp_path / "b", files).open(), "stack_files")
    assert "needs_yes" not in unchecked.payload and unchecked.state is ProposalState.PENDING
