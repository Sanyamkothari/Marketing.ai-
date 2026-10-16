"""The AI review: one more check after the rules' TREAT pass, run with a scripted model.

What the owner's rules ask of it, held in code:

* the AI's join becomes a pending, never-ticked item that carries the receipt and findings our code counted;
* a 4th blocking question is dropped; a query that keeps failing its own checks is retried twice, then shown with a
  stop finding;
* no client yes, no spending left, or a blocking question still open: no call to the AI at all;
* it runs once - an answer or a resume does not pay for another.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.agent.tools import AgentContext
from engine.config import BudgetConfig, LlmConfig, RunMode, load_use_case
from engine.doctor import visit
from engine.doctor.agent_review import MAX_BLOCKING, agent_review, lazy_review
from engine.doctor.ai_guard import AiGuard
from engine.doctor.case import Case, CaseStore, VisitState
from engine.doctor.config import AiLimitsConfig
from engine.doctor.diagnose import LoadedFile
from engine.llm import FakeLLMClient, FakeLLMMode
from engine.storage import LocalStorage
from tests.unit.agent.egress_canary import guardrails
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.test_visit import _MEMORY, _Cached
from tests.unit.doctor.visit_support import ANALYST, CLIENT, FIRST, GOAL_A, World, option, question

ASHA = Principal(user_id=ANALYST, username="asha", roles=frozenset({Role.ANALYST}), kind="user")
JOIN = (
    "select c.customer_id, sum(o.amount) as total_amount from t_customers_oct c "
    "left join t_orders o on c.customer_id = o.customer_id group by c.customer_id"
)


class Script(FakeLLMClient):
    """A scripted model: one JSON action per call, `finish` when the script runs out."""

    def __init__(self, actions: Iterable[dict[str, Any]]) -> None:
        super().__init__(mode=FakeLLMMode.GROUNDED)
        self.queue = list(actions)

    def _body(self, system: str, user: str) -> str:
        del system, user
        return json.dumps(self.queue.pop(0) if self.queue else {"action": "finish", "text": "I am done."})

    @property
    def asked(self) -> int:
        return len([c for c in self.calls if c.kind == "complete"])


@pytest.fixture
def world() -> World:
    return vs.world()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def _orders(world: World) -> LoadedFile:
    rng = np.random.default_rng(7)
    frame = pd.DataFrame(
        {
            "order_id": [f"O{i}" for i in range(3_000)],
            "customer_id": [f"C{100000 + int(i)}" for i in rng.integers(0, 1_200, 3_000)],
            "amount": np.round(rng.gamma(2.0, 300.0, 3_000), 2),
        }
    )
    return vs.loaded("up-orders", "orders.csv", frame, world.offers[0].config)


def _files(world: World) -> tuple[LoadedFile, ...]:
    return (world.files[FIRST], _orders(world))


def _guard(client: Script, *, consent: bool = True, calls: int = 60) -> AiGuard:
    return AiGuard(
        client,
        job_id="test",
        llm=LlmConfig(),
        budget=BudgetConfig(cache=False, max_calls_per_run=100_000, max_cost_usd_per_run=1_000.0),
        limits=AiLimitsConfig(max_calls_per_case=calls, max_tokens_per_case=600_000),
        consented=lambda _client: consent,
    )


def _review(guard: AiGuard) -> visit.DetectorPort:
    def context_for(case: Case, files: tuple[LoadedFile, ...]) -> AgentContext:
        config = load_use_case(GOAL_A)
        return AgentContext(
            use_case_id=GOAL_A,
            config=config,
            config_root=None,
            upload_id=files[0].upload_id,
            mode=RunMode.TRAIN,
            profile=files[0].profile,
            frame=files[0].frame,
        )

    return agent_review(
        context_for,
        meter=guard,
        guardrails=guardrails(),
        principal=ASHA,
        registry=_Cached(visit.visit_registry(), _MEMORY),
    )


def _sql(expect: dict[str, Any], name: str = "customer_totals") -> dict[str, Any]:
    return {
        "action": "propose_sql",
        "args": {
            "description": "Add up the order amount per customer",
            "sql": JOIN,
            "output_name": name,
            "files": ["F1", "F2"],
            "expect": expect,
        },
    }


def _ask(column: str) -> dict[str, Any]:
    return {
        "action": "ask_question",
        "file": "F1",
        "args": {
            "question": "Q1",
            "column": column,
            "values": {"file": "customers_oct.csv"},
            "choices": ["customer_id", "account_no"],
        },
    }


def _run(world: World, storage: LocalStorage, client: Script, **kwargs: Any) -> tuple[Any, Script]:
    case = vs.open_case(storage, world)
    return _review(_guard(client, **kwargs))(case, _files(world), None), client


def test_the_ai_proposes_a_join_and_it_is_a_pending_item_with_our_receipt(
    world: World, storage: LocalStorage
) -> None:
    client = Script(
        [
            {"action": "get_profile", "file": "F2", "args": {}},
            _sql({"keys_unique": True, "max_keys_lost_pct": 0}),
            {"action": "finish", "text": "I added up the orders for each customer."},
        ]
    )
    found, _ = _run(world, storage, client)
    assert len(found.items) == 1
    item = found.items[0]
    assert item.source is ProposalSource.AI
    assert item.state is ProposalState.PENDING and item.confidence is AgentConfidence.CHECK
    assert item.payload["stage"] == "post_build" and item.upload_id is None
    receipt = item.payload["receipt"]
    assert receipt["rows_out"] == 1_200 and receipt["keys_repeated"] == 0, "counted by our code"
    assert item.payload["step"]["kind"] == "sql_query"
    assert not any(f["level"] == "stop" for f in item.payload["findings"])
    assert any(r.tool == "propose_sql_step" for r in found.evidence), "the evidence the item rests on is kept"
    assert found.model_calls == 3 == client.asked and found.tokens > 0
    assert found.summary == "I added up the orders for each customer."
    assert found.sent, "what was sent to the AI is recorded for the screen"


def test_a_query_that_keeps_failing_its_own_checks_is_retried_twice_then_shown_with_a_stop(
    world: World, storage: LocalStorage
) -> None:
    wrong = {"rows_out_equals": 5}
    client = Script([_sql(wrong), _sql(wrong), _sql(wrong), _sql(wrong)])
    found, _ = _run(world, storage, client)
    assert client.asked >= 4, "the fourth try was asked for"
    assert len(found.items) == 1, "one item, not one per try"
    findings = found.items[0].payload["findings"]
    stops = [f for f in findings if f["code"] == "EXPECTATION_FAILED"]
    assert stops and "too often" in stops[-1]["message"]
    assert found.items[0].payload["needs_yes"] is True


def test_a_query_that_passes_on_the_retry_is_flagged_for_the_changed_checks_not_stopped(
    world: World, storage: LocalStorage
) -> None:
    client = Script([_sql({"rows_out_equals": 5}), _sql({"rows_out_equals": 1_200})])
    found, _ = _run(world, storage, client)
    codes = {(f["level"], f["code"]) for f in found.items[0].payload["findings"]}
    assert ("flag", "EXPECTATION_CHANGED_AFTER_FAILURE") in codes, "a rewritten check is shown to the person"
    assert not any(level == "stop" and code == "EXPECTATION_FAILED" for level, code in codes)


def test_a_rewritten_check_stays_flagged_when_the_query_is_sent_again(
    world: World, storage: LocalStorage
) -> None:
    passing = {"rows_out_equals": 1_200}
    client = Script([_sql({"rows_out_equals": 5}), _sql(passing), _sql(passing)])
    found, _ = _run(world, storage, client)
    codes = {f["code"] for f in found.items[0].payload["findings"]}
    assert "EXPECTATION_CHANGED_AFTER_FAILURE" in codes, "a later clean try does not wash out the rewrite"


def test_a_fourth_blocking_question_is_dropped(world: World, storage: LocalStorage) -> None:
    columns = ["customer_id", "account_no", "age", "visits_90d"]
    client = Script([_ask(c) for c in columns])
    found, _ = _run(world, storage, client)
    assert MAX_BLOCKING == 3
    assert len(found.asks) == 3 and all(a.blocking for a in found.asks)
    assert [a.column for a in found.asks] == columns[:3]
    assert all(a.proven is None for a in found.asks), "the AI can never prove an answer"


def test_a_question_with_a_number_nobody_measured_is_refused(world: World, storage: LocalStorage) -> None:
    bad = {
        "action": "ask_question",
        "file": "F1",
        "args": {
            "question": "Q2",
            "column": "age",
            "values": {"count": 4_417, "file": "customers_oct.csv"},
            "choices": [],
        },
    }
    found, _ = _run(world, storage, Script([bad]))
    assert found.asks == (), "4,417 is in no tool result"


def test_no_consent_means_no_call_to_the_ai(world: World, storage: LocalStorage) -> None:
    client = Script([])
    found, _ = _run(world, storage, client, consent=False)
    assert client.asked == 0 and not client.calls
    assert found.items == () and found.model_calls == 0 and found.summary == ""


def test_a_case_with_no_spending_left_makes_no_call(world: World, storage: LocalStorage) -> None:
    client = Script([])
    case = vs.open_case(storage, world)
    spent = case.model_copy(update={"counters": case.counters.model_copy(update={"model_calls": 5})})
    found = _review(_guard(client, calls=5))(spent, _files(world), None)
    assert client.asked == 0 and found.items == ()


def test_no_ai_service_means_the_review_finds_nothing(world: World, storage: LocalStorage) -> None:
    found = lazy_review(lambda: None)(vs.open_case(storage, world), _files(world), None)
    assert found.items == () and found.asks == () and found.model_calls == 0


def test_two_bad_replies_end_the_review_and_keep_what_was_made(world: World, storage: LocalStorage) -> None:
    client = Script([{"action": "no_such_thing"}, {"action": "drop_all_rows"}, _ask("age")])
    found, _ = _run(world, storage, client)
    assert client.asked == 2 and found.asks == ()


# -- inside the visit ----------------------------------------------------------------------------------------
def _deps(storage: LocalStorage, world: World, port: visit.DetectorPort) -> visit.VisitDeps:
    return vs.deps_for(storage, world, registry=_Cached(visit.visit_registry(), _MEMORY), agent=port)


def _opened(storage: LocalStorage, world: World, port: visit.DetectorPort) -> Case:
    created = CaseStore(storage, clock=world.clock).create(
        client_id=CLIENT, created_by=ANALYST, files=[vs.case_file(FIRST, world)]
    )
    return visit.resume(CLIENT, created.case_id, _deps(storage, world, port), actor=ANALYST)


def _answer_key(storage: LocalStorage, world: World, case: Case, port: visit.DetectorPort) -> Case:
    q = question(case, "KEY_AMBIGUOUS")
    return visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "customer_id"), _deps(storage, world, port),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip


def test_the_review_waits_for_the_blocking_question_then_runs_once(
    world: World, storage: LocalStorage
) -> None:
    client = Script(
        [
            {"action": "get_profile", "file": "F1", "args": {}},
            {"action": "finish", "text": "Everything I looked at is in order."},
        ]
    )
    port = _review(_guard(client))
    case = _opened(storage, world, port)
    assert case.state is VisitState.ASK and client.asked == 0, "no AI on a guess"
    case = _answer_key(storage, world, case, port)
    assert client.asked == 2, "the review ran once the key was decided"
    assert case.counters.model_calls == 2 and case.counters.tokens > 0, "its spending is on the case"
    assert case.chat and case.chat[-1].role == "agent" and "in order" in case.chat[-1].text
    asked = client.asked
    again = visit.resume(CLIENT, case.case_id, _deps(storage, world, port), actor=ANALYST)
    assert client.asked == asked, "a resume does not pay for another review"
    assert again.version >= case.version


def _dirty(world: World) -> World:
    """One customers file whose `city` is spelled several ways, so a spelling fix changes values."""
    frame = vs.customers(seed=1)
    frame["city"] = np.resize(["Pune", "pune ", "PUNE", "Mumbai", "mumbai", "Mumbai "], len(frame))
    return World(
        files={FIRST: vs.loaded(FIRST, "customers_oct.csv", frame, world.offers[0].config)},
        offers=world.offers,
    )


def _step_column(item: Any) -> Any:
    step = item.payload["step"]
    assert isinstance(step, dict)
    return step["column"]


def test_nothing_the_review_proposes_is_ticked_in_the_case(world: World, storage: LocalStorage) -> None:
    world = _dirty(world)
    client = Script(
        [
            {"action": "inspect_column", "file": "F1", "args": {"column": "city"}},
            {
                "action": "propose_step",
                "file": "F1",
                "args": {
                    "kind": "normalise_text",
                    "column": "city",
                    "params": {},
                    "reason": "Pune is spelled three ways.",
                    "evidence_ids": ["r1"],
                },
            },
        ]
    )
    port = _review(_guard(client))
    case = _answer_key(storage, world, _opened(storage, world, port), port)
    ai_items = [i for i in case.items if i.source is ProposalSource.AI]
    assert len(ai_items) == 1 and _step_column(ai_items[0]) == "city"
    assert ai_items[0].state is ProposalState.PENDING and ai_items[0].confidence is AgentConfidence.CHECK


def test_no_consent_inside_the_visit_the_rules_go_on_alone(world: World, storage: LocalStorage) -> None:
    client = Script([])
    port = _review(_guard(client, consent=False))
    case = _answer_key(storage, world, _opened(storage, world, port), port)
    assert client.asked == 0 and case.state in (VisitState.APPROVE, VisitState.PREVIEW)
    assert not any(i.source is ProposalSource.AI for i in case.items)


def _ask_with(effects: list[Any] | None) -> dict[str, Any]:
    ask = _ask("customer_id")
    if effects is not None:
        ask["args"]["effects"] = effects
    return ask


def test_the_models_sentence_for_each_choice_is_kept_when_it_states_no_number(
    world: World, storage: LocalStorage
) -> None:
    said = ["Joins the files on the customer ID.", "Joins the files on the account number."]
    found, _ = _run(world, storage, Script([_ask_with(said)]))
    options = found.asks[0].options
    assert [o.says for o in options[:2]] == said
    assert options[-1].says == "", "'I don't know' says nothing of its own"


def test_a_sentence_with_a_number_or_none_is_dropped_and_the_catalogue_adds_none_of_its_own(
    world: World, storage: LocalStorage
) -> None:
    for effects in (
        None,
        ["Drops 4,000 customers.", "Joins the files on the account number."],
        ["a", "b", "c"],
    ):
        found, _ = _run(world, storage, Script([_ask_with(effects)]))
        says = [o.says for o in found.asks[0].options[:2]]
        if effects and len(effects) == 2:
            assert says == ["", effects[1]], "only the sentence with a number is dropped"
        else:
            assert says == ["", ""], effects


def test_an_answer_with_no_step_says_so_instead_of_claiming_an_outcome(
    world: World, storage: LocalStorage
) -> None:
    from engine.doctor.protocol import NO_STEP, settle

    found, _ = _run(
        world, storage, Script([_ask_with(["Joins on the customer ID.", "Joins on the account number."])])
    )
    settled = settle(found.asks[0], now=datetime(2026, 1, 1, tzinfo=UTC))
    assert settled.question is not None
    # An AI-made question carries no step, so answering it changes nothing: the screen may not claim an outcome.
    assert [o.effect for o in settled.question.options[:2]] == [NO_STEP, NO_STEP]
