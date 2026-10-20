"""What counts as a yes (Q24) decides the outcome the model learns from.

The person types a rule, the agent proposes ONE query step that builds a 0/1 column on the customer table and asks Q24;
our code counts the yes rate; only the person's click records the outcome (and ticks the query). Around it: a column
the file already has with its yes values, the rules' own chips without an AI, a rule that reads the window after the
as-of date only for the outcome (a feature built from it is refused), and what the prescription, the replay, the
readiness check and the Ready sentence then say.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.contracts import ProposalSource, ProposalState
from engine.config import RunMode, load_use_case
from engine.doctor import registry as doctor_registry
from engine.doctor import visit
from engine.doctor.case import Case
from engine.doctor.diagnose import Offer
from engine.doctor.prescription import prescription_from_case
from engine.doctor.prescription_replay import ReplayFile, replay_prescription
from engine.doctor.readiness import ReadinessInputs, check_readiness
from engine.doctor.readiness_probes import SourceScan
from engine.storage import LocalStorage
from tests.fixtures.doctor_eval.chat_model import ChatScript
from tests.unit.doctor import visit_support as vs
from tests.unit.doctor.chat_support import Talk
from tests.unit.doctor.visit_support import ANALYST, GOAL_A, World

ORDERS = "up-orders"
AS_OF = "2026-09-30"
RULE = "a sale = an order over 500 within 30 days"
TYPED = "a sale is an order over 500 within 30 days of the end of September"
CUSTOMERS = 2_000

SALE_SQL = (
    "SELECT c.*, COALESCE(w.is_sale, 0) AS is_sale FROM t_customers c LEFT JOIN ("
    "SELECT customer_id, 1 AS is_sale FROM t_orders WHERE order_amount > 500 "
    "AND CAST(order_date AS DATE) > $as_of AND CAST(order_date AS DATE) <= $as_of + INTERVAL 30 DAY "
    "GROUP BY customer_id) w ON c.customer_id = w.customer_id"
)
LEAKY_SQL = (
    "SELECT c.*, COALESCE(w.is_sale, 0) AS is_sale, COALESCE(f.spent_in_window, 0) AS spent_in_window "
    "FROM t_customers c LEFT JOIN ("
    "SELECT customer_id, 1 AS is_sale FROM t_orders WHERE order_amount > 500 "
    "AND CAST(order_date AS DATE) > $as_of AND CAST(order_date AS DATE) <= $as_of + INTERVAL 30 DAY "
    "GROUP BY customer_id) w ON c.customer_id = w.customer_id LEFT JOIN ("
    "SELECT customer_id, SUM(order_amount) AS spent_in_window FROM t_orders "
    "WHERE CAST(order_date AS DATE) > $as_of GROUP BY customer_id) f ON c.customer_id = f.customer_id"
)


@pytest.fixture(autouse=True)
def unified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_registry, "unified_on", lambda _config: True)


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "data")


def customers_frame() -> pd.DataFrame:
    return vs.customers(rows=CUSTOMERS, seed=3).drop(columns=["converted", "churned", "snapshot_date"])


def orders_frame() -> pd.DataFrame:
    """Orders before and after 2026-09-30: those in the 30 days after it are the outcome window."""
    rng = np.random.default_rng(8)
    rows = 5_000
    ids = rng.integers(0, CUSTOMERS, rows)
    days = pd.Timestamp("2026-07-01") + pd.to_timedelta(rng.integers(0, 125, rows), unit="D")
    return pd.DataFrame(
        {
            "customer_id": [f"C{100000 + i}" for i in ids],
            "order_amount": np.round(rng.gamma(2.0, 260.0, rows), 2),
            "order_date": days.strftime("%Y-%m-%d"),
        }
    )


def truth() -> tuple[int, int]:
    """Customers with an order over 500 in the 30 days after the as-of date, and all customers."""
    orders = orders_frame()
    day = pd.to_datetime(orders["order_date"])
    window = orders[
        (orders["order_amount"] > 500) & (day > AS_OF) & (day <= pd.Timestamp(AS_OF) + pd.Timedelta(days=30))
    ]
    return int(window["customer_id"].nunique()), CUSTOMERS


def world() -> World:
    config = load_use_case(GOAL_A)
    return World(
        files={
            vs.FIRST: vs.loaded(vs.FIRST, "customers.csv", customers_frame(), config),
            ORDERS: vs.loaded(ORDERS, "orders.csv", orders_frame(), config),
        },
        offers=(Offer(config=config, stage="Acquire"),),
    )


def propose(sql: str = SALE_SQL, name: str = "customer_sales") -> dict[str, Any]:
    return {
        "action": "propose_sql",
        "args": {
            "description": "Mark each customer who ordered over 500 in the 30 days after the as-of date",
            "sql": sql,
            "output_name": name,
            "files": ["F1", "F2"],
            "as_of": AS_OF,
            "reads_future": True,
            "expect": {"keys_unique": True, "rows_out_equals": CUSTOMERS},
        },
    }


def ask_sale(column: str = "is_sale", name: str = "customer_sales") -> dict[str, Any]:
    return {
        "action": "ask",
        "file": "F1",
        "args": {
            "question": "Q24",
            "values": {"file": "customers.csv"},
            "choices": [RULE],
            "outcomes": [{"column": column, "sql": name}],
        },
    }


def suggest(seen: Any) -> dict[str, Any]:
    return {"action": "suggest_answer", "args": {"question_id": seen.last("question_id"), "option_id": "c0"}}


def reply(text: str) -> dict[str, Any]:
    return {"action": "reply", "text": text}


def talk_with(storage: LocalStorage, script: dict[str, list[Any]]) -> Talk:
    talk = Talk(storage, world(), ChatScript(script), files=(vs.FIRST, ORDERS))
    talk.goal()
    return talk


def accepted_outcomes(case: Case) -> list[Any]:
    return [i for i in case.items if i.kind == "outcome_column" and i.state is ProposalState.ACCEPTED]


def sale_script(sql: str = SALE_SQL) -> dict[str, list[Any]]:
    return {
        TYPED: [
            propose(sql),
            ask_sale(),
            suggest,
            reply(
                "I understood: a sale is an order over 500 in the 30 days after the end of September. Use this?"
            ),
        ]
    }


# ---------------------------------------------------------------------------
# The typed rule becomes the outcome
# ---------------------------------------------------------------------------
def test_a_typed_rule_is_counted_asked_and_only_the_persons_click_makes_it_the_outcome(
    storage: LocalStorage,
) -> None:
    yes, total = truth()
    talk = talk_with(storage, sale_script())
    case = talk.say(TYPED)

    # The query step is the agent's proposal: it waits for the person, it is never applied for them.
    (query,) = [i for i in case.items if i.payload.get("step", {}).get("kind") == "sql_query"]
    assert (
        query.source is ProposalSource.AI
        and query.state is ProposalState.PENDING
        and query.decided_by is None
    )
    # The question blocks Approve; the counts on the answer are ours, measured on the whole file.
    q = next(q for q in case.questions if q.code == "Q24_WHAT_COUNTS")
    assert q.blocking and q.answer is None and q.goal_id == GOAL_A
    assert q.options[0].label == f"{RULE}: {yes:,} of {total:,} customers ({yes / total:.1%})"
    assert visit.open_blocking(case) == (q,)
    assert not accepted_outcomes(case), "nothing is recorded by a suggestion"
    suggestion = talk.reply.suggestion
    assert suggestion is not None and (suggestion.question_id, suggestion.option_id) == (q.question_id, "c0")

    # The person's click records the outcome, ticks the query, and the Ready sentence names it in plain words.
    case = talk.answer(q.question_id, "c0")
    (outcome,) = accepted_outcomes(case)
    assert outcome.decided_by == ANALYST and outcome.goal_id == GOAL_A
    assert outcome.payload["column"] == "is_sale" and outcome.payload["outcome_sql"] == "customer_sales"
    assert outcome.payload["counts"] == {
        "rows": total,
        "yes": yes,
        "no": total - yes,
        "yes_rate": round(yes / total, 4),
    }
    (ticked,) = [i for i in case.items if i.item_id == query.item_id]
    assert ticked.state is ProposalState.ACCEPTED and ticked.decided_by == ANALYST
    assert visit.open_blocking(case) == ()
    sentence = visit.approval_sentence(case)
    assert f"The model learns from this: {RULE}: {yes:,} of {total:,} customers." in sentence
    assert sentence.endswith("Nothing else changes.")


def test_the_prescription_the_replay_and_the_readiness_check_use_the_new_column(
    storage: LocalStorage,
) -> None:
    talk = talk_with(storage, sale_script())
    case = talk.say(TYPED)
    case = talk.answer(next(q for q in case.questions if q.code == "Q24_WHAT_COUNTS").question_id, "c0")
    w = talk.world
    prescription = prescription_from_case(case, storage=storage, files=tuple(w.load(case)))

    section = prescription.section(GOAL_A)
    assert section is not None and section.roles["outcome"] == "is_sale"
    assert section.outcome is None, "a query-built column has no value map: the query is the definition"
    assert prescription.post_build is not None and prescription.post_build.target == "is_sale"
    assert [s.kind.value for s in prescription.post_build.steps] == ["sql_query"]

    # Replayed on the files as training does: the query runs under the full leak check with the outcome let off.
    files = [
        ReplayFile(slot=part.slot, frame=w.files[part.upload_ids[0]].frame, upload_id=part.upload_ids[0])
        for part in prescription.files
    ]
    done = replay_prescription(prescription, files, mode="train", goal_id=GOAL_A)
    assert done.frame is not None and "is_sale" in done.frame.columns
    yes, total = truth()
    assert len(done.frame) == total and int(done.frame["is_sale"].sum()) == yes

    # Readiness reads the column as the outcome: nothing about it fails.
    report = check_readiness(
        ReadinessInputs(
            frame=done.frame,
            config=load_use_case(GOAL_A),
            mode=RunMode.TRAIN,
            primary_key="customer_id",
            target="is_sale",
            upload_id=vs.FIRST,
            row_count=len(done.frame),
            scan=SourceScan(file_format="parquet", rows_parsed=len(done.frame)),
            goal=GOAL_A,
        )
    )
    assert report.verdict in {"ready", "ready_with_warnings"}, report.verdict_reason


# ---------------------------------------------------------------------------
# The leak check still applies to the new column
# ---------------------------------------------------------------------------
def test_a_feature_built_from_the_outcome_window_is_refused_and_no_question_is_asked(
    storage: LocalStorage,
) -> None:
    talk = talk_with(storage, sale_script(LEAKY_SQL))
    case = talk.say(TYPED)
    assert not [q for q in case.questions if q.code == "Q24_WHAT_COUNTS"]
    refused = [r for r in talk.client.seen[-1].results if r.get("error") == "AGENT_OUTCOME_REFUSED"]
    assert refused and "FUTURE_EVENTS_LEAKED" in refused[0]["message"]
    assert not accepted_outcomes(case)


def test_an_outcome_read_from_the_window_must_say_so_and_a_rule_needs_an_as_of_date(
    storage: LocalStorage,
) -> None:
    quiet = propose()
    quiet["args"] = {
        **quiet["args"],
        "reads_future": False,
    }  # the outcome reads later orders but does not say it
    script = {TYPED: [quiet, ask_sale(), reply("Not asked.")]}
    talk = talk_with(storage, script)
    case = talk.say(TYPED)
    assert not [q for q in case.questions if q.code == "Q24_WHAT_COUNTS"]
    assert any("FUTURE_EVENTS_LEAKED" in str(r.get("message")) for r in talk.client.seen[-1].results)

    undated = propose(
        "SELECT c.*, COALESCE(w.is_sale, 0) AS is_sale FROM t_customers c LEFT JOIN ("
        "SELECT customer_id, 1 AS is_sale FROM t_orders WHERE order_amount > 500 GROUP BY customer_id) w "
        "ON c.customer_id = w.customer_id"
    )
    undated["args"] = {k: v for k, v in undated["args"].items() if k not in {"as_of", "reads_future"}}
    talk = talk_with(storage, {TYPED: [undated, ask_sale(), reply("It was refused.")]})
    case = talk.say(TYPED)
    assert not [q for q in case.questions if q.code == "Q24_WHAT_COUNTS"]
    assert any("OUTCOME_NEEDS_AS_OF" in str(r.get("message")) for r in talk.client.seen[-1].results)


def test_an_outcome_a_column_alone_decides_is_refused(storage: LocalStorage) -> None:
    sql = (
        "SELECT c.*, CASE WHEN c.monthly_spend > 800 THEN 1 ELSE 0 END AS is_sale FROM t_customers c "
        "WHERE $as_of IS NOT NULL"
    )
    talk = talk_with(storage, {TYPED: [propose(sql), ask_sale(), reply("Not asked.")]})
    case = talk.say(TYPED)
    assert not [q for q in case.questions if q.code == "Q24_WHAT_COUNTS"]
    assert any("OUTCOME_FROM_A_FEATURE" in str(r.get("message")) for r in talk.client.seen[-1].results)


def test_the_outcome_belongs_to_the_goal(storage: LocalStorage) -> None:
    """No goal chosen: the agent is told to wait; nothing is asked."""
    talk = Talk(storage, world(), ChatScript(sale_script()), files=(vs.FIRST, ORDERS))
    case = talk.say(TYPED)
    assert not [q for q in case.questions if q.code == "Q24_WHAT_COUNTS"]
    assert any(r.get("error") == "AGENT_QUESTION_REFUSED" for r in talk.client.seen[-1].results)


# ---------------------------------------------------------------------------
# A column the file has, and the rules' chips without an AI
# ---------------------------------------------------------------------------
def with_converted() -> World:
    config = load_use_case(GOAL_A)
    frame = customers_frame()
    rng = np.random.default_rng(2)
    frame["converted_30d"] = rng.choice(["Y", "N", "N"], len(frame))
    w = world()
    w.files[vs.FIRST] = vs.loaded(vs.FIRST, "customers.csv", frame, config)
    return w


def test_a_column_the_file_has_becomes_the_outcome_with_its_yes_values(storage: LocalStorage) -> None:
    script = {
        "yes is Y": [
            {
                "action": "ask",
                "file": "F1",
                "args": {
                    "question": "Q24",
                    "values": {"file": "customers.csv"},
                    "choices": ["a yes = converted_30d is Y"],
                    "outcomes": [{"column": "converted_30d", "yes": ["Y"]}],
                },
            },
            suggest,
            reply("I understood: Y in converted_30d is a yes. Use this?"),
        ]
    }
    w = with_converted()
    talk = Talk(storage, w, ChatScript(script), files=(vs.FIRST,))
    talk.goal()
    case = talk.say("yes is Y")
    q = next(q for q in case.questions if q.code == "Q24_WHAT_COUNTS")
    frame = w.files[vs.FIRST].frame
    yes = int((frame["converted_30d"] == "Y").sum())
    assert q.options[0].label.startswith(f"a yes = converted_30d is Y: {yes:,} of {len(frame):,} customers")
    case = talk.answer(q.question_id, "c0")
    (outcome,) = accepted_outcomes(case)
    assert outcome.payload["column"] == "converted_30d" and "outcome_sql" not in outcome.payload
    prescription = prescription_from_case(case, storage=storage, files=tuple(w.load(case)))
    section = prescription.section(GOAL_A)
    assert section is not None and section.outcome is not None and section.outcome.approval is not None
    assert (
        section.outcome.approval.approved_by == ANALYST
    ), "the person's click is the approval of the value map"
    meaning = section.outcome.definition.meaning_of()
    assert meaning == {"N": "no", "Y": "yes"}
    assert "The model learns from this: a yes = converted_30d is Y" in visit.approval_sentence(case)


def test_a_yes_value_the_column_does_not_have_is_refused(storage: LocalStorage) -> None:
    script = {
        "oops": [
            {
                "action": "ask",
                "file": "F1",
                "args": {
                    "question": "Q24",
                    "values": {"file": "customers.csv"},
                    "choices": ["a yes = converted_30d is Maybe"],
                    "outcomes": [{"column": "converted_30d", "yes": ["Maybe"]}],
                },
            },
            reply("That value is not in the file."),
        ]
    }
    talk = Talk(storage, with_converted(), ChatScript(script), files=(vs.FIRST,))
    talk.goal()
    case = talk.say("oops")
    assert not [q for q in case.questions if q.code == "Q24_WHAT_COUNTS"]
    assert any(r.get("error") == "AGENT_OUTCOME_REFUSED" for r in talk.client.seen[-1].results)


def test_without_an_ai_the_rules_offer_the_detected_outcome_columns_as_chips_with_their_counts(
    storage: LocalStorage,
) -> None:
    w = vs.world()
    case = vs.open_case(storage, w)
    deps = vs.deps_for(storage, w)
    case = visit.resume(vs.CLIENT, case.case_id, deps, actor=ANALYST)
    q = vs.question(case, "KEY_AMBIGUOUS")
    case = visit.answer(vs.CLIENT, case.case_id, q.question_id, vs.option(q, "customer_id"), deps,
                        expected_version=case.version, actor=ANALYST)  # fmt: skip
    case = visit.set_goal(vs.CLIENT, case.case_id, GOAL_A, deps, expected_version=case.version, actor=ANALYST)
    chips = vs.question(case, "OUTCOME_COLUMN")
    frame = w.files[vs.FIRST].frame
    yes = int((frame["converted"] == 1).sum())
    chip = next(o for o in chips.options if o.label.split(" (")[0] == "converted")
    assert chip.label.split(" (")[0] == "converted"
    case = visit.answer(vs.CLIENT, case.case_id, chips.question_id, chip.option_id, deps,
                        expected_version=case.version, actor=ANALYST)  # fmt: skip
    (outcome,) = accepted_outcomes(case)
    assert outcome.payload["column"] == "converted" and outcome.payload["counts"]["yes"] == yes
    assert f"{yes:,} of {len(frame):,} customers" in visit.approval_sentence(case)
