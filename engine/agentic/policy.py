"""The policy engine: default-deny rules over a proposal (Plan K M132, design §2; invariants I3, I4).

**Rules are pure functions** ``(ProposalSubject, PolicyContext) -> Decision | None``: they read only the
context, which :func:`build_context` fills from the engine's own objects - the use case's holdout and the
holdout ledger's epoch (M92), the M108 cost estimate and cap, the scoring run's per-channel contactability
(M99, with M131's reasons when present), the arbitration caps (M101), the campaign's registered test plan
(M94) - and the principals. No rule does I/O, so the whole policy is unit-testable and fast, and nothing an
agent writes in a prompt can reach it: the context is computed by the server from stored artefacts, never
taken from the payload when the server can compute it.

**Default deny.** A proposal kind with no rule set in `configs/agentic/policy.yaml` is refused
(`POLICY_NO_RULES`); a payload that does not match its kind's shape is refused (`POLICY_PAYLOAD_INVALID`);
otherwise it is allowed only when every applicable rule allows it. Every rule of the set is evaluated, so a
refusal lists every rule it breaks.

**The rules at launch (design §2.2).**

| Rule | Refuses (code) |
|---|---|
| `holdout.immutable` | a holdout share below the one in force (configured, or the ledger's epoch), a scope other than the use case's (`run` for a persistent holdout, or one persistent scope for another), held-out customers named as treated, or no holdout at all; for a test plan, a plan whose campaign holds nobody back or that asks for another share than the assignment's (`POLICY_HOLDOUT_IMMUTABLE`) |
| `budget.cap` | the M108 estimate above `governance.max_run_cost_usd` (or unpriced while a cap is set), offer spending above `uplift.policy.total_budget`, contacts above `uplift.policy.budget_contacts` (`POLICY_BUDGET_CAP`) |
| `consent.channel` | a plan reaching more customers on the channel than the run's contactable eligible rows there (the list on a channel holds only those, M99), a channel whose contactability the run did not record, or one recorded without a configured column (`POLICY_CONSENT_CHANNEL`); row counts and reasons only |
| `frequency.cap` | more contacts per customer than `contact_cap_per_customer`, or more on the channel than its `channel_caps` entry (`POLICY_FREQUENCY_CAP`) |
| `test.registered` | a campaign plan with no frozen test plan for its campaign, or a different one from the plan cited (`POLICY_TEST_NOT_REGISTERED`); a test plan for a campaign that already has another frozen plan, or was measured before one was registered (`POLICY_TEST_PLAN_FIXED`) |
| `role.separation` | at approval, the person who proposed it approving it (`POLICY_ROLE_SEPARATION`), the existing rule of `engine.approvals.separation_refusal` |

The thresholds live in `configs/agentic/policy.yaml`; its sha256 is the policy version every ledger entry
records (`engine.agentic.config.policy_version`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import Field, ValidationError

from engine.access.roles import Principal
from engine.agentic.config import PolicyConfig
from engine.agentic.errors import AgenticError
from engine.config import StrictBase
from engine.measurement.plan import TestPlanInput

if TYPE_CHECKING:
    from engine.agentic.registry import ToolContext

__all__ = [
    "RULES",
    "CampaignPlanPayload",
    "ContactabilityFacts",
    "CostFacts",
    "Decision",
    "HoldoutFacts",
    "HoldoutTerms",
    "PolicyContext",
    "ProposalKind",
    "ProposalSubject",
    "RealisedFacts",
    "Rule",
    "TestPlanPayload",
    "allowed",
    "build_context",
    "contactability_columns_missing",
    "contactability_reasons",
    "evaluate",
    "payload_model",
]

ProposalKind = Literal["campaign_plan", "test_plan", "copy_arms"]
"""What a proposal proposes. `copy_arms` has no rule set yet (M140), so it is refused by default deny."""


# ---------------------------------------------------------------------------
# Payloads: the shape of each kind's proposal (canonical JSON; no prose beyond the plan's own words)
# ---------------------------------------------------------------------------
class RealisedFacts(StrictBase):
    """Who a campaign's assignment compares: counts, never a key (`engine.measurement.plan.RealisedPopulation`)."""

    population_rows: int
    n_treat: int
    n_holdout: int
    n_explore: int
    holdout_fraction: float


class TestPlanPayload(StrictBase):
    """`draft_test_plan`'s proposal: register this plan for this campaign (frozen only when approved)."""

    __test__ = False

    campaign_id: str
    plan: TestPlanInput = Field(description="The plan as the person would register it, window resolved.")
    requested_holdout_fraction: float | None = Field(
        default=None, description="A holdout share the agent asked for; the assignment's own decides."
    )
    realised: RealisedFacts = Field(description="The campaign's assignment when the proposal was drafted.")
    plan_hash: str = Field(description="`plan_hash` the frozen plan will have (`freeze_plan` in dry mode).")


class HoldoutTerms(StrictBase):
    fraction: float | None = Field(default=None, ge=0.0, le=1.0, description="The holdout share proposed.")
    scope: Literal["run", "use_case", "universal"] | None = None
    treat_members: bool = Field(default=False, description="Whether held-out customers would be contacted.")


class CampaignPlanPayload(StrictBase):
    """A campaign plan (M139 drafts these; M132 checks them): send this run's list on this channel."""

    use_case_id: str
    run_id: str = Field(description="The scoring run whose list would be sent.")
    campaign_id: str | None = Field(default=None, description="The campaign whose frozen test plan it cites.")
    channel: str
    holdout: HoldoutTerms | None = Field(
        default=None, description="The holdout terms; omitting them is refused."
    )
    test_plan_hash: str | None = None
    contacts_per_customer: int = Field(default=1, ge=1)
    planned_contacts: int | None = Field(default=None, ge=0)
    offer_spend_inr: float | None = Field(default=None, ge=0.0)


_PAYLOADS: Final[dict[str, type[StrictBase]]] = {
    "test_plan": TestPlanPayload,
    "campaign_plan": CampaignPlanPayload,
}


def payload_model(kind: str) -> type[StrictBase] | None:
    """The payload shape of a proposal kind, or None for a kind with no shape yet."""
    return _PAYLOADS.get(kind)


# ---------------------------------------------------------------------------
# Decisions and the context
# ---------------------------------------------------------------------------
class Decision(StrictBase):
    """One rule's verdict on one proposal."""

    allowed: bool
    rule: str = Field(
        description='e.g. "holdout.immutable", "budget.cap", "consent.channel"; "default" for default deny.'
    )
    code: str | None = Field(description="The `PLAN_K_CODES` refusal code; null when allowed.")
    detail: dict[str, Any] = Field(default_factory=dict, description="Counts, shares and ids: never a row.")


class ProposalSubject(StrictBase):
    """What the rules see of a proposal: its kind, its payload and who proposed it."""

    kind: str
    payload: dict[str, Any]
    proposed_by: str = Field(description="The user id the proposal was made for (the agent acts for them).")


class HoldoutFacts(StrictBase):
    scope: Literal["run", "use_case", "universal"]
    fraction: float = Field(description="The configured effective share (`effective_holdout_fraction`).")
    epoch: int | None = None
    epoch_fraction: float | None = Field(default=None, description="The ledger's share in the current epoch.")
    salt_id: str | None = None

    @property
    def in_force(self) -> float:
        """The smallest share a proposal may keep: the configured one, or the epoch's when that is larger."""
        return max(self.fraction, self.epoch_fraction or 0.0)


class CostFacts(StrictBase):
    estimated_usd: float | None
    known_usd: float | None
    cap_usd: float | None
    over_cap: bool | None
    needs_confirmation: bool


class ContactabilityFacts(StrictBase):
    channels: tuple[str, ...]
    eligible_rows: int = Field(description="Rows neither suppressed nor held out as control.")
    channel_counts: dict[str, int] = Field(
        description="Per channel: eligible rows not contactable on it (M99's `channel_counts`; rows)."
    )
    reasons: dict[str, dict[str, int]] = Field(
        default_factory=dict, description="Per channel, row counts by reason code (from the run's summary)."
    )
    columns_missing: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        description="Per channel, the configured columns the scoring file lacked (names, not counts).",
    )

    def contactable(self, channel: str) -> int:
        """Eligible rows that may be contacted on `channel`: the most a list on it can hold (rows)."""
        return max(0, self.eligible_rows - int(self.channel_counts.get(channel, 0)))


class PolicyContext(StrictBase):
    """Everything the rules read, computed by the server (:func:`build_context`)."""

    thresholds: PolicyConfig
    approver: Principal | None = Field(default=None, description="Set at approval only.")
    holdout: HoldoutFacts | None = None
    realised: RealisedFacts | None = Field(default=None, description="A test plan's campaign, as stored now.")
    dry_plan_hash: str | None = Field(default=None, description="What the plan would hash to if frozen now.")
    cost: CostFacts | None = None
    offer_budget_inr: float | None = None
    contact_budget: int | None = None
    contactability: ContactabilityFacts | None = None
    contact_cap_per_customer: int | None = None
    channel_caps: dict[str, int] = Field(default_factory=dict)
    registered_plan_hash: str | None = None
    campaign_measured: bool = Field(
        default=False, description="A test plan's campaign already has a stored measurement."
    )


Rule = Callable[[ProposalSubject, PolicyContext], Decision | None]


def _allow(rule: str, **detail: Any) -> Decision:
    return Decision(allowed=True, rule=rule, code=None, detail=detail)


def _refuse(rule: str, code: str, reason: str, **detail: Any) -> Decision:
    return Decision(allowed=False, rule=rule, code=code, detail={"reason": reason, **detail})


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------
_HOLDOUT: Final[str] = "holdout.immutable"


def holdout_immutable(subject: ProposalSubject, ctx: PolicyContext) -> Decision | None:
    """I4: no agent path shrinks, skips or reassigns the holdout."""
    tolerance = ctx.thresholds.holdout.tolerance
    code = "POLICY_HOLDOUT_IMMUTABLE"
    if subject.kind == "test_plan":
        payload = TestPlanPayload.model_validate(subject.payload)
        realised = ctx.realised or payload.realised
        if realised.n_holdout <= 0:
            return _refuse(
                _HOLDOUT, code, "The campaign holds nobody back, so it has no holdout to test against."
            )
        asked = payload.requested_holdout_fraction
        if asked is not None and abs(asked - realised.holdout_fraction) > tolerance:
            return _refuse(
                _HOLDOUT,
                code,
                "A test plan cannot change the holdout: it is the campaign's assignment.",
                requested=asked,
                in_force=realised.holdout_fraction,
            )
        return _allow(_HOLDOUT, in_force=realised.holdout_fraction)
    if subject.kind == "campaign_plan":
        payload_c = CampaignPlanPayload.model_validate(subject.payload)
        terms = payload_c.holdout
        if terms is None or terms.fraction is None or terms.fraction <= 0.0:
            return _refuse(_HOLDOUT, code, "The plan holds nobody back: every campaign keeps its holdout.")
        if terms.treat_members:
            return _refuse(_HOLDOUT, code, "The plan contacts held-out customers.")
        facts = ctx.holdout
        if facts is None:  # the use case could not be read: its holdout in force is unknown, so deny
            return _refuse(_HOLDOUT, code, "The use case's holdout could not be read, so it cannot be kept.")
        if terms.scope is not None and terms.scope != facts.scope:
            return _refuse(
                _HOLDOUT,
                code,
                (
                    "The use case keeps a persistent holdout; a plan cannot replace it with a per-run draw."
                    if terms.scope == "run"
                    else "The plan names another holdout than the use case's, which would hold back other customers."
                ),
                scope=facts.scope,
                proposed_scope=terms.scope,
            )
        if terms.fraction < facts.in_force - tolerance:
            return _refuse(
                _HOLDOUT,
                code,
                "The plan holds back a smaller share than the one in force.",
                proposed=terms.fraction,
                in_force=facts.in_force,
                epoch=facts.epoch,
            )
        return _allow(_HOLDOUT, proposed=terms.fraction)
    return None


_BUDGET: Final[str] = "budget.cap"


def budget_cap(subject: ProposalSubject, ctx: PolicyContext) -> Decision | None:
    """The M108 cost cap and the use case's offer budget; raising them is an Admin's change, not an agent's."""
    if subject.kind != "campaign_plan":
        return None
    payload = CampaignPlanPayload.model_validate(subject.payload)
    code = "POLICY_BUDGET_CAP"
    cost = ctx.cost
    if cost is not None and cost.cap_usd is not None:
        if cost.over_cap:
            return _refuse(
                _BUDGET,
                code,
                "The run's cost estimate is above the cost cap.",
                estimate_usd=cost.known_usd,
                cap_usd=cost.cap_usd,
            )
        if cost.needs_confirmation and ctx.thresholds.budget.refuse_unpriced_when_capped:
            return _refuse(
                _BUDGET,
                code,
                "The run's cost cannot be worked out while a cost cap is set; a person must confirm it.",
                cap_usd=cost.cap_usd,
            )
    if (
        payload.offer_spend_inr is not None
        and ctx.offer_budget_inr is not None
        and payload.offer_spend_inr > ctx.offer_budget_inr
    ):
        return _refuse(
            _BUDGET,
            code,
            "The offers would cost more than the offer budget.",
            proposed_inr=payload.offer_spend_inr,
            budget_inr=ctx.offer_budget_inr,
        )
    if (
        payload.planned_contacts is not None
        and ctx.contact_budget is not None
        and payload.planned_contacts > ctx.contact_budget
    ):
        return _refuse(
            _BUDGET,
            code,
            "The plan contacts more customers than the budget allows.",
            proposed=payload.planned_contacts,
            budget=ctx.contact_budget,
        )
    return _allow(_BUDGET)


_CONSENT: Final[str] = "consent.channel"


def consent_channel(subject: ProposalSubject, ctx: PolicyContext) -> Decision | None:
    """Nobody the plan would contact on the channel is someone the run recorded as not contactable on it.

    The run's list on a channel holds only the eligible customers contactable on it: the treat list never
    sends a customer anything on a channel they are not contactable on (M99), so opt-outs elsewhere in the run
    are not on the list and do not refuse the plan. What the rule checks is the plan's own reach: its
    `planned_contacts` may not exceed the run's contactable eligible rows on the channel by more than
    `consent.max_not_contactable` (0). It refuses, too, a channel the run did not record, or recorded without
    a configured column (whose consent was then never read), while `consent.require_recorded` is on.
    """
    if subject.kind != "campaign_plan":
        return None
    payload = CampaignPlanPayload.model_validate(subject.payload)
    code = "POLICY_CONSENT_CHANNEL"
    facts = ctx.contactability
    require = ctx.thresholds.consent.require_recorded
    if facts is None or payload.channel not in facts.channels:
        if require:
            return _refuse(
                _CONSENT,
                code,
                "The scoring run did not record who may be contacted on this channel.",
                channel=payload.channel,
            )
        return _allow(_CONSENT, channel=payload.channel)
    missing = facts.columns_missing.get(payload.channel, ())
    if missing and require:
        return _refuse(
            _CONSENT,
            code,
            "The scoring file lacked a column the channel is configured with, so its consent was not read.",
            channel=payload.channel,
            columns_missing=list(missing),
        )
    contactable = facts.contactable(payload.channel)
    beyond = 0 if payload.planned_contacts is None else max(0, payload.planned_contacts - contactable)
    if beyond > ctx.thresholds.consent.max_not_contactable:
        return _refuse(
            _CONSENT,
            code,
            "The plan would contact more customers on this channel than the run may contact on it.",
            channel=payload.channel,
            planned_contacts=payload.planned_contacts,
            contactable_rows=contactable,
            not_contactable_rows=int(facts.channel_counts.get(payload.channel, 0)),
            reasons=facts.reasons.get(payload.channel, {}),
        )
    return _allow(
        _CONSENT,
        channel=payload.channel,
        contactable_rows=contactable,
        not_contactable_rows=int(facts.channel_counts.get(payload.channel, 0)),
    )


_FREQUENCY: Final[str] = "frequency.cap"


def frequency_cap(subject: ProposalSubject, ctx: PolicyContext) -> Decision | None:
    """M101's caps: contacts per customer across use cases, and per channel."""
    if subject.kind != "campaign_plan":
        return None
    payload = CampaignPlanPayload.model_validate(subject.payload)
    code = "POLICY_FREQUENCY_CAP"
    cap = ctx.contact_cap_per_customer
    if cap is not None and payload.contacts_per_customer > cap:
        return _refuse(
            _FREQUENCY,
            code,
            "More contacts per customer than the contact cap.",
            proposed=payload.contacts_per_customer,
            cap=cap,
        )
    channel_cap = ctx.channel_caps.get(payload.channel)
    if (
        channel_cap is not None
        and payload.planned_contacts is not None
        and payload.planned_contacts > channel_cap
    ):
        return _refuse(
            _FREQUENCY,
            code,
            "More contacts on this channel than its cap.",
            channel=payload.channel,
            proposed=payload.planned_contacts,
            cap=channel_cap,
        )
    return _allow(_FREQUENCY)


_TEST: Final[str] = "test.registered"


def plan_registered(subject: ProposalSubject, ctx: PolicyContext) -> Decision | None:
    """A campaign plan stands on a frozen test plan (M94), and on the one it cites; a test plan is proposed
    only while it can still be registered as the plan route would (`POST /campaigns/{id}/plan`)."""
    if subject.kind == "test_plan":
        return _test_plan_registrable(subject, ctx)
    if subject.kind != "campaign_plan":
        return None
    payload = CampaignPlanPayload.model_validate(subject.payload)
    code = "POLICY_TEST_NOT_REGISTERED"
    if payload.campaign_id is None or ctx.registered_plan_hash is None:
        return _refuse(_TEST, code, "There is no frozen test plan for this campaign; register one first.")
    if payload.test_plan_hash != ctx.registered_plan_hash:
        return _refuse(
            _TEST,
            code,
            "The plan cited is not the campaign's frozen test plan.",
            registered=ctx.registered_plan_hash,
        )
    return _allow(_TEST, plan_hash=ctx.registered_plan_hash)


def _test_plan_registrable(subject: ProposalSubject, ctx: PolicyContext) -> Decision:
    """Refuse a test plan nobody could approve into being: another plan is frozen, or the campaign was measured."""
    payload = TestPlanPayload.model_validate(subject.payload)
    code = "POLICY_TEST_PLAN_FIXED"
    proposed = ctx.dry_plan_hash or payload.plan_hash
    registered = ctx.registered_plan_hash
    if registered is not None and registered != proposed:
        return _refuse(
            _TEST,
            code,
            "This campaign already has another frozen test plan; change it through an amendment, with a reason.",
            registered=registered,
        )
    if registered is None and ctx.campaign_measured:
        return _refuse(
            _TEST,
            code,
            "This campaign has already been measured, so a plan registered now would not be fixed in advance.",
        )
    return _allow(_TEST, plan_hash=proposed)


_ROLES: Final[str] = "role.separation"


def role_separation(subject: ProposalSubject, ctx: PolicyContext) -> Decision | None:
    """At approval: whoever proposed it may not approve it (the existing rule, DEC-862)."""
    if ctx.approver is None:
        return None
    from engine.approvals import separation_refusal

    if separation_refusal(ctx.approver, subject.proposed_by) is not None:
        return _refuse(
            _ROLES, "POLICY_ROLE_SEPARATION", "You proposed this, so another Approver has to decide on it."
        )
    return _allow(_ROLES)


RULES: Final[Mapping[str, Rule]] = {
    _HOLDOUT: holdout_immutable,
    _BUDGET: budget_cap,
    _CONSENT: consent_channel,
    _FREQUENCY: frequency_cap,
    _TEST: plan_registered,
    _ROLES: role_separation,
}
"""Every rule by name. The mutation test passes `evaluate` this map with one rule removed."""


def evaluate(
    subject: ProposalSubject, ctx: PolicyContext, *, rules: Mapping[str, Rule] = RULES
) -> tuple[Decision, ...]:
    """Every applicable rule's decision on `subject`; one `default` refusal when the kind has no rule set.

    A rule named in the rule set but missing from `rules` decides nothing (that is how the mutation test
    disables one); a rule set with no rule that applies allows nothing either - default deny holds.
    """
    names = ctx.thresholds.rule_sets.get(subject.kind)
    if not names:
        return (
            _refuse("default", "POLICY_NO_RULES", f"No policy rules are set for a {subject.kind} proposal."),
        )
    shape = payload_model(subject.kind)
    if shape is not None:
        try:
            shape.model_validate(subject.payload)
        except ValidationError as exc:
            fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
            return (
                _refuse(
                    "default",
                    "POLICY_PAYLOAD_INVALID",
                    "The proposal is not shaped as its kind requires.",
                    fields=fields,
                ),
            )
    decisions = [
        decision
        for name in names
        if (rule := rules.get(name)) is not None and (decision := rule(subject, ctx)) is not None
    ]
    if not decisions:
        return (_refuse("default", "POLICY_NO_RULES", f"No rule applies to this {subject.kind} proposal."),)
    return tuple(decisions)


def allowed(decisions: tuple[Decision, ...]) -> bool:
    """A proposal passes only when there is at least one decision and every one allows it."""
    return bool(decisions) and all(decision.allowed for decision in decisions)


# ---------------------------------------------------------------------------
# The context, from the engine's own objects (the only I/O of this module)
# ---------------------------------------------------------------------------
def build_context(
    subject: ProposalSubject,
    tool_ctx: ToolContext,
    policy: PolicyConfig,
    *,
    approver: Principal | None = None,
) -> PolicyContext:
    """The facts the rules read for `subject`, computed now from stored artefacts (never from the payload)."""
    base: dict[str, Any] = {"thresholds": policy, "approver": approver}
    shape = payload_model(subject.kind)
    if shape is None:
        return PolicyContext(**base)
    try:
        payload = shape.model_validate(subject.payload)
    except ValidationError:
        return PolicyContext(**base)  # `evaluate` refuses the shape with POLICY_PAYLOAD_INVALID
    if isinstance(payload, TestPlanPayload):
        return PolicyContext(**base, **_test_plan_facts(payload, tool_ctx))
    if isinstance(payload, CampaignPlanPayload):
        return PolicyContext(**base, **_campaign_plan_facts(payload, tool_ctx))
    return PolicyContext(**base)  # pragma: no cover - every shape is handled above


def _campaign(tool_ctx: ToolContext, campaign_id: str) -> Any:
    if tool_ctx.campaigns is None:
        raise AgenticError("CAMPAIGN_NOT_FOUND", "No campaign store is available to this call.", status=404)
    campaign = tool_ctx.campaigns.get(campaign_id)
    if campaign is None:
        raise AgenticError("CAMPAIGN_NOT_FOUND", f"No campaign with id {campaign_id!r}.", status=404)
    return campaign


def _test_plan_facts(payload: TestPlanPayload, tool_ctx: ToolContext) -> dict[str, Any]:
    from engine.agentic.proposals import dry_test_plan
    from engine.measurement.campaign import REPORT_FILENAME, campaign_key

    campaign = _campaign(tool_ctx, payload.campaign_id)
    plan, realised = dry_test_plan(tool_ctx, campaign, payload.plan)
    facts: dict[str, Any] = {
        "realised": RealisedFacts(
            population_rows=realised.population_rows,
            n_treat=realised.n_treat,
            n_holdout=realised.n_holdout,
            n_explore=realised.n_explore,
            holdout_fraction=realised.holdout_fraction,
        ),
        "dry_plan_hash": plan.plan_hash,
        "registered_plan_hash": campaign.test_plan_hash,
        "campaign_measured": tool_ctx.storage.exists(campaign_key(campaign.campaign_id, REPORT_FILENAME)),
    }
    return facts


def _campaign_plan_facts(payload: CampaignPlanPayload, tool_ctx: ToolContext) -> dict[str, Any]:
    from engine.config import ConfigError, RunMode, load_use_case
    from engine.decide.arbitrate import load_arbitration_config

    facts: dict[str, Any] = {}
    try:
        config = load_use_case(payload.use_case_id, tool_ctx.config_root)
    except ConfigError:
        config = None
    if config is not None:
        facts["holdout"] = _holdout_facts(config, tool_ctx)
        facts["cost"] = _cost_facts(config, RunMode.SCORE, tool_ctx)
        facts["offer_budget_inr"] = config.uplift.policy.total_budget
        facts["contact_budget"] = config.uplift.policy.budget_contacts
    facts["contactability"] = _contactability_facts(payload.run_id, tool_ctx)
    arbitration = load_arbitration_config(tool_ctx.config_root)
    facts["contact_cap_per_customer"] = arbitration.contact_cap_per_customer
    facts["channel_caps"] = dict(arbitration.channel_caps)
    if payload.campaign_id is not None and tool_ctx.campaigns is not None:
        campaign = tool_ctx.campaigns.get(payload.campaign_id)
        facts["registered_plan_hash"] = None if campaign is None else campaign.test_plan_hash
    return facts


def _holdout_facts(config: Any, tool_ctx: ToolContext) -> HoldoutFacts:
    from engine.holdout.spec import UNIVERSAL_SCOPE_KEY, effective_holdout_fraction

    scope = config.actions.holdout.scope
    fraction = float(effective_holdout_fraction(config.actions))
    if scope == "run" or tool_ctx.platform_engine is None:
        return HoldoutFacts(scope=scope, fraction=fraction)
    from engine.holdout.salt import HoldoutLedger

    key = config.id if scope == "use_case" else UNIVERSAL_SCOPE_KEY
    entry = HoldoutLedger(tool_ctx.platform_engine).entry(scope, key)
    if entry is None:
        return HoldoutFacts(scope=scope, fraction=fraction)
    return HoldoutFacts(
        scope=scope,
        fraction=fraction,
        epoch=entry.epoch,
        epoch_fraction=entry.fraction,
        salt_id=entry.salt_id,
    )


def _cost_facts(config: Any, mode: Any, tool_ctx: ToolContext) -> CostFacts:
    from engine.aws.run_cost import cached_price_table, estimate_run_cost
    from engine.generative.budget import load_prices
    from engine.settings import Settings

    estimate = estimate_run_cost(
        config,
        mode,
        settings=tool_ctx.settings if tool_ctx.settings is not None else Settings(),
        table=cached_price_table(tool_ctx.config_root),
        llm_prices=load_prices(tool_ctx.config_root),
        fx=None,
    )
    return CostFacts(
        estimated_usd=estimate.estimated_usd,
        known_usd=estimate.known_usd,
        cap_usd=estimate.cap_usd,
        over_cap=estimate.over_cap,
        needs_confirmation=estimate.needs_confirmation,
    )


def contactability_reasons(summary: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """Per channel, row counts by reason code, from a run's `channel_contactability.json` (as stored).

    `not_contactable` is M99's count of eligible customers not contactable on the channel; M131's optional
    `inquiry_lapsed_rows` and `legacy_unregistered_rows` are read when the run wrote them. Every value is a
    number of rows; the configured columns the file lacked are names, kept apart by
    :func:`contactability_columns_missing`. No customer is named.
    """
    out: dict[str, dict[str, int]] = {}
    for source in summary.get("by_channel", ()):
        if not isinstance(source, Mapping):
            continue
        channel = str(source.get("channel"))
        reasons: dict[str, int] = {"not_contactable": int(source.get("not_contactable_eligible_rows") or 0)}
        for field, code in (
            ("inquiry_lapsed_rows", "inquiry_lapsed"),
            ("legacy_unregistered_rows", "legacy_unregistered"),
        ):
            value = source.get(field)
            if isinstance(value, int):
                reasons[code] = value
        out[channel] = reasons
    return out


def contactability_columns_missing(summary: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Per channel, the configured columns the scoring file lacked (M99's `columns_missing`), by name."""
    return {
        str(source.get("channel")): tuple(str(name) for name in source.get("columns_missing") or ())
        for source in summary.get("by_channel", ())
        if isinstance(source, Mapping)
    }


def _contactability_facts(run_id: str, tool_ctx: ToolContext) -> ContactabilityFacts | None:
    from engine.agentic.artefacts import read_stored_json
    from engine.decide.contactability import CHANNEL_CONTACTABILITY_SUMMARY_FILENAME
    from engine.storage import run_key

    summary = read_stored_json(tool_ctx.storage, run_key(run_id, CHANNEL_CONTACTABILITY_SUMMARY_FILENAME))
    if summary is None:
        return None
    return ContactabilityFacts(
        channels=tuple(summary.get("channels", ())),
        eligible_rows=int(summary.get("eligible_rows", 0)),
        channel_counts={str(k): int(v) for k, v in dict(summary.get("channel_counts", {})).items()},
        reasons=contactability_reasons(summary),
        columns_missing=contactability_columns_missing(summary),
    )
