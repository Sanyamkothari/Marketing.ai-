"""The editor gate is enforced by the server: `POST /v2/content/{id}/status` refuses to approve a message
the Critic failed, held or never checked (docs/v2/PITCH.md "Nothing goes out without passing",
PROMISE_MAP row 23, ARCHITECTURE_REVIEW section 8 "Fix first" 1).

The items here are reviewed by the real `Critic` (a scripted judge), so the notes are the ones production
writes, not hand-made strings.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from api.v2.deps import ConflictError, Services, build_demo_services, editor_passed
from api.v2.insights import review_content
from autonomy.agents.base import ScriptedLLM
from autonomy.agents.critic import RUBRIC, BrandRules, Critic
from autonomy.contracts import Channel, ContentItem, ContentStatus, Theme
from autonomy.fakes.samples import sample_brief
from engine.llm import LLMError

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
RULES = BrandRules.from_brief(sample_brief(NOW), brand_voice="Warm and brief.", allowed_domains=())
GOOD_BODY = "Come back to {{top_category}} with {{offer_label}} this week. Unsubscribe any time."


def _item(content_id: str, **overrides: object) -> ContentItem:
    base: dict[str, object] = {
        "content_id": content_id,
        "goal_id": "goal_winback",
        "channel": Channel.EMAIL,
        "theme": Theme.SAVINGS,
        "offer_id": "pct10",
        "subject": "Something for you, {{first_name}}",
        "body": GOOD_BODY,
        "slots": ("first_name", "top_category", "offer_label"),
        "created_at": NOW,
    }
    base.update(overrides)
    return ContentItem.model_validate(base)


def _judge(score: int) -> ScriptedLLM:
    verdict = json.dumps({"scores": [{"criterion": c, "score": score, "reason": "ok"} for c in RUBRIC]})
    return ScriptedLLM(lambda _s, _p: verdict, model_id="other-family")


def _down(_system: str, _prompt: str) -> str:
    raise LLMError("unavailable", "the judge is down")


def _reviewed() -> dict[str, ContentItem]:
    """One message per way the editor can answer, each written by the real Critic."""
    passed = Critic(_judge(5)).review([_item("ci_passed")], RULES)[0]
    broke_a_rule = Critic(_judge(5)).review(
        [_item("ci_rule", body="Results guaranteed. Unsubscribe any time.")], RULES
    )[0]
    judged_bad = Critic(_judge(1)).review([_item("ci_judged")], RULES)[0]
    held = Critic(ScriptedLLM(_down)).review([_item("ci_held")], RULES)[0]
    rules_alone = Critic(ScriptedLLM(_down)).review(
        [_item("ci_optional")], RULES.model_copy(update={"judge_optional": True})
    )[0]
    unchecked = _item("ci_unchecked")
    return {i.content_id: i for i in (passed, broke_a_rule, judged_bad, held, rules_alone, unchecked)}


def test_the_critic_wrote_the_verdicts_this_file_relies_on() -> None:
    items = _reviewed()
    assert items["ci_passed"].status is ContentStatus.APPROVED
    assert items["ci_rule"].status is ContentStatus.REJECTED
    assert items["ci_judged"].status is ContentStatus.REJECTED
    assert items["ci_held"].status is ContentStatus.DRAFT
    assert items["ci_optional"].status is ContentStatus.APPROVED
    assert items["ci_unchecked"].critic_notes == ()


@pytest.mark.parametrize(
    ("content_id", "passed"),
    [
        ("ci_passed", True),
        ("ci_optional", True),
        ("ci_rule", False),
        ("ci_judged", False),
        ("ci_held", False),
        ("ci_unchecked", False),
    ],
)
def test_editor_passed_reads_the_critics_last_word(content_id: str, passed: bool) -> None:
    assert editor_passed(_reviewed()[content_id]) is passed


def test_the_screen_never_calls_a_held_message_passed() -> None:
    """A judge outage between two runs leaves a half-scored DRAFT; the screen must not offer Approve on it."""
    half = _item("ci_half").model_copy(
        update={
            "critic_notes": (
                "judge run 1 [honesty, brand_voice]: honesty 5 (ok); brand_voice 5 (ok)",
                "judge unavailable (run 2: unavailable)",
                "held as a draft until the judge is back",
            )
        }
    )
    assert editor_passed(half) is False
    assert review_content(half).passed is not True


@pytest.fixture
def services() -> Services:
    built = build_demo_services(NOW, n_customers=30, days=3)
    goal = built.workspace.goals()[0]
    built.workspace.put_content(
        [i.model_copy(update={"goal_id": goal.goal_id}) for i in _reviewed().values()]
    )
    return built


@pytest.fixture
def client(services: Services) -> Iterator[Any]:
    pytest.importorskip("fastapi", reason="FastAPI is not installed here; the v2 CI job runs these")
    pytest.importorskip("httpx", reason="the FastAPI test client needs httpx")
    from fastapi.testclient import TestClient

    from api.v2.app import create_app

    with TestClient(create_app(services)) as c:
        yield c


def _status(services: Services, content_id: str) -> ContentStatus:
    item = next(c for c in services.workspace.content(approved_only=False) if c.content_id == content_id)
    return item.status


@pytest.mark.parametrize("content_id", ["ci_rule", "ci_judged", "ci_held", "ci_unchecked"])
def test_the_api_refuses_to_approve_what_the_editor_did_not_pass(
    client: Any, services: Services, content_id: str
) -> None:
    before = _status(services, content_id)
    r = client.post(f"/v2/content/{content_id}/status", json={"status": "approved"})
    assert r.status_code == 409 and "editor" in r.text
    assert _status(services, content_id) is before  # nothing was changed


def test_the_service_refuses_the_same_way(services: Services) -> None:
    with pytest.raises(ConflictError, match="editor"):
        services.set_content_status("ci_rule", ContentStatus.APPROVED)


def test_a_failed_message_can_still_be_rejected_or_retired_by_a_person(client: Any) -> None:
    assert client.post("/v2/content/ci_rule/status", json={"status": "rejected"}).status_code == 200
    assert client.post("/v2/content/ci_held/status", json={"status": "retired"}).status_code == 200


def test_a_message_the_editor_passed_can_be_approved_and_a_person_can_turn_it_down_and_back(
    client: Any, services: Services
) -> None:
    for content_id in ("ci_passed", "ci_optional"):
        r = client.post(f"/v2/content/{content_id}/status", json={"status": "approved"})
        assert r.status_code == 200
        assert ContentItem.model_validate(r.json()).status is ContentStatus.APPROVED
    assert client.post("/v2/content/ci_passed/status", json={"status": "rejected"}).status_code == 200
    # The editor's pass is still on the record, so the person may change their mind.
    assert client.post("/v2/content/ci_passed/status", json={"status": "approved"}).status_code == 200
    assert _status(services, "ci_passed") is ContentStatus.APPROVED
