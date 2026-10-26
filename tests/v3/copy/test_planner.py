"""Drafting through the Creative and the Critic, the cost guard, and the opt-in switch."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from autonomy.agents.base import ScriptedLLM
from autonomy.agents.creative import CHANNEL_BODY_MAX, EMAIL_FOOTER, Creative
from autonomy.agents.critic import BrandRules, Critic
from autonomy.content.config import CopyConfig, CostGuard
from autonomy.content.planner import CopyPlanner, WeeklyPlan, _cell_plan, select_top
from autonomy.content.scoring import PastSend, ScoringModel
from autonomy.contracts import Channel, ContentStatus, CustomerState, Theme
from autonomy.fakes.samples import FREE_SHIP, TEN_OFF, sample_brief

from .synth import NOW, VOICE, creative_script, judge, make_item, make_states

BRIEF = sample_brief(NOW)
RULES = BrandRules.from_brief(BRIEF, brand_voice=VOICE, allowed_domains=("shop.example.com",))
CFG = CopyConfig(enabled=True, min_types=3, max_types=4, drafts_per_type=6, drafts_per_call=2)


def _planner(cfg: CopyConfig, bad_theme: str | None = None) -> tuple[CopyPlanner, ScriptedLLM, ScriptedLLM]:
    writer, referee = ScriptedLLM(creative_script(bad_theme)), judge(4)
    return CopyPlanner(cfg, Creative(writer), Critic(referee)), writer, referee


def _run(planner: CopyPlanner, n: int = 60, **kw):  # type: ignore[no-untyped-def]
    args = {"rules": RULES, "brand_voice": VOICE, "now": NOW, **kw}
    return planner.run(BRIEF, make_states(n), **args)


def test_the_creative_writes_per_type_with_the_type_in_the_prompt_and_unique_ids() -> None:
    llm = ScriptedLLM(creative_script())
    creative = Creative(llm)
    args = {
        "channel": Channel.EMAIL,
        "offer": TEN_OFF,
        "theme": Theme.SAVINGS,
        "n": 3,
        "brand_voice": VOICE,
        "now": NOW,
    }
    a = creative.write_for_type(BRIEF, type_id="ct001", type_note="lapsed, low spend", **args)  # type: ignore[arg-type]
    b = creative.write_for_type(BRIEF, type_id="ct002", type_note="recent buyer, high spend", **args)  # type: ignore[arg-type]
    assert len(a) == len(b) == 3
    assert "Customer type: lapsed, low spend" in llm.calls[0][1]
    assert "Customer type: recent buyer, high spend" in llm.calls[1][1]
    assert not {i.content_id for i in a} & {i.content_id for i in b}
    for item in a:
        assert item.status is ContentStatus.DRAFT and item.theme is Theme.SAVINGS and item.offer_id == "pct10"
        assert item.body.endswith(EMAIL_FOOTER) and "offer_label" in item.slots
    wa = creative.write_for_type(
        BRIEF, type_id="ct001", type_note="x", channel=Channel.WHATSAPP, offer=FREE_SHIP,
        theme=Theme.CONVENIENCE, n=2, brand_voice=VOICE, now=NOW,
    )  # fmt: skip
    assert all(i.template_name and "_ct001_" in i.template_name for i in wa)
    assert len({i.template_name for i in wa}) == 2
    assert all(len(i.body) <= CHANNEL_BODY_MAX[Channel.WHATSAPP] for i in wa)
    with pytest.raises(ValueError):
        creative.write_for_type(BRIEF, type_id="t", type_note="x", **{**args, "n": 0})  # type: ignore[arg-type]


def test_the_default_library_is_unchanged_by_the_hooks() -> None:
    # `build_library` never passes a type, so ids and prompts are exactly what they were.
    llm = ScriptedLLM(creative_script())
    items = Creative(llm).build_library(BRIEF, brand_voice=VOICE, n_per_cell=2, now=NOW)
    assert items and all("Customer type" not in p for _, p in llm.calls)
    # Pinned: printed by the same call against autonomy/agents/creative.py at base commit 672602ea.
    assert len(items) == 28
    assert [i.content_id for i in items[:3]] == [
        "ci_05d4f7556e47fe6ca561",
        "ci_3935a318e29081508391",
        "ci_cff1a78e7f8fc2975b93",
    ]
    wa = [(i.content_id, i.template_name) for i in items if i.template_name][:2]
    assert wa == [
        ("ci_b8a98ec38b2162c06b07", "goal_winback_v1_whatsapp_none_reminder_0"),
        ("ci_fcafd18458d3cc8cacf5", "goal_winback_v1_whatsapp_none_reminder_1"),
    ]


def test_drafts_per_type_are_within_five_to_ten_and_all_pass_the_critic_to_go_live() -> None:
    planner, writer, referee = _planner(CFG)
    plan = _run(planner)
    assert plan.enabled and 1 <= len(plan.plans) <= CFG.max_types
    assert len(plan.types) == len(plan.plans) == 3
    for tp in plan.plans:
        assert 5 <= len(tp.written) <= 10
        assert len(tp.live) == CFG.top_k
        assert {i.status for i in tp.live} == {ContentStatus.APPROVED}  # the Critic's own verdict, unchanged
        assert all(i.content_id in tp.scores for i in tp.live)
        assert len({i.theme for i in tp.live}) > 1  # no data yet: variety, not three of one theme
    assert len(referee.calls) == 2 * sum(len(tp.written) for tp in plan.plans)  # judged twice, alone
    assert plan.writes_used == sum(len(tp.written) for tp in plan.plans) == 6 * 3
    assert plan.llm_calls == len(writer.calls) == 3 * 3
    assert not plan.scoring_used and plan.scoring is not None  # no past sends: theme labels
    assert plan.board is not None and set(plan.board.tests) == {tp.type_id for tp in plan.plans}
    assert "3 of 3 customer types written" in plan.summary()


def test_a_draft_that_breaks_the_rules_never_goes_live() -> None:
    planner, _, _ = _planner(CFG, bad_theme="urgency")
    plan = _run(planner)
    assert plan.rejected > 0
    for tp in plan.plans:
        assert all("guaranteed" not in i.body for i in tp.live)
        assert all(i.status is ContentStatus.APPROVED for i in tp.approved)
    assert sum(i.status is ContentStatus.REJECTED for tp in plan.plans for i in tp.written) == plan.rejected


def test_the_cost_guard_caps_writes_and_skips_the_smaller_types() -> None:
    cfg = CFG.model_copy(update={"max_writes_per_week": 12})  # room for two types of six
    planner, writer, _ = _planner(cfg)
    plan = _run(planner)
    assert plan.writes_used == 12 <= plan.write_cap
    assert len(plan.plans) == 2 and len(plan.skipped) == 1 and "cost guard" in plan.skipped[0]
    assert len(writer.calls) == plan.llm_calls == 6
    # the types written first are the biggest
    sizes = {t.type_id: t.size for t in plan.types}
    written = {tp.type_id for tp in plan.plans}
    skipped_id = plan.skipped[0].split(":")[0]
    assert sizes[skipped_id] <= min(sizes[t] for t in written)


def test_the_default_cap_is_types_times_drafts_and_a_zero_cap_writes_nothing() -> None:
    assert CopyConfig().write_cap == 300 * 8
    assert CopyConfig(max_types=100, drafts_per_type=10).write_cap == 1000
    planner, writer, _ = _planner(CFG.model_copy(update={"max_writes_per_week": 0}))
    plan = _run(planner)
    assert plan.plans == [] and writer.calls == [] and len(plan.skipped) == 3
    guard = CostGuard(5)
    guard.spend(3)
    assert guard.left == 2 and not guard.allow(3)
    with pytest.raises(RuntimeError):
        guard.spend(3)


def test_off_by_default_does_nothing_at_all() -> None:
    assert CopyConfig().enabled is False
    planner, writer, referee = _planner(CopyConfig())
    plan = _run(planner)
    assert not plan.enabled and plan.plans == [] and plan.types == () and plan.board is None
    assert writer.calls == [] and referee.calls == []
    assert plan.summary().startswith("Per-type copy is off")


def test_a_model_outage_falls_back_to_template_copy_and_the_week_still_completes() -> None:
    def down(_s: str, _p: str) -> str:
        raise RuntimeError("provider down")

    creative = Creative(ScriptedLLM(down))
    planner = CopyPlanner(CFG, creative, Critic(judge(4)))
    plan = _run(planner)
    assert plan.notes and any("could not be reached" in n for n in plan.notes)
    assert plan.plans  # fixed template copy still gave each type something that passed the Critic
    assert plan.llm_calls == 0  # template copy is not a model call


def test_the_same_week_run_twice_gives_the_same_plan() -> None:
    a = _run(_planner(CFG)[0])
    b = _run(_planner(CFG)[0])
    assert [(t.type_id, [i.content_id for i in t.live]) for t in a.plans] == [
        (t.type_id, [i.content_id for i in t.live]) for t in b.plans
    ]


def test_cells_are_spread_and_never_asked_twice() -> None:
    cells = [(o, t) for o in (FREE_SHIP, TEN_OFF) for t in (Theme.SAVINGS, Theme.URGENCY)]
    plan = _cell_plan(cells, drafts=7, per_call=2, offset=5)
    assert sum(n for *_, n in plan) == 7 and len(plan) == 4
    assert len({(o.offer_id, t) for o, t, _ in plan}) == len(plan)
    assert all(1 <= n <= 2 for *_, n in plan)
    few = _cell_plan(cells[:2], drafts=10, per_call=2, offset=0)  # fewer cells than calls
    assert sum(n for *_, n in few) == 10 and len(few) == 2


def test_select_top_prefers_a_new_theme_among_equal_scores() -> None:
    items = [
        make_item("a1", theme=Theme.SAVINGS),
        make_item("a2", theme=Theme.SAVINGS),
        make_item("b1", theme=Theme.URGENCY),
        make_item("c1", theme=Theme.REMINDER),
    ]
    equal = [(i, 0.05) for i in items]
    assert [i.content_id for i in select_top(equal, 3)] == ["a1", "b1", "c1"]
    clear = [(items[0], 0.09), (items[1], 0.08), (items[2], 0.01), (items[3], 0.02)]
    assert [i.content_id for i in select_top(clear, 3)] == ["a1", "a2", "c1"]  # real differences win


def _ids(plan: WeeklyPlan) -> tuple[set[str], set[str]]:
    items = [i for tp in plan.plans for i in tp.written]
    return {i.content_id for i in items}, {i.template_name for i in items if i.template_name}


def test_ids_and_template_names_are_never_shared_between_weeks_or_between_different_types() -> None:
    # ct000 is the largest type, refitted every week: next week it is usually a different group.
    states_b = [s for s in make_states(90, seed=9) if int(s.customer_id[1:]) % 3 != 0]
    week1 = _run(_planner(CFG)[0], channel=Channel.WHATSAPP)
    other_types = _planner(CFG)[0].run(
        BRIEF, states_b, rules=RULES, brand_voice=VOICE, now=NOW, channel=Channel.WHATSAPP
    )
    next_week = _run(_planner(CFG)[0], channel=Channel.WHATSAPP, now=NOW + timedelta(days=7))
    assert week1.plans and other_types.plans and next_week.plans
    assert week1.plans[0].type_id == other_types.plans[0].type_id == "ct000"
    assert week1.plans[0].label != other_types.plans[0].label
    ids1, names1 = _ids(week1)
    assert names1 and len(names1) == len(ids1)
    for other in (other_types, next_week):
        ids, names = _ids(other)
        assert not ids & ids1 and not names & names1


def test_a_goal_with_no_offers_is_skipped_with_a_note_not_a_crash() -> None:
    brief = BRIEF.model_copy(update={"envelope": BRIEF.envelope.model_copy(update={"offers": ()})})
    planner, writer, _ = _planner(CFG)
    plan = planner.run(brief, make_states(60), rules=RULES, brand_voice=VOICE, now=NOW)
    assert plan.plans == [] and writer.calls == []
    assert any("nothing written" in n for n in plan.notes)


def test_a_channel_outside_the_envelope_is_refused() -> None:
    with pytest.raises(ValueError, match="not in the envelope"):
        _run(_planner(CFG)[0], channel=Channel.SMS)


def _planted_past_sends(states: list[CustomerState]) -> list[PastSend]:
    """Sixty past drafts of different lengths. Recent buyers (kind 0) buy short copy, lapsed one-time
    buyers (kind 1) buy long copy, the middle kind does not care. Each draft went to every customer twice."""
    rng = np.random.default_rng(4)
    rows: list[PastSend] = []
    for j in range(60):
        words = 5 + (j * 7) % 40
        item = make_item(f"past{j}", words=words)
        for s in states:
            kind = int(s.customer_id[1:]) % 3
            rate = (0.05 + 0.45 * (45 - words) / 40, 0.05 + 0.45 * (words - 5) / 40, 0.2)[kind]
            rows.extend(PastSend(item, s, NOW, bool(b)) for b in rng.random(2) < rate)
    return rows


def test_with_enough_history_the_scoring_model_is_used_and_picks_the_live_drafts() -> None:
    states = make_states(60)
    model = ScoringModel(CFG)
    writer, referee = ScriptedLLM(creative_script()), judge(4)
    planner = CopyPlanner(CFG, Creative(writer), Critic(referee), scoring=model)
    plan = planner.run(
        BRIEF, states, rules=RULES, brand_voice=VOICE, now=NOW, past_sends=_planted_past_sends(states)
    )
    assert plan.scoring is not None and plan.scoring.used, plan.scoring and plan.scoring.sentence()
    assert plan.scoring_used and model.usable
    assert plan.plans
    for tp in plan.plans:
        ranked = model.rank(tp.approved, tp.type_id)  # the fitted logistic model, since it is usable
        assert tp.scores == {i.content_id: s for i, s in ranked}
        assert [i.content_id for i in tp.live] == [i.content_id for i in select_top(ranked, CFG.top_k)]
        live_scores = [tp.scores[i.content_id] for i in tp.live]
        rest = [s for cid, s in tp.scores.items() if cid not in {i.content_id for i in tp.live}]
        assert min(live_scores) >= max(rest, default=0.0) - 1e-12  # the model's best drafts go live
    # The model scores the same draft wording differently for different types; theme labels with no
    # history for these themes could not.
    first = {tp.type_id: sorted(tp.scores.values()) for tp in plan.plans}
    assert len({tuple(round(v, 9) for v in s) for s in first.values()}) > 1
