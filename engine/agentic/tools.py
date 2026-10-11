"""The first tools (Plan K M132, design §1.2): thin wrappers over engine functions that already exist.

| Tool | Kind | Wraps | Mirrors (its `RoutePolicy`) |
|---|---|---|---|
| `plan_test` | read | `engine.measurement.planner.power_preview` | `POST /measurement/power-preview` (Viewer) |
| `check_contactable` | read | the run's `channel_contactability.json` (`engine.decide.contactability`, M99, + M131's reasons) | `GET /runs/{run_id}/artefacts/{name}` (Viewer; audited as that route is) |
| `value_segment` | read | the run's `expected_gross_value.json` (`engine.decide.value`, M97) | `GET /runs/{run_id}/artefacts/{name}` (Viewer; audited as that route is) |
| `measure` | read | the campaign's stored report (`engine.measurement.measure.measure_campaign`, M94) | `GET /campaigns/{campaign_id}` (Viewer) |
| `audit_campaign` | read | the campaign's stored `audit.json` and report (`engine.measurement.audit`, M103) | `GET /campaigns/{campaign_id}` (Viewer) |
| `get_proof_pack` | read | `engine.pilot.proof.build_proof` (M104) | `GET /pilot/proof/{campaign_id}` (Viewer) |
| `evaluate_policy` | read | the run's stored `ope_report.json` and `risk_comparison.json` (`engine.uplift.ope`, M96, M137) | `GET /runs/{run_id}/risk-comparison` (Viewer) |
| `check_before_contact` | read | the run's stored `compliance_decisions.json` (`engine.compliance.handoff`, M134): counts allowed and withheld by reason, never a row (M141) | `GET /runs/{run_id}/artefacts/{name}` (Viewer; audited as that route is) |
| `draft_test_plan` | propose | `engine.measurement.plan.freeze_plan` in dry mode (`engine.agentic.proposals`) | `POST /campaigns/{campaign_id}/plan` (Analyst) |

**Compute once.** A read tool reads what the product already computed and stored; it never measures,
audits or evaluates again. Something not yet computed is answered with `null` and a plain reason (which route
computes it), never with a fabricated value or 0; something stored that no longer reads is refused with
`TOOL_ARTEFACT_UNREADABLE` (`engine.agentic.artefacts`), never reported as "not computed". `get_proof_pack` builds the pack on request, as its route
does: the pack is itself a read of stored artefacts. Its build time is left out, so the same artefacts give the
same evidence id.

**Aggregates only.** Every output is counts, rates, intervals and sentences the engine wrote; no tool opens a
row-level file except `draft_test_plan`, which reads the campaign's assignment for its counts (as the plan route
does) and returns the counts only.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import Field

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agentic.errors import AgenticError
from engine.agentic.proposals import DraftTestPlanInput, Proposal, draft_test_plan
from engine.agentic.registry import AgentTool, EvidenceLabel, ToolContext, ToolKind, ToolRegistry
from engine.config import StrictBase
from engine.measurement.audit import AuditReadout
from engine.measurement.planner import PowerPreview, PowerPreviewRequest, power_preview
from engine.uplift.contracts import IncrementalityReport, OpeReport, RiskComparison

__all__ = ["ID_PATTERN", "default_registry"]

ID_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$"
"""A run or campaign id: letters, digits, `_` and `-`, so no id can name a path outside its folder."""

_ARTEFACT_POLICY: Final[RoutePolicy] = RoutePolicy(
    role=Role.VIEWER,
    action="runs.artefact_download",
    purpose="download a run artefact",
    object_type="run",
    object_param="run_id",
    audit_reads=True,
)
_ARTEFACT_ROUTE: Final[tuple[str, str]] = ("GET", "/runs/{run_id}/artefacts/{name}")
_CAMPAIGN_POLICY: Final[RoutePolicy] = RoutePolicy(
    role=Role.VIEWER,
    action="campaigns.read",
    purpose="see a campaign",
    object_type="campaign",
    object_param="campaign_id",
)
_CAMPAIGN_ROUTE: Final[tuple[str, str]] = ("GET", "/campaigns/{campaign_id}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _run_exists(ctx: ToolContext, run_id: str) -> None:
    from engine.runs import RUN_FILENAME
    from engine.storage import run_key

    if not ctx.storage.exists(run_key(run_id, RUN_FILENAME)):
        raise AgenticError("RUN_NOT_FOUND", f"No run with id {run_id!r}.", status=404)


def _stored_json(ctx: ToolContext, key: str) -> dict[str, Any] | None:
    """None when nothing is stored at `key`; `TOOL_ARTEFACT_UNREADABLE` when something is but does not read."""
    from engine.agentic.artefacts import read_stored_json

    return read_stored_json(ctx.storage, key)


def _stored_model(ctx: ToolContext, key: str, model: type[Any]) -> Any:
    """None when nothing is stored at `key`; `TOOL_ARTEFACT_UNREADABLE` when it does not read or validate."""
    from engine.agentic.artefacts import read_stored_model

    return read_stored_model(ctx.storage, key, model)


def _campaign(ctx: ToolContext, campaign_id: str) -> Any:
    from engine.agentic.policy import _campaign as load

    return load(ctx, campaign_id)


class RunInput(StrictBase):
    run_id: str = Field(pattern=ID_PATTERN, description="The run.")


class CampaignInput(StrictBase):
    campaign_id: str = Field(pattern=ID_PATTERN, description="The campaign.")


# ---------------------------------------------------------------------------
# plan_test
# ---------------------------------------------------------------------------
def _plan_test(given: PowerPreviewRequest, _ctx: ToolContext) -> PowerPreview:
    return power_preview(given)


PLAN_TEST: Final[AgentTool[PowerPreviewRequest, PowerPreview]] = AgentTool(
    name="plan_test",
    kind=ToolKind.READ,
    description="How small a change each control-group share can see, and what holding it back costs.",
    input_model=PowerPreviewRequest,
    output_model=PowerPreview,
    route_policy=RoutePolicy(
        role=Role.VIEWER, action="measurement.power_preview", purpose="preview how big a test needs to be"
    ),
    mirrors=("POST", "/measurement/power-preview"),
    evidence_label=lambda _out: "Estimated",
    run=_plan_test,
)


# ---------------------------------------------------------------------------
# check_contactable
# ---------------------------------------------------------------------------
class ChannelReasons(StrictBase):
    channel: str
    contactable_rows: int = Field(
        description="Rows contactable on the channel (channel_contactability.json)."
    )
    not_contactable_eligible_rows: int = Field(
        description="Eligible rows (neither suppressed nor held back) not contactable on it."
    )
    ledger_applied: bool = Field(
        description="Whether the consent ledger's records for the channel were applied."
    )
    reasons: dict[str, int] = Field(
        description="Rows by reason code (not_contactable, and M131's inquiry_lapsed, legacy_unregistered)."
    )
    columns_missing: tuple[str, ...] = Field(
        default=(), description="Configured columns the scoring file lacked: names, not a count."
    )


class ContactabilityOut(StrictBase):
    run_id: str
    recorded: bool = Field(description="Whether the run recorded per-channel contactability.")
    reason: str | None = Field(description="Why not, when it did not; null otherwise.")
    use_case_id: str | None = None
    rows: int | None = Field(default=None, description="Rows scored.")
    eligible_rows: int | None = Field(default=None, description="Rows neither suppressed nor held back.")
    ledger_purpose: str | None = None
    channels: tuple[ChannelReasons, ...] = ()
    # Plan K M139: the held-back group (`scoring_summary.json`'s `control_group_rows`), so a test is sized on
    # everyone the campaign compares - the eligible rows plus the held-back ones - as the product's own
    # preview sizes it (`realised.population_rows`). Left out while unknown, so earlier outputs are unchanged.
    held_back_rows: int | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Rows held back as the control group (drawn from the unsuppressed rows); null when unknown.",
    )


def _check_contactable(given: RunInput, ctx: ToolContext) -> ContactabilityOut:
    from engine.agentic.policy import contactability_columns_missing, contactability_reasons
    from engine.decide.contactability import CHANNEL_CONTACTABILITY_SUMMARY_FILENAME
    from engine.storage import run_key

    _run_exists(ctx, given.run_id)
    summary = _stored_json(ctx, run_key(given.run_id, CHANNEL_CONTACTABILITY_SUMMARY_FILENAME))
    if summary is None:
        return ContactabilityOut(
            run_id=given.run_id,
            recorded=False,
            reason=(
                "This run did not record who may be contacted on each channel: its use case sets no "
                "actions.suppression.channels, or it is not a scoring run."
            ),
        )
    reasons = contactability_reasons(summary)
    missing = contactability_columns_missing(summary)
    channels = tuple(
        ChannelReasons(
            channel=str(source["channel"]),
            contactable_rows=int(source["contactable_rows"]),
            not_contactable_eligible_rows=int(source["not_contactable_eligible_rows"]),
            ledger_applied=bool(source.get("ledger")),
            reasons=reasons.get(str(source["channel"]), {}),
            columns_missing=missing.get(str(source["channel"]), ()),
        )
        for source in summary.get("by_channel", ())
    )
    scoring = _stored_json(ctx, run_key(given.run_id, "scoring_summary.json"))
    held_back = None if scoring is None else scoring.get("control_group_rows")
    return ContactabilityOut(
        run_id=given.run_id,
        recorded=True,
        reason=None,
        use_case_id=summary.get("use_case_id"),
        rows=summary.get("rows"),
        eligible_rows=summary.get("eligible_rows"),
        ledger_purpose=summary.get("ledger_purpose"),
        channels=channels,
        held_back_rows=None if held_back is None else int(held_back),
    )


CHECK_CONTACTABLE: Final[AgentTool[RunInput, ContactabilityOut]] = AgentTool(
    name="check_contactable",
    kind=ToolKind.READ,
    description="Per channel, how many of a scoring run's customers may be contacted, and the reasons as codes.",
    input_model=RunInput,
    output_model=ContactabilityOut,
    route_policy=_ARTEFACT_POLICY,
    mirrors=_ARTEFACT_ROUTE,
    evidence_label=lambda _out: "Descriptive",
    run=_check_contactable,
    artefact_refs=lambda given, _out: (given.run_id,),
)


# ---------------------------------------------------------------------------
# value_segment
# ---------------------------------------------------------------------------
class ValueSegmentInput(RunInput):
    band: str | None = Field(default=None, max_length=64, description="One band only; every band when null.")


class BandValue(StrictBase):
    band: str
    rows: int = Field(description="Contactable rows in the band.")
    rows_valued: int = Field(description="Of those, rows whose value is known.")
    total_inr: float = Field(description="Their expected gross value, in rupees (not incremental).")


class ValueSegmentOut(StrictBase):
    run_id: str
    valued: bool = Field(description="Whether the run computed an expected gross value.")
    reason: str | None = Field(description="Why not, or why the band asked for is absent; null otherwise.")
    label: str | None = Field(default=None, description="Always 'not incremental' when valued.")
    value_column: str | None = None
    contactable_rows: int | None = None
    rows_missing_value: int | None = Field(default=None, description="Rows with no value: never filled in.")
    total_inr: float | None = Field(
        default=None, description="All contactable rows' expected gross value, rupees."
    )
    bands: tuple[BandValue, ...] = ()
    note: str | None = Field(default=None, description="What the number is, and what it is not.")


def _value_segment(given: ValueSegmentInput, ctx: ToolContext) -> ValueSegmentOut:
    from engine.decide.value import EXPECTED_GROSS_VALUE_FILENAME, ExpectedGrossValue
    from engine.storage import run_key

    _run_exists(ctx, given.run_id)
    report = _stored_model(ctx, run_key(given.run_id, EXPECTED_GROSS_VALUE_FILENAME), ExpectedGrossValue)
    if report is None:
        return ValueSegmentOut(
            run_id=given.run_id,
            valued=False,
            reason=(
                "This run has no expected gross value: it is computed only on a propensity scoring run whose "
                "use case sets uplift.policy.value_column and whose file carries that column."
            ),
        )
    bands = tuple(
        BandValue(band=band.band, rows=band.rows, rows_valued=band.rows_valued, total_inr=band.total)
        for band in report.by_band
        if given.band is None or band.band == given.band
    )
    reason = None if bands or given.band is None else f"The run has no band called {given.band!r}."
    return ValueSegmentOut(
        run_id=given.run_id,
        valued=True,
        reason=reason,
        label=report.label,
        value_column=report.value_column,
        contactable_rows=report.contactable_rows,
        rows_missing_value=report.rows_missing_value,
        total_inr=report.total if given.band is None else None,
        bands=bands,
        note=report.note,
    )


VALUE_SEGMENT: Final[AgentTool[ValueSegmentInput, ValueSegmentOut]] = AgentTool(
    name="value_segment",
    kind=ToolKind.READ,
    description="What a propensity list is worth before anyone asks what it causes (not incremental), by band.",
    input_model=ValueSegmentInput,
    output_model=ValueSegmentOut,
    route_policy=_ARTEFACT_POLICY,
    mirrors=_ARTEFACT_ROUTE,
    evidence_label=lambda _out: "Descriptive",
    run=_value_segment,
    artefact_refs=lambda given, _out: (given.run_id,),
)


# ---------------------------------------------------------------------------
# measure
# ---------------------------------------------------------------------------
class MeasureOut(StrictBase):
    campaign_id: str
    measured: bool = Field(description="Whether the campaign has a stored measurement.")
    reason: str | None = Field(description="Why not; null when measured.")
    test_plan_hash: str | None = Field(
        default=None, description="The campaign's registered test plan, if any."
    )
    report: IncrementalityReport | None = Field(default=None, description="The stored report, as measured.")


def _report_label(report: IncrementalityReport | None) -> EvidenceLabel:
    if report is None:
        return "Descriptive"
    if report.early_look:
        return "Not proven"
    return "Causal" if report.causal else "Descriptive"


def _measure(given: CampaignInput, ctx: ToolContext) -> MeasureOut:
    from engine.measurement.campaign import REPORT_FILENAME, campaign_key

    campaign = _campaign(ctx, given.campaign_id)
    report = _stored_model(ctx, campaign_key(given.campaign_id, REPORT_FILENAME), IncrementalityReport)
    if report is None:
        return MeasureOut(
            campaign_id=given.campaign_id,
            measured=False,
            reason="This campaign has not been measured yet. Add its outcomes and measure it (POST /campaigns/{id}/measure).",
            test_plan_hash=campaign.test_plan_hash,
        )
    return MeasureOut(
        campaign_id=given.campaign_id,
        measured=True,
        reason=None,
        test_plan_hash=campaign.test_plan_hash,
        report=report,
    )


MEASURE: Final[AgentTool[CampaignInput, MeasureOut]] = AgentTool(
    name="measure",
    kind=ToolKind.READ,
    description="A campaign's measured result: treated against held back, with 95% intervals (as stored).",
    input_model=CampaignInput,
    output_model=MeasureOut,
    route_policy=_CAMPAIGN_POLICY,
    mirrors=_CAMPAIGN_ROUTE,
    evidence_label=lambda out: _report_label(out.report),
    run=_measure,
    artefact_refs=lambda given, out: tuple(
        ref
        for ref in (given.campaign_id, out.test_plan_hash, out.report.run_id if out.report else None)
        if ref
    ),
)


# ---------------------------------------------------------------------------
# audit_campaign
# ---------------------------------------------------------------------------
class AuditOut(StrictBase):
    campaign_id: str
    audited: bool = Field(description="Whether the campaign is an audited outside campaign.")
    reason: str | None = Field(description="Why not; null when audited.")
    audit: AuditReadout | None = None
    report: IncrementalityReport | None = None


def _audit_label(out: AuditOut) -> EvidenceLabel:
    if out.audit is None:
        return "Descriptive"
    if out.audit.causal_basis == "verified_random":
        return "Causal"
    return "Not proven" if out.audit.causal_basis == "declared_random" else "Descriptive"


def _audit_campaign(given: CampaignInput, ctx: ToolContext) -> AuditOut:
    from engine.measurement.audit import AUDIT_FILENAME
    from engine.measurement.campaign import REPORT_FILENAME, campaign_key

    _campaign(ctx, given.campaign_id)
    audit = _stored_model(ctx, campaign_key(given.campaign_id, AUDIT_FILENAME), AuditReadout)
    if audit is None:
        return AuditOut(
            campaign_id=given.campaign_id,
            audited=False,
            reason="This campaign was not audited: an audit is made for another tool's campaign (POST /campaigns/audit).",
        )
    report = _stored_model(ctx, campaign_key(given.campaign_id, REPORT_FILENAME), IncrementalityReport)
    return AuditOut(campaign_id=given.campaign_id, audited=True, reason=None, audit=audit, report=report)


AUDIT_CAMPAIGN: Final[AgentTool[CampaignInput, AuditOut]] = AgentTool(
    name="audit_campaign",
    kind=ToolKind.READ,
    description="How an outside campaign was read and what its numbers may claim (stored audit).",
    input_model=CampaignInput,
    output_model=AuditOut,
    route_policy=_CAMPAIGN_POLICY,
    mirrors=_CAMPAIGN_ROUTE,
    evidence_label=_audit_label,
    run=_audit_campaign,
    artefact_refs=lambda given, _out: (given.campaign_id,),
)


# ---------------------------------------------------------------------------
# get_proof_pack
# ---------------------------------------------------------------------------
class ProofLineOut(StrictBase):
    label: str
    value: str | None = Field(default=None, description="The printed value, a traced figure's text.")
    low: str | None = None
    high: str | None = None
    missing: str | None = Field(default=None, description="Why the line is not measured.")


class ProofSectionOut(StrictBase):
    key: str
    title: str
    status: str
    reason: str | None = None
    lines: tuple[ProofLineOut, ...] = ()
    table_rows: int = Field(
        default=0, description="Rows of the section's table (the table itself is on the page)."
    )


class ProofOut(StrictBase):
    campaign_id: str
    available: bool = Field(description="Whether a pack can be drawn now.")
    code: str | None = Field(default=None, description="PROOF_NOT_MATURE or PROOF_SYNTHETIC_DATA when not.")
    reason: str | None = None
    results_available_on: str | None = None
    claim: str | None = Field(default=None, description="proven, stated_random or descriptive.")
    claim_label: str | None = None
    headline: str | None = None
    measured_on: str | None = None
    sections: tuple[ProofSectionOut, ...] = ()
    artefacts: tuple[str, ...] = Field(
        default=(), description="Every artefact the pack read: aggregates only."
    )


def _text(figure: Any) -> str | None:
    return None if figure is None else str(figure.text)


def _get_proof_pack(given: CampaignInput, ctx: ToolContext) -> ProofOut:
    from engine.pilot.proof import ProofNotFoundError, ProofRefusedError, build_proof

    try:
        view = build_proof(ctx.storage, given.campaign_id, root=ctx.config_root, now=ctx.clock())
    except ProofNotFoundError as exc:
        raise AgenticError(
            "CAMPAIGN_NOT_FOUND", f"No campaign with id {given.campaign_id!r}.", status=404
        ) from exc
    except ProofRefusedError as exc:
        return ProofOut(
            campaign_id=given.campaign_id,
            available=False,
            code=exc.code,
            reason=exc.message,
            results_available_on=(
                None if exc.results_available_on is None else exc.results_available_on.isoformat()
            ),
        )
    sections = tuple(
        ProofSectionOut(
            key=section.key,
            title=section.title,
            status=section.status,
            reason=section.reason,
            lines=tuple(
                ProofLineOut(
                    label=line.label,
                    value=_text(line.value),
                    low=_text(line.low),
                    high=_text(line.high),
                    missing=line.missing,
                )
                for line in section.lines
            ),
            table_rows=0 if section.table is None else len(section.table.rows),
        )
        for section in view.sections
    )
    return ProofOut(
        campaign_id=given.campaign_id,
        available=True,
        claim=view.claim,
        claim_label=view.claim_label,
        headline=view.headline,
        measured_on=_text(view.measured_on),
        sections=sections,
        artefacts=view.artefacts,
    )


_CLAIM_LABELS: Final[dict[str, EvidenceLabel]] = {
    "proven": "Causal",
    "stated_random": "Not proven",
    "descriptive": "Descriptive",
}

GET_PROOF_PACK: Final[AgentTool[CampaignInput, ProofOut]] = AgentTool(
    name="get_proof_pack",
    kind=ToolKind.READ,
    description="A campaign's Value Proof Pack in short: the claim, the headline and each section's traced figures.",
    input_model=CampaignInput,
    output_model=ProofOut,
    route_policy=RoutePolicy(
        role=Role.VIEWER,
        action="pilot.proof_read",
        object_type="campaign",
        object_param="campaign_id",
        purpose="see a campaign's Value Proof Pack",
    ),
    mirrors=("GET", "/pilot/proof/{campaign_id}"),
    evidence_label=lambda out: _CLAIM_LABELS.get(out.claim or "", "Descriptive"),
    run=_get_proof_pack,
    artefact_refs=lambda given, out: (given.campaign_id, *out.artefacts),
)


# ---------------------------------------------------------------------------
# evaluate_policy
# ---------------------------------------------------------------------------
class EvaluatePolicyOut(StrictBase):
    run_id: str
    ope: OpeReport | None = Field(
        default=None, description="The stored off-policy estimate (IPS, SNIPS, DR)."
    )
    ope_reason: str | None = Field(default=None, description="Why there is none; null when there is.")
    risk_comparison: RiskComparison | None = Field(
        default=None,
        description="The stored equal-budget comparison (M96; M137's rollout bound when present).",
    )
    risk_comparison_reason: str | None = None


def _evaluate_policy(given: RunInput, ctx: ToolContext) -> EvaluatePolicyOut:
    from engine.storage import run_key
    from engine.uplift.contracts import OPE_FILENAME, RISK_COMPARISON_FILENAME

    _run_exists(ctx, given.run_id)
    ope = _stored_model(ctx, run_key(given.run_id, OPE_FILENAME), OpeReport)
    comparison = _stored_model(ctx, run_key(given.run_id, RISK_COMPARISON_FILENAME), RiskComparison)
    return EvaluatePolicyOut(
        run_id=given.run_id,
        ope=ope,
        ope_reason=(
            None
            if ope is not None
            else "No candidate policy has been evaluated on this run (POST /runs/{id}/uplift/ope)."
        ),
        risk_comparison=comparison,
        risk_comparison_reason=(
            None
            if comparison is not None
            else (
                "This run has no equal-budget comparison. An uplift training run computes it when "
                "uplift.evidence.risk_comparison is turned on."
            )
        ),
    )


def _policy_label(out: EvaluatePolicyOut) -> EvidenceLabel:
    """`Not proven` when either stored artefact rests on treatment acknowledged as not random; `Estimated`
    when there is an estimate; `Descriptive` when there is none."""
    stored = [item for item in (out.ope, out.risk_comparison) if item is not None]
    if any(not item.causal for item in stored):
        return "Not proven"
    return "Estimated" if stored else "Descriptive"


EVALUATE_POLICY: Final[AgentTool[RunInput, EvaluatePolicyOut]] = AgentTool(
    name="evaluate_policy",
    kind=ToolKind.READ,
    description="How a candidate targeting policy would have done on randomised logs, as stored for the run.",
    input_model=RunInput,
    output_model=EvaluatePolicyOut,
    route_policy=RoutePolicy(
        role=Role.VIEWER,
        action="measurement.risk_comparison",
        purpose="see whether uplift ranking beats risk ranking at the same budget",
        object_type="run",
        object_param="run_id",
    ),
    mirrors=("GET", "/runs/{run_id}/risk-comparison"),
    evidence_label=_policy_label,
    run=_evaluate_policy,
    artefact_refs=lambda given, _out: (given.run_id,),
)


# ---------------------------------------------------------------------------
# check_before_contact (Plan K M141, DEC-1408 (f))
# ---------------------------------------------------------------------------
class GateCounts(StrictBase):
    """What the pre-send compliance gate did to a run's planned list: counts of customers, and one count of the
    gate's decision rows (one per customer and planned channel), named as such. Never a row of a file. Over MCP the
    counts are one group as a whole: any of them from 1 to 9 withholds them all (`api.mcp.suppress`)."""

    customers: int = Field(description="Customers the list would have contacted before the gate.")
    sent_rows: int = Field(description="Of those, customers the list sends (on a channel the gate allowed).")
    withheld_rows: int = Field(
        description="Customers the gate took off the list: no planned channel allowed."
    )
    rerouted_rows: int = Field(description="Customers sent on another planned channel than the list chose.")
    decision_rows_without_consent_evidence: int = Field(
        description=(
            "Decision rows (one per customer and planned channel, not customers) the run's contactability file "
            "did not cover: their consent reference is null."
        )
    )


class CheckBeforeContactOut(StrictBase):
    run_id: str
    checked: bool = Field(description="Whether the gate decided the run's current list.")
    reason: str | None = Field(description="Why not, when it did not; null otherwise.")
    use_case_id: str | None = None
    purpose: str | None = Field(default=None, description="The consent purpose the gate decided for.")
    counts: GateCounts | None = Field(
        default=None, description="Customers allowed, withheld and moved; null when not checked or withheld."
    )
    withheld: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Customers withheld per reason code (COMPLIANCE_*). A customer counts under each reason that closed "
            "one of their channels, so the counts can add up to more than withheld_rows."
        ),
    )
    rule_version: str | None = Field(default=None, description="sha256 of the rules the gate applied.")
    decisions_sha256: str | None = Field(
        default=None, description="sha256 of the gate's decision file, as signed on the ledger."
    )
    gate_evidence_id: str | None = Field(
        default=None, description="The evidence id of the gate's own signed ledger entry."
    )
    gate_ledger_entry_hash: str | None = Field(
        default=None, description="That entry's entry_hash on the deployment's chain."
    )


def _check_before_contact(given: RunInput, ctx: ToolContext) -> CheckBeforeContactOut:
    from engine.compliance.handoff import COMPLIANCE_SUMMARY_FILENAME, ComplianceDecisionsSummary
    from engine.decide.treat_list import TREAT_LIST_SUMMARY_FILENAME
    from engine.storage import run_key

    _run_exists(ctx, given.run_id)

    def unchecked(reason: str) -> CheckBeforeContactOut:
        return CheckBeforeContactOut(run_id=given.run_id, checked=False, reason=reason)

    listed = _stored_json(ctx, run_key(given.run_id, TREAT_LIST_SUMMARY_FILENAME))
    if listed is None:
        return unchecked("No list has been built for this run yet: it is built with the run's treat list.")
    signed = listed.get("compliance_decisions_sha256")
    if not isinstance(signed, str):
        return unchecked(
            "The run's list was built with the pre-send compliance gate off (compliance.gate.enabled), so no "
            "contact was checked before it."
        )
    summary = _stored_model(
        ctx, run_key(given.run_id, COMPLIANCE_SUMMARY_FILENAME), ComplianceDecisionsSummary
    )
    if summary is None or summary.decisions_sha256 != signed:
        return unchecked(
            "The gate's decision record is not the one the run's current list was built with; build the list "
            "again to check it."
        )
    return CheckBeforeContactOut(
        run_id=given.run_id,
        checked=True,
        reason=None,
        use_case_id=summary.use_case_id,
        purpose=summary.purpose,
        counts=GateCounts(
            customers=summary.customers,
            sent_rows=summary.customers - summary.withheld_rows,
            withheld_rows=summary.withheld_rows,
            rerouted_rows=summary.rerouted_rows,
            decision_rows_without_consent_evidence=summary.rows_without_consent_evidence,
        ),
        withheld=dict(summary.withheld),
        rule_version=summary.rule_version,
        decisions_sha256=summary.decisions_sha256,
        gate_evidence_id=summary.evidence_id,
        gate_ledger_entry_hash=summary.ledger_entry_hash,
    )


CHECK_BEFORE_CONTACT: Final[AgentTool[RunInput, CheckBeforeContactOut]] = AgentTool(
    name="check_before_contact",
    kind=ToolKind.READ,
    description=(
        "Before a planned list is sent: how many customers the pre-send compliance gate allowed, moved to another "
        "channel or withheld, by reason code (consent, WhatsApp opt-in, DLT template, links). Counts only."
    ),
    input_model=RunInput,
    output_model=CheckBeforeContactOut,
    route_policy=_ARTEFACT_POLICY,
    mirrors=_ARTEFACT_ROUTE,
    evidence_label=lambda _out: "Descriptive",
    run=_check_before_contact,
    artefact_refs=lambda given, _out: (given.run_id,),
)


# ---------------------------------------------------------------------------
# draft_test_plan (propose)
# ---------------------------------------------------------------------------
class DraftOut(StrictBase):
    proposal_id: str
    kind: str
    status: str = Field(description="pending (an Approver decides) or refused (with codes).")
    payload_digest: str
    plan_hash: str | None = Field(description="What the plan will hash to once frozen.")
    codes: tuple[str, ...] = Field(description="Refusal codes; empty when pending.")


def _draft(given: DraftTestPlanInput, ctx: ToolContext) -> DraftOut:
    proposal: Proposal = draft_test_plan(given, ctx)
    return DraftOut(
        proposal_id=proposal.proposal_id,
        kind=proposal.kind,
        status=proposal.status,
        payload_digest=proposal.payload_digest,
        plan_hash=proposal.payload.get("plan_hash"),
        codes=proposal.codes,
    )


DRAFT_TEST_PLAN: Final[AgentTool[DraftTestPlanInput, DraftOut]] = AgentTool(
    name="draft_test_plan",
    kind=ToolKind.PROPOSE,
    description="Propose a campaign's test plan; it is frozen only when an Approver approves the proposal.",
    input_model=DraftTestPlanInput,
    output_model=DraftOut,
    route_policy=RoutePolicy(
        role=Role.ANALYST,
        action="campaigns.plan",
        purpose="register a campaign's test plan",
        object_type="campaign",
        object_param="campaign_id",
    ),
    mirrors=("POST", "/campaigns/{campaign_id}/plan"),
    evidence_label=lambda _out: "Estimated",
    run=_draft,
    artefact_refs=lambda given, out: tuple(ref for ref in (given.campaign_id, out.plan_hash) if ref),
)


ALL_TOOLS: Final[tuple[AgentTool[Any, Any], ...]] = (
    PLAN_TEST,
    CHECK_CONTACTABLE,
    VALUE_SEGMENT,
    MEASURE,
    AUDIT_CAMPAIGN,
    GET_PROOF_PACK,
    EVALUATE_POLICY,
    CHECK_BEFORE_CONTACT,
    DRAFT_TEST_PLAN,
)


def default_registry() -> ToolRegistry:
    """A registry holding every agent-safe tool (M132's, and M141's `check_before_contact`)."""
    registry = ToolRegistry()
    for tool in ALL_TOOLS:
        registry.register(tool)
    return registry
