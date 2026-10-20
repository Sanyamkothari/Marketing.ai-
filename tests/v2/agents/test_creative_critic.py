"""Creative builds a labelled DRAFT library; Critic rejects rule breaks before the judge, then judges twice."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from autonomy.agents.base import ScriptedLLM
from autonomy.agents.creative import (
    ALLOWED_SLOTS,
    CHANNEL_BODY_MAX,
    EMAIL_FOOTER,
    EMAIL_SUBJECT_MAX,
    OFFER_THEMES,
    Creative,
)
from autonomy.agents.critic import RUBRIC, BrandRules, Critic, check_rules
from autonomy.contracts import Channel, ContentItem, ContentStatus, OfferKind, Theme
from autonomy.fakes.samples import FREE_SHIP, TEN_OFF, sample_brief
from engine.llm import LLMCompletion

NOW = datetime(2026, 10, 1, tzinfo=UTC)
VOICE = "Warm, plain and brief. Never pushy."


def _field(prompt: str, name: str) -> str:
    match = re.search(rf"^{name}: ([a-z_]+)", prompt, re.MULTILINE)
    assert match, prompt
    return match.group(1)


def _creative_script(system: str, prompt: str) -> str:
    assert "Creative" in system
    channel, theme = _field(prompt, "Channel"), _field(prompt, "Theme")
    has_offer = "{{offer_label}}" in prompt
    variants = []
    for k in range(2):
        body = f"Hi {{{{first_name}}}}, {theme} idea {k} for {{{{top_category}}}}."
        if has_offer:
            body += " Use {{offer_label}}."
        variants.append({"subject": f"A {theme} note {k}" if channel == "email" else None, "body": body})
    return json.dumps({"variants": variants})


# --- Creative --------------------------------------------------------------------------------


def test_library_covers_every_cell_with_code_assigned_labels() -> None:
    brief = sample_brief(NOW)
    llm = ScriptedLLM(_creative_script)
    items = Creative(llm).build_library(brief, brand_voice=VOICE, n_per_cell=2, now=NOW)

    offers = brief.envelope.offers
    cells = sum(len(OFFER_THEMES[o.kind]) for o in offers) * len(brief.envelope.channels)
    assert len(llm.calls) == cells
    assert len(items) == 2 * cells
    assert len({i.content_id for i in items}) == len(items)
    for item in items:
        assert item.status is ContentStatus.DRAFT
        assert item.goal_id == brief.goal_id
        assert item.created_at == NOW
        assert set(item.slots) <= set(ALLOWED_SLOTS)
        assert "first_name" in item.slots
        assert ("offer_label" in item.slots) == (item.offer_id != "none")
        if item.channel is Channel.EMAIL:
            assert item.subject and len(item.subject) <= EMAIL_SUBJECT_MAX
            assert item.body.endswith(EMAIL_FOOTER)
            assert item.template_name is None
        else:
            assert item.subject is None
            assert item.template_name and re.fullmatch(r"[a-z0-9_]+", item.template_name)
            assert len(item.body) <= CHANNEL_BODY_MAX[Channel.WHATSAPP]
    # No savings copy for the no-offer cells: themes follow the offer kind.
    assert {i.theme for i in items if i.offer_id == "none"} == set(OFFER_THEMES[OfferKind.NONE])


def test_library_is_deterministic_and_respects_chosen_themes() -> None:
    brief = sample_brief(NOW)
    a = Creative(ScriptedLLM(_creative_script)).build_library(
        brief, brand_voice=VOICE, now=NOW, themes=[Theme.LOYALTY]
    )
    b = Creative(ScriptedLLM(_creative_script)).build_library(
        brief, brand_voice=VOICE, now=NOW, themes=[Theme.LOYALTY]
    )
    assert a == b
    assert {i.theme for i in a} == {Theme.LOYALTY}
    assert len(a) == 2 * len(brief.envelope.channels) * len(brief.envelope.offers)


def test_overlong_and_subjectless_variants_are_dropped_and_bad_cells_skipped() -> None:
    def script(_system: str, prompt: str) -> str:
        channel, theme = _field(prompt, "Channel"), _field(prompt, "Theme")
        if theme == "education":
            return "not json at all"
        if channel == "email":
            return json.dumps(
                {
                    "variants": [
                        {"subject": "x" * (EMAIL_SUBJECT_MAX + 1), "body": "Too long a subject."},
                        {"subject": None, "body": "No subject."},
                        {"subject": "Fine", "body": "Fine body. Unsubscribe any time."},
                    ]
                }
            )
        return json.dumps({"variants": [{"body": "y" * 1025}, {"body": "Short and fine."}]})

    brief = sample_brief(NOW)
    creative = Creative(ScriptedLLM(script))
    items = creative.build_library(brief, brand_voice=VOICE, n_per_cell=3, now=NOW)

    assert all(i.theme is not Theme.EDUCATION for i in items)
    assert len(creative.failures) == 2  # education cell, once per channel
    assert all(len(i.body) <= 1024 for i in items if i.channel is Channel.WHATSAPP)
    emails = [i for i in items if i.channel is Channel.EMAIL]
    assert emails and all(i.subject == "Fine" for i in emails)
    assert all(i.body == "Fine body. Unsubscribe any time." for i in emails)  # hint present: no footer
    assert any("subject over" in d for d in creative.dropped)
    assert any("without a subject" in d for d in creative.dropped)
    assert any("body over 1024" in d for d in creative.dropped)


# --- Critic: rules ---------------------------------------------------------------------------


def _item(**overrides: object) -> ContentItem:
    base: dict[str, object] = {
        "content_id": "ci_test",
        "goal_id": "goal_winback",
        "channel": Channel.EMAIL,
        "theme": Theme.SAVINGS,
        "offer_id": "pct10",
        "subject": "Something for you, {{first_name}}",
        "body": "Come back to {{top_category}} with {{offer_label}} this week. Unsubscribe any time.",
        "slots": ("first_name", "top_category", "offer_label"),
        "created_at": NOW,
    }
    base.update(overrides)
    return ContentItem.model_validate(base)


RULES = BrandRules.from_brief(sample_brief(NOW), brand_voice=VOICE, allowed_domains=("shop.example.com",))


def _verdict(score: int) -> str:
    return json.dumps({"scores": [{"criterion": c, "score": score, "reason": "fine"} for c in RUBRIC]})


def _judge(score: int = 4) -> ScriptedLLM:
    return ScriptedLLM(lambda _s, _p: _verdict(score), model_id="other-family")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"subject": "x" * 71}, "subject is 71"),
        ({"subject": None}, "needs a subject"),
        ({"body": "Hello {{top_category}}."}, "unsubscribe hint"),
        (
            {"channel": Channel.WHATSAPP, "subject": None, "template_name": "t", "body": "z" * 1025},
            "whatsapp allows 1024",
        ),
        ({"channel": Channel.WHATSAPP, "subject": None, "body": "Hi."}, "template name"),
        ({"body": "Hi {{loyalty_points}}. Unsubscribe any time."}, "unknown slots: loyalty_points"),
        ({"body": "Hi {{first_name}. Unsubscribe any time."}, "malformed braces"),
        ({"body": "Hi {first_name}. Unsubscribe any time."}, "malformed braces"),
        ({"body": "HUGE savings today. Unsubscribe any time."}, "ALL-CAPS shouting: HUGE"),
        ({"body": "Come back now!! Unsubscribe any time."}, "exclamation"),
        ({"body": "Results guaranteed. Unsubscribe any time."}, "banned phrase: 'guaranteed'"),
        ({"body": "A Risk Free trial. Unsubscribe any time."}, "banned phrase: 'risk-free'"),
        ({"body": "Act now or lose it. Unsubscribe any time."}, "act now or lose"),
        ({"body": "Take 20% off. Unsubscribe any time."}, "unapproved discount figure: 20%"),
        ({"body": "Save $15 today. Unsubscribe any time."}, "unapproved money figure: 15"),
        (
            {"offer_id": "none", "body": "Take 10% off. Unsubscribe any time.", "slots": ()},
            "unapproved discount figure: 10%",
        ),
        ({"offer_id": "pct50"}, "not an approved offer"),
        ({"offer_id": "none"}, "has no offer"),
        ({"body": "See https://evil.example.net/x. Unsubscribe any time."}, "evil.example.net"),
        ({"body": "Visit bit.ly/abc. Unsubscribe any time."}, "bit.ly"),
    ],
)
def test_rule_breaks_are_rejected_without_calling_the_judge(
    overrides: dict[str, object], expected: str
) -> None:
    judge = _judge()
    [reviewed] = Critic(judge).review([_item(**overrides)], RULES)
    assert reviewed.status is ContentStatus.REJECTED
    assert any(expected in note for note in reviewed.critic_notes), reviewed.critic_notes
    assert all(note.startswith("rule: ") for note in reviewed.critic_notes)
    assert judge.calls == []


def test_clean_copy_passes_the_rules() -> None:
    assert check_rules(_item(), RULES) == []
    ok = _item(body="Take 10% off at https://www.shop.example.com/back. Unsubscribe any time.")
    assert check_rules(ok, RULES) == []
    free_ship = _item(offer_id=FREE_SHIP.offer_id, body="Free shipping, {{first_name}}. Opt out any time.")
    assert check_rules(free_ship, RULES) == []
    assert (
        check_rules(_item(offer_id=TEN_OFF.offer_id, body="SALE on now. Unsubscribe any time."), RULES) == []
    )


# --- Critic: judge ---------------------------------------------------------------------------


def test_judge_runs_twice_per_item_alone_with_the_rubric_reordered() -> None:
    judge = _judge(4)
    items = [_item(content_id="ci_a"), _item(content_id="ci_b", subject="Another one")]
    reviewed = Critic(judge, judge_model_id="other-family").review(items, RULES)

    assert [r.status for r in reviewed] == [ContentStatus.APPROVED, ContentStatus.APPROVED]
    assert len(judge.calls) == 4
    for (_s1, first), (_s2, second) in (judge.calls[0:2], judge.calls[2:4]):
        orders = [sorted(RUBRIC, key=prompt.index) for prompt in (first, second)]
        assert orders[0] == list(reversed(orders[1]))
        assert first.count("- body:") == 1  # one item per call, never side by side
    assert "ci_b" not in judge.calls[0][1] and "Another one" not in judge.calls[0][1]
    assert reviewed[0].critic_notes[-1] == "approved"
    assert sum(n.startswith("judge run") for n in reviewed[0].critic_notes) == 2


def test_judge_model_id_is_passed_through() -> None:
    class RecordingJudge(ScriptedLLM):
        def __init__(self) -> None:
            super().__init__(lambda _s, _p: _verdict(5))
            self.model_ids: list[str | None] = []

        def complete(
            self,
            prompt: str,
            *,
            system: str = "",
            model_id: str | None = None,
            max_tokens: int = 512,
            temperature: float = 0.0,
        ) -> LLMCompletion:
            self.model_ids.append(model_id)
            return super().complete(
                prompt, system=system, model_id=model_id, max_tokens=max_tokens, temperature=temperature
            )

    judge = RecordingJudge()
    Critic(judge, judge_model_id="judge-family-x").review([_item()], RULES)
    assert judge.model_ids == ["judge-family-x", "judge-family-x"]


@pytest.mark.parametrize(("score", "status"), [(2, ContentStatus.REJECTED), (3, ContentStatus.APPROVED)])
def test_threshold(score: int, status: ContentStatus) -> None:
    [reviewed] = Critic(_judge(score), threshold=3).review([_item()], RULES)
    assert reviewed.status is status


def test_either_run_below_threshold_rejects() -> None:
    calls = {"n": 0}

    def script(_system: str, prompt: str) -> str:
        calls["n"] += 1
        # Position bias: the judge marks down whichever criterion comes first, but only in run 2.
        first = min(RUBRIC, key=prompt.index)
        low = calls["n"] == 2
        return json.dumps(
            {
                "scores": [
                    {"criterion": c, "score": 2 if (low and c == first) else 5, "reason": "r"} for c in RUBRIC
                ]
            }
        )

    [reviewed] = Critic(ScriptedLLM(script)).review([_item()], RULES)
    assert calls["n"] == 2
    assert reviewed.status is ContentStatus.REJECTED
    assert "run 2 scored" in reviewed.critic_notes[-1]


def test_a_judge_that_never_answers_validly_rejects_fail_closed() -> None:
    incomplete = json.dumps({"scores": [{"criterion": "honesty", "score": 5, "reason": "ok"}]})
    judge = ScriptedLLM(lambda _s, _p: incomplete)
    [reviewed] = Critic(judge).review([_item()], RULES)
    assert reviewed.status is ContentStatus.REJECTED
    assert len(judge.calls) == 4  # each run asks, then repairs once
    assert "no valid verdict" in reviewed.critic_notes[-1]


def test_rubric_order_is_seeded() -> None:
    item = _item()
    assert Critic(_judge(), seed=7).rubric_orders(item) == Critic(_judge(), seed=7).rubric_orders(item)


def test_creative_to_critic_end_to_end() -> None:
    brief = sample_brief(NOW)
    items = Creative(ScriptedLLM(_creative_script)).build_library(brief, brand_voice=VOICE, now=NOW)
    reviewed = Critic(_judge(4)).review(items, BrandRules.from_brief(brief, brand_voice=VOICE))
    assert reviewed and all(r.status is ContentStatus.APPROVED for r in reviewed), [
        r.critic_notes for r in reviewed if r.status is not ContentStatus.APPROVED
    ]


def test_duplicate_emails_are_caught_even_when_the_footer_is_added() -> None:
    def script(_system: str, prompt: str) -> str:
        subject = "Hello" if _field(prompt, "Channel") == "email" else None
        return json.dumps({"variants": [{"subject": subject, "body": "Same words."}] * 2})

    creative = Creative(ScriptedLLM(script))
    items = creative.build_library(sample_brief(NOW), brand_voice=VOICE, now=NOW, themes=[Theme.LOYALTY])
    emails = [i for i in items if i.channel is Channel.EMAIL]
    assert emails and all(i.body == f"Same words.\n\n{EMAIL_FOOTER}" for i in emails)
    per_cell = {(i.offer_id, i.theme) for i in emails}
    assert len(emails) == len(per_cell)  # one per cell; the repeat was dropped
    assert any("duplicate body" in d for d in creative.dropped)


def test_whatsapp_template_names_change_with_the_brief_version() -> None:
    brief = sample_brief(NOW)
    revised = brief.model_copy(update={"version": 2})
    names = [
        {
            i.template_name
            for i in Creative(ScriptedLLM(_creative_script)).build_library(
                b, brand_voice=VOICE, now=NOW, themes=[Theme.LOYALTY]
            )
            if i.channel is Channel.WHATSAPP
        }
        for b in (brief, revised)
    ]
    assert names[0] and names[1] and not names[0] & names[1]


def test_sms_opt_out_keyword_is_not_shouting() -> None:
    sms = _item(
        channel=Channel.SMS,
        offer_id="none",
        subject=None,
        body="Thanks, {{first_name}}. New {{top_category}} just landed. Reply STOP to opt out.",
        slots=("first_name", "top_category"),
    )
    assert check_rules(sms, RULES) == []
