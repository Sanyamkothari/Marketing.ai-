"""P4 'LLM unavailable': every Studio agent degrades to code-written output instead of raising.

The Analyst writes its template briefing, the Creative writes fixed template copy that passes the
Critic's rules on every channel, the Strategist keeps the plan, and the Critic holds rule-passing
copy as DRAFT (never APPROVED) unless the brand made the judge optional. All on a dead model.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from autonomy.agents.analyst import Analyst
from autonomy.agents.base import ScriptedLLM
from autonomy.agents.creative import (
    CHANNEL_BODY_MAX,
    EMAIL_FOOTER,
    TEMPLATE_COPY,
    Creative,
    template_variants,
)
from autonomy.agents.critic import BrandRules, Critic, check_rules
from autonomy.agents.strategist import Strategist
from autonomy.contracts import (
    NO_OFFER,
    Briefing,
    Channel,
    ContentItem,
    ContentStatus,
    Estimate,
    HealthStatus,
    OfferKind,
    OfferOption,
    ProofSummary,
    Theme,
)
from autonomy.fakes.samples import FREE_SHIP, TEN_OFF, sample_brief, sample_goal
from engine.llm import LLMCompletion, LLMError

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)
GOAL = "goal_winback"
GIFT = OfferOption(offer_id="gift", label="A free tote bag", kind=OfferKind.GIFT, cost_per_redemption=3.0)


class DeadLLM:
    """Every call raises `exc`; counts the calls."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or LLMError("LLM_UNAVAILABLE", "the model provider is unreachable")
        self.calls = 0

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        self.calls += 1
        raise self.exc

    def embed(self, texts: Sequence[str], *, model_id: str | None = None) -> tuple[tuple[float, ...], ...]:
        raise self.exc

    def count_tokens(self, text: str, *, model_id: str | None = None) -> int:
        return max(1, len(text) // 4)


def _proof() -> ProofSummary:
    return ProofSummary(
        goal_id=GOAL,
        as_of=NOW,
        agent_n=800,
        baseline_n=100,
        holdout_n=100,
        incremental_revenue=Estimate(point=410.0, low=150.0, high=680.0),
        incremental_profit=None,
        lift_vs_baseline=None,
        spend=82.5,
    )


# --- Analyst ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc", [LLMError("LLM_UNAVAILABLE", "down"), ConnectionError("reset"), TimeoutError()]
)
def test_analyst_uses_its_template_at_once_when_the_model_is_down(exc: Exception) -> None:
    llm = DeadLLM(exc)
    result = Analyst(llm).write_briefing(
        sample_goal(NOW, GOAL), _proof(), HealthStatus(as_of=NOW), {}, "weekly", NOW
    )
    assert result.source == "template"
    assert result.briefing.sections and result.briefing.headline
    assert llm.calls == 1  # no second ask of a provider that is down
    assert any("could not be reached" in p for p in result.problems)


# --- Creative --------------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [LLMError("LLM_UNAVAILABLE", "down"), ConnectionError("reset")])
def test_creative_writes_template_copy_for_every_cell_and_asks_only_once(exc: Exception) -> None:
    brief = sample_brief(NOW, GOAL)
    llm = DeadLLM(exc)
    creative = Creative(llm)
    items = creative.build_library(brief, brand_voice="warm", now=NOW)
    assert llm.calls == 1  # the first failure switches the rest of the build to templates
    assert items and all(i.status is ContentStatus.DRAFT for i in items)
    assert {i.channel for i in items} == set(brief.envelope.channels)
    assert {i.offer_id for i in items} == {o.offer_id for o in brief.envelope.offers}
    assert creative.templated and not creative.failures
    rules = BrandRules.from_brief(brief, brand_voice="warm")
    assert all(check_rules(i, rules) == [] for i in items), [
        (i.content_id, check_rules(i, rules)) for i in items
    ]
    again = Creative(DeadLLM(exc)).build_library(brief, brand_voice="warm", now=NOW)
    assert again == items  # deterministic


@pytest.mark.parametrize("channel", list(Channel))
@pytest.mark.parametrize("offer", [NO_OFFER, TEN_OFF, FREE_SHIP, GIFT])
@pytest.mark.parametrize("theme", list(Theme))
def test_every_template_cell_passes_the_critics_rules(
    channel: Channel, offer: OfferOption, theme: Theme
) -> None:
    brief = sample_brief(NOW, GOAL)
    brief = brief.model_copy(
        update={"envelope": brief.envelope.model_copy(update={"channels": (channel,), "offers": (offer,)})}
    )
    items = Creative(DeadLLM()).build_library(brief, brand_voice="warm", now=NOW, themes=[theme])
    assert len(items) == min(2, len(TEMPLATE_COPY[theme]))
    rules = BrandRules.from_brief(brief, brand_voice="warm")
    for item in items:
        assert check_rules(item, rules) == []
        assert len(item.body) <= CHANNEL_BODY_MAX[channel]
        assert ("offer_label" in item.slots) == (offer.kind is not OfferKind.NONE)
        if channel is Channel.EMAIL:
            assert item.subject and item.body.endswith(EMAIL_FOOTER)
        else:
            assert item.subject is None


def test_template_copy_reads_well_when_a_slot_falls_back() -> None:
    """'our latest picks' (the top_category fallback) must not land after 'your' or before 'order'."""
    for theme, pairs in TEMPLATE_COPY.items():
        for subject, body in pairs:
            for text in (subject, body):
                assert "your {{top_category}}" not in text.lower(), (theme, text)
                assert "our {{top_category}}" not in text.lower(), (theme, text)
                assert "{{top_category}} order" not in text, (theme, text)


def test_template_variants_cover_every_theme_and_cap_at_n() -> None:
    assert set(TEMPLATE_COPY) == set(Theme)
    assert len(template_variants(Channel.SMS, TEN_OFF, Theme.SAVINGS, 1)) == 1


def test_an_unreadable_answer_still_skips_its_cell_rather_than_templating() -> None:
    creative = Creative(ScriptedLLM(lambda _s, _p: "not json"))
    items = creative.build_library(sample_brief(NOW, GOAL), brand_voice="warm", now=NOW)
    assert items == [] and creative.failures and not creative.templated


# --- Critic ----------------------------------------------------------------------------------


def _passing_item() -> ContentItem:
    brief = sample_brief(NOW, GOAL)
    [item] = Creative(DeadLLM()).build_library(
        brief.model_copy(
            update={
                "envelope": brief.envelope.model_copy(
                    update={"channels": (Channel.EMAIL,), "offers": (TEN_OFF,)}
                )
            }
        ),
        brand_voice="warm",
        n_per_cell=1,
        now=NOW,
        themes=[Theme.SAVINGS],
    )
    return item


def _judge(score: int) -> ScriptedLLM:
    def script(_system: str, prompt: str) -> str:
        return json.dumps(
            {"scores": [{"criterion": c, "score": score, "reason": "ok"} for c in ("brand_voice", "honesty")]}
        )

    return ScriptedLLM(script)


@pytest.mark.parametrize("exc", [LLMError("LLM_UNAVAILABLE", "down"), ConnectionError("reset")])
def test_critic_holds_rule_passing_copy_as_draft_while_the_judge_is_down(exc: Exception) -> None:
    item = _passing_item()
    rules = BrandRules.from_brief(sample_brief(NOW, GOAL), brand_voice="warm")
    llm = DeadLLM(exc)
    broken = item.model_copy(update={"content_id": "ci_broken", "body": "New in {{top_category}}."})
    held, rejected, other = Critic(llm).review([item, broken, item], rules)  # the whole review survives
    assert held.status is ContentStatus.DRAFT and other.status is ContentStatus.DRAFT
    assert any(n.startswith("judge unavailable") for n in held.critic_notes)
    assert any(n.startswith("judge unavailable (not asked") for n in other.critic_notes)
    assert held.critic_notes[-1] == other.critic_notes[-1] == "held as a draft until the judge is back"
    assert rejected.status is ContentStatus.REJECTED  # rules still reject while the judge is down
    assert llm.calls == 1  # the judge is not asked again, item after item, once it has failed


def test_critic_approves_on_rules_alone_only_when_the_brand_says_judge_optional() -> None:
    item = _passing_item()
    rules = BrandRules.from_brief(sample_brief(NOW, GOAL), brand_voice="warm").model_copy(
        update={"judge_optional": True}
    )
    [reviewed] = Critic(DeadLLM()).review([item], rules)
    assert reviewed.status is ContentStatus.APPROVED
    assert any(n.startswith("judge unavailable") for n in reviewed.critic_notes)


def test_critic_rejects_when_one_run_already_failed_before_the_judge_went_down() -> None:
    calls = {"n": 0}

    def script(_system: str, prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] > 1:
            raise LLMError("LLM_UNAVAILABLE", "down")
        return _judge(1).complete(prompt).text

    [reviewed] = Critic(ScriptedLLM(script)).review(
        [_passing_item()], BrandRules.from_brief(sample_brief(NOW, GOAL), brand_voice="warm")
    )
    assert reviewed.status is ContentStatus.REJECTED


def test_strategist_adjust_keeps_the_plan_on_any_llm_error() -> None:
    approved = sample_brief(NOW, GOAL)
    report = Briefing(briefing_id="b", goal_id=GOAL, as_of=NOW, period="weekly", headline="Quiet")
    adjusted = Strategist(DeadLLM(LLMError("LLM_REFUSED", "no"))).adjust_brief(approved, report, NOW)
    assert adjusted.envelope == approved.envelope and "No change this week" in adjusted.rationale
