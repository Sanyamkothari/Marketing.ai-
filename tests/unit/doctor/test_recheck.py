"""Next month's recheck: the approved plan replays through the one materialisation, and only what changed is asked."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.contracts import ProposalState, RecipeStep, RecipeStepKind
from engine.config import load_use_case
from engine.doctor.case import Case, CaseApproval, CaseFile, CaseItem, CaseStore, VisitState, payload_digest
from engine.doctor.decisions import decisions
from engine.doctor.diagnose import LoadedFile
from engine.doctor.materialize import Full, materialize
from engine.doctor.recheck import (
    RecheckError,
    moved_dates,
    open_recheck_case,
    read_report,
    recheck,
)
from engine.stages.ingest import profile_dataset
from engine.storage import LocalStorage

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
CONFIGS = Path(__file__).resolve().parents[3] / "configs"
OLD_START = date(2024, 1, 1)
ROWS = 3000


def month(
    start: date, *, seed: int, rate: float = 0.2, amount: str = "amount", spend_empty: float = 0.02
) -> pd.DataFrame:
    """A customer file: IDs, a text amount, a region, the day each customer joined and a yes/no outcome."""
    rng = np.random.default_rng(seed)
    days = [start + timedelta(days=int(n)) for n in rng.integers(0, 150, ROWS)]
    spend = [f"{x:.2f}" for x in rng.uniform(5, 500, ROWS)]
    for n in rng.choice(ROWS, int(ROWS * spend_empty), replace=False):
        spend[int(n)] = ""
    return pd.DataFrame(
        {
            "customer_id": [f"C{seed}-{n}" for n in range(ROWS)],
            amount: spend,
            "region": rng.choice(["north", "south", "east", "west"], ROWS),
            "joined": [d.isoformat() for d in days],
            "churned": (rng.random(ROWS) < rate).astype(int),
        }
    )


def loaded(frame: pd.DataFrame, upload_id: str, name: str = "customers.csv") -> LoadedFile:
    profile = profile_dataset(
        frame,
        load_use_case("rca", CONFIGS),
        upload_id=upload_id,
        file_name=name,
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame),
    )
    return LoadedFile(upload_id=upload_id, file_name=name, frame=frame, profile=profile)


def item(item_id: str, kind: str, payload: dict[str, Any]) -> CaseItem:
    return CaseItem.model_validate(
        {
            "item_id": item_id,
            "kind": kind,
            "source": "rules",
            "confidence": "sure",
            "state": ProposalState.ACCEPTED.value,
            "upload_id": "up-old",
            "payload": payload,
            "payload_digest": payload_digest(payload),
            "proposed_at": NOW,
            "decided_by": "asha",
            "decided_at": NOW,
        }
    )


def ready_case(*, window: int | None = None) -> Case:
    parse = RecipeStep(
        order=1, kind=RecipeStepKind.PARSE_NUMBER, column="amount", params={}, reason="read the amount"
    )
    items = [
        item("item_parse", "recipe_step", {"step": parse.model_dump(mode="json")}),
        item("item_key", "key", {"column": "customer_id"}),
        item("item_out", "outcome_column", {"column": "churned", "upload_id": "up-old"}),
        item("item_when", "as_of_column", {"column": "joined", "upload_id": "up-old"}),
        item("item_dates", "setting", {"as_of": "2024-06-01", "training_as_of": "2024-03-01"}),
    ]
    if window is not None:
        items.append(item("item_window", "setting", {"path": "outcome.window_days", "value": window}))
    return Case(
        case_id="case_0123456789abcdef",
        client_id="default",
        created_by="asha",
        created_at=NOW,
        updated_at=NOW,
        state=VisitState.FOLLOW_UP,
        version=3,
        title="Churn",
        files=(CaseFile(upload_id="up-old", file_name="customers.csv", added_at=NOW, added_by="asha"),),
        items=tuple(items),
        approvals=(
            CaseApproval(
                approval_id="ap1",
                item_ids=tuple(i.item_id for i in items),
                approved_by="asha",
                approved_at=NOW,
            ),
        ),
    )


def run(new: pd.DataFrame, case: Case | None = None) -> Any:
    case = case or ready_case()
    old = loaded(month(OLD_START, seed=1), "up-old")
    return recheck(case, {"up-old": "up-new"}, [old], [loaded(new, "up-new")], now=NOW)


# -- the cases the task names -----------------------------------------------------------------------------------
def test_a_same_shape_file_passes_with_zero_questions() -> None:
    result = run(month(OLD_START + timedelta(days=30), seed=2))
    report = result.report
    assert report.passed, [f.text for f in report.findings]
    assert report.questions == () and report.stops == ()
    # the outcome says what it means again, and the dates item moved with the data: both wait for a yes
    assert report.kept_items == 3 and report.asked_again == 2
    assert [q for q in result.questions if q.answer is None] == []


def test_a_renamed_column_asks_exactly_one_question() -> None:
    new = month(OLD_START + timedelta(days=30), seed=2, amount="bill_amount")
    result = run(new)
    report = result.report
    assert [f.code for f in report.questions] == ["COLUMN_RENAMED"]
    assert report.stops == ()
    (question,) = [q for q in result.questions if q.answer is None]
    assert question.column == "amount" and question.code == "RECHECK_COLUMN_RENAMED"
    assert report.questions[0].subject == "question@RECHECK_COLUMN_RENAMED:amount#up-new"
    # the yes adds one alias item; nothing else about the approved plan changes
    yes, no = result.offers[question.question_id]
    assert yes.kind == "column_alias" and yes.payload == {
        "column": "bill_amount",
        "as": "amount",
        "upload_id": "up-new",
    }
    assert no.kind is None


def test_a_big_outcome_rate_shift_is_a_stop() -> None:
    report = run(month(OLD_START + timedelta(days=30), seed=2, rate=0.6)).report
    stops = report.stops
    assert [f.code for f in stops] == ["OUTCOME_RATE_SHIFT"]
    assert stops[0].subject == "data_check@OUTCOME_RATE_SHIFT:up-new:churned"
    assert not report.passed


def test_an_empty_share_that_jumps_and_a_new_category_are_asked() -> None:
    new = month(OLD_START + timedelta(days=30), seed=2, spend_empty=0.4)
    new.loc[:300, "region"] = "central"
    report = run(new).report
    assert "NULL_RATE_JUMP" in {f.code for f in report.questions}
    # a new value in a closed column stops the replay (the prescription contract's rule); it is not a yes-able question
    assert "NEW_CATEGORY" in {f.code for f in report.stops}


def test_too_few_rows_is_a_hard_stop() -> None:
    report = run(month(OLD_START + timedelta(days=30), seed=2).head(400)).report
    hard = [f for f in report.stops if f.code == "TOO_FEW_ROWS"]
    assert hard and hard[0].hard


def test_a_column_the_plan_reads_that_is_gone_is_a_stop_but_an_unused_one_is_a_question() -> None:
    new = month(OLD_START + timedelta(days=30), seed=2).drop(columns=["amount", "region"])
    report = run(new).report
    by_column = {f.column: f for f in report.findings if f.code == "COLUMN_MISSING"}
    assert by_column["amount"].level == "stop"
    assert by_column["region"].level == "question"


def test_a_new_column_is_said_not_asked() -> None:
    new = month(OLD_START + timedelta(days=30), seed=2)
    new["coupon"] = 1
    report = run(new).report
    assert [(f.code, f.level) for f in report.findings] == [("COLUMN_ADDED", "note")]
    assert report.passed


# -- dates ------------------------------------------------------------------------------------------------------
def test_the_dates_move_with_the_data() -> None:
    dates = run(month(OLD_START + timedelta(days=30), seed=2)).report.dates
    assert dates.moved
    shift = (dates.data_end_after - dates.data_end_before).days
    assert shift > 0
    assert dates.as_of_after == min(
        date(2024, 6, 1) + timedelta(days=shift), dates.data_end_after + timedelta(days=1)
    )
    assert dates.training_as_of_after <= dates.data_end_after


def test_a_file_that_is_not_newer_moves_nothing_and_asks() -> None:
    result = run(month(OLD_START, seed=2))
    assert not result.report.dates.moved
    assert result.report.dates.training_as_of_after == date(2024, 3, 1)
    assert "DATE_RANGE_NOT_FORWARD" in {f.code for f in result.report.questions}


def test_the_snapshot_never_reads_the_future() -> None:
    end_before, end_after = date(2024, 5, 30), date(2024, 6, 29)
    moved = moved_dates(
        end_before=end_before,
        end_after=end_after,
        as_of=date(2024, 6, 1),
        training=date(2024, 3, 1),
        window=90,
    )
    assert moved.as_of_after <= end_after + timedelta(days=1)  # type: ignore[operator]
    assert moved.training_as_of_after + timedelta(days=90) <= end_after  # type: ignore[operator]
    # the old snapshot was valid for the old data; held to the window it is never pushed past the new data
    far = moved_dates(
        end_before=end_before,
        end_after=date(2024, 6, 2),
        as_of=date(2024, 6, 1),
        training=date(2024, 5, 20),
        window=30,
    )
    assert far.training_as_of_after + timedelta(days=30) <= date(2024, 6, 2)  # type: ignore[operator]


def test_a_query_that_froze_the_old_snapshot_moves_with_it() -> None:
    sql = (
        "SELECT customer_id, churned, joined, amount, region, "
        "count(*) FILTER (WHERE CAST(joined AS DATE) > $as_of) AS later FROM t_customers GROUP BY ALL"
    )
    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.SQL_QUERY,
        column="outcome",
        params={
            "sql": sql,
            "inputs": ["customers"],
            "output_name": "outcome",
            "description": "d",
            "as_of": "2024-03-01",
        },
        reason="r",
    )
    case = ready_case()
    case = case.model_copy(
        update={"items": (*case.items, item("item_q", "recipe_step", {"step": step.model_dump(mode="json")}))}
    )
    result = run(month(OLD_START + timedelta(days=30), seed=2), case)
    moved = next(i for i in result.items if i.item_id == "item_q")
    assert moved.payload["step"]["params"]["as_of"] == result.report.dates.training_as_of_after.isoformat()
    # and the decisions the follow-up case reads are the moved ones
    follow = decisions(case.model_copy(update={"items": result.items, "questions": result.questions}))
    assert follow.training_as_of_date == result.report.dates.training_as_of_after


# -- the one materialisation ------------------------------------------------------------------------------------
def test_the_replay_is_the_materialisation_with_an_accepted_alias() -> None:
    case = ready_case()
    new = loaded(month(OLD_START + timedelta(days=30), seed=2, amount="bill_amount"), "up-new")
    alias = item("alias_1", "column_alias", {"column": "bill_amount", "as": "amount", "upload_id": "up-old"})
    plan = materialize(
        case.model_copy(update={"files": case.files}),
        [*case.items, alias],
        [loaded(new.frame, "up-old")],
        Full(),
    )
    assert not plan.failed
    assert "bill_amount" not in plan.tables["customers"].columns
    assert pd.api.types.is_numeric_dtype(plan.tables["customers"]["amount"])  # the approved step ran on it


def test_a_case_that_is_not_ready_is_refused() -> None:
    case = ready_case().model_copy(update={"state": VisitState.ASK})
    with pytest.raises(RecheckError) as refused:
        run(month(OLD_START + timedelta(days=30), seed=2), case)
    assert refused.value.code == "RECHECK_NOT_READY"


def test_files_must_match_the_case() -> None:
    old = loaded(month(OLD_START, seed=1), "up-old")
    with pytest.raises(RecheckError) as refused:
        recheck(
            ready_case(), {"other": "up-new"}, [old], [loaded(month(OLD_START, seed=2), "up-new")], now=NOW
        )
    assert refused.value.code == "RECHECK_FILES_MISMATCH"


# -- the follow-up case -----------------------------------------------------------------------------------------
def test_the_follow_up_case_keeps_approvals_and_links_back(tmp_path: Path) -> None:
    store = CaseStore(LocalStorage(tmp_path))
    source = store.create(
        client_id="default",
        created_by="asha",
        title="Churn",
        files=[CaseFile(upload_id="up-old", file_name="customers.csv", added_at=NOW, added_by="asha")],
    )
    case = ready_case().model_copy(
        update={"case_id": source.case_id, "version": source.version, "created_at": source.created_at}
    )
    new = month(OLD_START + timedelta(days=30), seed=2, amount="bill_amount")
    old = loaded(month(OLD_START, seed=1), "up-old")
    result = recheck(case, {"up-old": "up-new"}, [old], [loaded(new, "up-new")], now=NOW)
    made = open_recheck_case(
        store,
        case,
        result,
        [CaseFile(upload_id="up-new", file_name="customers_next.csv", added_at=NOW, added_by="asha")],
        actor="asha",
    )
    assert made.case_id != case.case_id
    assert made.recheck_of is not None and made.recheck_of.source_case_id == case.case_id
    assert made.upload_ids() == ("up-new",)
    assert made.state is VisitState.ASK  # one open question
    assert [i.item_id for i in made.items] == [i.item_id for i in case.items]
    # the outcome's meaning is asked again; the rest keeps its approval
    assert {i.item_id for i in made.items if i.state is ProposalState.PENDING} == {"item_out"}
    assert all(i.decided_by == "asha" for i in made.items if i.state is ProposalState.ACCEPTED)
    assert all(i.upload_id == "up-new" for i in made.items)
    assert made.approvals == case.approvals
    assert made.visit is not None and made.visit.taken_in == ("up-new",)
    assert len([q for q in made.questions if q.answer is None]) == 1
    assert store.get("default", case.case_id).upload_ids() == ("up-old",)  # the old case is untouched
    assert read_report(LocalStorage(tmp_path), "default", made.case_id) is None  # the API stores the report


# -- hard rules, protected columns, dates ---------------------------------------------------------------------
def test_the_floor_counts_the_prepared_training_table_not_the_raw_file() -> None:
    drop = RecipeStep(
        order=1,
        kind=RecipeStepKind.REMOVE_ROWS,
        column="region",
        params={"operator": "equals", "values": ["north", "south", "east"]},
        reason="only the west",
    )
    case = ready_case()
    case = case.model_copy(
        update={
            "items": (*case.items, item("item_drop", "recipe_step", {"step": drop.model_dump(mode="json")}))
        }
    )
    report = run(month(OLD_START + timedelta(days=30), seed=2), case).report  # 3,000 raw rows, about 750 left
    floor = [f for f in report.stops if f.code == "TOO_FEW_ROWS"]
    assert floor and floor[0].hard and report.rows_after < 1_000


def test_the_hard_stops_are_hard_in_the_ledger_not_acknowledgeable_doubts() -> None:
    from engine.doctor.ledger import findings_of

    case = ready_case()
    result = run(month(OLD_START + timedelta(days=30), seed=2).head(400), case)
    follow = case.model_copy(update={"items": result.items, "questions": result.questions, "doubts": ()})
    hard = {f.code: f for f in findings_of(follow)}
    assert hard["TOO_FEW_ROWS"].level == "hard"
    assert all(r.kind != "acknowledge" for r in hard["TOO_FEW_ROWS"].resolutions)  # no yes lifts it
    assert hard["TOO_FEW_POSITIVES"].level == "hard"


def test_an_outcome_that_is_no_longer_yes_no_is_a_hard_stop() -> None:
    new = month(OLD_START + timedelta(days=30), seed=2)
    new["churned"] = new["churned"].map({0: "no", 1: "yes"})
    stops = {f.code: f for f in run(new).report.stops}
    assert stops["OUTCOME_NOT_YES_NO"].hard


def test_a_new_value_in_a_closed_column_is_a_stop_and_a_non_yes_no_outcome_is_hard() -> None:
    new = month(OLD_START + timedelta(days=30), seed=2)
    new.loc[:50, "region"] = "central"
    assert not {(f.code, f.column): f for f in run(new).report.stops}[("NEW_CATEGORY", "region")].hard
    new.loc[:50, "churned"] = 2
    assert {f.code: f for f in run(new).report.stops}["OUTCOME_NOT_YES_NO"].hard


def test_the_end_of_the_data_is_the_named_date_column_never_a_future_one() -> None:
    new = month(OLD_START + timedelta(days=30), seed=2)
    new["contract_end"] = "2031-01-01"  # a date in the future that is not the day each row describes
    dates = run(new).report.dates
    expected = pd.to_datetime(new["joined"]).max().date()
    assert dates.data_end_after == expected
    assert dates.training_as_of_after <= expected and dates.as_of_after <= expected + timedelta(days=1)
    # a file dated after today is read to today at most
    later = run(month(date(2027, 1, 1), seed=2)).report.dates
    assert later.data_end_after == NOW.date()


def test_a_date_column_that_cannot_be_found_is_asked_not_silently_kept() -> None:
    gone = month(OLD_START + timedelta(days=30), seed=2).drop(columns=["joined"])
    report = run(gone).report
    assert "DATE_END_NOT_FOUND" in {f.code for f in report.questions}
    assert not report.dates.moved


# -- the follow-up case goes through the visit ---------------------------------------------------------------
def test_the_follow_up_case_goes_on_through_the_visit_without_a_new_round(tmp_path: Path) -> None:
    from engine.doctor import visit
    from engine.doctor.diagnose import Offer
    from tests.unit.doctor.visit_support import World, deps_for

    storage = LocalStorage(tmp_path)
    store = CaseStore(storage)
    source = store.create(
        client_id="default",
        created_by="asha",
        title="Churn",
        files=[CaseFile(upload_id="up-old", file_name="customers.csv", added_at=NOW, added_by="asha")],
    )
    case = ready_case().model_copy(
        update={"case_id": source.case_id, "version": source.version, "created_at": source.created_at}
    )
    old = loaded(month(OLD_START, seed=1), "up-old")
    new = loaded(
        month(OLD_START + timedelta(days=30), seed=2, amount="bill_amount"), "up-new", "customers_next.csv"
    )
    result = recheck(case, {"up-old": "up-new"}, [old], [new], now=NOW)
    made = open_recheck_case(
        store,
        case,
        result,
        [CaseFile(upload_id="up-new", file_name="customers_next.csv", added_at=NOW, added_by="asha")],
        actor="asha",
    )
    assert made.state is VisitState.ASK
    world = World(
        files={"up-new": new}, offers=(Offer(config=load_use_case("rca", CONFIGS), stage="Retain"),)
    )
    deps = deps_for(storage, world)
    question = next(q for q in made.questions if q.answer is None)
    after = visit.answer(
        "default",
        made.case_id,
        question.question_id,
        "yes",
        deps,
        expected_version=made.version,
        actor="asha",
    )
    # the rename was the only thing asked: no INTAKE round, no second question about the files
    assert after.visit is not None and after.visit.taken_in == ("up-new",)
    assert [
        q.question_id for q in after.questions if q.answer is None and q.code.startswith("RECHECK_")
    ] == []
    assert after.state is not VisitState.INTAKE
    parse = next(i for i in after.items if i.item_id == "item_parse")
    assert parse.state is ProposalState.ACCEPTED and parse.decided_by == "asha"
    assert any(i.kind == "column_alias" and i.state is ProposalState.ACCEPTED for i in after.items)
    assert after.approvals[: len(case.approvals)] == case.approvals


# -- the state, the amount goal, the dates the person says yes to, one broken file -------------------------------
def test_a_case_past_approve_but_not_ready_is_refused() -> None:
    for state in (VisitState.PRESCRIBE, VisitState.VERIFY):
        with pytest.raises(RecheckError) as refused:
            run(
                month(OLD_START + timedelta(days=30), seed=2),
                ready_case().model_copy(update={"state": state}),
            )
        assert refused.value.code == "RECHECK_NOT_READY"


def spend_month(start: date, *, seed: int, spenders: float = 0.3) -> pd.DataFrame:
    frame = month(start, seed=seed)
    rng = np.random.default_rng(seed + 100)
    frame["churned"] = np.where(rng.random(ROWS) < spenders, np.round(rng.uniform(5, 900, ROWS), 2), 0.0)
    return frame


def test_an_amount_goal_is_rechecked_by_its_amount_floor_not_as_yes_no() -> None:
    config = load_use_case("customer-spend", CONFIGS)
    old = loaded(spend_month(OLD_START, seed=1), "up-old")
    same = recheck(
        ready_case(),
        {"up-old": "up-new"},
        [old],
        [loaded(spend_month(OLD_START + timedelta(days=30), seed=2), "up-new")],
        now=NOW,
        config=config,
    ).report
    assert not {"OUTCOME_NOT_YES_NO", "TOO_FEW_POSITIVES", "OUTCOME_RATE_SHIFT"} & {
        f.code for f in same.stops
    }
    few = recheck(
        ready_case(),
        {"up-old": "up-new"},
        [old],
        [loaded(spend_month(OLD_START + timedelta(days=30), seed=2, spenders=0.03), "up-new")],
        now=NOW,
        config=config,
    ).report
    stops = {f.code: f for f in few.stops}
    assert stops["TOO_FEW_POSITIVES"].hard and "non-zero amount" in stops["TOO_FEW_POSITIVES"].text
    assert "OUTCOME_RATE_SHIFT" in stops


def test_the_floors_are_the_configured_ones() -> None:
    config = load_use_case("rca", CONFIGS)
    strict = config.model_copy(
        update={"validation": config.validation.model_copy(update={"min_rows": 5_000, "min_positive": 2_000})}
    )
    old = loaded(month(OLD_START, seed=1), "up-old")
    new = loaded(month(OLD_START + timedelta(days=30), seed=2), "up-new")
    report = recheck(ready_case(), {"up-old": "up-new"}, [old], [new], now=NOW, config=strict).report
    assert {"TOO_FEW_ROWS", "TOO_FEW_POSITIVES"} <= {f.code for f in report.stops if f.hard}


def test_an_answered_date_is_proposed_again_for_the_persons_yes() -> None:
    from engine.doctor.case import CaseOption, CaseQuestion

    question = CaseQuestion(
        question_id="q.as_of.base",
        code="Q08_AS_OF",
        text="What date is this data true as of?",
        options=(CaseOption(option_id="typed", label="Another date", sets="as_of"),),
        answer="typed",
        answered_by="asha",
        answered_at=NOW,
        answer_value="2024-06-01",
    )
    case = ready_case().model_copy(update={"questions": (question,)})
    result = run(month(OLD_START + timedelta(days=30), seed=2), case)
    moved = next(q for q in result.questions if q.question_id == "q.as_of.base")
    assert moved.answer_value == result.report.dates.as_of_after.isoformat()  # type: ignore[union-attr]
    assert moved.assumed and moved.answered_by is None and moved.recommended == "typed"
    assert moved.why.startswith("The data now ends ")
    from engine.doctor.review import _question_decision

    shown = _question_decision(moved)
    assert shown.decided_by == "rules" and shown.needs_yes


def test_one_file_that_fails_does_not_hide_drift_in_the_other() -> None:
    case = ready_case()
    other = pd.DataFrame({"customer_id": [f"C1-{n}" for n in range(ROWS)], "score": ["12"] * ROWS})
    broken = other.assign(score=["not a number"] * ROWS)
    parse = RecipeStep(order=1, kind=RecipeStepKind.PARSE_NUMBER, column="score", params={}, reason="r")
    step = item("item_score", "recipe_step", {"step": parse.model_dump(mode="json")}).model_copy(
        update={"upload_id": "up-old2"}
    )
    two = case.model_copy(
        update={
            "files": (
                *case.files,
                CaseFile(upload_id="up-old2", file_name="scores.csv", added_at=NOW, added_by="asha"),
            ),
            "items": (*case.items, step),
        }
    )
    report = recheck(
        two,
        {"up-old": "up-new", "up-old2": "up-new2"},
        [loaded(month(OLD_START, seed=1), "up-old"), loaded(other, "up-old2", "scores.csv")],
        [
            loaded(month(OLD_START + timedelta(days=30), seed=2, rate=0.6), "up-new"),
            loaded(broken, "up-new2", "scores.csv"),
        ],
        now=NOW,
    ).report
    assert report.rows_after == 2 * ROWS  # the run failed (raw rows are counted), and still the drift is said
    assert "OUTCOME_RATE_SHIFT" in {f.code for f in report.stops}


def _dropping_north() -> Case:
    drop = RecipeStep(
        order=1,
        kind=RecipeStepKind.REMOVE_ROWS,
        column="region",
        params={"operator": "equals", "values": ["north"]},
        reason="r",
    )
    case = ready_case()
    return case.model_copy(
        update={
            "items": (*case.items, item("item_drop", "recipe_step", {"step": drop.model_dump(mode="json")}))
        }
    )


def test_a_stop_said_yes_to_in_the_approved_run_is_not_asked_again() -> None:
    from engine.doctor.visit_contracts import Acknowledgement, CaseVisit

    case = _dropping_north()
    asked = run(month(OLD_START + timedelta(days=30), seed=2), case).report
    (stop,) = [f for f in asked.stops if f.code == "PLAN_LOSES_TOO_MUCH"]
    baseline = materialize(case, case.items, [loaded(month(OLD_START, seed=1), "up-old")], Full())
    found = next(f for f in baseline.findings if f.code == "PLAN_LOSES_TOO_MUCH")
    said = Acknowledgement(key=found.key, count=10_000, by="asha", at=NOW, reason="known")
    acked = case.model_copy(update={"visit": CaseVisit(acknowledged=(said,))})
    carried = run(month(OLD_START + timedelta(days=30), seed=2), acked)
    again = carried.report
    assert "PLAN_LOSES_TOO_MUCH" not in {f.code for f in again.stops}, "the yes carries while the count holds"
    # and the follow-up case holds the yes under the new run's key, so its APPROVE does not ask it again
    assert {a.key for a in carried.acknowledged} > {found.key}
    small = acked.model_copy(
        update={"visit": CaseVisit(acknowledged=(said.model_copy(update={"count": 1}),))}
    )
    grown = run(month(OLD_START + timedelta(days=30), seed=2), small).report
    assert "PLAN_LOSES_TOO_MUCH" in {f.code for f in grown.stops}, "a bigger loss needs a new yes"
    assert stop.subject.startswith("data_check@PLAN_LOSES_TOO_MUCH:")


def test_the_training_table_reads_no_row_after_the_training_date() -> None:
    sql = (
        "SELECT customer_id, churned, joined, amount, region FROM t_customers "
        "WHERE CAST(joined AS DATE) <= $as_of"
    )
    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.SQL_QUERY,
        column="kept",
        params={"sql": sql, "inputs": ["customers"], "output_name": "kept", "description": "d"},
        reason="r",
    )
    case = ready_case()
    case = case.model_copy(
        update={
            "items": (*case.items, item("item_kept", "recipe_step", {"step": step.model_dump(mode="json")}))
        }
    )
    result = run(month(OLD_START + timedelta(days=30), seed=2), case)
    training = result.report.dates.training_as_of_after
    assert training is not None
    new = loaded(month(OLD_START + timedelta(days=30), seed=2), "up-new")
    follow = case.model_copy(update={"items": result.items})
    files = [CaseFile(upload_id="up-new", file_name="customers.csv", added_at=NOW, added_by="asha")]
    from engine.doctor.materialize import materialize_plan

    built = materialize_plan(
        files,
        [i for i in follow.items if i.state is ProposalState.ACCEPTED],
        [new],
        Full(),
        training_as_of=training,
    )
    table = built.tables["kept"] if "kept" in built.tables else built.tables["customers"]
    assert (pd.to_datetime(table["joined"]).dt.date <= training).all()
