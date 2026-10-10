"""The policy engine (design §2; invariants I3, I4): one test per rule and per refusal code, and default deny.

The rules are pure functions of `(ProposalSubject, PolicyContext)`, so every test builds the context by hand
from the facts a rule reads; `tests/agentic/scenarios/` builds the same facts from a real run.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest

from engine.access.roles import LOCAL_OPERATOR, Principal, Role
from engine.agentic.codes import PLAN_K_CODES
from engine.agentic.config import KNOWN_RULES, load_policy, policy_version
from engine.agentic.errors import AgenticError
from engine.agentic.policy import (
    RULES,
    ContactabilityFacts,
    CostFacts,
    Decision,
    HoldoutFacts,
    PolicyContext,
    ProposalSubject,
    RealisedFacts,
    allowed,
    evaluate,
)

PLAN_HASH = "b" * 64
ASHA = Principal(user_id="u_asha", username="asha", roles=frozenset({Role.ANALYST, Role.APPROVER}))
RAVI = Principal(user_id="u_ravi", username="ravi", roles=frozenset({Role.APPROVER}))


@pytest.fixture(scope="module")
def policy(config_root: Path) -> Any:
    return load_policy(config_root)


def payload(**change: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "use_case_id": "win-back-campaign",
        "run_id": "r_1",
        "campaign_id": "c_1",
        "channel": "email",
        "holdout": {"fraction": 0.10, "scope": "run", "treat_members": False},
        "test_plan_hash": PLAN_HASH,
        "contacts_per_customer": 1,
        "planned_contacts": 300,
        "offer_spend_inr": 1_000.0,
    }
    base.update(change)
    return base


def context(policy: Any, **change: Any) -> PolicyContext:
    facts: dict[str, Any] = {
        "thresholds": policy,
        "holdout": HoldoutFacts(scope="run", fraction=0.10),
        "cost": CostFacts(
            estimated_usd=None, known_usd=None, cap_usd=None, over_cap=None, needs_confirmation=False
        ),
        "offer_budget_inr": 5_000.0,
        "contact_budget": None,
        "contactability": ContactabilityFacts(
            channels=("sms", "email"),
            eligible_rows=1_080,
            channel_counts={"sms": 268, "email": 0},
            reasons={"sms": {"not_contactable": 268}, "email": {"not_contactable": 0}},
        ),
        "contact_cap_per_customer": 1,
        "channel_caps": {"email": 500, "sms": 400},
        "registered_plan_hash": PLAN_HASH,
    }
    facts.update(change)
    return PolicyContext(**facts)


def subject(kind: str = "campaign_plan", by: str = "u_asha", **change: Any) -> ProposalSubject:
    return ProposalSubject(kind=kind, payload=payload(**change), proposed_by=by)


def refusals(decisions: tuple[Decision, ...]) -> set[str]:
    return {d.code for d in decisions if not d.allowed and d.code is not None}


def test_a_campaign_plan_every_rule_allows_is_allowed(policy: Any) -> None:
    decisions = evaluate(subject(), context(policy))
    assert allowed(decisions)
    assert {d.rule for d in decisions} == set(KNOWN_RULES) - {"role.separation"}  # no approver yet


CASES: list[tuple[str, dict[str, Any], dict[str, Any], str]] = [
    # holdout.immutable (I4)
    ("holdout shrunk", {"holdout": {"fraction": 0.02, "scope": "run"}}, {}, "POLICY_HOLDOUT_IMMUTABLE"),
    ("holdout omitted", {"holdout": None}, {}, "POLICY_HOLDOUT_IMMUTABLE"),
    ("holdout zero", {"holdout": {"fraction": 0.0, "scope": "run"}}, {}, "POLICY_HOLDOUT_IMMUTABLE"),
    (
        "holdout treated",
        {"holdout": {"fraction": 0.1, "treat_members": True}},
        {},
        "POLICY_HOLDOUT_IMMUTABLE",
    ),
    (
        "persistent replaced by a run draw",
        {},
        {"holdout": HoldoutFacts(scope="use_case", fraction=0.10)},
        "POLICY_HOLDOUT_IMMUTABLE",
    ),
    (
        "one persistent scope for another",
        {"holdout": {"fraction": 0.10, "scope": "universal"}},
        {"holdout": HoldoutFacts(scope="use_case", fraction=0.10)},
        "POLICY_HOLDOUT_IMMUTABLE",
    ),
    (
        "a persistent scope on a per-run holdout",
        {"holdout": {"fraction": 0.10, "scope": "use_case"}},
        {},
        "POLICY_HOLDOUT_IMMUTABLE",
    ),
    (
        "below the epoch's share",
        {"holdout": {"fraction": 0.10, "scope": "universal"}},
        {"holdout": HoldoutFacts(scope="universal", fraction=0.10, epoch=2, epoch_fraction=0.15)},
        "POLICY_HOLDOUT_IMMUTABLE",
    ),
    (
        "use case unreadable",
        {"use_case_id": "no-such-use-case"},
        {"holdout": None},
        "POLICY_HOLDOUT_IMMUTABLE",
    ),
    # budget.cap
    (
        "over the cost cap",
        {},
        {
            "cost": CostFacts(
                estimated_usd=2.0, known_usd=2.0, cap_usd=1.0, over_cap=True, needs_confirmation=True
            )
        },
        "POLICY_BUDGET_CAP",
    ),
    (
        "unpriced while capped",
        {},
        {
            "cost": CostFacts(
                estimated_usd=None, known_usd=None, cap_usd=1.0, over_cap=None, needs_confirmation=True
            )
        },
        "POLICY_BUDGET_CAP",
    ),
    ("over the offer budget", {"offer_spend_inr": 9_000.0}, {}, "POLICY_BUDGET_CAP"),
    ("over the contact budget", {"planned_contacts": 300}, {"contact_budget": 200}, "POLICY_BUDGET_CAP"),
    # consent.channel
    (
        "more contacts than may be contacted",
        {"channel": "sms", "planned_contacts": 813},  # 1,080 eligible, 268 not contactable on SMS: 812 may be
        {"channel_caps": {}},
        "POLICY_CONSENT_CHANNEL",
    ),
    (
        "a configured column the file lacked",
        {},
        {
            "contactability": ContactabilityFacts(
                channels=("sms", "email"),
                eligible_rows=1_080,
                channel_counts={"sms": 268, "email": 0},
                columns_missing={"email": ("email_valid",)},
            )
        },
        "POLICY_CONSENT_CHANNEL",
    ),
    ("channel not recorded", {"channel": "push"}, {}, "POLICY_CONSENT_CHANNEL"),
    ("run recorded nothing", {}, {"contactability": None}, "POLICY_CONSENT_CHANNEL"),
    # frequency.cap
    ("two contacts each", {"contacts_per_customer": 2}, {}, "POLICY_FREQUENCY_CAP"),
    ("over the channel cap", {"planned_contacts": 501}, {}, "POLICY_FREQUENCY_CAP"),
    # test.registered
    ("no frozen plan", {}, {"registered_plan_hash": None}, "POLICY_TEST_NOT_REGISTERED"),
    ("no campaign named", {"campaign_id": None}, {}, "POLICY_TEST_NOT_REGISTERED"),
    ("another plan cited", {"test_plan_hash": "c" * 64}, {}, "POLICY_TEST_NOT_REGISTERED"),
]


@pytest.mark.parametrize(("label", "change", "facts", "code"), CASES, ids=[case[0] for case in CASES])
def test_each_rule_refuses_with_its_code(
    policy: Any, label: str, change: dict[str, Any], facts: dict[str, Any], code: str
) -> None:
    decisions = evaluate(subject(**change), context(policy, **facts))
    assert not allowed(decisions), label
    assert refusals(decisions) == {code}
    refused = next(d for d in decisions if not d.allowed)
    assert refused.detail.get("reason"), "every refusal says why in words"
    assert code in PLAN_K_CODES


def test_a_refusal_lists_every_rule_it_breaks(policy: Any) -> None:
    decisions = evaluate(
        subject(channel="push", holdout=None, contacts_per_customer=3),
        context(policy, registered_plan_hash=None),
    )
    assert refusals(decisions) == {
        "POLICY_HOLDOUT_IMMUTABLE",
        "POLICY_CONSENT_CHANNEL",
        "POLICY_FREQUENCY_CAP",
        "POLICY_TEST_NOT_REGISTERED",
    }


def test_consent_refusals_carry_counts_and_reason_codes_never_a_customer(policy: Any) -> None:
    decisions = evaluate(subject(channel="sms", planned_contacts=900), context(policy, channel_caps={}))
    refused = next(d for d in decisions if not d.allowed)
    assert refused.detail["planned_contacts"] == 900 and refused.detail["contactable_rows"] == 812
    assert refused.detail["not_contactable_rows"] == 268
    assert refused.detail["reasons"] == {"not_contactable": 268}
    assert all(isinstance(value, int) for value in refused.detail["reasons"].values()), "rows only"


def test_opt_outs_off_the_list_do_not_refuse_a_plan_within_the_contactable_rows(policy: Any) -> None:
    """A quarter of the run opted out of SMS; they are not on the SMS list (M99), so a plan for 300 is fine."""
    decisions = evaluate(subject(channel="sms", planned_contacts=300), context(policy))
    assert allowed(decisions)
    consent = next(d for d in decisions if d.rule == "consent.channel")
    assert consent.detail["contactable_rows"] == 812 and consent.detail["not_contactable_rows"] == 268
    assert allowed(evaluate(subject(channel="sms", planned_contacts=None), context(policy)))
    assert allowed(evaluate(subject(channel="sms", planned_contacts=400), context(policy)))  # the SMS cap


def test_a_missing_column_is_named_not_counted(policy: Any) -> None:
    facts = ContactabilityFacts(
        channels=("email",),
        eligible_rows=10,
        channel_counts={"email": 0},
        reasons={"email": {"not_contactable": 0}},
        columns_missing={"email": ("email_valid",)},
    )
    refused = next(
        d for d in evaluate(subject(), context(policy, contactability=facts)) if d.rule == "consent.channel"
    )
    assert refused.detail["columns_missing"] == ["email_valid"]
    assert "column_missing" not in refused.detail and "reasons" not in refused.detail


def test_role_separation_refuses_the_proposer_and_no_one_else(policy: Any) -> None:
    own = evaluate(subject(), context(policy, approver=ASHA))
    assert refusals(own) == {"POLICY_ROLE_SEPARATION"}
    assert allowed(evaluate(subject(), context(policy, approver=RAVI)))
    # With sign-in off there is one operator holding every role: the rule cannot be enforced (DEC-862).
    assert allowed(evaluate(subject(by=LOCAL_OPERATOR.user_id), context(policy, approver=LOCAL_OPERATOR)))


def test_a_kind_with_no_rule_set_is_refused_by_default(policy: Any) -> None:
    for kind in ("copy_arms", "send_sms_now", "campaign"):
        decisions = evaluate(ProposalSubject(kind=kind, payload={}, proposed_by="u"), context(policy))
        assert refusals(decisions) == {"POLICY_NO_RULES"}
        assert not allowed(decisions)


def test_a_payload_not_shaped_as_its_kind_is_refused(policy: Any) -> None:
    decisions = evaluate(
        ProposalSubject(kind="campaign_plan", payload={**payload(), "skip_holdout": True}, proposed_by="u"),
        context(policy),
    )
    assert refusals(decisions) == {"POLICY_PAYLOAD_INVALID"}


def test_disabling_every_rule_never_allows_anything(policy: Any) -> None:
    """The mutation harness removes rules; with none left a kind is still refused (default deny)."""
    assert refusals(evaluate(subject(), context(policy), rules={})) == {"POLICY_NO_RULES"}


def test_a_test_plan_cannot_change_or_skip_the_holdout(policy: Any) -> None:
    realised = RealisedFacts(
        population_rows=1_000, n_treat=900, n_holdout=100, n_explore=0, holdout_fraction=0.1
    )
    plan = {"metric": "m", "outcome_column": "y", "analysis_date": "2030-01-01"}

    def test_plan(**change: Any) -> ProposalSubject:
        body = {"campaign_id": "c_1", "plan": plan, "realised": realised.model_dump(), "plan_hash": PLAN_HASH}
        return ProposalSubject(kind="test_plan", payload={**body, **change}, proposed_by="u_asha")

    assert allowed(evaluate(test_plan(), context(policy, realised=realised)))
    assert allowed(evaluate(test_plan(requested_holdout_fraction=0.102), context(policy, realised=realised)))
    shrink = evaluate(test_plan(requested_holdout_fraction=0.01), context(policy, realised=realised))
    assert refusals(shrink) == {"POLICY_HOLDOUT_IMMUTABLE"}
    nobody = realised.model_copy(update={"n_holdout": 0, "holdout_fraction": 0.0})
    assert refusals(evaluate(test_plan(), context(policy, realised=nobody))) == {"POLICY_HOLDOUT_IMMUTABLE"}


def test_a_test_plan_nobody_could_register_is_refused_at_draft(policy: Any) -> None:
    """Another plan frozen, or a campaign already measured: refused when drafted, not left for an Approver."""
    realised = RealisedFacts(
        population_rows=1_000, n_treat=900, n_holdout=100, n_explore=0, holdout_fraction=0.1
    )
    body = {
        "campaign_id": "c_1",
        "plan": {"metric": "m", "outcome_column": "y", "analysis_date": "2030-01-01"},
        "realised": realised.model_dump(),
        "plan_hash": PLAN_HASH,
    }
    proposal = ProposalSubject(kind="test_plan", payload=body, proposed_by="u_asha")

    def facts(**change: Any) -> PolicyContext:
        given: dict[str, Any] = {
            "realised": realised,
            "dry_plan_hash": PLAN_HASH,
            "registered_plan_hash": None,
        }
        return context(policy, **{**given, **change})

    assert allowed(evaluate(proposal, facts()))
    assert allowed(evaluate(proposal, facts(registered_plan_hash=PLAN_HASH))), "the same plan again"
    other = evaluate(proposal, facts(registered_plan_hash="c" * 64))
    assert refusals(other) == {"POLICY_TEST_PLAN_FIXED"}
    assert next(d for d in other if not d.allowed).detail["registered"] == "c" * 64
    measured = evaluate(proposal, facts(campaign_measured=True))
    assert refusals(measured) == {"POLICY_TEST_PLAN_FIXED"}
    assert "POLICY_TEST_PLAN_FIXED" in PLAN_K_CODES


def test_the_policy_version_is_the_sha256_of_the_file(config_root: Path, tmp_path: Path) -> None:
    path = config_root / "agentic" / "policy.yaml"
    assert policy_version(config_root) == hashlib.sha256(path.read_bytes()).hexdigest()
    copy = tmp_path / "configs"
    shutil.copytree(config_root / "agentic", copy / "agentic")
    (copy / "agentic" / "policy.yaml").write_text(
        path.read_text(encoding="utf-8").replace("tolerance: 0.005", "tolerance: 0.004"), encoding="utf-8"
    )
    assert policy_version(copy) != policy_version(config_root)
    assert load_policy(copy).holdout.tolerance == 0.004


def test_the_policy_file_is_checked(tmp_path: Path) -> None:
    (tmp_path / "agentic").mkdir()
    with pytest.raises(AgenticError):
        load_policy(tmp_path)  # missing: there is no default policy to fall back on
    (tmp_path / "agentic" / "policy.yaml").write_text(
        "rule_sets:\n  test_plan: [holdout.skip]\n", encoding="utf-8"
    )
    with pytest.raises(AgenticError):
        load_policy(tmp_path)


def test_the_shipped_rule_sets_name_every_rule(policy: Any) -> None:
    assert set(RULES) == set(KNOWN_RULES)
    assert set(policy.rule_sets["campaign_plan"]) == set(KNOWN_RULES)
    assert set(policy.rule_sets["test_plan"]) == {"holdout.immutable", "test.registered", "role.separation"}
    assert "copy_arms" not in policy.rule_sets
