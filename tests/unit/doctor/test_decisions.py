"""One decisions model (root-cause plan, Step 2): what the person decided is read from ONE place.

`engine/doctor/decisions.py` answers key, outcome, as-of date, as-of column, outcome window and consent from the
accepted items and the answered questions. These tests pin its precedence, that an answered option IS a decision, that
the as-of date reaches a query step through the materialiser (the step keeps `$as_of` symbolic), and - as an
architecture test - that no other module of `engine/doctor` reads those items by kind or parses the as-of question.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pandas as pd
import pytest

from engine.agent.contracts import ProposalState, RecipeStep, RecipeStepKind
from engine.doctor.case import Case, CaseItem, CaseOption, CaseQuestion
from engine.doctor.decisions import DECISION_TARGETS, decisions, decisions_from, role_columns, sets_of_effect
from engine.doctor.diagnose import LoadedFile
from engine.doctor.materialize import Full, materialize
from engine.doctor.questions import CATALOGUE

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]


def item(
    item_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    state: ProposalState = ProposalState.ACCEPTED,
    upload_id: str | None = "up-1",
    goal_id: str | None = None,
    decided_by: str | None = "person",
) -> CaseItem:
    return CaseItem.model_validate(
        {
            "item_id": item_id,
            "kind": kind,
            "source": "rules",
            "confidence": "sure",
            "state": state.value,
            "upload_id": upload_id,
            "goal_id": goal_id,
            "payload": payload,
            "payload_digest": item_id,
            "proposed_at": NOW,
            "decided_by": decided_by,
            "decided_at": NOW,
        }
    )


def as_of_question(
    answer: str | None, typed: str | None = None, *, sets: str | None = "as_of"
) -> CaseQuestion:
    return CaseQuestion(
        question_id="q.q08",
        code="Q08_AS_OF",
        text="What date is this data true as of?",
        options=(
            CaseOption(option_id="c0", label="The latest date in the file: 2025-03-31", sets=cast(Any, sets)),
            CaseOption(option_id="c1", label="I will type a date", sets=cast(Any, sets), value_kind="date"),
            CaseOption(option_id="c2", label="I don't know", sets=cast(Any, "note" if sets else None)),
        ),
        answer=answer,
        answer_value=typed,
        answered_by="person",
        answered_at=NOW,
    )


def case_of(
    items: list[CaseItem] = (),  # type: ignore[assignment]
    questions: list[CaseQuestion] = (),  # type: ignore[assignment]
    active: str | None = None,
) -> Case:
    goals = [] if active is None else [SimpleNamespace(goal_id=active, active=True)]
    return cast(Case, SimpleNamespace(items=list(items), questions=list(questions), files=(), goals=goals))


# -- precedence -------------------------------------------------------------------------------------------------
def test_the_first_accepted_key_of_a_file_wins_and_the_rest_are_counted() -> None:
    found = decisions(
        case_of(
            [
                item("k1", "key", {"column": "CustomerID"}),
                item("k2", "key", {"column": "Other"}),
                item("k3", "key", {"column": "Pending"}, state=ProposalState.PENDING),
                item("k4", "key", {"column": "Gone"}, state=ProposalState.REJECTED),
                item("k5", "key", {"column": "B"}, upload_id="up-2"),
            ]
        )
    )
    assert found.key("up-1") == "CustomerID"
    assert found.key("up-2") == "B"
    assert {u: d.column for u, d in found.keys.items()} == {"up-1": "CustomerID", "up-2": "B"}
    assert found.ignored == 1
    assert found.keys["up-1"].source == "item:k1" and found.keys["up-1"].by == "person"


def test_the_outcome_the_as_of_column_and_the_consent_are_per_goal() -> None:
    found = decisions_from(
        [
            item("o1", "outcome_column", {"upload_id": "up-1", "column": "bought"}, goal_id="g1"),
            item("o2", "outcome_column", {"upload_id": "up-1", "column": "churned"}, goal_id="g2"),
            item("t1", "as_of_column", {"upload_id": "up-1", "column": "when"}, goal_id="g1"),
            item("c1", "consent_column", {"upload_id": "up-1", "column": "ok"}, goal_id="g1"),
        ]
    )
    assert found.pair("outcome", "g1") == ("up-1", "bought")
    assert found.pair("outcome", "g2") == ("up-1", "churned")
    assert {g: d.column for g, d in found.outcomes.items()} == {"g1": "bought", "g2": "churned"}
    assert found.as_of_column("g1") is not None and found.as_of_column("g2") is None
    assert found.as_of_columns["up-1"].column == "when"
    assert found.consent("g1") is not None and found.consent("g2") is None
    assert found.outcome_of_upload("up-1") == "bought"
    assert found.column_of_upload("outcome", "up-1", goal_id="g2") == "churned"


def test_a_pending_decision_decides_nothing() -> None:
    found = decisions(case_of([item("o", "outcome_column", {"column": "x"}, state=ProposalState.PENDING)]))
    assert found.records == ()
    assert found.as_of is None and found.as_of_date is None


def test_the_outcome_window_is_the_newest_accepted_setting() -> None:
    older = item("w1", "setting", {"path": "uplift.outcome_window_days", "value": 30}, goal_id="g1")
    newer = item("w2", "setting", {"path": "uplift.outcome_window_days", "value": 60}, goal_id="g1")
    object.__setattr__(older, "decided_at", datetime(2026, 10, 1, tzinfo=UTC))
    object.__setattr__(newer, "decided_at", datetime(2026, 10, 5, tzinfo=UTC))
    found = decisions(case_of([older, newer], active="g1"))
    assert found.window_days("g1") == 60
    assert found.window_days("other") is None


def test_the_digest_follows_what_was_decided_and_not_who_or_when() -> None:
    a = decisions(case_of([item("k", "key", {"column": "id"}, decided_by="ann")]))
    b = decisions(case_of([item("k9", "key", {"column": "id"}, decided_by="bob")]))
    c = decisions(case_of([item("k", "key", {"column": "other"})]))
    assert a.digest == b.digest != c.digest
    assert decisions(case_of()).digest != a.digest


def test_the_items_alone_read_the_same_as_the_case() -> None:
    items = [item("k", "key", {"column": "id"})]
    assert decisions_from(items).key("up-1") == decisions(case_of(items)).key("up-1") == "id"


# -- the as-of date ---------------------------------------------------------------------------------------------
def test_the_as_of_date_from_an_item_an_answer_or_a_typed_value() -> None:
    assert decisions(case_of([item("a", "recipe_step", {"as_of": "2025-02-28"})])).as_of_iso == "2025-02-28"
    assert decisions(case_of(questions=[as_of_question("c0")])).as_of_iso == "2025-03-31"
    typed = decisions(case_of(questions=[as_of_question("c1", "2025-01-15")]))
    assert typed.as_of_date == date(2025, 1, 15)
    assert (
        typed.as_of is not None and typed.as_of.source == "question:q.q08:c1" and typed.as_of.by == "person"
    )
    assert (
        decisions(case_of(questions=[as_of_question("c1")])).as_of is None
    )  # a date to type, none typed yet
    assert decisions(case_of(questions=[as_of_question("c2")])).as_of is None  # "I don't know" sets nothing


def test_an_older_case_that_stored_no_target_reads_q8_as_the_as_of_date() -> None:
    assert decisions(case_of(questions=[as_of_question("c0", sets=None)])).as_of_iso == "2025-03-31"


def test_an_option_that_is_a_note_sets_nothing_even_when_its_label_has_a_date() -> None:
    question = as_of_question("c0")
    options = (question.options[0].model_copy(update={"sets": "note"}), *question.options[1:])
    assert decisions(case_of(questions=[question.model_copy(update={"options": options})])).as_of is None


def test_the_item_beats_the_answer_and_a_date_a_query_step_froze_is_not_a_decision() -> None:
    step = {"kind": "sql_query", "params": {"as_of": "2025-06-01"}}
    frozen = item("q", "recipe_step", {"step": step})
    assert (
        decisions(case_of([frozen])).as_of is None
    )  # the step keeps its own date; it is not the whole case's
    answered = case_of([frozen], [as_of_question("c1", "2025-02-01")])
    assert decisions(answered).as_of_iso == "2025-02-01"
    with_item = case_of(
        [frozen, item("a", "recipe_step", {"as_of": "2025-02-28"})], [as_of_question("c1", "2025-02-01")]
    )
    assert decisions(with_item).as_of_iso == "2025-02-28"
    assert decisions(with_item).ignored == 1


def test_one_scope_the_shared_base_and_the_active_goals_section() -> None:
    mine = item("a", "recipe_step", {"as_of": "2025-02-28"}, goal_id="g1")
    other = item("b", "outcome_column", {"upload_id": "up-1", "column": "x"}, goal_id="g2")
    case = case_of([mine, other], active="g1")
    assert decisions(case).as_of_iso == "2025-02-28" and decisions(case).pair("outcome", "g2") is None
    assert decisions(case_of([mine, other], active="g2")).as_of is None


def test_a_second_accepted_key_is_found_when_the_first_names_a_column_the_file_lacks() -> None:
    found = decisions(
        case_of([item("k1", "key", {"column": "Gone"}), item("k2", "key", {"column": "CustomerID"})])
    )
    assert found.key("up-1") == "Gone"
    assert found.column("key", "up-1", {"CustomerID", "x"}) == "CustomerID"
    assert found.column("key", "up-1", {"nothing"}) is None


def test_the_ai_never_sees_an_unreal_date() -> None:
    assert decisions(case_of([item("a", "recipe_step", {"as_of": "2025-13-45"})])).as_of is None
    assert decisions(case_of(questions=[as_of_question("c1", "not a date")])).as_of is None


def test_the_columns_the_ai_may_not_touch_are_named_in_any_state() -> None:
    ids, protected = role_columns(
        case_of(
            [
                item("k", "key", {"column": "id"}, state=ProposalState.PENDING),
                item("o", "outcome_column", {"column": "bought"}, state=ProposalState.REJECTED),
                item("c", "consent_column", {"column": "ok"}),
                item("s", "recipe_step", {"column": "plain"}),
            ]
        )
    )
    assert ids == {"id"} and protected == {"bought", "ok"}


# -- an answer is a decision ------------------------------------------------------------------------------------
def test_every_option_of_the_as_of_question_declares_what_it_sets() -> None:
    from engine.doctor.protocol import _case_question
    from engine.doctor.questions import build_ask

    spec = CATALOGUE["Q8"]
    assert all(option.sets == "as_of" for option in spec.options)
    values = {"latest_date": "2025-03-31", "name_date": "2025-02-28", "back_date": "2025-01-31"}
    ask = build_ask("Q8", values, rows=100)
    question = _case_question(ask, "q", ask.options)
    declared = [o.sets for o in question.options]
    assert declared[:-1] == ["as_of"] * (len(declared) - 1)
    assert declared[-1] == "note"  # "I don't know" says so
    assert "does not change the data" not in question.options[0].effect


def test_every_declared_target_is_one_the_decisions_model_knows() -> None:
    for spec in CATALOGUE.values():
        for option in spec.options:
            assert option.sets is None or option.sets in DECISION_TARGETS, spec.qid
    assert sets_of_effect(None) == "note"
    assert sets_of_effect("key") == "key" and sets_of_effect("outcome_column") == "outcome"
    assert sets_of_effect("stack") is None


# -- the as-of date reaches a query step through the materialiser ------------------------------------------------
DAYS = pd.date_range("2025-01-01", periods=120, freq="D")
SQL = "SELECT Customer, COUNT(*) AS n FROM t_log WHERE day <= $as_of GROUP BY Customer"


def log_file() -> LoadedFile:
    frame = pd.DataFrame({"Customer": [f"C{i % 4}" for i in range(120)], "day": DAYS})
    return LoadedFile(upload_id="up-1", file_name="log.csv", frame=frame, profile=cast(Any, None))


def query_item(frozen: str | None = None) -> CaseItem:
    params: dict[str, Any] = {"sql": SQL, "inputs": ["log"], "output_name": "made", "description": "counts"}
    if frozen is not None:
        params["as_of"] = frozen
    step = RecipeStep(order=1, kind=RecipeStepKind.SQL_QUERY, column="made", params=params, reason="counts")
    return item("query", "recipe_step", {"step": step.model_dump(mode="json")}, decided_by=None)


def counted(case: Case, items: list[CaseItem]) -> int:
    prepared = materialize(case, items, (log_file(),), Full(), twice=False)
    assert not prepared.stopped, [f.code for f in prepared.findings]
    return int(prepared.tables["log"]["n"].sum())


def test_a_typed_as_of_answer_is_the_dollar_as_of_of_a_query_that_froze_none() -> None:
    items = [query_item()]
    case = case_of(items, [as_of_question("c1", "2025-01-31")])
    assert counted(case, items) == 31  # the step keeps `$as_of` symbolic; the materialiser binds the answer


def test_a_later_as_of_answer_reaches_a_query_step_already_in_the_plan() -> None:
    items = [query_item(frozen="2025-03-01")]
    assert counted(case_of(items), items) == 60  # nothing decided: the step keeps its own frozen date
    later = case_of(items, [as_of_question("c1", "2025-01-10")])
    assert counted(later, items) == 10  # the person answered later: the answer, not the old frozen date


def test_a_query_that_needs_an_as_of_date_nobody_gave_is_a_stop_not_a_guess() -> None:
    items = [query_item()]
    prepared = materialize(case_of(items), items, (log_file(),), Full(), twice=False)
    assert [f.code for f in prepared.findings if f.level == "stop"] == ["PLAN_STEP_CANNOT_RUN"]


def test_the_decisions_digest_moves_the_plan_count_basis() -> None:
    assert (
        decisions(case_of(questions=[as_of_question("c1", "2025-01-31")])).digest
        != decisions(case_of(questions=[as_of_question("c1", "2025-02-01")])).digest
    )


# -- architecture: only decisions.py reads the decisions out of the items ----------------------------------------
ROLE_KINDS = frozenset({"key", "outcome_column", "as_of_column", "consent_column"})
"""The item kinds that are decisions: reading them by kind, or the as-of question by code, belongs here only."""
ALLOWED = {"engine/doctor/decisions.py"}


def _strings(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Tuple | ast.Set | ast.List):
        return {s for elt in node.elts for s in _strings(elt)}
    return set()


def readers() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted((ROOT / "engine/doctor").glob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Compare):
                sides = [node.left, *node.comparators]
                kind_read = any(isinstance(s, ast.Attribute) and s.attr == "kind" for s in sides)
                named = set().union(*(_strings(s) for s in sides))
                if kind_read and named & ROLE_KINDS:
                    found.setdefault(rel, []).append(
                        f"line {node.lineno}: reads items of kind {sorted(named & ROLE_KINDS)}"
                    )
                if "Q08_AS_OF" in named:
                    found.setdefault(rel, []).append(f"line {node.lineno}: parses the as-of question")
    return found


def test_only_the_decisions_module_reads_the_decisions_out_of_the_items() -> None:
    found = readers()
    assert not found, f"call decisions(case) instead: {found}"


@pytest.mark.parametrize("name", ["_key_of", "_decided", "case_as_of", "accepted_column"])
def test_the_old_private_readers_are_gone(name: str) -> None:
    for path in sorted((ROOT / "engine/doctor").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        # `AgentReview._decided` lists the plan for the prompt, and `tools_time._decided` reads the Guided-setup
        # agent's own session: neither reads a decision out of the case's items
        if name == "_decided" and path.name in {"agent_review.py", "tools_time.py"}:
            continue
        assert name not in defined, f"{path.name} still defines {name}"


def test_a_decided_date_wins_over_a_steps_rule_in_the_prescription_copy_too() -> None:
    from engine.doctor.prescription import _bound_as_of

    rule: dict[str, Any] = {"rule": "latest_date_in_file", "table": "log", "column": "day"}
    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.SQL_QUERY,
        column="made",
        params={
            "sql": SQL,
            "inputs": ["log"],
            "output_name": "made",
            "as_of": "2025-03-01",
            "as_of_rule": rule,
        },
        reason="counts",
    )
    bound = _bound_as_of(step, "2025-01-31")
    assert bound.params["as_of"] == "2025-01-31" and "as_of_rule" not in bound.params
    assert _bound_as_of(step, None) is step  # nothing decided: the rule stands
