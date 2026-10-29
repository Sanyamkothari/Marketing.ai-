"""The AI review: one more check after the rules' TREAT pass, run with a scripted model.

What the owner's rules ask of it, held in code:

* the AI's join becomes a pending, never-ticked item that carries the receipt and findings our code counted;
* a 4th blocking question is dropped; a query that keeps failing its own checks is retried twice, then shown with a
  stop finding;
* no client yes, no spending left, or a blocking question still open: no call to the AI at all;
* it runs once - an answer or a resume does not pay for another;
* a review whose first call the AI service did not answer did not happen: the chat and the timeline say why, what
  was spent still counts, and the review is tried again when TREAT next runs.
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
from engine.agent.loop import UNAVAILABLE
from engine.agent.tools import AgentContext
from engine.config import BudgetConfig, LlmConfig, RunMode, load_use_case
from engine.doctor import visit
from engine.doctor.agent_review import agent_review, lazy_review
from engine.doctor.agent_turn import DoctorTurn
from engine.doctor.ai_guard import AiGuard
from engine.doctor.case import Case, CaseStore, TimelineKind, VisitState
from engine.doctor.config import AiLimitsConfig
from engine.doctor.diagnose import LoadedFile
from engine.doctor.limits import limits
from engine.llm import FakeLLMClient, FakeLLMMode, LLMCompletion
from engine.llm_http import PROBLEMS, LLMHttpError
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
    # recorded against the file its tables come from, with every file it reads, so a case of several files builds it
    assert item.payload["stage"] == "post_build" and item.upload_id == FIRST
    assert len(item.payload["input_uploads"]) == 2 and item.payload["input_uploads"][0] == FIRST
    receipt = item.payload["receipt"]
    assert receipt["rows_out"] == 1_200 and receipt["keys_repeated"] == 0, "counted by our code"
    assert item.payload["step"]["kind"] == "sql_query"
    assert not any(f["level"] == "stop" for f in item.payload["findings"])
    assert any(r.tool == "propose_sql_step" for r in found.evidence), "the evidence the item rests on is kept"
    assert found.model_calls == 3 == client.asked and found.tokens > 0
    assert found.summary == "I added up the orders for each customer."
    assert found.sent, "what was sent to the AI is recorded for the screen"


def test_the_ai_can_look_at_its_working_copy_and_what_it_reads_has_no_personal_value(
    world: World, storage: LocalStorage
) -> None:
    from engine.agent.contracts import DecidedBy, RecipeStep, RecipeStepKind
    from engine.doctor.case import CaseItem, payload_digest

    step = RecipeStep(
        order=1,
        kind=RecipeStepKind.REMOVE_ROWS,
        column="marketing_opt_in",
        params={"operator": "equals", "values": [0]},
        reason="Leave out customers who did not opt in.",
        decided_by=DecidedBy.USER,
    )
    payload: dict[str, Any] = {"upload_id": FIRST, "step": step.model_dump(mode="json")}
    item = CaseItem(
        item_id="i-optin",
        kind="recipe_step",
        title=step.reason,
        source=ProposalSource.RULES,
        confidence=AgentConfidence.SURE,
        state=ProposalState.ACCEPTED,
        upload_id=FIRST,
        payload=payload,
        payload_digest=payload_digest(payload),
        proposed_at=vs.T0,
    )
    case = vs.open_case(storage, world)
    case = case.model_copy(update={"items": (item,)})
    client = Script(
        [
            {"action": "preview_working_copy", "file": "F1", "args": {"rows": 2}},
            {"action": "finish", "text": "I looked at the working copy."},
        ]
    )
    found = _review(_guard(client))(case, _files(world), None)
    looked = [r for r in found.evidence if r.tool == "preview_working_copy"]
    assert looked and looked[0].kind.value == "read", "the look is evidence like any other"
    (shown,) = looked[0].output["files"]
    opted = shown["after"]["columns"].index("marketing_opt_in")
    assert {row[opted] for row in shown["after"]["rows"]} == {"1"}, "the rows left out are not in the copy"
    second = [c for c in client.calls if c.kind == "complete"][1]
    assert "changed_columns" in second.prompt, "the result was put in the next prompt, through the gate"


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
    assert limits().asks.blocking == 3
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


class _Down(Script):
    """A model that fails its first call while `broken`, as `error`, and is billed for it when the error says so."""

    def __init__(self, actions: Iterable[dict[str, Any]], error: LLMHttpError) -> None:
        super().__init__(actions)
        self.error = error
        self.broken = True

    def complete(self, prompt: str, **kwargs: Any) -> LLMCompletion:
        if self.broken:
            super().complete(prompt, **kwargs)  # recorded in `calls`, like any other
            raise self.error
        return super().complete(prompt, **kwargs)


@pytest.mark.parametrize(
    ("error", "kind", "calls"),
    [
        (LLMHttpError("out_of_room", input_tokens=40, output_tokens=4096), "out_of_room", 1),
        (LLMHttpError("key_expired", status=401), "key_expired", 0),
    ],
    ids=["out-of-room-is-billed", "expired-key-is-not"],
)
def test_a_review_the_ai_service_did_not_answer_says_why_and_is_tried_again(
    world: World, storage: LocalStorage, error: LLMHttpError, kind: str, calls: int
) -> None:
    client = _Down([], error)
    port = _review(_guard(client))
    case = _answer_key(storage, world, _opened(storage, world, port), port)
    assert client.asked == 1, "the review's first call was made and not answered"
    assert case.counters.model_calls == calls, "a call the provider billed counts, one it did not does not"
    assert "author.new_file" not in (
        case.visit.done if case.visit else {}
    ), "the review did not happen: it is not remembered, and the files are owed to it again"
    [entry] = case.chat
    sentence, fix = PROBLEMS[kind]
    assert entry.role == "agent" and entry.text == f"{UNAVAILABLE} {sentence} {fix}"
    code = "LLM_REFUSED" if kind == "out_of_room" else "LLM_UNAVAILABLE"
    assert (entry.blocked_by, entry.error_codes) == ("unavailable", (code, f"LLM_{kind.upper()}"))
    [event] = [e for e in CaseStore(storage).timeline(CLIENT, case.case_id) if e.kind is TimelineKind.CHAT]
    assert event.details["review"] is True and event.details["blocked_by"] == "unavailable"
    assert event.details["error_codes"] == f"{code},LLM_{kind.upper()}"
    for text in (entry.text, str(event.details)):
        assert "4096" not in text and "401" not in text, "only our sentences and codes"

    # The service answers again; the next time TREAT runs (a changed answer), the review is made.
    client.broken = False
    q = next(q for q in case.questions if q.code == "KEY_AMBIGUOUS")
    again = visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "account_no"), _deps(storage, world, port),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    assert client.asked == 2 and "author.new_file" in (again.visit.done if again.visit else {})
    assert [(c.text, c.blocked_by) for c in again.chat[-1:]] == [
        ("I am done.", None)
    ], "its own words this time"


def test_a_review_that_fails_the_same_way_again_is_not_told_twice_in_the_chat(
    world: World, storage: LocalStorage
) -> None:
    """The cause was said last; saying it again would only repeat it. The timeline still records each try."""
    client = _Down([], LLMHttpError("key_expired", status=401))
    port = _review(_guard(client))
    case = _answer_key(storage, world, _opened(storage, world, port), port)
    [told] = case.chat
    q = next(q for q in case.questions if q.code == "KEY_AMBIGUOUS")
    again = visit.answer(
        CLIENT, case.case_id, q.question_id, option(q, "account_no"), _deps(storage, world, port),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    assert client.asked == 2, "the review was tried again"
    assert again.chat == (told,), "and failed the same way, which the chat had said"
    tries = [e for e in CaseStore(storage).timeline(CLIENT, case.case_id) if e.details.get("review")]
    assert len(tries) == 2


def test_a_review_that_got_some_answers_before_the_service_failed_stays_done(
    world: World, storage: LocalStorage
) -> None:
    """Partial work stands as it always did; only a review that never got an answer is asked for again."""

    class _DiesSecond(Script):
        replied = 0

        def complete(self, prompt: str, **kwargs: Any) -> LLMCompletion:
            if self.replied:
                raise LLMHttpError("key_expired", status=401)
            self.replied += 1
            return super().complete(prompt, **kwargs)

    client = _DiesSecond([{"action": "get_profile", "file": "F1", "args": {}}])
    port = _review(_guard(client))
    case = _answer_key(storage, world, _opened(storage, world, port), port)
    assert client.replied == 1 and client.asked == 1, "the second call failed"
    assert "author.new_file" in (
        case.visit.done if case.visit else {}
    ), "one reply was enough for it to have happened"
    assert not case.chat, "and it found nothing to say"


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


# -- doubts and requests for more data (the design §6.7, §6.8) ------------------------------------------------
def _doubt(
    *, meaning: bool = False, because: str = "the `age` column may be in months", **extra: Any
) -> dict[str, Any]:
    args = {
        "because": because,
        "would_do": "leave the column as written and flag it",
        "if_not": "the ages would be read ten times too large",
        "evidence_ids": ["r1"],
        "meaning": meaning,
    }
    return {"action": "express_doubt", "file": "F1", "args": {**args, **extra}}


def _request(**extra: Any) -> dict[str, Any]:
    args = {
        "kind": "more_data",
        "what": "the date of each customer's last order",
        "why": "Win back needs it.",
        "columns": ["customer_id", "last_order_date"],
        "ask_whom": "the billing team",
    }
    return {"action": "request_data", "file": "F1", "args": {**args, **extra}}


_LOOK = {"action": "get_profile", "file": "F1", "args": {}}


def test_a_doubt_is_kept_in_three_parts_and_does_not_block(world: World, storage: LocalStorage) -> None:
    found, _ = _run(world, storage, Script([_LOOK, _doubt()]))
    (doubt,) = found.doubts
    assert doubt.because.startswith("the `age` column") and doubt.would_do and doubt.if_not
    assert doubt.evidence_ids, "it rests on the look it cites"
    assert found.asks == () and found.items == (), "a doubt alone asks nothing and changes nothing"


def test_a_doubt_with_a_number_no_look_holds_is_refused(world: World, storage: LocalStorage) -> None:
    bad = _doubt(because="the `age` column has 4,417 values in months")
    found, _ = _run(world, storage, Script([_LOOK, bad]))
    assert found.doubts == () and found.asks == ()


def test_a_doubt_about_meaning_also_asks_and_blocks_but_changes_nothing(
    world: World, storage: LocalStorage
) -> None:
    found, _ = _run(world, storage, Script([_LOOK, _doubt(meaning=True)]))
    assert len(found.doubts) == 1
    (ask,) = found.asks
    assert ask.code == "DOUBT_MEANING" and ask.blocking and ask.meaning_bearing and ask.recommended is None
    assert all(option.effect.kind is None for option in ask.options), "no answer changes any data"
    assert found.items == ()


def test_a_meaning_doubt_counts_against_the_blocking_limit(world: World, storage: LocalStorage) -> None:
    script = [_LOOK, *(_doubt(meaning=True, because=f"column {n} may mean something else") for n in range(4))]
    found, _ = _run(world, storage, Script(script))
    assert len(found.asks) == limits().asks.blocking == len(found.doubts)


def test_a_request_for_more_data_names_what_the_file_and_whom(world: World, storage: LocalStorage) -> None:
    found, _ = _run(world, storage, Script([_request()]))
    (request,) = found.requests
    assert request.kind == "more_data" and request.ask_whom == "the billing team"
    assert request.columns == ("customer_id", "last_order_date") and request.per == "one row per customer"
    assert found.items == () and found.asks == ()


def test_a_request_without_a_reason_or_with_an_unknown_kind_is_refused(
    world: World, storage: LocalStorage
) -> None:
    found, _ = _run(world, storage, Script([_request(why=""), _request(kind="delete_everything")]))
    assert found.requests == ()


def test_doubts_and_requests_reach_the_case_and_only_a_meaning_doubt_blocks(
    world: World, storage: LocalStorage
) -> None:
    client = Script([_LOOK, _doubt(), _request(), {"action": "finish", "text": "Two things to read."}])
    port = _review(_guard(client))
    case = _answer_key(storage, world, _opened(storage, world, port), port)
    assert [d.because.startswith("the `age`") for d in case.doubts] == [True]
    (asked,) = [q for q in case.questions if q.code == "DATA_REQUESTED"]
    assert not asked.blocking and asked.answer is None
    assert not visit.open_blocking(case), "a doubt about anything else, and a request, do not stop approval"
    assert not any(i.state is ProposalState.ACCEPTED and i.source is ProposalSource.AI for i in case.items)


def test_a_meaning_doubt_stops_approval_until_the_person_answers(world: World, storage: LocalStorage) -> None:
    client = Script([_LOOK, _doubt(meaning=True), {"action": "finish", "text": "One thing to check."}])
    port = _review(_guard(client))
    case = _answer_key(storage, world, _opened(storage, world, port), port)
    (blocking,) = visit.open_blocking(case)
    assert blocking.code == "DOUBT_MEANING" and blocking.recommended is None
    assert case.state is VisitState.ASK
    answered = visit.answer(
        CLIENT, case.case_id, blocking.question_id, option(blocking, "Yes, that is right"), _deps(storage, world, port),
        expected_version=case.version, actor=ANALYST,
    )  # fmt: skip
    assert not visit.open_blocking(answered) and answered.doubts == case.doubts


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


def _goal_case(storage: LocalStorage, world: World) -> Case:
    from engine.doctor.case import CaseGoal

    case = vs.open_case(storage, world)
    goal = CaseGoal(goal_id="retail-win-back", active=True, set_at=vs.T0, set_by=ANALYST)
    return case.model_copy(update={"goals": (goal,)})


def test_the_goal_brief_is_callable_for_the_active_goal_only(world: World, storage: LocalStorage) -> None:
    case = _goal_case(storage, world)
    client = Script(
        [
            {
                "action": "load_goal_brief",
                "file": "F1",
                "args": {"goal_id": "retail-win-back", "mode": "train"},
            },
            {"action": "load_goal_brief", "file": "F1", "args": {"goal_id": "telco-churn"}},
            {"action": "finish", "text": "I read the goal."},
        ]
    )
    found = _review(_guard(client))(case, _files(world), None)
    briefs = [r for r in found.evidence if r.tool == "load_goal_brief"]
    assert len(briefs) == 1 and briefs[0].output["goal_id"] == "retail-win-back"
    assert briefs[0].output["brief_hash"], "the brief comes back through the registry"
    sent = [s for s in found.sent if s["tool"] == "load_goal_brief"]
    assert len(sent) == 1, "and is recorded as sent through the gate"
    assert "retail-win-back" in str(sent[0]), "with the goal named in what was sent"


def test_a_brief_of_another_goal_is_refused_as_unknown_and_the_mode_is_the_visits(
    world: World, storage: LocalStorage
) -> None:
    from engine.agent.tools import AgentToolError

    review = _build(_goal_case(storage, world), world)
    with pytest.raises(AgentToolError) as raised:
        review._act({"action": "load_goal_brief", "file": "F1", "args": {"goal_id": "telco-churn"}})
    assert raised.value.code == "AGENT_GOAL_UNKNOWN"
    review._act(
        {"action": "load_goal_brief", "file": "F1", "args": {"goal_id": "retail-win-back", "mode": "audit"}}
    )
    assert review.turn.looks[-1].result["mode"] == "train", "the model cannot change the visit's mode"


def _named(tools: list[dict[str, str]]) -> list[str]:
    return [t["name"] for t in tools]


@pytest.mark.parametrize("with_goal", [False, True])
def test_every_tool_the_prompt_lists_is_a_tool_the_ai_can_call(
    world: World, storage: LocalStorage, with_goal: bool
) -> None:
    """A guard: the turn's listed tools all dispatch (never AGENT_TOOL_UNKNOWN)."""
    from engine.agent.tools import AgentToolError

    case = _goal_case(storage, world) if with_goal else vs.open_case(storage, world)
    checked = 0
    review = _build(case, world)
    listed = _named(review._tools())
    assert ("load_goal_brief" in listed) is with_goal
    for name in listed:
        try:
            review._act({"action": name, "file": "F1", "args": {}})
        except AgentToolError as exc:
            assert exc.code != "AGENT_TOOL_UNKNOWN", f"the turn lists {name} but cannot call it"
        except Exception:
            pass
        checked += 1
    assert checked >= 2


def _build(case: Case, world: World) -> DoctorTurn:
    def context_for(_case: Case, files: tuple[LoadedFile, ...]) -> AgentContext:
        return AgentContext(
            use_case_id=GOAL_A,
            config=load_use_case(GOAL_A),
            config_root=None,
            upload_id=files[0].upload_id,
            mode=RunMode.TRAIN,
            profile=files[0].profile,
            frame=files[0].frame,
        )

    return DoctorTurn(
        case,
        _files(world),
        None,
        context_for,
        _guard(Script([])),
        guardrails(),
        ASHA,
        None,
        _Cached(visit.visit_registry(), _MEMORY),
        chat_steps=3,
    )


def test_the_plan_view_lists_the_ais_own_waiting_query(world: World, storage: LocalStorage) -> None:
    from engine.doctor.case import CaseItem, payload_digest

    payload: dict[str, Any] = {
        "step": {
            "order": 1,
            "kind": "sql_query",
            "params": {"sql": "select 1", "output_name": "t"},
            "reason": "r",
        }
    }
    item = CaseItem(
        item_id="i-query",
        kind="recipe_step",
        title="Table t",
        source=ProposalSource.AI,
        confidence=AgentConfidence.CHECK,
        state=ProposalState.PENDING,
        upload_id=None,
        payload=payload,
        payload_digest=payload_digest(payload),
        proposed_at=vs.T0,
    )
    case = vs.open_case(storage, world).model_copy(update={"items": (item,)})
    client = Script([{"action": "finish", "text": "Looked."}])
    _review(_guard(client))(case, _files(world), None)
    shown = next(c for c in client.calls if c.kind == "complete").prompt
    assert '"by": "you"' in shown and '"state": "waiting"' in shown and "all files" in shown


def test_the_ai_proposes_behaviour_features_and_they_are_a_pending_never_ticked_item(
    world: World, storage: LocalStorage
) -> None:
    orders = _orders(world)
    days = pd.date_range("2025-01-01", periods=len(orders.frame), freq="h")
    dated = orders.frame.assign(order_date=[d.strftime("%Y-%m-%d") for d in days])
    files = (world.files[FIRST], vs.loaded("up-orders", "orders.csv", dated, world.offers[0].config))
    action = {
        "action": "propose_features",
        "file": "F2",
        "args": {
            "entity_key": "customer_id",
            "time_column": "order_date",
            "as_of": "2025-04-01",
            "preset": "rfm",
            "amount_column": "amount",
            "order_column": "order_id",
        },
    }
    client = Script([action, {"action": "finish", "text": "I added behaviour features."}])
    case = vs.open_case(storage, world)
    found = _review(_guard(client))(case, files, None)
    assert len(found.items) == 1
    item = found.items[0]
    assert item.source is ProposalSource.AI and item.state is ProposalState.PENDING
    assert item.payload["step"]["kind"] == "sql_query" and item.payload["needs_yes"] in (True, False)
    assert [f["name"] for f in item.payload["features"]][:2] == ["days_since_last_order", "orders_90d"]
    assert "as_of" not in item.payload and item.payload["features_as_of"] == "2025-04-01"
    assert not any(
        f["code"].startswith("FEATURE_") and f["level"] == "stop" for f in item.payload["findings"]
    )
    assert any(r.tool == "propose_features" for r in found.evidence)


def test_read_guide_carries_the_rules_and_the_section_once_and_the_system_text_stays_the_same(
    world: World, storage: LocalStorage
) -> None:
    read = {"action": "read_guide", "args": {"name": "features", "section": "Buckets and interactions"}}
    client = Script(
        [
            read,
            read,
            {"action": "read_guide", "args": {"name": "nope"}},
            {"action": "finish", "text": "Looked."},
        ]
    )
    _review(_guard(client))(vs.open_case(storage, world), _files(world), None)
    calls = [c for c in client.calls if c.kind == "complete"]
    assert len(calls) == 4
    assert len({c.system for c in calls}) == 1  # the stable part is byte-identical whatever the results are
    last = calls[-1].prompt
    assert last.count("Never target-encode") == 1  # the Rules, once, though the guide was asked for twice
    assert last.count("Buckets follow the use case") == 1
    assert "already read above" in last
    assert "Guides:" in last and "AGENT_GUIDE_UNKNOWN" in last  # the refusal lists the valid names
    assert "One-hot, scaling" not in last  # a section that was not asked for is not sent
    assert (
        "## Guides" in calls[0].system and "- features:" in calls[0].system
    )  # the index is in the prompt, the guides are not
    assert "Never target-encode" not in calls[0].system
    assert "Never target-encode" not in calls[0].prompt  # not sent before it was read


def test_a_refused_read_guide_is_not_a_strike_and_a_guide_part_is_carried_once(
    world: World, storage: LocalStorage
) -> None:
    def read(section: str | None) -> dict[str, Any]:
        args = {"name": "features", **({"section": section} if section else {})}
        return {"action": "read_guide", "args": args}

    client = Script(
        [
            read("nope"),
            read("nada"),
            {"action": "read_guide", "args": {"name": "zzz"}},
            read("What to build"),
            read("Receipt"),  # the Rules are already above: only the section is sent
            read("buckets"),  # a unique start of a title is enough
            read(None),  # the whole guide
            read("Receipt"),  # in the whole guide already
            {"action": "finish", "text": "Looked."},
        ]
    )
    _review(_guard(client))(vs.open_case(storage, world), _files(world), None)
    calls = [c for c in client.calls if c.kind == "complete"]
    assert len(calls) == 9  # three refusals did not end the review
    assert calls[4].prompt.count("Never target-encode") == 1
    assert calls[5].prompt.count("Never target-encode") == 1 and "Fill rate per feature" in calls[5].prompt
    assert (
        calls[6].prompt.count("Never target-encode") == 1 and "Buckets follow the use case" in calls[6].prompt
    )
    assert calls[7].prompt.count("Never target-encode") == 2  # the whole guide after parts is sent whole
    assert "already read above" in calls[8].prompt
