"""The briefing's numbers: plain words from the Analyst, one measurement across screens, one figure per line on Live.

Three things are pinned here:

1. The Analyst's template briefing says "people we never contact" (never "holdout"), leads with the profit
   sentence and its range, and carries a "What it learned" section only when every number in it is backed.
2. `GET /v2/briefing` rebuilds the Analyst's measured sections from the proof and health served now, for that
   goal, so the briefing, Live's "Extra profit so far" and Proof's headline are one figure read at one time.
3. Live's briefing lines (`briefFigure` in live.js) show one figure each, money always with its range.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v2.deps import Services, attach_services, build_demo_services
from api.v2.routes import router
from autonomy.agents.analyst import (
    BASELINE_TITLE,
    HEALTH_TITLE,
    LEARNED_TITLE,
    MONEY_TITLE,
    Analyst,
    LearnedNote,
    template_briefing,
)
from autonomy.agents.base import ScriptedLLM
from autonomy.agents.claims import check_numbers
from autonomy.contracts import Briefing, BriefingSection, Estimate, HealthStatus, ProofSummary
from autonomy.fakes.samples import sample_goal

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
LIVE_JS: Final[Path] = ROOT / "ui" / "v2" / "screens" / "live.js"
NOW: Final[datetime] = datetime(2026, 10, 19, 9, tzinfo=UTC)
LATER: Final[datetime] = NOW + timedelta(days=1)

HEALTH = HealthStatus(as_of=NOW, delivery_rate=1.0, complaint_rate=0.0, contacts_per_customer_week=0.37)


def _proof(
    profit: Estimate | None, lift: Estimate | None = None, *, at: datetime = NOW, goal_id: str | None = None
) -> ProofSummary:
    return ProofSummary(
        goal_id=goal_id,
        as_of=at,
        agent_n=385,
        baseline_n=41,
        holdout_n=64,
        incremental_revenue=None,
        incremental_profit=profit,
        lift_vs_baseline=lift,
        spend=17.0,
    )


SNAPSHOT = Estimate(point=1846.0, low=636.0, high=3056.0)
CURRENT = Estimate(point=1906.0, low=697.0, high=3115.0)
LIFT = Estimate(point=4.26, low=1.17, high=7.34)


def _all_text(b: Briefing) -> str:
    return " ".join([b.headline, *(s.title for s in b.sections), *(s.body for s in b.sections)])


# --- 1. the Analyst's words ------------------------------------------------------------------------


def test_template_uses_plain_words_and_leads_with_profit_and_its_range() -> None:
    b = template_briefing(sample_goal(NOW), _proof(SNAPSHOT, LIFT), HEALTH, {}, "weekly", NOW)
    assert "holdout" not in _all_text(b).lower()
    assert (
        b.headline
        == "It made an extra $1,846 in profit compared with people we never contact ($636 to $3,056)."
    )
    assert [s.title for s in b.sections] == [MONEY_TITLE, BASELINE_TITLE, HEALTH_TITLE]
    assert b.sections[0].body.startswith(
        "It made an extra $1,846 in profit compared with people we never contact"
    )
    assert "likely range $636 to $3,056" in b.sections[0].body
    assert "likely range $1.17 to $7.34" in b.sections[1].body
    assert b.sections[2].body.startswith("Each customer got 0.37 messages a week")


def test_unclear_and_missing_profit_say_not_sure_yet() -> None:
    unclear = template_briefing(
        None, _proof(Estimate(point=300, low=-200, high=900)), HEALTH, {}, "daily", NOW
    )
    assert unclear.headline.startswith("Not sure yet") and "-$200 to $900" in unclear.headline
    missing = template_briefing(None, _proof(None), HEALTH, {}, "daily", NOW)
    assert missing.sections[0].body.startswith("Not sure yet")
    for b in (unclear, missing):
        assert "holdout" not in _all_text(b).lower()


def test_what_it_learned_is_a_section_only_when_its_numbers_are_backed() -> None:
    learned: list[str | LearnedNote] = [
        LearnedNote(
            "Free shipping is beating the discount; the share offered the discount fell from 31% to 18%",
            {"discount_share_before": 0.31, "discount_share_now": 0.18},
        ),
        "Evenings work better than mornings for email",
        "It sends 40% fewer messages now",  # a digit with nothing behind it: left out
    ]
    b = template_briefing(None, _proof(SNAPSHOT), HEALTH, {}, "weekly", NOW, learned=learned)
    lesson = next(s for s in b.sections if s.title == LEARNED_TITLE)
    assert "31% to 18%" in lesson.body and "Evenings work better" in lesson.body
    assert "40%" not in lesson.body
    assert check_numbers(lesson.body, lesson.numbers) == []
    none = template_briefing(
        None, _proof(SNAPSHOT), HEALTH, {}, "weekly", NOW, learned=["It sends 40% fewer"]
    )
    assert LEARNED_TITLE not in [s.title for s in none.sections]


def test_the_analyst_falls_back_to_the_same_template_and_tells_the_model_the_words() -> None:
    def down(system: str, prompt: str) -> str:
        raise RuntimeError("provider down")

    llm = ScriptedLLM(down)
    goal = sample_goal(NOW)
    result = Analyst(llm).write_briefing(goal, _proof(SNAPSHOT, LIFT), HEALTH, {}, "weekly", NOW)
    assert result.source == "template"
    assert result.briefing == template_briefing(goal, _proof(SNAPSHOT, LIFT), HEALTH, {}, "weekly", NOW)
    prompts: list[str] = []

    def echo(system: str, prompt: str) -> str:
        prompts.append(prompt)
        return "{}"

    Analyst(ScriptedLLM(echo)).briefing(goal, _proof(SNAPSHOT), HEALTH, {}, "weekly", NOW)
    assert prompts and "people we never contact" in prompts[0] and "likely range" in prompts[0]


# --- 2. one measurement across screens -------------------------------------------------------------


def _services(stored: list[Briefing], proofs: dict[str | None, ProofSummary | None]) -> Services:
    base = build_demo_services(NOW, n_customers=10, days=1)
    return dataclasses.replace(
        base,
        briefing=lambda gid: next((b for b in stored if b.goal_id == gid), None),
        proof=lambda gid: proofs.get(gid),
        health=lambda: HEALTH,
    )


def _client(services: Services) -> TestClient:
    app = FastAPI()
    attach_services(app, services)
    app.include_router(router)
    return TestClient(app)


def _figure(sections: Any, key: str) -> float:
    return float(next(s["numbers"][key] for s in sections if key in s["numbers"]))


def test_the_briefing_endpoint_serves_the_proof_measured_now() -> None:
    goal = sample_goal(NOW)
    snap = template_briefing(goal, _proof(SNAPSHOT, LIFT, goal_id=goal.goal_id), HEALTH, {}, "weekly", NOW)
    learned = BriefingSection(title=LEARNED_TITLE, body="Evenings work better for email.")
    stored = snap.model_copy(update={"sections": (*snap.sections, learned)})
    now_proof = _proof(CURRENT, LIFT, at=LATER, goal_id=goal.goal_id)
    with _client(_services([stored], {goal.goal_id: now_proof})) as c:
        body = c.get("/v2/briefing", params={"goal_id": goal.goal_id}).json()
        proof = c.get("/v2/proof", params={"goal_id": goal.goal_id}).json()
    assert _figure(body["sections"], "incremental_profit") == proof["incremental_profit"]["point"] == 1906.0
    assert _figure(body["sections"], "incremental_profit_low") == proof["incremental_profit"]["low"]
    assert "$1,906" in body["headline"] and "$1,846" not in json.dumps(body)
    assert body["as_of"].startswith("2026-10-20")  # the measurement's time, not the snapshot's
    assert body["briefing_id"] == stored.briefing_id and body["goal_id"] == goal.goal_id
    assert body["sections"][-1]["title"] == LEARNED_TITLE  # what it learned is kept as written


def test_a_goal_briefing_uses_that_goals_own_proof() -> None:
    """The Approve goal's briefing was written from the programme-wide figure; its own proof is not sure yet."""
    goal = sample_goal(NOW)
    programme_wide = template_briefing(goal, _proof(SNAPSHOT), HEALTH, {}, "weekly", NOW)
    own = _proof(None, goal_id=goal.goal_id, at=LATER)
    with _client(_services([programme_wide], {goal.goal_id: own})) as c:
        body = c.get("/v2/briefing", params={"goal_id": goal.goal_id}).json()
    assert "incremental_profit" not in json.dumps(body["sections"])
    assert body["headline"] == "Too early to call: results are still coming in."
    assert body["sections"][0]["body"].startswith("Not sure yet")


def test_a_briefing_already_current_or_not_the_analysts_is_served_as_stored() -> None:
    goal = sample_goal(NOW)
    proof = _proof(SNAPSHOT, LIFT, goal_id=goal.goal_id)
    current = template_briefing(goal, proof, HEALTH, {}, "weekly", NOW)
    worded = current.model_copy(update={"headline": "A calm week: it made an extra $1,846 ($636 to $3,056)."})
    other = Briefing(briefing_id="b_other", goal_id=None, as_of=NOW, period="daily", headline="Quiet day")
    services = _services([worded, other], {goal.goal_id: proof, None: _proof(CURRENT, at=LATER)})
    assert services.current_briefing(goal.goal_id) == worded  # the model's words survive when nothing moved
    assert services.current_briefing(None) == other
    assert _services([worded], {}).current_briefing(goal.goal_id) == worded  # no proof: nothing to measure


# --- 3. Live's figure lines --------------------------------------------------------------------------


NODE_CHECK = """
const live = await import(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map((sec) => [live.briefKind(sec), live.briefFigure(sec, "USD")])));
"""


def _sec(**numbers: float) -> dict[str, Any]:
    return {"title": "t", "body": "b", "numbers": numbers}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_live_shows_one_figure_per_line_with_its_range() -> None:
    cases = [
        _sec(
            agent_customers=389,
            holdout_customers=65,
            incremental_profit=1906.0091,
            incremental_profit_low=697.329,
            incremental_profit_high=3114.689,
        ),
        _sec(
            baseline_customers=41,
            lift_per_customer=4.2554,
            lift_per_customer_low=1.1688,
            lift_per_customer_high=7.3419,
        ),
        _sec(
            contacts_per_customer_week=0.3711,
            complaint_rate=0.0,
            delivery_rate=1.0,
            alerts=1,
            critical_alerts=0,
        ),
        _sec(agent_customers=371, holdout_customers=65, spend=0.0),
        _sec(
            agent_customers=1,
            holdout_customers=1,
            incremental_profit=300,
            incremental_profit_low=-200,
            incremental_profit_high=900,
        ),
        _sec(baseline_customers=41),
        _sec(lapsing_customers=1900),
    ]
    out = subprocess.run(
        ["node", "--input-type=module", "-e", NODE_CHECK, str(LIVE_JS), json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(out.stdout) == [
        ["money", "$1,906 ($697 to $3,115)"],
        ["baseline", "+$4.26 a customer ($1.17 to $7.34)"],
        ["health", "0.37 messages a week, 0% complaints"],
        ["money", "Not sure yet"],
        ["money", "Not sure yet (-$200 to $900)"],
        ["baseline", "Not sure yet"],
        [None, None],
    ]


def test_the_programme_briefing_is_rebuilt_and_keeps_its_other_facts_in_order() -> None:
    """goal_id None: the programme-wide briefing is measured against the programme's proof, facts kept after."""
    snap = template_briefing(None, _proof(SNAPSHOT, LIFT), HEALTH, {"contacts chosen": 12.0}, "daily", NOW)
    services = _services([snap], {None: _proof(CURRENT, LIFT, at=LATER)})
    served = services.current_briefing(None)
    assert served is not None and served.goal_id is None and served.as_of == LATER
    assert [s.title for s in served.sections] == [*(s.title for s in snap.sections)]
    assert served.sections[0].numbers["incremental_profit"] == 1906.0
    assert served.sections[-1] == snap.sections[-1]  # "What else we noticed" as written


def test_learned_money_is_shown_in_the_goals_currency() -> None:
    goal = sample_goal(NOW).model_copy(update={"currency": "EUR"})
    note = LearnedNote("Free shipping kept 120 more in margin", {"extra_margin": 120.0})
    drafts = Analyst(ScriptedLLM(lambda s, p: "{}")).sections(
        goal, _proof(SNAPSHOT), HEALTH, {}, learned=[note]
    )
    lesson = next(d for d in drafts if d.title == LEARNED_TITLE)
    assert lesson.shown["extra_margin"] == "€120"
