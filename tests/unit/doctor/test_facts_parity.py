"""Golden parity: the fact functions suggest what the rules of `visit.py` wrote (the design §1.3, package P2).

The rules are gone from the visit (the default author applies what the facts suggest), so the parity is against what
they wrote, recorded once from them in `tests/fixtures/doctor_facts_golden.json`: on the frames of the scenario
harness (`scripts/doctor_scenario_data.py`), with no goal and with the goal chosen before any rule has run, the item
ids, the question ids with their codes, recommendations and options, and the doubts. Keep the id formulas byte for
byte: a stored case re-proposes everything when one changes. The one replaced question is the audience's:
`BUILD_AUDIENCE` is now the goal's setting question (`GOAL_SETTING`, `engine.doctor.goal_settings`).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.config import load_use_case
from engine.doctor import visit
from engine.doctor.case import (
    Case,
    CaseDoubt,
    CaseFile,
    CaseGoal,
    CaseItem,
    CaseQuestion,
    CaseStore,
    payload_digest,
)
from engine.doctor.diagnose import Offer
from engine.doctor.facts_goal import goal_facts, goal_fit_facts, suggest_build
from engine.doctor.goal_brief import GoalBrief, load_goal_brief
from engine.doctor.protocol import Ask, AskOption, DataRequest, question_id_of, request_data
from engine.doctor.tools_facts import (
    AskPerson,
    Create,
    Doubt,
    Facts,
    Request,
    Suggestion,
    Withdraw,
    detector_facts,
    draft_item_id,
    key_facts,
    role_step_facts,
)
from engine.doctor.visit import Detected, VisitDeps
from engine.doctor.visit_contracts import OptionEffect
from engine.storage import LocalStorage
from scripts.doctor_scenario_data import (
    generate_bank_marketing,
    generate_churn_list,
    generate_customers_and_orders,
    generate_hillstrom_campaign,
    generate_order_log,
    generate_two_currencies,
    generate_wide_monthly,
)
from tests.unit.doctor.visit_support import ANALYST, CLIENT, T0, World, loaded
from tests.unit.doctor.visit_support import customers as customers_frame

Row = tuple[Any, ...]


def _ids(prefix: str, count: int) -> list[str]:
    return [f"up-{prefix}-{n}" for n in range(count)]


def _scenarios() -> dict[str, tuple[str, list[tuple[str, Any]]]]:
    """Scenario name -> (goal, [(file name, frame)]), the frames of `scripts/doctor_scenarios.py`."""
    customers, orders = generate_customers_and_orders()
    return {
        "order_log": ("retail-win-back", [("order_log.csv", generate_order_log())]),
        "churn_list": ("telco-churn", [("churn_list.csv", generate_churn_list())]),
        "bank_marketing": ("bank-term-deposit", [("bank_marketing.csv", generate_bank_marketing())]),
        "hillstrom": ("hillstrom-email", [("hillstrom.csv", generate_hillstrom_campaign())]),
        "two_currencies": ("customer-spend", [("two_currencies.csv", generate_two_currencies())]),
        "wide_monthly": ("customer-spend", [("wide_monthly.csv", generate_wide_monthly())]),
        "customers": ("targeted-advertisement", [("customers.csv", customers_frame(seed=1))]),
        "customers_unnamed": (
            "rca",
            [
                (
                    "customers.csv",
                    customers_frame(seed=2)
                    .rename(columns={"snapshot_date": "reported_on"})
                    .drop(columns=["marketing_opt_in"]),
                )
            ],
        ),
        "customers_no_dates": (
            "rca",
            [("customers.csv", customers_frame(seed=3).drop(columns=["snapshot_date"]))],
        ),
        "customers_and_orders": (
            "retail-win-back",
            [("customers.csv", customers), ("orders.csv", orders)],
        ),
    }


def _world(goal: str, named: list[tuple[str, Any]], name: str) -> World:
    config = load_use_case(goal)
    files = {
        upload_id: loaded(upload_id, file_name, frame, config)
        for upload_id, (file_name, frame) in zip(_ids(name, len(named)), named, strict=True)
    }
    return World(files=files, offers=(Offer(config=config, stage="Acquire"),))


# --- what the facts suggest, in the shape the golden records ----------------------------------------------------
GOLDEN = json.loads((Path(__file__).parents[2] / "fixtures" / "doctor_facts_golden.json").read_text("utf-8"))


def _fact_items(facts: Sequence[Facts]) -> list[list[str]]:
    return [
        [draft_item_id(d), d.kind, AgentConfidence(d.confidence).value]
        for f in facts
        for s in f.suggested
        if isinstance(s.action, Create)
        for d in (s.action.draft,)
    ]


def _options(question: CaseQuestion, effects: Sequence[OptionEffect]) -> list[str]:
    return [e.option_id for e in effects if e.option_id in {o.option_id for o in question.options}]


def _fact_questions(facts: Sequence[Facts]) -> list[list[Any]]:
    found: list[list[Any]] = []
    for f in facts:
        for s in f.suggested:
            action = s.action
            if isinstance(action, AskPerson):
                a = action.ask
                found.append(
                    [
                        question_id_of(a.code, a.subject, a.goal_id),
                        a.code,
                        a.recommended,
                        [o.effect.option_id for o in a.options],
                    ]
                )
            elif isinstance(action, Request):
                q, effects = request_data(action.request)
                found.append([q.question_id, q.code, q.recommended, _options(q, effects)])
    return found


def _fact_doubts(facts: Sequence[Facts]) -> list[str]:
    return [s.action.doubt.doubt_id for f in facts for s in f.suggested if isinstance(s.action, Doubt)]


def _compare(
    deps: VisitDeps, case: Case, brief: GoalBrief | None, recorded: dict[str, Any]
) -> dict[str, int]:
    """The facts on `case` say what the rules said on it (recorded in the golden); the counts of what was compared,
    to see it was not empty."""
    run = visit._Run(deps, ANALYST)
    parts: tuple[Facts, ...]
    if brief is None:
        fit = goal_fit_facts(run, case)
        diagnosis = {
            **(case.diagnosis or {"files": [], "links": [], "stacks": []}),
            "use_cases": fit.facts["use_cases"],
        }
        parts = (key_facts(run, case, diagnosis), fit)
    else:
        parts = (key_facts(run, case), *goal_facts(run, case, brief))
    assert _fact_items(parts) == recorded["items"]
    questions = _fact_questions(parts)
    # The audience is no longer asked by a rule of its own: it is the goal's setting, asked from the goal's
    # configuration (`GOAL_SETTING`, in the place `BUILD_AUDIENCE` was).
    assert [q for q in questions if q[1] != "GOAL_SETTING"] == [
        q for q in recorded["questions"] if q[1] != "BUILD_AUDIENCE"
    ]
    assert sum(q[1] == "GOAL_SETTING" for q in questions) == sum(
        q[1] == "BUILD_AUDIENCE" for q in recorded["questions"]
    )
    assert _fact_doubts(parts) == recorded["doubts"]
    return {
        **{f"item:{kind}": 1 for _id, kind, _c in recorded["items"]},
        **{f"question:{q[1]}": 1 for q in recorded["questions"]},
        **{f"recommended:{q[1]}": 1 for q in recorded["questions"] if q[2] is not None},
        **{f"doubt:{d.split('.')[1]}": 1 for d in recorded["doubts"]},
    }


def _open(tmp_path: Path, name: str) -> tuple[VisitDeps, Case, str]:
    goal_id, named = _SCENARIOS[name]
    world = _world(goal_id, named, name)
    storage = LocalStorage(tmp_path)
    deps = VisitDeps(storage=storage, load_files=world.load, offers=world.offers, clock=world.clock)
    files = [
        CaseFile(upload_id=u, file_name=f.file_name, added_at=T0, added_by=ANALYST)
        for u, f in world.files.items()
    ]
    return (
        deps,
        CaseStore(storage, clock=world.clock).create(client_id=CLIENT, created_by=ANALYST, files=files),
        goal_id,
    )


_SCENARIOS = _scenarios()


def test_the_facts_suggest_what_the_rules_wrote(tmp_path: Path) -> None:
    """Every scenario, with no goal and with the goal chosen; together they reach every kind of suggestion the rules
    made."""
    seen: set[str] = set()
    for name in sorted(_SCENARIOS):
        (tmp_path / name).mkdir()
        deps, fresh, goal_id = _open(tmp_path / name, name)
        chosen = fresh.model_copy(
            update={"goals": (CaseGoal(goal_id=goal_id, active=True, set_at=T0, set_by=ANALYST),)}
        )
        try:
            seen.update(_compare(deps, fresh, None, GOLDEN[name]["no_goal"]))
            seen.update(_compare(deps, chosen, load_goal_brief(goal_id), GOLDEN[name]["goal"]))
        except AssertionError as exc:
            raise AssertionError(f"scenario {name}: the facts differ from what the rules wrote") from exc
    wanted = {
        "item:key",
        "item:outcome_column",
        "question:KEY_AMBIGUOUS",
        "question:KEY_NOT_UNIQUE",
        "question:BUILD_AUDIENCE",
        "question:DATA_REQUESTED",
        "recommended:KEY_NOT_UNIQUE",
        "doubt:outcome_missing",
    }
    assert wanted <= seen, f"the scenarios never reached {sorted(wanted - seen)}"


def _accepted_setting(goal_id: str, setting: str, value: str) -> CaseItem:
    payload = {"goal_id": goal_id, "setting": setting, "value": value}
    return CaseItem(
        item_id=f"goal_setting.{goal_id}.{setting}",
        kind="goal_setting",
        title=f"{setting}: {value}",
        source=ProposalSource.RULES,
        confidence=AgentConfidence.SURE,
        state=ProposalState.ACCEPTED,
        payload=payload,
        payload_digest=payload_digest(payload),
        proposed_at=T0,
        decided_by=ANALYST,
        decided_at=T0,
        goal_id=goal_id,
    )


def test_the_build_is_suggested_through_the_sql_step(tmp_path: Path) -> None:
    """`suggest_build` is the goal's build: one `recipe_step` that went through `propose_sql_step`, once the person
    has said which customers it is about."""
    deps, fresh, goal_id = _open(tmp_path, "order_log")
    brief = load_goal_brief(goal_id)
    chosen = fresh.model_copy(
        update={"goals": (CaseGoal(goal_id=goal_id, active=True, set_at=T0, set_by=ANALYST),)}
    )
    # the audience is the person's to pick: until then the facts ask, and make no build
    first = suggest_build(visit._Run(deps, ANALYST), chosen, brief)
    asked = [s.action.ask for s in first.suggested if isinstance(s.action, AskPerson)]
    assert [a.code for a in asked] == ["GOAL_SETTING"] and first.facts == {"offered": True}
    assert not [s for s in first.suggested if isinstance(s.action, Create)]
    # with it picked the build is a step with a trial receipt
    answered = chosen.model_copy(update={"items": (_accepted_setting(goal_id, "audience", "active"),)})
    made = suggest_build(visit._Run(deps, ANALYST), answered, brief)
    [(draft, evidence)] = [
        (s.action.draft, made.evidence) for s in made.suggested if isinstance(s.action, Create)
    ]
    assert draft.payload["step"] and draft.payload["needs_yes"] is True and "buildable" in draft.payload
    assert draft.payload["audience"]["audience_id"] == "active"
    assert [e.tool for e in evidence] == ["propose_sql_step"]
    assert draft.evidence_ids == (evidence[0].evidence_id,)
    # the window is a setting too: the person's 60 days are the build's, not the default 90
    sixty = answered.model_copy(
        update={"items": (*answered.items, _accepted_setting(goal_id, "window_days", "60"))}
    )
    again = suggest_build(visit._Run(deps, ANALYST), sixty, brief)
    [other] = [s.action.draft for s in again.suggested if isinstance(s.action, Create)]
    assert other.payload["buildable"]["window_days"] == 60 and draft_item_id(other) != draft_item_id(draft)


class _Finds:
    """A detector that finds one of everything (the port of `VisitDeps.detectors`)."""

    reads_current = True

    def __call__(self, case: Case, files: Any, brief: Any) -> Detected:
        item = visit._item(
            "recipe_step",
            {"column": "age"},
            title="Blank the placeholder 99 in age.",
            upload_id="up-x",
            confidence=AgentConfidence.CHECK,
            evidence=(),
            now=T0,
        )
        return Detected(
            items=(item,),
            doubts=(
                CaseDoubt(
                    doubt_id="d.sure", because="a column looks odd", would_do="leave it", if_not="we guess"
                ),
            ),
            requests=(DataRequest(kind="more_data", what="the date of each order", why="the goal needs it"),),
            asks=(
                Ask(
                    code="CODE_X",
                    subject={"upload_id": "up-x"},
                    text="Which one?",
                    why="Two readings.",
                    options=(AskOption(label="A", effect=OptionEffect(option_id="a")),),
                    rows=10,
                ),
            ),
        )


def test_the_detectors_findings_are_suggestions(tmp_path: Path) -> None:
    goal_id, named = _SCENARIOS["churn_list"]
    world = _world(goal_id, named, "churn_list")
    detector = _Finds()
    deps = VisitDeps(
        storage=LocalStorage(tmp_path),
        load_files=world.load,
        offers=world.offers,
        clock=world.clock,
        detectors=(detector,),
    )
    files = [
        CaseFile(upload_id=u, file_name=f.file_name, added_at=T0, added_by=ANALYST)
        for u, f in world.files.items()
    ]
    case = CaseStore(deps.storage, clock=world.clock).create(
        client_id=CLIENT, created_by=ANALYST, files=files
    )
    found = detector_facts(visit._Run(deps, ANALYST), case, None)
    made = detector(case, (), None)
    assert [row[0] for row in _fact_items([found])] == [i.item_id for i in made.items]
    assert _fact_doubts([found]) == [d.doubt_id for d in made.doubts]
    asks = [s.action.ask.code for s in found.suggested if isinstance(s.action, AskPerson)]
    assert asks == ["CODE_X"]
    assert [q[0] for q in _fact_questions([found]) if q[1] == "DATA_REQUESTED"] == [
        request_data(r)[0].question_id for r in made.requests
    ]


def test_a_step_on_the_id_or_outcome_column_is_suggested_for_withdrawal(tmp_path: Path) -> None:
    """Such a step can never run: the fact says so, and the author takes it out."""
    goal_id, named = _SCENARIOS["churn_list"]
    world = _world(goal_id, named, "churn_list")
    storage = LocalStorage(tmp_path)
    upload_id = next(iter(world.files))
    files = [CaseFile(upload_id=upload_id, file_name="churn_list.csv", added_at=T0, added_by=ANALYST)]
    case = CaseStore(storage, clock=world.clock).create(client_id=CLIENT, created_by=ANALYST, files=files)

    def item(
        item_id: str, kind: str, payload: dict[str, Any], state: ProposalState = ProposalState.ACCEPTED
    ) -> CaseItem:
        return CaseItem(
            item_id=item_id,
            kind=kind,
            title=item_id,
            source=ProposalSource.RULES,
            confidence=AgentConfidence.SURE,
            state=state,
            upload_id=upload_id,
            payload=payload,
            payload_digest=payload_digest(payload),
            proposed_at=T0,
            decided_by=ANALYST,
            decided_at=T0,
        )

    def step(kind: str, column: str) -> dict[str, Any]:
        return {"step": {"kind": kind, "column": column}}

    case = case.model_copy(
        update={
            "items": (
                item("key.base.a", "key", {"upload_id": upload_id, "column": "customerID"}),
                item("outcome.g.a", "outcome_column", {"upload_id": upload_id, "column": "Churn"}),
                item("step.on.key", "recipe_step", step("map_values", "customerID")),
                item("step.on.outcome", "recipe_step", step("map_boolean", "Churn")),
                item("step.derive", "recipe_step", step("derive", "customerID")),  # reads it: stays
                item("step.rows", "recipe_step", step("remove_rows", "customerID")),  # rows by ID: stays
                item("step.other", "recipe_step", step("map_values", "Contract")),
                item("step.left", "recipe_step", step("map_values", "customerID"), ProposalState.REJECTED),
            ),
            "goals": (CaseGoal(goal_id=goal_id, active=True, set_at=T0, set_by=ANALYST),),
        }
    )
    case = case.model_copy(
        update={
            "items": tuple(
                i.model_copy(update={"goal_id": goal_id if i.kind == "outcome_column" else None})
                for i in case.items
            )
        }
    )
    suggested = {
        s.action.item_id: s.action.reason
        for s in role_step_facts(case).suggested
        if isinstance(s.action, Withdraw)
    }
    assert sorted(suggested) == ["step.on.key", "step.on.outcome"]
    assert "'customerID' is the ID column, which no step changes" in suggested["step.on.key"]
    assert "'Churn' is the outcome column, which no step changes" in suggested["step.on.outcome"]


def test_suggestions_have_stable_ids_and_the_case_is_not_written(tmp_path: Path) -> None:
    deps, fresh, goal_id = _open(tmp_path, "customers")
    case = fresh.model_copy(
        update={"goals": (CaseGoal(goal_id=goal_id, active=True, set_at=T0, set_by=ANALYST),)}
    )
    before = case.model_dump_json()
    brief = load_goal_brief(goal_id)

    def suggested() -> list[Suggestion]:
        run = visit._Run(deps, ANALYST)
        return [s for f in (key_facts(run, case), *goal_facts(run, case, brief)) for s in f.suggested]

    first, again = suggested(), suggested()
    assert first and [s.id for s in first] == [s.id for s in again]
    assert len({s.id for s in first}) == len(first)
    assert all(s.reason for s in first)
    assert all(
        s.action.basis == f"s:{s.id}" for s in first if isinstance(s.action, Create)
    ), "an item cites the suggestion it came from"
    assert case.model_dump_json() == before
