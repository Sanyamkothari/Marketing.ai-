"""Analyst numbers come from code (LLM, retry, then template); Conversation always honours stop."""

from __future__ import annotations

import json
import random
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from autonomy.agents.analyst import Analyst, BriefingText, find_problems, suggest_budget
from autonomy.agents.base import ScriptedLLM
from autonomy.agents.claims import check_numbers
from autonomy.agents.conversation import REPLY_TEMPLATES, Conversation, stop_keyword
from autonomy.contracts import (
    Alert,
    Briefing,
    Channel,
    ChannelStats,
    CustomerState,
    Estimate,
    Event,
    EventType,
    GoalKind,
    GoalStatus,
    HealthStatus,
    ProofSummary,
)
from autonomy.fakes.folder import SimpleStateFolder
from autonomy.fakes.samples import sample_goal

NOW = datetime(2026, 10, 1, 9, tzinfo=UTC)


# --- claims.check_numbers --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "numbers"),
    [
        ("We made $1,235 in profit.", {"p": 1234.56}),
        ("We made $1,234.56 exactly.", {"p": 1234.56}),
        ("About $1.2k so far.", {"p": 1234.56}),
        ("Complaints ran at 0.08% of deliveries.", {"rate": 0.0008}),
        ("12% opened, or 12.5 percent.", {"rate": 0.1249}),
        ("Lift of 3 percentage points.", {"lift": 0.03}),
        ("Revenue reached $3M.", {"rev": 3_040_000}),
        ("Revenue reached 2.5 million.", {"rev": 2_500_000}),
        ("1,900 customers are drifting away.", {"n": 1873}),
        ("Profit fell by $200.", {"p": -200.0}),
        ("EUR 40 per order.", {"aov": 40.0}),
        ("The 95% range is wide.", {"level": 0.95}),
        ("No numbers here at all.", {}),
        ("It took 5 minutes.", {"m": 5}),
    ],
)
def test_numbers_backed_by_code_pass(text: str, numbers: dict[str, float]) -> None:
    assert check_numbers(text, numbers) == []


@pytest.mark.parametrize(
    ("text", "numbers", "bad"),
    [
        ("We made $5,500 in profit.", {"p": 1234.56}, ["$5,500"]),
        ("2,000 customers are drifting away.", {"n": 1873}, ["2,000"]),  # too coarse a rounding
        ("$1,240 so far.", {"p": 1234.56}, ["$1,240"]),
        ("Revenue reached $3M.", {"rev": 3_600_000}, ["$3M"]),
        ("Complaints ran at 0.8%.", {"rate": 0.0008}, ["0.8%"]),
        ("On 20 Oct we sent 1st wave.", {}, ["20", "1"]),
        ("USD 41 per order, USD 41 again.", {"aov": 40.0}, ["USD 41"]),  # listed once
        ("We made $5m.", {"p": 5.0}, ["$5m"]),  # a lone m is million, never let through as 5
        ("Up 12%.", {"n": 0.5}, ["12%"]),
    ],
)
def test_invented_numbers_are_returned_as_written(
    text: str, numbers: dict[str, float], bad: list[str]
) -> None:
    assert check_numbers(text, numbers) == bad


def test_plain_numbers_do_not_match_a_rate_times_100() -> None:
    """Only a percentage may be read as a fraction; '8 customers' is not 0.08."""
    assert check_numbers("8 customers", {"rate": 0.08}) == ["8"]
    assert check_numbers("8%", {"rate": 0.08}) == []


# --- Analyst briefing ------------------------------------------------------------------------------


def _proof(
    profit: Estimate | None = None, lift: Estimate | None = None, spend: float = 820.5
) -> ProofSummary:
    return ProofSummary(
        goal_id="goal_winback",
        as_of=NOW,
        agent_n=8000,
        baseline_n=1000,
        holdout_n=1000,
        incremental_revenue=Estimate(point=4100.0, low=1500.0, high=6800.0),
        incremental_profit=profit,
        lift_vs_baseline=lift,
        spend=spend,
    )


PROFIT_UP = Estimate(point=1234.56, low=400.0, high=2100.0)
HEALTH = HealthStatus(
    as_of=NOW,
    delivery_rate=0.981,
    complaint_rate=0.0004,
    unsubscribe_rate=0.0021,
    contacts_per_customer_week=1.4,
    alerts=(Alert(alert_id="al_1", at=NOW, severity="warning", code="unsub_rising", message="Unsubs up"),),
)
FACTS = {"lapsing_customers": 1900.0}


def _sections(prompt: str) -> dict[str, list[str]]:
    """Section key -> the numbers as code wants them shown, read back out of the Analyst prompt."""
    out: dict[str, list[str]] = {}
    key = ""
    for line in prompt.splitlines():
        if line.startswith("Section key: "):
            key = line.removeprefix("Section key: ")
            out[key] = []
        found = re.search(r"\(write it as (.+)\)$", line)
        if found and key:
            out[key].append(found.group(1))
    return out


def _good_answer(prompt: str, *, invent: str = "", drop: str = "") -> str:
    sections = [
        {"key": k, "body": f"In short: {', '.join(shown) or 'nothing to report'}.{invent}"}
        for k, shown in _sections(prompt).items()
        if k != drop
    ]
    return json.dumps({"headline": "Steady progress against the holdout.", "sections": sections})


def _analyst(answers: Sequence[str | None]) -> tuple[Analyst, ScriptedLLM]:
    """Answer i-th call with answers[i]; None means 'a clean answer built from the prompt'."""
    state = {"i": 0}

    def script(system: str, prompt: str) -> str:
        assert "Analyst" in system
        i = min(state["i"], len(answers) - 1)
        state["i"] += 1
        answer = answers[i]
        return _good_answer(prompt) if answer is None else answer

    llm = ScriptedLLM(script)
    return Analyst(llm), llm


def _assert_every_number_backed(briefing: Briefing) -> None:
    every: dict[str, float] = {}
    for s in briefing.sections:
        assert check_numbers(s.body, s.numbers) == [], (s.title, s.body)
        every.update({f"{s.title}.{k}": v for k, v in s.numbers.items()})
    assert check_numbers(briefing.headline, every) == [], briefing.headline


def test_clean_llm_briefing_is_used_with_code_numbers() -> None:
    analyst, llm = _analyst([None])
    goal = sample_goal(NOW)
    result = analyst.write_briefing(goal, _proof(PROFIT_UP), HEALTH, FACTS, "daily", NOW)

    assert result.source == "llm" and len(llm.calls) == 1
    b = result.briefing
    assert b.headline == "Steady progress against the holdout."
    titles = [s.title for s in b.sections]
    assert titles == [
        "Extra profit vs people we never contact",
        "Against the simple rule",
        "Customer health",
        "What else we noticed",
    ]
    money = b.sections[0].numbers
    assert money["incremental_profit"] == 1234.56
    assert money["budget_left"] == pytest.approx(5000 - 820.5)
    assert "$1,235" in b.sections[0].body
    assert b.sections[3].numbers == FACTS
    _assert_every_number_backed(b)


def test_invented_number_triggers_one_retry_listing_it() -> None:
    analyst, llm = _analyst([_good_answer_with_invention(), None])
    result = analyst.write_briefing(sample_goal(NOW), _proof(PROFIT_UP), HEALTH, FACTS, "weekly", NOW)

    assert result.source == "llm_retry" and len(llm.calls) == 2
    assert "$9,999" in llm.calls[1][1] and "problems" in llm.calls[1][1]
    assert any("$9,999" in p for p in result.problems)
    _assert_every_number_backed(result.briefing)


def _good_answer_with_invention() -> str:
    return json.dumps(
        {
            "headline": "We made $9,999!",
            "sections": [
                {"key": k, "body": "We made $9,999."} for k in ("money", "baseline", "health", "facts")
            ],
        }
    )


def test_falls_back_to_template_when_the_retry_still_invents() -> None:
    analyst, llm = _analyst([_good_answer_with_invention()])
    result = analyst.write_briefing(sample_goal(NOW), _proof(PROFIT_UP), HEALTH, FACTS, "daily", NOW)

    assert result.source == "template" and len(llm.calls) == 2
    b = result.briefing
    assert "$9,999" not in b.headline and all("$9,999" not in s.body for s in b.sections)
    assert b.headline == (
        "It made an extra $1,235 in profit compared with people we never contact ($400 to $2,100)."
    )
    assert "likely range $400 to $2,100" in b.sections[0].body
    _assert_every_number_backed(b)


def test_unparseable_answers_fall_back_to_template() -> None:
    analyst, llm = _analyst(["not json at all"])
    result = analyst.write_briefing(None, _proof(None), HealthStatus(as_of=NOW), {}, "daily", NOW)

    assert result.source == "template"
    assert len(llm.calls) == 4  # each attempt asks once and repairs once
    assert result.briefing.headline == "Too early to call: results are still coming in."
    assert result.briefing.goal_id == "goal_winback"  # from the proof when there is no goal
    _assert_every_number_backed(result.briefing)


def test_missing_section_is_a_problem() -> None:
    analyst, _ = _analyst([None])
    drafts = analyst.sections(sample_goal(NOW), _proof(PROFIT_UP), HEALTH, FACTS)
    prompt = "\n".join(f"Section key: {d.key}" for d in drafts)
    text = BriefingText.model_validate_json(_good_answer(prompt, drop="health"))
    assert find_problems(text, drafts) == ["section 'health' is missing"]


@pytest.mark.parametrize(
    ("profit", "headline", "phrase"),
    [
        (Estimate(point=300.0, low=-200.0, high=900.0), "Not sure yet", "which includes zero"),
        (Estimate(point=-700.0, low=-1100.0, high=-250.0), "less profit than the people", "$700 less profit"),
    ],
)
def test_template_states_direction_from_the_interval(profit: Estimate, headline: str, phrase: str) -> None:
    analyst, _ = _analyst(["{}"])
    b = analyst.briefing(sample_goal(NOW), _proof(profit), HEALTH, {}, "daily", NOW)
    assert headline in b.headline and phrase in b.sections[0].body
    _assert_every_number_backed(b)


def test_critical_paused_alert_leads_the_template_headline() -> None:
    health = HealthStatus(
        as_of=NOW,
        complaint_rate=0.0012,
        alerts=(
            Alert(
                alert_id="a", at=NOW, severity="critical", code="complaints", message="x", auto_paused=True
            ),
        ),
    )
    analyst, llm = _analyst(["{}"])
    b = analyst.briefing(sample_goal(NOW), _proof(PROFIT_UP), health, {}, "daily", NOW)
    assert b.headline.startswith("A critical alert paused")
    assert "Complaints are above the safe limit" in llm.calls[0][1]
    _assert_every_number_backed(b)


def test_template_numbers_always_pass_the_check() -> None:
    """Formatting and tolerance agree for many shapes of results, including tiny and negative ones."""
    rng = random.Random(7)
    analyst, _ = _analyst(["{}"])
    for _ in range(60):
        scale = 10 ** rng.randint(-1, 6)
        lo, hi = sorted(rng.uniform(-1, 1) * scale for _ in range(2))
        profit = Estimate(point=(lo + hi) / 2, low=lo, high=hi)
        lift = Estimate(point=(lo + hi) / 20, low=lo / 10, high=hi / 10) if lo < 0 else None
        health = HealthStatus(as_of=NOW, delivery_rate=rng.random(), complaint_rate=rng.random() / 1000)
        facts = {"avg_order_value": rng.uniform(1, 500), "lapsing_customers": float(rng.randint(0, 99999))}
        b = analyst.briefing(
            None, _proof(profit, lift, spend=rng.uniform(0, 5000)), health, facts, "daily", NOW
        )
        _assert_every_number_backed(b)


def test_briefing_ids_are_deterministic() -> None:
    a1, _ = _analyst([None])
    a2, _ = _analyst([None])
    goal, proof = sample_goal(NOW), _proof(PROFIT_UP)
    b1 = a1.briefing(goal, proof, HEALTH, FACTS, "daily", NOW)
    b2 = a2.briefing(goal, proof, HEALTH, FACTS, "daily", NOW)
    b3 = a2.briefing(goal, proof, HEALTH, FACTS, "daily", NOW + timedelta(days=1))
    assert b1 == b2
    assert b1.briefing_id != b3.briefing_id


# --- Analyst opportunities -------------------------------------------------------------------------


def _no_llm(system: str, prompt: str) -> str:
    raise AssertionError("opportunities must not call a model")


def test_lapsing_customers_with_value_become_a_draft_win_back_goal() -> None:
    analyst = Analyst(ScriptedLLM(_no_llm))
    summary = {
        "Lapsing": {"customers": 1900, "avg_value": 120.0, "margin_rate": 0.4, "median_days": 75},
        "loyal": {"customers": 5000, "avg_value": 300.0},
        "cart_abandoners": {"customers": 5, "avg_value": 80.0},  # too few to bother
    }
    goals = analyst.opportunities(summary, NOW, business_id="demo")

    assert len(goals) == 1
    g = goals[0]
    assert g.kind is GoalKind.WIN_BACK and g.status is GoalStatus.DRAFT
    assert g.budget == 1400.0  # 1900 x 120 x 0.4 x 0.05 x 0.3 = 1,368, to two significant figures
    assert g.text.startswith("Win back the 1,900 lapsing customers") and "$1,400" in g.text
    assert g.ends_at == NOW + timedelta(days=30)
    assert analyst.opportunities(summary, NOW, business_id="demo") == goals  # deterministic ids


def test_no_opportunity_when_already_running_or_no_past_value() -> None:
    analyst = Analyst(ScriptedLLM(_no_llm))
    running = sample_goal(NOW).model_copy(update={"status": GoalStatus.ACTIVE})
    lapsing = {"lapsing": {"customers": 1900, "avg_value": 120.0}}
    assert analyst.opportunities(lapsing, NOW, existing=[running]) == []
    assert analyst.opportunities({"lapsing": {"customers": 1900, "avg_value": 0.0}}, NOW) == []


def test_one_goal_per_kind_biggest_first() -> None:
    analyst = Analyst(ScriptedLLM(_no_llm))
    summary = {
        "lapsed": {"customers": 500, "avg_value": 100.0},
        "lapsing": {"customers": 3000, "avg_value": 100.0},
        "at_risk": {"customers": 800, "avg_value": 200.0, "contactable": 400},
    }
    goals = analyst.opportunities(summary, NOW)
    assert [g.kind for g in goals] == [GoalKind.WIN_BACK, GoalKind.RETENTION]
    assert "3,000 lapsing" in goals[0].text and "400 at risk" in goals[1].text


# --- Conversation ----------------------------------------------------------------------------------


class StubSignals:
    """Answers every text with one fixed label, or raises."""

    def __init__(self, label: str, prob: float = 0.95, fail: bool = False) -> None:
        self.label, self.prob, self.fail = label, prob, fail
        self.calls = 0

    def classify(
        self, texts: Sequence[str], *, question: str, labels: Sequence[str]
    ) -> list[tuple[str, float]]:
        self.calls += 1
        assert "stop" in labels and "not_now" in labels and question
        if self.fail:
            raise RuntimeError("classifier down")
        return [(self.label, self.prob) for _ in texts]


def _reply(text: str, *, event_id: str = "ev_r1", channel: Channel = Channel.WHATSAPP) -> Event:
    return Event(
        event_id=event_id,
        customer_id="c1",
        type=EventType.REPLY,
        ts=NOW - timedelta(minutes=5),
        source="whatsapp",
        channel=channel,
        decision_id="dec_1",
        text=text,
    )


def _state() -> CustomerState:
    ok = ChannelStats(address_known=True, consent=True)
    return CustomerState(customer_id="c1", as_of=NOW, channels={Channel.EMAIL: ok, Channel.WHATSAPP: ok})


@pytest.mark.parametrize(
    "text",
    [
        "STOP",
        "stop!",
        "Stop.",
        "Unsubscribe me please",
        "please opt-out",
        "OPT OUT",
        "remove me from this list",
        "Cancel",
        "don’t text me again",
        "Love the deals but STOP messaging me",
        "arrêt",
    ],
)
def test_stop_words_always_win_over_the_classifier(text: str) -> None:
    signals = StubSignals("interested", 0.99)
    outcome = Conversation(signals).handle_reply(_reply(text), _state(), NOW)

    assert outcome.intent == "stop" and outcome.matched
    assert signals.calls == 0  # the safety net answers before any classifier runs
    assert not outcome.needs_human and outcome.suggested_reply == REPLY_TEMPLATES["stop"]
    unsubs = outcome.events_to_record
    assert [e.type for e in unsubs] == [EventType.UNSUBSCRIBE, EventType.UNSUBSCRIBE]
    assert [e.channel for e in unsubs] == [Channel.WHATSAPP, Channel.EMAIL]  # the reply's channel first
    assert unsubs[0].decision_id == "dec_1" and unsubs[1].decision_id is None
    assert all(e.ts == NOW - timedelta(minutes=5) for e in unsubs)  # effective from when they asked


def test_stop_is_honoured_once_folded_into_state() -> None:
    folder = SimpleStateFolder()
    state = _state()
    outcome = Conversation(StubSignals("other")).handle_reply(_reply("STOP"), state, NOW)
    for e in outcome.events_to_record:
        state = folder.fold(state, e)
    assert state.contactable_channels() == ()


def test_classifier_stop_is_honoured_and_stop_can_be_limited_to_the_channel() -> None:
    outcome = Conversation(StubSignals("stop", 0.7), stop_all_channels=False).handle_reply(
        _reply("I'm done with these"), _state(), NOW
    )
    assert outcome.intent == "stop" and outcome.matched is None
    assert [e.channel for e in outcome.events_to_record] == [Channel.WHATSAPP]


def test_not_stop_words_do_not_trip_the_net() -> None:
    for text in ("Not now, maybe next month", "What time do you close?", "Yes please, send me the link"):
        assert stop_keyword(text) is None, text


@pytest.mark.parametrize(
    ("text", "days"), [("Not now, maybe next month", 30), ("busy, try next week", 7), ("later", 30)]
)
def test_not_now_records_a_snooze(text: str, days: int) -> None:
    outcome = Conversation(StubSignals("not_now")).handle_reply(_reply(text), _state(), NOW)
    assert outcome.intent == "not_now" and not outcome.needs_human
    (pref,) = outcome.events_to_record
    assert pref.type is EventType.PREFERENCE
    assert pref.attrs["key"] == "snooze_until"
    assert pref.attrs["value"] == (NOW + timedelta(days=days)).isoformat()


def test_complaint_word_records_a_complaint_and_hands_off() -> None:
    outcome = Conversation(StubSignals("interested")).handle_reply(_reply("This is spam"), _state(), NOW)
    assert outcome.intent == "complaint" and outcome.needs_human
    (c,) = outcome.events_to_record
    assert c.type is EventType.COMPLAINT and c.channel is Channel.WHATSAPP and c.decision_id == "dec_1"


@pytest.mark.parametrize(("label", "human"), [("question", True), ("interested", True), ("other", False)])
def test_other_intents_record_nothing_and_use_fixed_templates(label: str, human: bool) -> None:
    outcome = Conversation(StubSignals(label)).handle_reply(_reply("hello there"), _state(), NOW)
    assert outcome.intent == label and outcome.events_to_record == []
    assert outcome.needs_human is human
    assert outcome.suggested_reply == REPLY_TEMPLATES[label]


def test_unsure_or_failing_classifier_goes_to_a_person() -> None:
    unsure = Conversation(StubSignals("other", 0.3)).handle_reply(_reply("hmm ok"), _state(), NOW)
    assert unsure.needs_human
    failed = Conversation(StubSignals("question", fail=True)).handle_reply(
        _reply("where is it"), _state(), NOW
    )
    assert failed.intent == "other" and failed.needs_human and failed.confidence == 0.0
    weird = Conversation(StubSignals("buy_now")).handle_reply(_reply("hey"), _state(), NOW)
    assert weird.intent == "other" and weird.needs_human


def test_works_with_the_keyword_fake_signals() -> None:
    from autonomy.fakes.channels import KeywordTextSignals

    outcome = Conversation(KeywordTextSignals()).handle_reply(_reply("I have a question"), None, NOW)
    assert outcome.intent == "question" and outcome.needs_human


def test_reply_events_have_deterministic_ids_and_reject_other_types() -> None:
    conv = Conversation(StubSignals("other"))
    first = conv.handle_reply(_reply("STOP"), _state(), NOW)
    again = conv.handle_reply(_reply("STOP"), _state(), NOW + timedelta(hours=1))
    other = conv.handle_reply(_reply("STOP", event_id="ev_r2"), _state(), NOW)
    ids = [e.event_id for e in first.events_to_record]
    assert ids == [e.event_id for e in again.events_to_record]
    assert len(set(ids)) == 2 and not set(ids) & {e.event_id for e in other.events_to_record}

    click = Event(
        event_id="ev_c", customer_id="c1", type=EventType.CLICK, ts=NOW, source="x", channel=Channel.EMAIL
    )
    with pytest.raises(ValueError, match="reply event"):
        conv.handle_reply(click, _state(), NOW)
    with pytest.raises(ValueError, match="different customer"):
        conv.handle_reply(_reply("hi"), _state().model_copy(update={"customer_id": "c2"}), NOW)


def test_budget_suggestion_is_code() -> None:
    from autonomy.agents.analyst import PLAYS, SegmentSummary

    win_back = next(p for p in PLAYS if p.kind is GoalKind.WIN_BACK)
    assert suggest_budget(SegmentSummary(customers=200, avg_value=50.0), win_back) == 60.0


def test_fact_names_with_digits_do_not_break_the_template() -> None:
    """A digit in a fact's name is not a number code computed, so the template must not write it."""
    analyst, llm = _analyst(["{}"])
    facts = {"repeat_rate_90d": 0.12, "top_3_share": 0.4}
    b = analyst.briefing(None, _proof(PROFIT_UP), HEALTH, facts, "daily", NOW)
    assert "90" not in b.sections[3].body and "3 share" not in b.sections[3].body
    assert "never write those digits" in llm.calls[0][1]
    _assert_every_number_backed(b)


def test_limits_and_interval_level_are_numbers_the_model_may_quote() -> None:
    """Notes point at the safe limits and the 95% range, so those numbers are listed for the model."""
    analyst, _ = _analyst(["{}"])
    lift = Estimate(point=0.4, low=-0.1, high=0.9)
    health = HealthStatus(as_of=NOW, complaint_rate=0.0012, delivery_rate=0.9)
    drafts = analyst.sections(sample_goal(NOW), _proof(PROFIT_UP, lift), health, {})
    baseline, health_draft = drafts[1], drafts[2]
    assert check_numbers("The likely range is the 95% one.", baseline.numbers) == []
    assert (
        check_numbers("Complaints are above the 0.08% limit, delivery under 95%.", health_draft.numbers) == []
    )


def test_remove_my_number_is_a_stop() -> None:
    assert stop_keyword("Please remove my number from your list") == "remove my number"


def test_a_nan_confidence_goes_to_a_person_instead_of_failing() -> None:
    outcome = Conversation(StubSignals("question", float("nan"))).handle_reply(
        _reply("where is my order"), _state(), NOW
    )
    assert outcome.intent == "question" and outcome.confidence == 0.0 and outcome.needs_human
