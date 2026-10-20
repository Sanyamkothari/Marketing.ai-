"""The endpoints that make PITCH.md's promises visible (api/v2/insights.py), and their mock files.

Fast: the fake-world demo (a few dozen customers, a few days), one small simulated shop for the
runtime's own decision ids and batches, and hand-built decisions for the cases the demos lack.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from api.v2 import insights
from api.v2.deps import BadRequestError, ConflictError, NotFoundError, Services, build_demo_services
from autonomy.contracts import (
    Action,
    AutonomyLevel,
    Batch,
    BatchStatus,
    Bid,
    Channel,
    ContentItem,
    ContentStatus,
    Decision,
    DecisionKind,
    Event,
    EventType,
    Goal,
    GoalStatus,
    Group,
    Theme,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
MOCK = Path(__file__).resolve().parents[3] / "ui" / "v2" / "mock"


@pytest.fixture
def services() -> Services:
    return build_demo_services(NOW, n_customers=40, days=4)


@pytest.fixture(scope="module")
def simulated() -> Services:
    pytest.importorskip("autonomy.runtime.demo", reason="the runtime demo is not installed here")
    return build_demo_services(
        NOW, n_customers=80, days=6, simulation=True, approve_goal_days=3, hold_batches=True
    )


@pytest.fixture
def client(services: Services) -> Iterator[Any]:
    pytest.importorskip("fastapi", reason="FastAPI is not installed here; the v2 CI job runs these")
    pytest.importorskip("httpx", reason="the FastAPI test client needs httpx")
    from fastapi.testclient import TestClient

    from api.v2.app import create_app

    with TestClient(create_app(services)) as c:
        yield c


def _goal(services: Services) -> Goal:
    return services.list_goals()[0]


def _decision(
    did: str,
    *,
    kind: DecisionKind = DecisionKind.NOTHING,
    group: Group = Group.AGENT,
    bids: tuple[Bid, ...] = (),
    blocked: tuple[str, ...] = (),
    price: float = 0.0,
    goal_id: str = "goal_winback",
    send_at: datetime = NOW,
) -> Decision:
    action = Action(goal_id=goal_id, channel=Channel.EMAIL, send_at=send_at)
    return Decision(
        decision_id=did,
        customer_id=f"c_{did}",
        made_at=NOW - timedelta(hours=1),
        kind=kind,
        action=action if kind is DecisionKind.ACT else None,
        group=group,
        probability=1.0,
        attention_price=price,
        reason="r",
        bids=bids,
        blocked_by=blocked,
        policy_version="t",
    )


def _bid(uplift: float, net: float, goal_id: str = "goal_winback") -> Bid:
    action = Action(goal_id=goal_id, channel=Channel.EMAIL, send_at=NOW)
    return Bid(goal_id=goal_id, action=action, uplift=uplift, value_if_converted=10, expected_net=net)


# --- left alone, and why --------------------------------------------------------------------------


def test_left_alone_reasons_name_the_most_binding_cause() -> None:
    reason = insights.left_alone_reason
    assert reason(_decision("a", group=Group.HOLDOUT)).code == "holdout"
    assert reason(_decision("b", group=Group.BASELINE)).code == "simple_rule"
    assert reason(_decision("c", bids=(_bid(0.0, -0.01),), price=0.01)).code == "no_effect"
    assert reason(_decision("d", bids=(_bid(0.02, 0.005),), price=0.01)).code == "not_worth_it"
    assert reason(_decision("e", bids=(_bid(0.02, 0.5),), price=0.01)).code == "testing_rest"
    assert reason(_decision("f", blocked=("no_consent:whatsapp", "weekly_cap"))).code == "weekly_cap"
    assert reason(_decision("g", blocked=("max_contacts_per_week",))).code == "weekly_cap"  # an older code
    assert reason(_decision("h", blocked=("preference:no_discounts",))).code == "no_discounts"
    assert reason(_decision("i")).code == "nothing_to_offer"
    assert reason(_decision("j", blocked=("something_new",))).code == "other"
    for code, words in insights.LEFT_ALONE_WORDS.items():
        assert words and "_" not in words, code  # words for people, never a bare code


def test_today_counts_every_decision_of_the_day(services: Services) -> None:
    goal = _goal(services)
    t = services.today(goal.goal_id)
    rows = [d for d in services.decisions.scan() if d.made_at.date() == t.date]
    assert t.customers_considered == len({d.customer_id for d in rows}) > 0
    assert t.contacted + t.left_alone == t.customers_considered
    assert sum(t.left_alone_by_reason.values()) == t.left_alone == sum(r.n for r in t.reasons)
    assert t.left_alone_by_reason.get("holdout", 0) == sum(1 for d in rows if d.group is Group.HOLDOUT)
    assert t.contacted == sum(1 for d in rows if d.kind is DecisionKind.ACT and d.group is not Group.HOLDOUT)
    sends = [e for e in services.events.scan() if e.type is EventType.SEND and e.ts.date() == t.date]
    assert t.messages_sent == len(sends)
    assert t.spend_to_date is not None and t.budget is not None
    assert t.budget_left == pytest.approx(t.budget - t.spend_to_date)
    assert t.last_run_at is not None and t.last_run_at.date() == t.date
    assert t.next_run_at is None  # the fake demo has no runtime, so no schedule to promise
    assert any("not only extra ones" in n for n in t.notes)
    empty = services.today(goal.goal_id, date(2020, 1, 1))
    assert empty.customers_considered == empty.messages_sent == empty.purchases == 0
    with pytest.raises(NotFoundError):
        services.today("goal_missing")


def test_today_counts_bid_less_decisions_for_their_goal(simulated: Services) -> None:
    """The runtime's ids carry the goal, so holdout and capped customers count for the right goal."""
    goals = [g.goal_id for g in simulated.list_goals()]
    assert len(goals) == 2
    per_goal = [simulated.today(g, NOW.date() - timedelta(days=1)) for g in goals]
    everyone = simulated.today(None, NOW.date() - timedelta(days=1))
    assert sum(t.customers_considered for t in per_goal) >= everyone.customers_considered > 0
    assert any(t.left_alone_by_reason.get("holdout") for t in per_goal)
    assert all(t.next_run_at is not None for t in per_goal)  # the runtime's daily sweep


# --- the feed and the customer panel --------------------------------------------------------------


def test_activity_rows_wrap_the_decision_with_what_happened(services: Services) -> None:
    goal = _goal(services)
    rows = services.activity(goal_id=goal.goal_id, limit=500)
    assert rows and all(r.goal_id == goal.goal_id for r in rows)
    acts = [r for r in rows if r.decision.kind is DecisionKind.ACT]
    alone = [r for r in rows if r.decision.kind is DecisionKind.NOTHING]
    assert acts and all(r.delivery is not None and r.left_alone is None for r in acts)
    assert alone and all(r.delivery is None and r.left_alone is not None for r in alone)
    assert {r.delivery.state for r in acts if r.delivery} <= {
        "sent",
        "delivered",
        "failed",
        "queued",
        "not_sent",
    }
    assert any(r.content is not None for r in acts)
    # Bid-less "nothing" decisions (the holdout) are kept by the goal filter, unlike /v2/decisions.
    assert any(r.decision.group is Group.HOLDOUT for r in rows)
    assert all(r.display_name is None for r in rows)  # the demo has no names, and none is made up
    assert len(services.activity(limit=3)) == 3
    with pytest.raises(NotFoundError):
        services.activity(goal_id="goal_missing")


def test_delivery_states_follow_events_then_batches_then_the_level() -> None:
    d = _decision("x", kind=DecisionKind.ACT, send_at=NOW + timedelta(hours=3))
    shadow = Goal(
        goal_id="goal_winback",
        business_id="b",
        text="t",
        budget=1,
        starts_at=NOW,
        created_at=NOW,
        autonomy=AutonomyLevel.SHADOW,
        status=GoalStatus.ACTIVE,
    )
    approve = shadow.model_copy(update={"autonomy": AutonomyLevel.APPROVE})
    batch = Batch(batch_id="b1", goal_id="goal_winback", created_at=NOW, decision_ids=("x",), n_contacts=1)
    ev = Event(
        event_id="e", customer_id="c_x", type=EventType.SEND, ts=NOW, source="t", channel=Channel.EMAIL
    )

    def state(events: list[Event], b: Batch | None, g: Goal) -> str:
        found = insights.delivery(d, events, b, g, None, NOW)
        assert found is not None
        return found.state

    assert state([], None, shadow) == "shadow"
    assert state([], batch, approve) == "held"
    assert state([], batch.model_copy(update={"status": BatchStatus.REJECTED}), approve) == "rejected"
    assert state([], batch.model_copy(update={"status": BatchStatus.APPROVED}), approve) == "approved"
    assert state([], None, approve) == "queued"
    assert state([ev], batch, approve) == "sent"
    deliver = ev.model_copy(update={"event_id": "e2", "type": EventType.DELIVER})
    assert state([ev, deliver], None, approve) == "delivered"
    bounce = ev.model_copy(update={"event_id": "e3", "type": EventType.BOUNCE})
    assert state([ev, bounce], None, approve) == "failed"
    assert insights.delivery(_decision("n"), [], None, approve, None, NOW) is None


def test_customer_insight_reads_habits_from_the_data(services: Services) -> None:
    cid = next(d.customer_id for d in services.decisions.scan() if d.kind is DecisionKind.ACT)
    ins = services.customer_insight(cid)
    assert ins.customer_id == cid and ins.offer_habit_words
    assert set(ins.deliveries) | set(ins.left_alone) == {
        d.decision_id for d in services.decisions.for_customer(cid)
    }
    with pytest.raises(NotFoundError):
        services.customer_insight("nobody")

    # A customer with a name, a reorder rhythm, offers and reading hours of their own.
    state = services.states.get(cid)
    assert state is not None
    counts = [0] * 24
    counts[19] = 5
    services.states.put([state.model_copy(update={"hour_counts": tuple(counts), "timezone": "Asia/Kolkata"})])
    t0 = NOW - timedelta(days=100)

    def buy(i: int, days: int, offer: str = "none") -> Event:
        return Event(
            event_id=f"buy{i}",
            customer_id=cid,
            type=EventType.PURCHASE,
            ts=t0 + timedelta(days=days),
            source="csv",
            value=30.0,
            item_id="sku_serum",
            attrs={"offer_id": offer, "item_name": "Vitamin C serum"},
        )

    signup = Event(
        event_id="s1",
        customer_id=cid,
        type=EventType.SIGNUP,
        ts=t0,
        source="csv",
        attrs={"first_name": "Priya K"},
    )
    services.events.append([signup, buy(1, 0), buy(2, 42), buy(3, 84, "pct10")])
    ins = services.customer_insight(cid)
    assert ins.display_name == "Priya"
    assert ins.best_hour == 19 and ins.best_hour_words is not None and "7 pm" in ins.best_hour_words
    assert ins.offer_habit == "sometimes" and ins.purchases_with_offer == 1
    assert ins.refill is not None and ins.refill.item_name == "Vitamin C serum"
    assert ins.refill.days_until_due == pytest.approx(42 - 16, abs=0.1)  # every 42 days; last 16 days ago
    assert not ins.refill.window_open
    assert ins.items["sku_serum"] == "Vitamin C serum"


def test_offer_habit_words() -> None:
    assert insights._offer_habit(0, 0)[0] == "unknown"
    assert insights._offer_habit(4, 0)[0] == "full_price"
    assert insights._offer_habit(4, 1)[0] == "sometimes"
    assert insights._offer_habit(4, 3)[0] == "needs_offer"


# --- the plan -------------------------------------------------------------------------------------


def test_plan_facts_count_the_audience_and_word_the_rules(services: Services) -> None:
    goal = _goal(services)
    plan = services.plan_facts(goal.goal_id)
    assert plan.audience_n == plan.agent_n + plan.baseline_n + plan.holdout_n
    codes = [r.code for r in plan.hard_rules]
    assert {"no_consent", "unsubscribed", "quiet_hours", "weekly_cap", "no_budget", "open_complaint"} <= set(
        codes
    )
    from autonomy.decide import rules

    assert set(codes) <= set(rules.ALL_CODES)  # the codes decide() itself uses
    assert plan.audience_words and all("_" not in w for w in plan.audience_words)
    assert plan.editor_passed + plan.editor_failed + plan.not_reviewed == len(plan.content)
    assert plan.autonomy_since is not None  # the demo's plan is approved
    with pytest.raises(NotFoundError):
        services.plan_facts("goal_missing")


def _item(notes: tuple[str, ...], status: ContentStatus, channel: Channel = Channel.EMAIL) -> ContentItem:
    return ContentItem(
        content_id="c1",
        channel=channel,
        theme=Theme.REMINDER,
        body="Hi",
        status=status,
        critic_notes=notes,
        created_at=NOW,
    )


def test_editor_checks_are_read_from_the_critics_notes() -> None:
    run = "judge run {n} [honesty, brand_voice]: honesty {h} (No invented claims.); brand_voice 5 (Warm.)"
    ok = insights.review_content(
        _item((run.format(n=1, h=5), run.format(n=2, h=4), "approved"), ContentStatus.APPROVED)
    )
    assert ok.reviewed_by == "critic" and ok.passed is True
    assert {c.check for c in ok.checks} == {
        "brand_voice",
        "honesty",
        "no_invented_numbers",
        "unsubscribe",
        "channel_rules",
    }
    failed = insights.review_content(
        _item(
            (run.format(n=1, h=2), run.format(n=2, h=5), "rejected: run 1 scored honesty 2 < 3"),
            ContentStatus.REJECTED,
        )
    )
    assert failed.reviewed_by == "critic" and failed.passed is False
    assert next(c for c in failed.checks if c.check == "honesty").passed is False
    ruled = insights.review_content(
        _item(
            ("rule: an email needs an unsubscribe hint", "rule: unapproved discount figure: 20%"),
            ContentStatus.REJECTED,
        )
    )
    by = {c.check: c for c in ruled.checks}
    assert by["unsubscribe"].passed is False and by["no_invented_numbers"].passed is False
    assert by["honesty"].passed is None  # the judge never ran
    person = insights.review_content(_item((run.format(n=1, h=5), "approved"), ContentStatus.REJECTED))
    assert person.reviewed_by == "person" and person.passed is True
    untouched = insights.review_content(_item((), ContentStatus.DRAFT))
    assert untouched.reviewed_by is None and untouched.passed is None and not untouched.checks
    free = insights.review_content(_item(("Urgency claims must be true.",), ContentStatus.DRAFT))
    assert free.passed is None and free.notes == ["Urgency claims must be true."]


def test_filter_words() -> None:
    from autonomy.contracts import AudienceFilter

    assert (
        insights.filter_words(AudienceFilter(field="purchases", op="gte", value=1))
        == "Has bought at least once"
    )
    assert insights.filter_words(AudienceFilter(field="purchases", op="eq", value=1)) == "Bought exactly once"
    assert (
        insights.filter_words(AudienceFilter(field="last_purchase_at", op="days_since_gt", value=60))
        == "Last purchase more than 60 days ago"
    )


# --- forecast, limits, data, channels, opportunities ----------------------------------------------


def test_expected_is_a_labelled_forecast_and_never_invents_one(
    services: Services, simulated: Services
) -> None:
    goal = _goal(services)
    fake = services.expected(goal.goal_id)
    assert fake.label == "forecast" and fake.contacts > 0
    assert fake.with_forecast == 0 and fake.expected_net is None  # the demo's rule makes no forecast
    for g in simulated.list_goals():
        e = simulated.expected(g.goal_id)
        if e.with_forecast:
            assert (
                e.expected_net is not None
                and e.expected_net.low <= e.expected_net.point <= e.expected_net.high
            )
            assert "not proof" in e.words


def test_health_limits_say_when_a_rule_cannot_be_checked(services: Services) -> None:
    limits = services.health_limits(_goal(services).goal_id)
    unsub = next(r for r in limits.rules if r.metric.value == "unsubscribe_multiple")
    assert limits.unsubscribe_baseline is None and unsub.judged is False and "not known yet" in unsub.words
    complaint = next(r for r in limits.rules if r.metric.value == "complaint_rate")
    assert complaint.judged and complaint.pause_at == 0.0008 and "0.08%" in complaint.words


def test_data_status_and_channels(services: Services) -> None:
    status = services.data_status()
    assert status.customers == services.states.count()
    assert status.orders == sum(1 for e in services.events.scan() if e.type is EventType.PURCHASE)
    assert status.sources and status.doctor_connected is False and not status.fixes
    report = {"fixes": [{"what": "Duplicate orders removed", "rows": 3}], "questions": [], "problems": ["x"]}
    services.data_doctor = lambda: report
    again = services.data_status()
    assert again.doctor_connected and again.fixes[0].rows == 3 and again.problems == ["x"]
    chans = {c.channel: c for c in services.channels()}
    assert chans[Channel.EMAIL].mode == "direct" and chans[Channel.EMAIL].ready
    assert chans[Channel.SMS].mode == "handoff" and not chans[Channel.SMS].ready
    assert chans[Channel.WEBHOOK].mode == "handoff"


def test_sized_opportunities_count_what_the_text_says(services: Services) -> None:
    sized = services.sized_opportunities()
    assert [s.goal for s in sized] == services.opportunities()
    for s in sized:
        assert str(s.audience_n) in s.goal.text and s.reason


def test_batch_breakdown(simulated: Services) -> None:
    batches = simulated.list_batches()
    assert batches
    b = simulated.batch_breakdown(batches[0].batch_id)
    assert sum(b.by_channel.values()) == sum(b.by_offer.values()) == batches[0].n_contacts
    assert "_" not in b.words
    with pytest.raises(NotFoundError):
        simulated.batch_breakdown("batch_missing")


# --- goals: words, budget, pause all --------------------------------------------------------------


def test_goal_words_are_read_for_a_budget_and_a_period() -> None:
    r = insights.read_goal_text("Win back customers who stopped buying, $5,000 this month", NOW)
    assert r.budget == 5000 and r.currency == "USD" and r.ends_at == datetime(2026, 11, 1, tzinfo=UTC)
    assert "$5,000" in r.words and "31 Oct 2026" in r.words
    r = insights.read_goal_text("Get repeat buyers, ₹50,000 over 30 days", NOW)
    assert r.budget == 50000 and r.currency == "INR" and r.ends_at == NOW + timedelta(days=30)
    assert insights.read_goal_text("5k dollars", NOW).budget == 5000
    nothing = insights.read_goal_text("Sell more socks", NOW)
    assert nothing.budget is None and nothing.ends_at is None


def test_create_goal_reads_the_budget_when_none_is_given(services: Services) -> None:
    goal, brief = services.create_goal("Win back lapsed buyers, $1,200 this month", None)
    assert goal.budget == 1200 and brief.envelope.budget == 1200
    assert goal.ends_at == datetime(2026, 11, 1, tzinfo=UTC)
    same, _ = services.create_goal("Win back lapsed buyers, $1,200 this month", 300)
    assert same.budget == 300 and same.ends_at is None  # a given budget is used as it is
    with pytest.raises(BadRequestError):
        services.create_goal("Win back lapsed buyers", None)


def test_set_budget_lowers_freely_and_never_raises_an_approved_plan(services: Services) -> None:
    goal = _goal(services)
    lowered, brief = services.set_budget(goal.goal_id, 100)
    assert lowered.budget == 100 and brief is not None and brief.approved
    assert brief.envelope.budget >= 100
    with pytest.raises(ConflictError):
        services.set_budget(goal.goal_id, brief.envelope.budget + 1)
    draft, _ = services.create_goal("Sell more", 500)
    _, draft_brief = services.set_budget(draft.goal_id, 50)
    assert draft_brief is not None and draft_brief.envelope.budget == 50
    with pytest.raises(BadRequestError):
        services.set_budget(goal.goal_id, float("inf"))


def test_pause_all(services: Services) -> None:
    active = [g for g in services.list_goals() if g.status is GoalStatus.ACTIVE]
    assert active
    paused = services.pause_all()
    assert [g.goal_id for g in paused] == [g.goal_id for g in active]
    assert all(g.status is not GoalStatus.ACTIVE for g in services.list_goals())
    assert services.pause_all() == []


# --- over HTTP ------------------------------------------------------------------------------------


def test_promise_endpoints_over_http(client: Any, services: Services) -> None:
    gid = _goal(services).goal_id
    cid = next(iter(services.states.scan())).customer_id
    ok = {
        f"/v2/today?goal_id={gid}": insights.Today,
        f"/v2/today?goal_id={gid}&day=2026-10-19": insights.Today,
        f"/v2/activity?goal_id={gid}&limit=5": insights.ActivityRow,
        f"/v2/customers/{cid}/insight": insights.CustomerInsight,
        f"/v2/goals/{gid}/plan": insights.PlanFacts,
        f"/v2/expected?goal_id={gid}": insights.Expected,
        f"/v2/health/limits?goal_id={gid}": insights.HealthLimits,
        "/v2/data/status": insights.DataStatus,
        "/v2/channels": insights.ChannelStatus,
        "/v2/opportunities/sized": insights.SizedOpportunity,
    }
    for path, model in ok.items():
        r = client.get(path)
        assert r.status_code == 200, path
        body = r.json()
        for row in body if isinstance(body, list) else [body]:
            model.model_validate(row)
    for path in (
        "/v2/today?goal_id=goal_missing",
        "/v2/goals/goal_missing/plan",
        "/v2/expected?goal_id=goal_missing",
        "/v2/customers/nobody/insight",
        "/v2/batches/batch_missing/breakdown",
    ):
        assert client.get(path).status_code == 404, path
    assert client.get("/v2/expected").status_code == 422  # a goal is required
    r = client.post("/v2/goals/read", json={"text": "Win back, $5,000 this month"})
    assert r.status_code == 200 and r.json()["budget"] == 5000
    r = client.post("/v2/goals", json={"text": "Win back, $800 in 14 days"})
    assert r.status_code == 200 and r.json()["goal"]["budget"] == 800 and r.json()["goal"]["ends_at"]
    assert client.post("/v2/goals", json={"text": "Win back"}).status_code == 400
    new_id = r.json()["goal"]["goal_id"]
    r = client.patch(f"/v2/goals/{new_id}", json={"budget": 200})
    assert r.status_code == 200 and r.json()["brief"]["envelope"]["budget"] == 200
    assert client.patch(f"/v2/goals/{gid}", json={"budget": 1e12}).status_code == 409
    assert client.patch(f"/v2/goals/{gid}", json={"budget": -1}).status_code == 422
    r = client.post("/v2/pause")
    assert r.status_code == 200 and all(g["status"] == "paused" for g in r.json()["paused"])


# --- the mock files are the real shapes -----------------------------------------------------------

MOCKS: dict[str, tuple[type[BaseModel], bool]] = {
    "today.json": (insights.Today, False),
    "activity.json": (insights.ActivityRow, True),
    "customer_insight.json": (insights.CustomerInsight, False),
    "plan.json": (insights.PlanFacts, False),
    "expected.json": (insights.Expected, False),
    "health_limits.json": (insights.HealthLimits, False),
    "data_status.json": (insights.DataStatus, False),
    "channels.json": (insights.ChannelStatus, True),
    "opportunities_sized.json": (insights.SizedOpportunity, True),
    "batch_breakdowns.json": (insights.BatchBreakdown, True),
}


@pytest.mark.parametrize("name", sorted(MOCKS))
def test_mock_files_have_the_api_shapes(name: str) -> None:
    model, many = MOCKS[name]
    data = json.loads((MOCK / name).read_text(encoding="utf-8"))
    rows = data if many else [data]
    assert rows
    for row in rows:
        assert model.model_validate(row).model_dump(mode="json") == row


def test_mock_files_agree_with_each_other() -> None:
    def load(name: str) -> Any:
        return json.loads((MOCK / name).read_text(encoding="utf-8"))

    today = insights.Today.model_validate(load("today.json"))
    assert today.contacted + today.left_alone == today.customers_considered
    assert sum(today.left_alone_by_reason.values()) == today.left_alone
    assert today.spend_to_date == load("proof.json")["spend"]
    assert today.budget_left == pytest.approx((today.budget or 0) - (today.spend_to_date or 0))
    decisions = {d["decision_id"] for d in load("decisions.json")}
    assert {r["decision"]["decision_id"] for r in load("activity.json")} == decisions
    batches = {b["batch_id"]: b for b in load("batches.json")}
    for b in load("batch_breakdowns.json"):
        assert sum(b["by_channel"].values()) == batches[b["batch_id"]]["n_contacts"]
    plan = insights.PlanFacts.model_validate(load("plan.json"))
    assert plan.audience_n == plan.agent_n + plan.baseline_n + plan.holdout_n
    content = {c["content_id"]: c for c in load("goal_detail.json")["content"]}
    assert {r.content_id for r in plan.content} == set(content)
    for r in plan.content:
        assert r == insights.review_content(ContentItem.model_validate(content[r.content_id]))
    opps = load("opportunities.json")
    assert [s["goal"] for s in load("opportunities_sized.json")] == opps
    assert load("customer_insight.json")["customer_id"] == load("customer.json")["state"]["customer_id"]
