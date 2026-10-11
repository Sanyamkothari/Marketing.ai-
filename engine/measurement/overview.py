"""What the Overview page shows, in one answer (Plan M M175, DEC-1605).

The Overview is the first page of the campaign-centred layout. It needs four things and a marketer should not wait for
four calls: what needs attention, the value proven to date, the campaigns that are live, and whether a new person has
done the first three things. Every one of them already exists, so this module **computes nothing new**. It only picks
from, and reshapes, answers other functions already give:

====================================  ==========================================================================
`cards`                               the first five of :func:`~engine.measurement.summary.build_summary`'s cards,
                                      in its order, each with its own traced figures (``cards_total`` says how many
                                      there are, so a sixth is never silently dropped)
`headline`                            one of the summary's totals, untouched: the rupee total when there is one
                                      (the value a campaign earned after contacts and offers), else the first
                                      total in its own unit. A floor: the sum of each campaign's lower bound
`campaigns`                           the first five campaigns that are not yet measured (``CampaignStatus.LIVE``),
                                      newest first, each with its status, next step and results date
`checklist`                           three yes/no facts: a connection or an upload exists, a campaign exists, a
                                      campaign has results (measured, or outcomes added)
====================================  ==========================================================================

**A campaign's status here.** Read from the record and its registered test plan, nothing else, by the one rule the
Campaigns pages use too (Plan M DEC-1606 (b), :func:`campaign_state`): ``draft`` (our own campaign, no plan and nothing
in yet, before its results date), ``live``, ``waiting_for_results`` (the results date has come) or ``measured``. The
results date is the plan's analysis date, else the end of the outcome window. Only campaigns not yet measured are
listed, so ``measured`` is not seen on this page.

``next_step`` says what to do about it: ``wait`` (results are not due), ``add_results`` (the date has come, no
outcomes yet), ``measure`` (outcomes are in, not yet read), ``plan`` (a draft, or no date to wait for) or ``read``
(measured). The page turns
the code and the date into a sentence; no sentence is made here, so the same code cannot be worded two ways.

A traced figure that does not resolve (:class:`~engine.measurement.summary.SummaryTraceError`) leaves the cards and the
headline out and sets ``summary_checked`` to false; the campaigns and the checklist are still drawn. The route says why
in the log, never a number it could not trace.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Final, Literal

from pydantic import AwareDatetime, Field

from engine.config import StrictBase
from engine.measurement.campaign import Campaign, CampaignKind, CampaignStatus
from engine.measurement.plan import TestPlan
from engine.measurement.summary import CampaignSummary, ProvenTotal, SummaryCard, unit_words
from engine.pilot.proof import Figure

__all__ = [
    "CAMPAIGNS_SHOWN",
    "CARDS_SHOWN",
    "Overview",
    "OverviewCampaign",
    "OverviewChecklist",
    "OverviewHeadline",
    "build_overview",
    "campaign_state",
    "headline_total",
    "results_date",
]

CARDS_SHOWN: Final[int] = 5
"""The most cards the Overview draws (Plan M §3: up to five)."""
CAMPAIGNS_SHOWN: Final[int] = 5
"""The most live campaigns the Overview lists (Plan M §3: up to five)."""

CampaignState = Literal["draft", "live", "waiting_for_results", "measured"]
NextStep = Literal["wait", "add_results", "measure", "plan", "read"]


class OverviewHeadline(StrictBase):
    """The one number: value proven to date, a floor, in one unit."""

    unit: str = Field(
        description="`rupees`, or the summary's own unit (`outcomes:<column>`, `amount:<column>`...)."
    )
    total: Figure = Field(
        description="The summary's total, a traced figure whose sources are every lower bound added."
    )
    label: str = Field(
        description="The summary's own sentence: `at least <total> <unit>, the sum of each campaign's lower bound`."
    )
    unit_words: str = Field(
        description="The unit in plain words, the part of `label` after the total (`extra <column> outcomes`, `in <column>`)."
    )
    rule: str = Field(description="How the totals are made, in the summary's one sentence.")
    campaigns_counted: int = Field(description="How many campaigns are added in this total.")


class OverviewCampaign(StrictBase):
    """One campaign that is not yet measured."""

    campaign_id: str
    name: str = Field(description="The campaign's name, as recorded.")
    use_case_id: str | None = None
    status: CampaignState
    next_step: NextStep
    results_date: date | None = Field(
        default=None,
        description=(
            "The results date: the registered test plan's analysis date; without a plan, the day the outcome window "
            "closes (treatment_start + outcome_window_days); null with neither."
        ),
    )


class OverviewChecklist(StrictBase):
    """The first-visit checklist: three facts, each read from real state."""

    connected: bool = Field(description="A saved connection or an uploaded file exists.")
    campaign: bool = Field(description="At least one campaign exists.")
    results: bool = Field(description="At least one campaign is measured or has its outcomes added.")


class Overview(StrictBase):
    """`GET /overview`: the four parts of the Overview page."""

    schema_version: int = 1
    cards: tuple[SummaryCard, ...] = Field(
        description=f"Up to {CARDS_SHOWN} of the summary's cards, in its order."
    )
    cards_total: int = Field(description="How many cards the summary has in all.")
    headline: OverviewHeadline | None = Field(
        default=None, description="Value proven to date; null while no campaign is proven."
    )
    summary_checked: bool = Field(
        description="False when a figure of the summary could not be traced: cards and headline are then left out."
    )
    campaigns: tuple[OverviewCampaign, ...] = Field(
        description=f"Up to {CAMPAIGNS_SHOWN} campaigns not yet measured, newest first."
    )
    campaigns_total: int = Field(description="How many campaigns are not yet measured.")
    checklist: OverviewChecklist
    built_at: AwareDatetime


def headline_total(summary: CampaignSummary) -> ProvenTotal | None:
    """The total the headline shows: rupees when the summary has one, else its first total, else None."""
    totals = summary.proven.totals
    return next((t for t in totals if t.unit == "rupees"), totals[0] if totals else None)


def results_date(campaign: Campaign, plan: TestPlan | None) -> date | None:
    """The day a campaign's result can be read: the test plan's analysis date; without a plan, the day every
    customer's outcome window has closed (`treatment_start` + `outcome_window_days`, in UTC); else None."""
    if plan is not None:
        return plan.analysis_date
    if campaign.outcome_window_days is None:
        return None
    start = campaign.treatment_start.astimezone(UTC)
    return (start + timedelta(days=campaign.outcome_window_days)).date()


def campaign_state(
    campaign: Campaign, plan: TestPlan | None, today: date, *, report: bool | None = None
) -> tuple[CampaignState, NextStep, date | None]:
    """A campaign's status, its next step and its results date (:func:`results_date`), from the record and its plan.

    The ONE status rule (Plan M DEC-1606 (b)), the same as the Campaigns pages' `ui/modules/campaigns/status.js`
    `campaignStatus()`; both are pinned to `tests/fixtures/campaigns/status_cases.json`. In order:

    * ``measured`` - the final result is stored (an early look never counts: the record stays live);
    * ``waiting_for_results`` - the results date has come;
    * ``draft`` - one of our own (scored) campaigns with no test plan, no results file and no stored report, whose
      results date has not come;
    * ``live`` - anything else. A results file or an early look before the date does not change it (the date decides).

    `report` says whether a report is stored (an early look included); None reads it from the record, which sets
    `measured_at` whenever one is stored.
    """
    when = results_date(campaign, plan)
    if campaign.status == CampaignStatus.MEASURED:
        return "measured", "read", when
    if when is not None and when <= today:
        return "waiting_for_results", ("measure" if campaign.outcomes is not None else "add_results"), when
    stored = campaign.measured_at is not None if report is None else report
    ours = campaign.kind == CampaignKind.SCORED
    if ours and plan is None and campaign.test_plan_hash is None and campaign.outcomes is None and not stored:
        return "draft", "plan", when
    return "live", ("wait" if when is not None else "plan"), when


def build_overview(
    *,
    summary: CampaignSummary | None,
    campaigns: Sequence[Campaign],
    plan_of: Callable[[str], TestPlan | None],
    connected: bool,
    now: datetime,
) -> Overview:
    """The Overview over `campaigns` (newest first) and an already verified `summary` (None: it could not be traced).

    `plan_of(campaign_id)` is the registered test plan in force, or None; it is asked only for the campaigns that are
    listed. `connected` is the one fact this module cannot read (the stores differ): a connection or an upload exists.
    """
    live = [c for c in campaigns if c.status == CampaignStatus.LIVE]
    rows: list[OverviewCampaign] = []
    for campaign in live[:CAMPAIGNS_SHOWN]:
        state, step, when = campaign_state(campaign, plan_of(campaign.campaign_id), now.date())
        rows.append(
            OverviewCampaign(
                campaign_id=campaign.campaign_id,
                name=campaign.name,
                use_case_id=campaign.use_case_id,
                status=state,
                next_step=step,
                results_date=when,
            )
        )
    headline = None
    cards: tuple[SummaryCard, ...] = ()
    cards_total = 0
    if summary is not None:
        cards, cards_total = summary.cards[:CARDS_SHOWN], len(summary.cards)
        total = headline_total(summary)
        if total is not None:
            headline = OverviewHeadline(
                unit=total.unit,
                total=total.total,
                label=total.label,
                unit_words=unit_words(total.unit, total.unit_column),
                rule=summary.proven.rule,
                campaigns_counted=len(total.campaigns),
            )
    return Overview(
        cards=cards,
        cards_total=cards_total,
        headline=headline,
        summary_checked=summary is not None,
        campaigns=tuple(rows),
        campaigns_total=len(live),
        checklist=OverviewChecklist(
            connected=connected,
            campaign=bool(campaigns),
            results=any(c.status == CampaignStatus.MEASURED or c.outcomes is not None for c in campaigns),
        ),
        built_at=now,
    )
