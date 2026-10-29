"""Golden parity: the fact functions suggest what the rules of `visit.py` write (the design §1.3, package P2).

On the frames of the scenario harness (`scripts/doctor_scenario_data.py`), at every state a visit reaches on the way
(no goal, the goal chosen, each blocking question answered), both are called on the same stored case: today's
`_base_rules`, `_generic_rules`, `_goal_rules` and `_recommend_customer_column`, and `key_facts`, `goal_fit_facts`
and `goal_facts`. The item ids, the question ids, the options and their payloads, the doubts and the evidence they
cite must be equal. Keep the id formulas byte for byte: a stored case re-proposes everything when one changes.
"""

from __future__ import annotations

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
    CaseOption,
    CaseQuestion,
    CaseStore,
    VisitState,
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


# --- what the rules wrote, and what the facts suggest, in one shape each ---------------------------------------
def _item(
    item_id: str,
    kind: str,
    title: str,
    upload: str | None,
    goal: str | None,
    confidence: Any,
    ev: Any,
    payload: Any,
) -> Row:
    return (item_id, kind, title, upload, goal, AgentConfidence(confidence), tuple(ev), payload)


def _rule_items(step: visit._Step) -> list[Row]:
    return [
        _item(i.item_id, i.kind, i.title, i.upload_id, i.goal_id, i.confidence, i.evidence_ids, i.payload)
        for i in step.items
    ]


def _fact_items(facts: Sequence[Facts]) -> list[Row]:
    return [
        _item(
            draft_item_id(d),
            d.kind,
            d.title,
            d.upload_id,
            d.goal_id,
            d.confidence,
            d.evidence_ids,
            d.payload,
        )
        for f in facts
        for s in f.suggested
        if isinstance(s.action, Create)
        for d in (s.action.draft,)
    ]


def _question(
    question_id: str,
    code: str,
    upload: str | None,
    column: str | None,
    goal: str | None,
    text: str,
    blocking: bool,
    recommended: str | None,
    why: str,
    options: Sequence[Row],
) -> Row:
    return (question_id, code, upload, column, goal, text, blocking, recommended, why, tuple(options))


def _options(question: CaseQuestion, effects: Sequence[OptionEffect]) -> list[Row]:
    """A question's options with what choosing each adds, as the rules wrote it."""
    options = {o.option_id: o for o in question.options}
    return [
        (e.option_id, options[e.option_id].label, options[e.option_id].safe, e.kind, e.title, e.payload)
        for e in effects
    ]


def _rule_questions(step: visit._Step) -> list[Row]:
    return [
        _question(
            q.question_id,
            q.code,
            q.upload_id,
            q.column,
            q.goal_id,
            q.text,
            q.blocking,
            q.recommended,
            q.why,
            _options(q, effects),
        )
        for q, effects in step.questions
    ]


def _fact_questions(facts: Sequence[Facts]) -> list[Row]:
    found: list[Row] = []
    for f in facts:
        for s in f.suggested:
            action = s.action
            if isinstance(action, AskPerson):
                a = action.ask
                found.append(
                    _question(
                        question_id_of(a.code, a.subject, a.goal_id),
                        a.code,
                        a.upload_id,
                        a.column,
                        a.goal_id,
                        a.text,
                        a.blocking,
                        a.recommended,
                        a.why,
                        [
                            (
                                o.effect.option_id,
                                o.label,
                                o.safe,
                                o.effect.kind,
                                o.effect.title,
                                o.effect.payload,
                            )
                            for o in a.options
                        ],
                    )
                )
            elif isinstance(action, Request):
                q, effects = request_data(action.request)
                found.append(
                    _question(
                        q.question_id,
                        q.code,
                        q.upload_id,
                        q.column,
                        q.goal_id,
                        q.text,
                        q.blocking,
                        q.recommended,
                        q.why,
                        _options(q, effects),
                    )
                )
    return found


def _fact_doubts(facts: Sequence[Facts]) -> list[CaseDoubt]:
    return [s.action.doubt for f in facts for s in f.suggested if isinstance(s.action, Doubt)]


def _evidence(facts: Sequence[Facts]) -> list[str]:
    return [e.evidence_id for f in facts for e in f.evidence]


def _same(step: visit._Step, facts: Sequence[Facts]) -> dict[str, int]:
    """The rules' step and the facts say the same; the counts of what was compared, to see it was not empty."""
    assert _fact_items(facts) == _rule_items(step)
    rule_questions = _rule_questions(step)
    fact_questions = _fact_questions(facts)
    assert fact_questions == rule_questions
    assert [d.model_dump() for d in _fact_doubts(facts)] == [d.model_dump() for d in step.doubts]
    assert _evidence(facts) == [e.evidence_id for e in step.evidence]
    return {
        "items": len(step.items),
        "questions": len(step.questions),
        "doubts": len(step.doubts),
        **{f"item:{i.kind}": 1 for i in step.items},
        **{f"question:{q.code}": 1 for q, _e in step.questions},
        **{f"recommended:{q.code}": 1 for q, _e in step.questions if q.recommended is not None},
        **{f"doubt:{d.doubt_id.split('.')[1]}": 1 for d in step.doubts},
    }


def _compare(deps: VisitDeps, case: Case, brief: GoalBrief | None, seen: set[str]) -> None:
    """Today's rules and the facts, on the same case."""
    step = visit._Step(to=VisitState.PREVIEW)
    run = visit._Run(deps, ANALYST)
    visit._base_rules(run, case, step)
    if brief is None:
        visit._generic_rules(run, case, step)
    else:
        visit._goal_rules(run, case, brief, step)
    visit._recommend_customer_column(case, step)

    run = visit._Run(deps, ANALYST)
    parts: tuple[Facts, ...]
    if brief is None:
        fit = goal_fit_facts(run, case)
        diagnosis = {
            **(case.diagnosis or {"files": [], "links": [], "stacks": []}),
            "use_cases": fit.facts["use_cases"],
        }
        assert step.updates.get("diagnosis") == diagnosis
        assert step.visit.get("missing_for") == tuple(fit.facts["missing_for"])
        parts = (key_facts(run, case, diagnosis), fit)
    else:
        parts = (key_facts(run, case), *goal_facts(run, case, brief))
        assert step.unevaluated == tuple(parts[1].facts["unevaluated"])
    seen.update(_same(step, parts))


def _answer(deps: VisitDeps, case: Case) -> Case | None:
    """The person answers the first open question with the option the rules recommend, else the first real one."""
    for q in case.questions:
        if q.answer is None and visit._in_section(q.goal_id, case):
            pick = q.recommended or next(
                (o.option_id for o in q.options if o.option_id != "none" and o.label != "I don't know"),
                q.options[0].option_id,
            )
            return visit.answer(
                CLIENT, case.case_id, q.question_id, pick, deps, expected_version=case.version, actor=ANALYST
            )
    return None


def _open(tmp_path: Path, world: World) -> tuple[VisitDeps, Case]:
    storage = LocalStorage(tmp_path)
    deps = VisitDeps(storage=storage, load_files=world.load, offers=world.offers, clock=world.clock)
    files = [
        CaseFile(upload_id=u, file_name=f.file_name, added_at=T0, added_by=ANALYST)
        for u, f in world.files.items()
    ]
    case = CaseStore(storage, clock=world.clock).create(client_id=CLIENT, created_by=ANALYST, files=files)
    return deps, visit.resume(CLIENT, case.case_id, deps, actor=ANALYST)


_SCENARIOS = _scenarios()


def _states(case: Case, goal_id: str) -> list[Case]:
    """The stored case as it was left, and what the rules saw on the way there: the case with no decision, then
    with the ID decided, the outcome too, the build too (what the person had decided at each point)."""
    layers: list[tuple[str, ...]] = [
        (),
        ("key",),
        ("key", "outcome_column"),
        ("key", "outcome_column", "recipe_step"),
        ("key", "outcome_column", "as_of_column"),
        ("key", "outcome_column", "as_of_column", "consent_column"),
    ]
    return [
        case.model_copy(update={"items": tuple(i for i in case.items if i.kind in kinds), "questions": ()})
        for kinds in layers
    ]


def _walk(name: str, tmp_path: Path, seen: set[str]) -> None:
    """One scenario: the rules and the facts agree at each state the visit passes."""
    goal_id, named = _SCENARIOS[name]
    world = _world(goal_id, named, name)
    storage = LocalStorage(tmp_path)
    deps = VisitDeps(storage=storage, load_files=world.load, offers=world.offers, clock=world.clock)
    store = CaseStore(storage, clock=world.clock)
    files = [
        CaseFile(upload_id=u, file_name=f.file_name, added_at=T0, added_by=ANALYST)
        for u, f in world.files.items()
    ]
    fresh = store.create(client_id=CLIENT, created_by=ANALYST, files=files)
    brief = load_goal_brief(goal_id)
    _compare(deps, fresh, None, seen)  # no goal: the key of each file, and what the data can power
    chosen = fresh.model_copy(
        update={"goals": (CaseGoal(goal_id=goal_id, active=True, set_at=T0, set_by=ANALYST),)}
    )
    _compare(deps, chosen, brief, seen)  # the goal chosen, before any rule has run

    case = visit.resume(CLIENT, fresh.case_id, deps, actor=ANALYST)
    case = visit.set_goal(CLIENT, case.case_id, goal_id, deps, expected_version=case.version, actor=ANALYST)
    snapshots = [case]
    for _ in range(4):  # each blocking question answered, then the rules look again
        answered = _answer(deps, case)
        if answered is None:
            break
        case = answered
        snapshots.append(case)
    for snapshot in snapshots:
        stored = visit.stored_brief(storage, snapshot)
        assert stored is not None
        _compare(deps, snapshot, stored, seen)
        for state in _states(snapshot, goal_id):
            _compare(deps, state, stored, seen)


def test_the_facts_suggest_what_the_rules_write(tmp_path: Path) -> None:
    """Every scenario, and together they reach every kind of suggestion the rules make."""
    seen: set[str] = set()
    for name in sorted(_SCENARIOS):
        try:
            (tmp_path / name).mkdir()
            _walk(name, tmp_path / name, seen)
        except AssertionError as exc:
            raise AssertionError(f"scenario {name}: the facts differ from the rules") from exc
    wanted = {
        "item:key",
        "item:outcome_column",
        "item:recipe_step",
        "item:as_of_column",
        "item:consent_column",
        "question:KEY_AMBIGUOUS",
        "question:KEY_NOT_UNIQUE",
        "question:OUTCOME_COLUMN",
        "question:BUILD_AUDIENCE",
        "question:AS_OF_COLUMN",
        "question:DATA_REQUESTED",
        "recommended:OUTCOME_COLUMN",
        "recommended:KEY_NOT_UNIQUE",
        "doubt:outcome_gone",
        "doubt:outcome_missing",
        "doubt:consent_missing",
        "doubt:time_missing",
    }
    assert wanted <= seen, f"the scenarios never reached {sorted(wanted - seen)}"


def test_the_build_is_suggested_through_the_sql_step(tmp_path: Path) -> None:
    """`suggest_build` is the rules' `_offer_build`: one `recipe_step` that went through `propose_sql_step`."""
    goal_id, named = _SCENARIOS["order_log"]
    world = _world(goal_id, named, "order_log")
    deps = VisitDeps(
        storage=LocalStorage(tmp_path), load_files=world.load, offers=world.offers, clock=world.clock
    )
    files = [
        CaseFile(upload_id=u, file_name=f.file_name, added_at=T0, added_by=ANALYST)
        for u, f in world.files.items()
    ]
    fresh = CaseStore(deps.storage, clock=world.clock).create(
        client_id=CLIENT, created_by=ANALYST, files=files
    )
    brief = load_goal_brief(goal_id)
    chosen = fresh.model_copy(
        update={"goals": (CaseGoal(goal_id=goal_id, active=True, set_at=T0, set_by=ANALYST),)}
    )
    # the audience is the person's to pick; with it picked the build is a step with a trial receipt
    first = suggest_build(visit._Run(deps, ANALYST), chosen, brief)
    asked = [s.action.ask for s in first.suggested if isinstance(s.action, AskPerson)]
    assert [a.code for a in asked] == ["BUILD_AUDIENCE"] and first.facts == {"offered": True}
    question = next(s.action.ask for s in first.suggested if isinstance(s.action, AskPerson))
    answered = chosen.model_copy(
        update={
            "questions": (
                CaseQuestion(
                    question_id=question_id_of(question.code, question.subject, question.goal_id),
                    code=question.code,
                    text=question.text,
                    options=(CaseOption(option_id="active", label="all"),),
                    answer="active",
                    goal_id=question.goal_id,
                ),
            )
        }
    )
    run = visit._Run(deps, ANALYST)
    step = visit._Step(to=VisitState.PREVIEW)
    assert visit._offer_build(run, answered, brief, step, run.current(answered).files, T0)
    made = suggest_build(visit._Run(deps, ANALYST), answered, brief)
    [(draft, evidence)] = [
        (s.action.draft, made.evidence) for s in made.suggested if isinstance(s.action, Create)
    ]
    assert [draft_item_id(draft)] == [i.item_id for i in step.items]
    assert draft.payload == step.items[0].payload
    assert draft.payload["step"] and draft.payload["needs_yes"] is True and "buildable" in draft.payload
    assert [e.tool for e in evidence] == ["propose_sql_step"]
    assert draft.evidence_ids == (evidence[0].evidence_id,)


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
    deps = VisitDeps(
        storage=LocalStorage(tmp_path),
        load_files=world.load,
        offers=world.offers,
        clock=world.clock,
        detectors=(_Finds(),),
    )
    files = [
        CaseFile(upload_id=u, file_name=f.file_name, added_at=T0, added_by=ANALYST)
        for u, f in world.files.items()
    ]
    case = CaseStore(deps.storage, clock=world.clock).create(
        client_id=CLIENT, created_by=ANALYST, files=files
    )
    step = visit._Step(to=VisitState.PREVIEW)
    visit._detect(visit._Run(deps, ANALYST), case, None, step)
    found = detector_facts(visit._Run(deps, ANALYST), case, None)
    assert _fact_items([found]) == _rule_items(step)
    assert [d.model_dump() for d in _fact_doubts([found])] == [d.model_dump() for d in step.doubts]
    asks = [s.action.ask.code for s in found.suggested if isinstance(s.action, AskPerson)]
    assert asks == ["CODE_X"]
    assert [q.question_id for q, _e in step.questions if q.code == "DATA_REQUESTED"] == [
        q[0] for q in _fact_questions([found]) if q[1] == "DATA_REQUESTED"
    ]


def test_a_step_on_the_id_or_outcome_column_is_suggested_for_withdrawal(tmp_path: Path) -> None:
    """`_withdraw_role_steps` took such a step back by itself; the fact says it, and the author withdraws it."""
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
    taken = visit._Step(to=VisitState.PREVIEW)
    visit._withdraw_role_steps(case, taken, T0)
    rejected = {
        i.item_id: i.withdrawn_because for i in taken.updates["items"] if i.state is ProposalState.REJECTED
    }
    rejected.pop("step.left")
    suggested = {
        s.action.item_id: s.action.reason
        for s in role_step_facts(case).suggested
        if isinstance(s.action, Withdraw)
    }
    assert suggested == rejected
    assert sorted(suggested) == ["step.on.key", "step.on.outcome"]


def test_suggestions_have_stable_ids_and_the_case_is_not_written(tmp_path: Path) -> None:
    goal_id, named = _SCENARIOS["customers"]
    world = _world(goal_id, named, "customers")
    deps = VisitDeps(
        storage=LocalStorage(tmp_path), load_files=world.load, offers=world.offers, clock=world.clock
    )
    files = [
        CaseFile(upload_id=u, file_name=f.file_name, added_at=T0, added_by=ANALYST)
        for u, f in world.files.items()
    ]
    fresh = CaseStore(deps.storage, clock=world.clock).create(
        client_id=CLIENT, created_by=ANALYST, files=files
    )
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
