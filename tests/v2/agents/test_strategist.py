"""Strategist (goal -> Brief, weekly narrowing) and the goal lifecycle state machine, on ScriptedLLM."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from autonomy.agents.base import ScriptedLLM
from autonomy.agents.orchestrator import GoalStage, IllegalTransitionError, Orchestrator, UnknownGoalError
from autonomy.agents.strategist import (
    TEMPLATES,
    BusinessFacts,
    Strategist,
    brief_within,
    load_prompt,
    numbers_in,
    template_brief,
)
from autonomy.contracts import (
    NO_OFFER,
    AutonomyLevel,
    Brief,
    Briefing,
    BriefingSection,
    Channel,
    FollowUpStep,
    Goal,
    GoalKind,
    GoalStatus,
    Objective,
    OfferKind,
    OfferOption,
)
from autonomy.fakes import InMemoryWorkspace
from autonomy.fakes.samples import FREE_SHIP, TEN_OFF, sample_content, sample_goal
from engine.llm import LLMError

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
VIP = OfferOption(
    offer_id="vip30", label="30% off", kind=OfferKind.PERCENT_OFF, amount=30, needs_approval=True
)
FACTS = BusinessFacts(
    business_id="demo",
    name="Demo Shop",
    channels=(Channel.EMAIL, Channel.WHATSAPP),
    offers=(TEN_OFF, FREE_SHIP, VIP),
    max_contacts_per_week=2,
)


def good_draft(**overrides: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "kind": "win_back",
        "objective": "gross_profit",
        "audience_description": "Past buyers quiet for 60 days",
        "audience_filters": [
            {"field": "purchases", "op": "gte", "value": 1},
            {"field": "last_purchase_at", "op": "days_since_gt", "value": 60},
        ],
        "channels": ["email", "whatsapp"],
        "offer_ids": ["freeship", "pct10"],
        "horizon_days": 14,
        "max_contacts_per_week": 2,
        "follow_ups": [{"after_days": 3, "when": "no_purchase"}],
        "rationale": "Lapsed buyers often return with a reminder; offers only where they help.",
    }
    draft.update(overrides)
    return draft


def scripted(draft: dict[str, Any] | str, adjust: dict[str, Any] | str = "") -> ScriptedLLM:
    def answer(system: str, prompt: str) -> str:
        assert system == load_prompt("strategist")
        reply = draft if prompt.startswith("Draft a brief") else adjust
        return reply if isinstance(reply, str) else json.dumps(reply)

    return ScriptedLLM(answer)


def report(**numbers: float) -> Briefing:
    return Briefing(
        briefing_id="br_1",
        goal_id="goal_winback",
        as_of=NOW,
        period="weekly",
        headline="WhatsApp is tiring people; the 10% offer is not paying for itself.",
        sections=(
            BriefingSection(title="Offers", body="Ten percent off lost money.", numbers=dict(numbers)),
        ),
    )


# --- draft_brief --------------------------------------------------------------------------------


def test_happy_path_builds_the_brief_from_the_models_picks() -> None:
    llm = scripted(good_draft())
    brief = Strategist(llm).draft_brief(sample_goal(NOW), business_facts=FACTS, now=NOW)

    assert brief.created_by == "strategist"
    assert brief.version == 1 and not brief.approved
    assert brief.goal_id == "goal_winback"
    assert brief.envelope.budget == 5000  # the goal's budget, set by code
    assert brief.envelope.channels == (Channel.EMAIL, Channel.WHATSAPP)
    assert [o.offer_id for o in brief.envelope.offers] == ["none", "freeship", "pct10"]
    assert brief.envelope.offers[0] == NO_OFFER
    assert [(f.field, f.op, f.value) for f in brief.audience.filters] == [
        ("purchases", "gte", 1),
        ("last_purchase_at", "days_since_gt", 60),
    ]
    assert brief.follow_ups[0].after_days == 3
    assert brief.rationale.startswith("Lapsed buyers often return")
    assert "Template" not in brief.rationale and "template" not in brief.rationale
    assert len(llm.calls) == 1


def test_same_inputs_give_the_same_brief() -> None:
    a = Strategist(scripted(good_draft())).draft_brief(sample_goal(NOW), business_facts=FACTS, now=NOW)
    b = Strategist(scripted(good_draft())).draft_brief(sample_goal(NOW), business_facts=FACTS, now=NOW)
    assert a == b


def test_invalid_enum_falls_back_to_the_template() -> None:
    llm = scripted(good_draft(channels=["fax"]))
    brief = Strategist(llm).draft_brief(sample_goal(NOW), business_facts=FACTS, now=NOW)

    assert len(llm.calls) == 2  # one repair attempt, then AgentOutputError
    assert brief.created_by == "strategist:template"
    assert "could not be read" in brief.rationale
    assert brief.audience.filters == TEMPLATES[GoalKind.WIN_BACK].filters
    # the template never includes an offer that needs approval
    assert "vip30" not in {o.offer_id for o in brief.envelope.offers}
    assert brief.envelope.budget == 5000


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"channels": ["email", "sms"]}, "channel sms"),
        ({"offer_ids": ["pct50"]}, "offer 'pct50'"),
        ({"audience_filters": [{"field": "credit_score", "op": "gt", "value": 1}]}, "credit_score"),
        ({"audience_filters": [{"field": "last_purchase_at", "op": "days_since_gt", "value": 47}]}, "47"),
        ({"audience_filters": [{"field": "purchases", "op": "days_since_gt", "value": 1}]}, "operator"),
        ({"max_contacts_per_week": 3}, "ceiling"),
        ({"kind": "cross_sell"}, "the goal is win_back"),
        ({"follow_ups": [{"after_days": 14, "when": "always"}], "horizon_days": 7}, "horizon"),
    ],
)
def test_values_outside_the_lists_fall_back(overrides: dict[str, Any], reason: str) -> None:
    brief = Strategist(scripted(good_draft(**overrides))).draft_brief(
        sample_goal(NOW), business_facts=FACTS, now=NOW
    )
    assert brief.created_by == "strategist:template"
    assert "outside the allowed lists" in brief.rationale
    assert reason in brief.rationale


def test_a_threshold_from_the_goal_text_is_allowed() -> None:
    goal = sample_goal(NOW).model_copy(update={"text": "Win back people quiet for 75 days, $5k"})
    draft = good_draft(
        audience_filters=[{"field": "last_purchase_at", "op": "days_since_gt", "value": 75}],
        audience_description="Quiet for 75 days",
    )
    brief = Strategist(scripted(draft)).draft_brief(goal, business_facts=FACTS, now=NOW)
    assert brief.created_by == "strategist"
    assert brief.audience.filters[0].value == 75
    assert brief.audience.description == "Quiet for 75 days"


def test_invented_numbers_in_the_words_are_replaced_by_code() -> None:
    draft = good_draft(
        rationale="This will make 12,345 dollars.", audience_description="The 3,210 best customers"
    )
    brief = Strategist(scripted(draft)).draft_brief(sample_goal(NOW), business_facts=FACTS, now=NOW)
    assert brief.created_by == "strategist"
    assert "12,345" not in brief.rationale and "3,210" not in brief.audience.description
    assert brief.audience.description.startswith("Purchases >= 1")


def test_custom_goal_falls_back_by_keyword() -> None:
    goal = sample_goal(NOW).model_copy(
        update={"kind": GoalKind.CUSTOM, "text": "Recover abandoned carts this week, $500"}
    )
    brief = Strategist(scripted("not json at all")).draft_brief(goal, business_facts=FACTS, now=NOW)
    assert brief.created_by == "strategist:template"
    assert brief.horizon_days == TEMPLATES[GoalKind.CART_RECOVERY].horizon_days
    assert {o.kind for o in brief.envelope.offers} <= {
        OfferKind.NONE,
        OfferKind.PERCENT_OFF,
        OfferKind.FREE_SHIPPING,
    }


@pytest.mark.parametrize("kind", list(GoalKind))
def test_every_kind_has_a_valid_template(kind: GoalKind) -> None:
    goal = sample_goal(NOW).model_copy(update={"kind": kind})
    brief = template_brief(goal, FACTS, NOW)
    assert brief.envelope.budget == goal.budget
    assert brief.envelope.offers[0] == NO_OFFER
    assert all(s.after_days <= brief.horizon_days for s in brief.follow_ups)
    assert brief.created_by == "strategist:template"


def test_facts_for_another_business_are_refused() -> None:
    with pytest.raises(ValueError, match="belongs to"):
        Strategist(scripted(good_draft())).draft_brief(
            sample_goal(NOW), business_facts=FACTS.model_copy(update={"business_id": "other"}), now=NOW
        )


def test_numbers_in_reads_money_and_percentages() -> None:
    assert numbers_in("$5k this month, 10% off, 1,900 people, 60-day") == {5000, 10, 1900, 60}


def test_numbers_in_catches_digits_glued_to_words() -> None:
    # "3x" and "14days" used to slip past the invented-number check
    assert numbers_in("we will 3x revenue in 14days, or 1.5x, over 5km") == {3, 14, 1.5, 5}


def test_rationale_with_a_glued_number_is_replaced() -> None:
    draft = good_draft(rationale="This will 37x your revenue.")
    brief = Strategist(scripted(draft)).draft_brief(sample_goal(NOW), business_facts=FACTS, now=NOW)
    assert "37x" not in brief.rationale


def test_an_unreachable_model_falls_back_to_the_template() -> None:
    def down(system: str, prompt: str) -> str:
        raise LLMError("LLM_UNAVAILABLE", "The model could not be reached.")

    brief = Strategist(ScriptedLLM(down)).draft_brief(sample_goal(NOW), business_facts=FACTS, now=NOW)
    assert brief.created_by == "strategist:template"
    assert "LLM_UNAVAILABLE" in brief.rationale
    later = Strategist(ScriptedLLM(down)).adjust_brief(approved_brief(), report(), NOW)
    assert later.version == 2 and later.envelope == approved_brief().envelope


# --- adjust_brief ---------------------------------------------------------------------------------


def approved_brief() -> Brief:
    brief = Strategist(scripted(good_draft())).draft_brief(sample_goal(NOW), business_facts=FACTS, now=NOW)
    return brief.model_copy(update={"approved_by": "founder", "approved_at": NOW})


def test_adjust_cannot_widen() -> None:
    brief = approved_brief()
    greedy = {
        "keep_channels": ["email", "whatsapp", "sms"],
        "keep_offer_ids": ["none", "freeship", "pct10", "vip30"],
        "max_contacts_per_week": 5,
        "keep_follow_up_days": [3, 7],
        "rationale": "Go bigger.",
    }
    later = NOW + timedelta(days=7)
    new = Strategist(scripted(good_draft(), greedy)).adjust_brief(brief, report(), later)

    assert brief_within(new, brief) == []
    assert new.version == 2 and new.approved and new.created_at == later
    assert new.envelope == brief.envelope
    assert new.follow_ups == brief.follow_ups
    assert "sms" in new.rationale and "vip30" in new.rationale and "No change" in new.rationale


def test_adjust_narrows_inside_the_envelope() -> None:
    brief = approved_brief()
    narrow = {
        "keep_channels": ["email"],
        "keep_offer_ids": ["freeship"],
        "max_contacts_per_week": 1,
        "keep_follow_up_days": [],
        "rationale": "The 10% offer lost money at -120.5 net.",
    }
    new = Strategist(scripted(good_draft(), narrow)).adjust_brief(brief, report(net=-120.5), NOW)

    assert new.envelope.channels == (Channel.EMAIL,)
    assert [o.offer_id for o in new.envelope.offers] == ["none", "freeship"]  # "no offer" always stays
    assert new.envelope.max_contacts_per_week == 1
    assert new.follow_ups == ()
    assert new.envelope.budget == brief.envelope.budget
    assert brief_within(new, brief) == []
    assert new.rationale.startswith("The 10% offer lost money")


def test_adjust_drops_a_follow_up_on_a_dropped_channel() -> None:
    pinned = FollowUpStep(after_days=3, when="no_purchase", channel=Channel.WHATSAPP)
    brief = approved_brief().model_copy(update={"follow_ups": (pinned,)})
    narrow = {"keep_channels": ["email"], "keep_offer_ids": ["freeship", "pct10"], "max_contacts_per_week": 2}
    new = Strategist(scripted(good_draft(), narrow)).adjust_brief(brief, report(), NOW)
    assert new.envelope.channels == (Channel.EMAIL,)
    assert new.follow_ups == ()
    assert brief_within(new, brief) == []
    # and the check itself refuses a follow-up left on a channel the envelope no longer has
    stale = new.model_copy(update={"follow_ups": (pinned,)})
    assert "a follow-up uses a channel outside the envelope" in brief_within(stale, brief)


def test_adjust_keeps_small_rates_from_the_report() -> None:
    answer = {
        "keep_channels": ["email", "whatsapp"],
        "keep_offer_ids": ["freeship", "pct10"],
        "max_contacts_per_week": 2,
        "rationale": "Complaints are at 0.0008, so hold steady.",
    }
    llm = scripted(good_draft(), answer)
    new = Strategist(llm).adjust_brief(approved_brief(), report(complaint_rate=0.0008), NOW)
    assert "complaint_rate=0.0008" in llm.calls[-1][1]  # shown to the model, not rounded to 0
    assert new.rationale.startswith("Complaints are at 0.0008")


def test_adjust_refuses_a_report_for_another_goal() -> None:
    other = report().model_copy(update={"goal_id": "goal_other"})
    with pytest.raises(ValueError, match="goal_other"):
        Strategist(scripted(good_draft(), "{}")).adjust_brief(approved_brief(), other, NOW)


def test_adjust_hides_reasoning_with_invented_numbers() -> None:
    answer = {
        "keep_channels": ["email", "whatsapp"],
        "keep_offer_ids": ["freeship", "pct10"],
        "max_contacts_per_week": 2,
        "rationale": "This made 98,765 dollars.",
    }
    new = Strategist(scripted(good_draft(), answer)).adjust_brief(approved_brief(), report(), NOW)
    assert "98,765" not in new.rationale


def test_adjust_keeps_the_brief_when_the_model_fails() -> None:
    brief = approved_brief()
    new = Strategist(scripted(good_draft(), "{oops")).adjust_brief(brief, report(), NOW)
    assert new.version == 2 and new.envelope == brief.envelope and new.approved
    assert "could not be read" in new.rationale


# --- orchestrator ----------------------------------------------------------------------------------


def setup(
    adjust_answer: dict[str, Any] | str = "",
) -> tuple[InMemoryWorkspace, Orchestrator, Strategist, Goal]:
    ws = InMemoryWorkspace()
    strategist = Strategist(scripted(good_draft(), adjust_answer))
    orch = Orchestrator(ws, analyst=lambda g, b, now: report(), adjust=strategist.adjust_brief)
    goal = sample_goal(NOW)
    orch.create(goal)
    return ws, orch, strategist, goal


def test_lifecycle_happy_path() -> None:
    narrow = {"keep_channels": ["email"], "keep_offer_ids": ["none"], "max_contacts_per_week": 1}
    _, orch, strategist, goal = setup(narrow)
    assert orch.stage(goal) is GoalStage.DRAFT

    orch.attach_brief(goal, strategist.draft_brief(goal, business_facts=FACTS, now=NOW))
    assert orch.stage(goal) is GoalStage.BRIEFED
    # a redraft before approval replaces the brief as a newer version
    redraft = orch.attach_brief(goal, strategist.draft_brief(goal, business_facts=FACTS, now=NOW))
    assert redraft.version == 2

    approved = orch.approve(goal, approved_by="founder", now=NOW, autonomy=AutonomyLevel.AUTOPILOT)
    assert approved.approved and orch.stage(goal) is GoalStage.APPROVED
    assert orch.goal(goal).autonomy is AutonomyLevel.AUTOPILOT

    assert orch.add_library(goal, sample_content(NOW)) is GoalStage.LIBRARY_READY
    assert orch.activate(goal, now=NOW).status is GoalStatus.ACTIVE
    assert orch.pause(goal).status is GoalStatus.PAUSED
    assert orch.resume(goal, now=NOW).status is GoalStatus.ACTIVE

    out = orch.weekly_cycle(goal, now=NOW + timedelta(days=7))
    assert out.changed and out.brief.version == 3
    assert out.brief.envelope.channels == (Channel.EMAIL,)
    assert orch.brief(goal) == out.brief

    assert orch.finish(goal).status is GoalStatus.DONE
    assert orch.stage(goal) is GoalStage.DONE


def test_guards_refuse_illegal_moves() -> None:
    ws, orch, strategist, goal = setup()
    with pytest.raises(UnknownGoalError):
        orch.stage("nope")
    with pytest.raises(IllegalTransitionError, match="already exists"):
        orch.create(goal)
    with pytest.raises(IllegalTransitionError, match="approve"):
        orch.approve(goal, approved_by="founder", now=NOW)
    with pytest.raises(IllegalTransitionError, match="activate"):
        orch.activate(goal, now=NOW)
    with pytest.raises(IllegalTransitionError, match="pause"):
        orch.pause(goal)
    with pytest.raises(IllegalTransitionError, match="weekly"):
        orch.weekly_cycle(goal, now=NOW)

    brief = strategist.draft_brief(goal, business_facts=FACTS, now=NOW)
    with pytest.raises(IllegalTransitionError, match="approval is its own step"):
        orch.attach_brief(goal, brief.model_copy(update={"approved_by": "x", "approved_at": NOW}))
    with pytest.raises(IllegalTransitionError, match="budget"):
        orch.attach_brief(
            goal, brief.model_copy(update={"envelope": brief.envelope.model_copy(update={"budget": 9e9})})
        )
    orch.attach_brief(goal, brief)
    with pytest.raises(IllegalTransitionError, match="needs a name"):
        orch.approve(goal, approved_by=" ", now=NOW)
    with pytest.raises(IllegalTransitionError, match="not approved"):
        orch.add_library(goal, sample_content(NOW))
    orch.approve(goal, approved_by="founder", now=NOW)
    with pytest.raises(IllegalTransitionError, match="only before approval"):
        orch.attach_brief(goal, brief)

    sms_only = [c.model_copy(update={"channel": Channel.SMS}) for c in sample_content(NOW)]
    with pytest.raises(IllegalTransitionError, match="no approved item fits"):
        orch.add_library(goal, sms_only)
    assert orch.stage(goal) is GoalStage.APPROVED

    orch.add_library(goal, sample_content(NOW))
    with pytest.raises(IllegalTransitionError, match="resume"):
        orch.resume(goal, now=NOW)
    with pytest.raises(IllegalTransitionError, match="finish"):
        orch.finish(goal)
    ended = NOW + timedelta(days=40)
    ws.put_goal(orch.goal(goal).model_copy(update={"ends_at": NOW + timedelta(days=30)}))
    with pytest.raises(IllegalTransitionError, match="end date"):
        orch.activate(goal, now=ended)
    orch.activate(goal, now=NOW)
    orch.finish(goal)
    with pytest.raises(IllegalTransitionError, match="resume"):
        orch.resume(goal, now=NOW)


def test_weekly_cycle_refuses_a_widened_brief() -> None:
    ws = InMemoryWorkspace()
    strategist = Strategist(scripted(good_draft()))

    def widen(brief: Brief, insights: Briefing, now: datetime) -> Brief:
        env = brief.envelope.model_copy(
            update={"channels": (*brief.envelope.channels, Channel.SMS), "budget": 10_000}
        )
        return brief.model_copy(update={"version": brief.version + 1, "envelope": env})

    orch = Orchestrator(ws, analyst=lambda g, b, now: report(), adjust=widen)
    goal = sample_goal(NOW)
    orch.create(goal)
    orch.attach_brief(goal, strategist.draft_brief(goal, business_facts=FACTS, now=NOW))
    orch.approve(goal, approved_by="founder", now=NOW)
    orch.add_library(goal, sample_content(NOW))
    orch.activate(goal, now=NOW)
    before = orch.brief(goal)

    with pytest.raises(IllegalTransitionError, match="channels widened; budget widened"):
        orch.weekly_cycle(goal, now=NOW)
    assert orch.brief(goal) == before


def test_weekly_cycle_refuses_a_changed_approval() -> None:
    ws = InMemoryWorkspace()
    strategist = Strategist(scripted(good_draft()))

    def resign(brief: Brief, insights: Briefing, now: datetime) -> Brief:
        return brief.model_copy(update={"version": brief.version + 1, "approved_by": "the agent"})

    orch = Orchestrator(ws, analyst=lambda g, b, now: report(), adjust=resign)
    goal = sample_goal(NOW)
    orch.create(goal)
    orch.attach_brief(goal, strategist.draft_brief(goal, business_facts=FACTS, now=NOW))
    orch.approve(goal, approved_by="founder", now=NOW)
    orch.add_library(goal, sample_content(NOW))
    orch.activate(goal, now=NOW)
    with pytest.raises(IllegalTransitionError, match="approval changed"):
        orch.weekly_cycle(goal, now=NOW)


def test_weekly_cycle_needs_its_callables() -> None:
    ws = InMemoryWorkspace()
    orch = Orchestrator(ws)
    goal = sample_goal(NOW).model_copy(update={"objective": Objective.REVENUE})
    orch.create(goal)
    orch.attach_brief(goal, template_brief(goal, FACTS, NOW))
    orch.approve(goal, approved_by="founder", now=NOW)
    orch.add_library(goal, sample_content(NOW))
    orch.activate(goal, now=NOW)
    with pytest.raises(ValueError, match="analyst"):
        orch.weekly_cycle(goal, now=NOW)
