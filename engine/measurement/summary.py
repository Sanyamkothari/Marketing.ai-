"""Warnings and the value proven to date, read from what was already measured (Plan J M105, DEC-1315).

The Results page needs two things beside the list of runs: what wants attention, the way an insight card does,
and a running total of value proven. Both are **computed only from artefacts that already exist**; nothing here
measures anything new, reads a customer row or invents a number.

**Cards.** Each appears only while its condition holds, names the artefacts it read, and carries only figures read
from them (a :class:`~engine.pilot.proof.Figure`, so the same independent re-check as the Value Proof Pack):

====================================  ==========================================================================
`CAMPAIGN_NO_CONTROL`                 `campaign.json`: nobody was held back in the population measured
`CAMPAIGN_EARLY_LOOK`                 `incrementality_report.json`: read before the test plan's analysis date
`PLAN_UNDERPOWERED`                   `test_plan.json` carries the warning, and there is no final result yet
`CONTROL_GROUP_CONTACTED`             `contact_readout.json`: held-back customers contacted, at least the tolerance
`DRIFT_DRIFTED`                       the scoring run's `drift.json` (or `uplift_drift.json`) says drifted
`CHALLENGER_READY`                    the model registry has a version waiting for approval (per use case)
`GROUP_BACKFIRED`                     the Value Proof Pack's own backfire rule (:mod:`engine.pilot.proof`, DEC-1314
                                      (h)), read from its proposals, not approved yet
`UPLIFT_NOT_BETTER_THAN_RISK`         the scoring run's `ranking_choice.json` (M96) says it did not beat risk
`EFFECT_FADING`                       the measured effect falls across a use case's cycles (:func:`fading_verdict`)
====================================  ==========================================================================

A campaign on generated data raises no card: its only line is the reason it is excluded below.

**The effect-fading rule.** A use case's measured cycles are its campaigns that are final, drawn at random by the
engine or verified, and not a programme, in the order they went out (the latest `fading_window` of them). Each
cycle's effect is read with its own noise, `se = (ci_high - ci_low) / (2 z)` from its stored range. The trend is
the inverse-variance weighted least-squares slope of the effect on the cycle number; the cycles are *fading* when
at least `fading_min_cycles` (3) are usable, the slope is negative, **its whole 95% range is below zero** and the
latest effect is below the first. That is one tail, so when the true effect never moves the chance of an alarm is
2.5% (`tests/statistical/test_fading_false_alarm.py`, nightly), and two cycles, however steep, are never enough.

**Value proven to date.** The sum of measured **lower bounds**, labelled exactly so ("at least ..., the sum of each
campaign's lower bound"), and only of campaigns the Value Proof Pack accepts and whose causal basis is
`engine_random` or `verified_random`. A lower bound is the low end of the 95% range of what the campaign changed,
read from the pack's own figures: a campaign that might have done harm adds a negative number. Units are never
mixed: yes/no outcomes ("extra outcomes", or "outcomes prevented" when the aim is fewer) and each amount are
totalled per outcome column, and rupees (the pack's net value after contacts and offers) have a total of their
own. **Customers are counted once:** campaigns that measure the same customers (the same scoring runs, or the
same assignment file of an audit) would add the same effect again, so only the latest measured is added and the
others are listed apart. Listed apart and never added: those, campaigns random only by the person's statement,
descriptive ones, and programme readouts (which cover the customers of the campaigns). Excluded with their
reason: generated data, an unfinished result, no control group. Every campaign of the installation is read.

**Traced.** The total is a :class:`~engine.pilot.proof.Figure` whose sources name every lower bound it adds;
:func:`build_summary` re-reads every source and every digit of its own words with
:func:`engine.pilot.proof.check_figures` and refuses a summary that does not resolve.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from statistics import NormalDist
from typing import TYPE_CHECKING, Final, Literal

from pydantic import AwareDatetime, Field

from engine.config import StrictBase
from engine.measurement.campaign import Campaign, CampaignKind
from engine.pilot.proof import (
    PROOF_NOT_MATURE,
    PROOF_NOT_TRACEABLE,
    PROOF_SYNTHETIC_DATA,
    ArtefactReader,
    Figure,
    FigureFormat,
    ProofLine,
    ProofNotFoundError,
    ProofRefusedError,
    ProofView,
    ProvenanceError,
    Source,
    build_proof,
    check_figures,
    derived,
    figures_of,
    format_value,
    free_text,
    safe_identifier,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from engine.registry import ModelRegistry
    from engine.storage import Storage

__all__ = [
    "CARD_CODES",
    "FADING_MIN_CYCLES",
    "SUMMARY_CODES",
    "SUMMARY_NOT_TRACEABLE",
    "ApartCampaign",
    "CampaignNote",
    "CampaignSummary",
    "CycleEffect",
    "FadingVerdict",
    "ProvenLine",
    "ProvenToDate",
    "ProvenTotal",
    "SummaryCard",
    "SummaryFact",
    "SummaryRules",
    "SummaryTraceError",
    "build_summary",
    "fading_verdict",
    "interval_se",
    "sum_figures",
]

# ---------------------------------------------------------------------------
# Codes (DEC-1315): each joins `engine.decide.codes.PLAN_J_CODES` with a `configs/pilot/help.yaml` entry.
# ---------------------------------------------------------------------------
CARD_CODES: Final[dict[str, str]] = {
    "no_control": "CAMPAIGN_NO_CONTROL",
    "early_look": "CAMPAIGN_EARLY_LOOK",
    "underpowered": "PLAN_UNDERPOWERED",
    "contamination": "CONTROL_GROUP_CONTACTED",
    "drift": "DRIFT_DRIFTED",
    "challenger_ready": "CHALLENGER_READY",
    "backfire": "GROUP_BACKFIRED",
    "uplift_not_better_than_risk": "UPLIFT_NOT_BETTER_THAN_RISK",
    "uplift_fading": "EFFECT_FADING",
}
"""The code each card carries. Three are codes the catalogue already explains."""
SUMMARY_NOT_TRACEABLE: Final[str] = "SUMMARY_NOT_TRACEABLE"
"""500: a number of the summary could not be traced back to the artefact it was read from; nothing is shown."""
SUMMARY_CODES: Final[frozenset[str]] = frozenset(
    {
        "CAMPAIGN_NO_CONTROL",
        "CAMPAIGN_EARLY_LOOK",
        "CONTROL_GROUP_CONTACTED",
        "CHALLENGER_READY",
        "GROUP_BACKFIRED",
        "EFFECT_FADING",
        SUMMARY_NOT_TRACEABLE,
    }
)
"""The codes M105 adds; `PLAN_UNDERPOWERED`, `DRIFT_DRIFTED` and `UPLIFT_NOT_BETTER_THAN_RISK` are reused."""

FADING_MIN_CYCLES: Final[int] = 3
"""Measured cycles a trend needs; fewer is never "fading", however steep."""
SCHEMA_VERSION: Final[int] = 1

CardKind = Literal[
    "no_control",
    "early_look",
    "underpowered",
    "contamination",
    "drift",
    "challenger_ready",
    "backfire",
    "uplift_not_better_than_risk",
    "uplift_fading",
]


class SummaryRules(StrictBase):
    """The few thresholds the cards use; defaults are the ones the DEC records."""

    contamination_share: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="Share of held-back customers contacted anyway at which the card appears.",
    )
    fading_min_cycles: int = Field(default=FADING_MIN_CYCLES, ge=3, description="Cycles a trend needs.")
    fading_window: int = Field(default=6, ge=3, description="The latest cycles of a use case that are read.")
    fading_confidence: float = Field(
        default=0.95,
        gt=0.5,
        lt=1.0,
        description="Confidence of the range of the trend that must lie below zero.",
    )


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------
class SummaryFact(StrictBase):
    """One labelled number of a card, a traced figure."""

    label: str
    value: Figure


class SummaryCard(StrictBase):
    """One thing that wants attention, with where it was read."""

    code: str = Field(description="A catalogue code with a plain-language entry.")
    kind: CardKind
    severity: Literal["warning", "info"]
    title: str
    text: str
    next_step: str = Field(description="What to do about it, in plain words.")
    campaign_id: str | None = Field(
        default=None, description="The campaign it is about; null for a use case."
    )
    campaign_name: Figure | None = None
    use_case_id: str | None = None
    facts: tuple[SummaryFact, ...] = ()
    read: tuple[str, ...] = Field(
        description="The artefacts it was read from (storage keys, or the registry)."
    )


class ProvenLine(StrictBase):
    """One campaign's lower bound, in one unit."""

    campaign_id: str
    campaign_name: Figure
    claim_label: str
    lower_bound: Figure


class ProvenTotal(StrictBase):
    """The sum of the lower bounds of one unit; never mixed with another unit."""

    unit: str = Field(
        description="`outcomes:<column>`, `prevented:<column>`, `amount:<column>` or `rupees`: one total per column."
    )
    unit_column: Figure | None = Field(default=None, description="The outcome column it is measured in.")
    total: Figure = Field(description="The sum, a figure whose sources are every lower bound added.")
    label: str = Field(description="`at least <total> <unit>, the sum of each campaign's lower bound`.")
    campaigns: tuple[ProvenLine, ...]


class CampaignNote(StrictBase):
    """A campaign that is not counted, or not priced, and why."""

    campaign_id: str
    campaign_name: Figure
    code: str | None = Field(default=None, description="The catalogue code of the reason, when it has one.")
    reason: str
    results_available_on: Figure | None = Field(
        default=None, description="The day it can be read as final, when an artefact records it."
    )
    results_available_label: str | None = Field(
        default=None, description="What that day is, in words; set exactly when the day is."
    )


class ApartCampaign(StrictBase):
    """A campaign listed beside the totals and never added to them."""

    campaign_id: str
    campaign_name: Figure
    kind: Literal["stated_random", "descriptive", "programme", "same_customers"]
    claim_label: str
    reason: str
    lower_bounds: tuple[ProvenLine, ...] = Field(
        default=(), description="What it shows, in its unit; empty for a descriptive one."
    )
    unit: str | None = None
    unit_label: str | None = Field(
        default=None, description="The unit of `lower_bounds` in words, e.g. `extra converted outcomes`."
    )
    counted_as: Figure | None = Field(
        default=None, description="For `same_customers`: the name of the campaign that is counted instead."
    )
    counted_as_id: str | None = None


class ProvenToDate(StrictBase):
    rule: str = Field(description="How the totals are made, in one sentence.")
    totals: tuple[ProvenTotal, ...]
    apart: tuple[ApartCampaign, ...]
    excluded: tuple[CampaignNote, ...]
    unpriced: tuple[CampaignNote, ...] = Field(
        description="Campaigns counted in a total above but not in rupees, with the reason."
    )


class CampaignSummary(StrictBase):
    """`GET /campaigns/summary`: what wants attention and the value proven to date; every number a traced figure."""

    schema_version: int = SCHEMA_VERSION
    cards: tuple[SummaryCard, ...]
    proven: ProvenToDate
    artefacts: tuple[str, ...] = Field(description="Every aggregate artefact read.")
    built_at: AwareDatetime


class SummaryTraceError(Exception):
    """Some figure of the summary does not resolve to its sources (`SUMMARY_NOT_TRACEABLE`)."""

    def __init__(self, failures: Sequence[str]) -> None:
        super().__init__(
            f"{len(failures)} figure(s) of the summary could not be traced: " + "; ".join(failures)
        )
        self.failures = tuple(failures)


# ---------------------------------------------------------------------------
# Pure helpers: the fading rule and the totals
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CycleEffect:
    """One cycle's measured effect and the noise it was read with."""

    value: float
    se: float


@dataclass(frozen=True)
class FadingVerdict:
    fading: bool
    slope: float | None
    slope_se: float | None
    reason: str


def interval_se(low: float, high: float, level: float) -> float | None:
    """The standard error behind a stored `level` range, `(high - low) / (2 z)`; None when it cannot be read."""
    if not (0.0 < level < 1.0) or not (math.isfinite(low) and math.isfinite(high)) or high <= low:
        return None
    return (high - low) / (2.0 * NormalDist().inv_cdf(0.5 + level / 2.0))


def fading_verdict(
    effects: Sequence[CycleEffect],
    *,
    min_cycles: int = FADING_MIN_CYCLES,
    confidence: float = 0.95,
) -> FadingVerdict:
    """Whether the effects, oldest first, fall by more than noise explains (the module docstring's rule)."""
    usable = [
        (position, effect)
        for position, effect in enumerate(effects)
        if math.isfinite(effect.value) and math.isfinite(effect.se) and effect.se > 0.0
    ]
    if len(usable) < min_cycles:
        return FadingVerdict(
            False, None, None, f"A trend needs at least {min_cycles} measured cycles with a readable range."
        )
    weights = [1.0 / (effect.se**2) for _, effect in usable]
    total = sum(weights)
    mean_x = sum(w * position for w, (position, _) in zip(weights, usable, strict=True)) / total
    sxx = sum(w * (position - mean_x) ** 2 for w, (position, _) in zip(weights, usable, strict=True))
    sxy = sum(
        w * (position - mean_x) * effect.value for w, (position, effect) in zip(weights, usable, strict=True)
    )
    slope = sxy / sxx
    slope_se = 1.0 / math.sqrt(sxx)
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    fading = slope < 0.0 and slope + z * slope_se < 0.0 and usable[-1][1].value < usable[0][1].value
    reason = (
        "The trend over the cycles is below zero by more than noise explains."
        if fading
        else "The cycles do not fall by more than noise explains."
    )
    return FadingVerdict(fading, slope, slope_se, reason)


_NAME: Final[re.Pattern[str]] = re.compile(r"s(\d+)")


def _shift(formula: str, offset: int) -> str:
    return _NAME.sub(lambda match: f"s{int(match.group(1)) + offset}", formula)


def sum_figures(figures: Sequence[Figure], fmt: FigureFormat) -> Figure:
    """The sum of `figures` as one figure: its sources are theirs, its formula adds theirs (names only)."""
    sources: list[Source] = []
    terms: list[str] = []
    value = 0.0
    for figure in figures:
        terms.append("(" + _shift(figure.formula or "s0", len(sources)) + ")")
        sources.extend(figure.sources)
        value += float(figure.value)
    return Figure(
        value=value,
        text=format_value(fmt, value),
        format=fmt,
        sources=tuple(sources),
        formula="+".join(terms),
    )


# ---------------------------------------------------------------------------
# Words (plain, no digit: every number is a figure beside them)
# ---------------------------------------------------------------------------
_RULE: Final[str] = (
    "Each total is the sum of every campaign's measured lower bound, so it is a floor, not an estimate. Only "
    "campaigns whose control group the engine drew at random, or whose random assignment it verified, are "
    "added, customers measured by more than one campaign are counted once, and each unit has a total of its "
    "own."
)
_APART: Final[dict[str, str]] = {
    "stated_random": (
        "The groups were random only by your statement, which the engine could not check, so what this campaign "
        "shows is listed here and is not added to a total."
    ),
    "descriptive": (
        "The groups were not chosen at random, so nothing in this campaign can be credited to it and it is not "
        "added to a total."
    ),
    "programme": (
        "A programme readout compares everyone outside the control group with it, so it covers the same "
        "customers as the campaigns; it is listed apart so that nothing is counted twice."
    ),
    "same_customers": (
        "It measures the same customers as the campaign named here, which is already counted, so it is not "
        "added again."
    ),
}
_EXCLUDED: Final[dict[str, str]] = {
    PROOF_SYNTHETIC_DATA: (
        "This campaign was measured on generated data, where the effect was planted, so it is not counted."
    ),
    PROOF_NOT_MATURE: "This campaign has no final result yet, so it is not counted.",
    PROOF_NOT_TRACEABLE: (
        "A number of this campaign could not be traced back to its record, so it is not counted."
    ),
    "no_lower_bound": "The groups were too small to compare, so no lower bound could be read for it.",
    "CAMPAIGN_NO_CONTROL": (
        "Nobody was held back, so what this campaign changed cannot be measured and it is never counted."
    ),
}
_AVAILABLE_LABEL: Final[str] = "Day the final result can be read"
_UNPRICED: Final[str] = (
    "No value inputs were entered for this campaign, or its cost could not be read, so its money is not counted."
)
_CLAIM_APART: Final[dict[str, str]] = {
    "stated_random": "Random by your statement, not verified",
    "descriptive": "Descriptive only",
}


@dataclass(frozen=True)
class _Words:
    title: str
    text: str
    next_step: str


_WORDS: Final[dict[CardKind, _Words]] = {
    "no_control": _Words(
        "No customers were held back",
        "Nobody was kept out of this campaign as a control group, so what it changed cannot be measured.",
        "Keep a random group of customers out of the next campaign so that its effect can be measured.",
    ),
    "early_look": _Words(
        "This result was read early",
        "The campaign was measured before the day its test plan fixed for the final reading, so the result can "
        "still move and is not a final verdict.",
        "Measure the campaign again on or after the planned day, and decide only then.",
    ),
    "underpowered": _Words(
        "The test may be too small for the effect you expect",
        "With the customers contacted and the customers kept back, an effect of the size you planned for would "
        "often be missed and read as no clear effect.",
        "Keep back more customers, contact more of them, or plan for a larger effect before the campaign goes out.",
    ),
    "contamination": _Words(
        "Customers kept back were contacted anyway",
        "Some customers in the control group were contacted, so the difference between the groups understates "
        "what the campaign does.",
        "Find out how they were reached and keep the control group out of every channel next time.",
    ),
    "drift": _Words(
        "The customers scored look different from the ones the model learned from",
        "The list for this campaign was made from customers who look quite different from those the model was "
        "trained on, so its ranking may be less reliable.",
        "Retrain the model on recent data before the next list is made.",
    ),
    "challenger_ready": _Words(
        "A new model is waiting for approval",
        "A newly trained model waits for someone to approve it before it is used for any list.",
        "Open the approvals to compare it with the model in use, then approve or decline it.",
    ),
    "backfire": _Words(
        "A group of customers did worse because of the campaign",
        "The measured difference for this group lies wholly on the harmful side, even allowing for the number "
        "of groups checked.",
        "Open the campaign's Value Proof Pack and, if you agree, approve leaving this group out of the next cycle.",
    ),
    "uplift_not_better_than_risk": _Words(
        "The model that picks who to contact does not beat ranking by risk",
        "The model that predicts who a campaign changes was checked on customers it had not seen and did not "
        "clearly beat plain risk ranking.",
        "Approve a risk model for this use case, or train the model on more customers, before the next list is "
        "made.",
    ),
    "uplift_fading": _Words(
        "The effect of the campaign is falling from one cycle to the next",
        "Across the cycles measured for this use case, the effect of the campaign has fallen by more than chance "
        "would explain.",
        "Refresh the offer or the message, check that the customers contacted have not changed, and keep the "
        "control group in place to see whether the effect recovers.",
    ),
}
_SEVERITY: Final[dict[CardKind, Literal["warning", "info"]]] = {
    "no_control": "warning",
    "early_look": "info",
    "underpowered": "warning",
    "contamination": "warning",
    "drift": "warning",
    "challenger_ready": "info",
    "backfire": "warning",
    "uplift_not_better_than_risk": "warning",
    "uplift_fading": "warning",
}
_RANKED_BY_RISK: Final[str] = " This contact list was ranked by the approved risk model instead."
_KEPT_UPLIFT: Final[str] = (
    " There was no approved risk model to fall back to, so the list keeps the model's own order: treat it "
    "with caution."
)
_STATED: Final[str] = "If the groups were random as you said: "


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------
_safe = safe_identifier
"""M104's own guard for an id that is one path segment (never `..`, a slash or empty)."""


@dataclass(frozen=True)
class _Candidate:
    """A proven campaign waiting to be added: of those measuring the same customers only the latest is."""

    rank: tuple[str, str, str]
    campaign: Campaign
    name: Figure
    view: ProofView
    bound: Figure
    net: Figure | None
    unit: str
    column: Figure


@dataclass
class _Cycle:
    campaign_id: str
    start: datetime
    report_key: str
    base: str
    good: bool
    effect: CycleEffect


class _Builder:
    def __init__(
        self,
        storage: Storage,
        registry: ModelRegistry | None,
        root: Path | None,
        now: datetime | None,
        rules: SummaryRules,
    ) -> None:
        self.storage = storage
        self.registry = registry
        self.root = root
        self.now = now
        self.rules = rules
        self.reader = ArtefactReader(storage)
        self.cards: list[SummaryCard] = []
        self.lines: dict[str, list[ProvenLine]] = {}
        self.unit_columns: dict[str, Figure] = {}
        self.apart: list[ApartCampaign] = []
        self.excluded: list[CampaignNote] = []
        self.unpriced: list[CampaignNote] = []
        self.cycles: dict[tuple[str, ...], list[_Cycle]] = {}
        self.view_artefacts: set[str] = set()
        self.candidates: dict[tuple[str, ...], list[_Candidate]] = {}
        self.carded: set[tuple[str, str]] = set()
        """`(kind, run id)` of the run-level cards already drawn: a run is carded once, however many campaigns use it."""

    # --- one campaign ---------------------------------------------------------------------------------
    def campaign(self, campaign: Campaign) -> None:
        cid = campaign.campaign_id
        if not _safe(cid):
            return
        record_key = f"campaigns/{cid}/campaign.json"
        name = self.reader.fig(record_key, "name", "text")
        if name is None:  # the campaign's files are gone (retention): nothing to read
            return
        view: ProofView | None = None
        refusal: ProofRefusedError | None = None
        traced = True
        try:
            view = build_proof(self.storage, cid, root=self.root, now=self.now)
        except ProofNotFoundError:
            return
        except ProofRefusedError as exc:
            refusal = exc
        except ProvenanceError:
            traced = False
        if refusal is not None and refusal.code == PROOF_SYNTHETIC_DATA:
            self._exclude(cid, name, PROOF_SYNTHETIC_DATA, None)
            return
        self._cards_of(campaign, name)
        if view is not None:
            self.view_artefacts.update(view.artefacts)
            self._backfire(campaign, name, view)
            self._count(campaign, name, view)
        elif refusal is not None:
            if self._nobody_held_back(campaign):  # it can never be measured: not "no final result yet"
                self._exclude(cid, name, CARD_CODES["no_control"], None)
            else:
                self._exclude(cid, name, refusal.code, self._available(campaign))
        elif not traced:
            self._exclude(cid, name, PROOF_NOT_TRACEABLE, None)

    def _available(self, campaign: Campaign) -> Figure | None:
        """The day the final result can be read, as a figure, from the artefact that records it."""
        cid = campaign.campaign_id
        report = f"campaigns/{cid}/incrementality_report.json"
        if self.reader.get(report, "early_look") is True:
            return self.reader.fig(f"campaigns/{cid}/test_plan.json", "analysis_date", "date")
        return self.reader.fig(report, "results_available_on", "date")

    def _nobody_held_back(self, campaign: Campaign) -> bool:
        held = self.reader.get(f"campaigns/{campaign.campaign_id}/campaign.json", "counts.intended_holdout")
        return campaign.kind is not CampaignKind.PROGRAMME and type(held) is int and held == 0

    def _exclude(self, cid: str, name: Figure, code: str, available: Figure | None) -> None:
        self.excluded.append(
            CampaignNote(
                campaign_id=cid,
                campaign_name=name,
                code=code if code.isupper() else None,
                reason=_EXCLUDED.get(code, _EXCLUDED[PROOF_NOT_MATURE]),
                results_available_on=available,
                results_available_label=_AVAILABLE_LABEL if available is not None else None,
            )
        )

    def _card(
        self,
        kind: CardKind,
        *,
        campaign_id: str | None,
        name: Figure | None,
        facts: Sequence[tuple[str, Figure | None]] = (),
        read: Sequence[str],
        use_case_id: str | None = None,
        title: str | None = None,
        extra: str = "",
    ) -> None:
        words = _WORDS[kind]
        self.cards.append(
            SummaryCard(
                code=CARD_CODES[kind],
                kind=kind,
                severity=_SEVERITY[kind],
                title=title or words.title,
                text=words.text + extra,
                next_step=words.next_step,
                campaign_id=campaign_id,
                campaign_name=name,
                use_case_id=use_case_id,
                facts=tuple(
                    SummaryFact(label=label, value=figure) for label, figure in facts if figure is not None
                ),
                read=tuple(read),
            )
        )

    # --- cards read from the campaign's own files ---------------------------------------------------------
    def _cards_of(self, campaign: Campaign, name: Figure) -> None:
        reader, cid = self.reader, campaign.campaign_id
        record = f"campaigns/{cid}/campaign.json"
        report = f"campaigns/{cid}/incrementality_report.json"
        plan = f"campaigns/{cid}/test_plan.json"
        contacts = f"campaigns/{cid}/contact_readout.json"

        if self._nobody_held_back(campaign):
            self._card(
                "no_control",
                campaign_id=cid,
                name=name,
                use_case_id=campaign.use_case_id,
                facts=[
                    (
                        "Customers meant to be contacted",
                        reader.fig(record, "counts.intended_treated", "count"),
                    ),
                    ("Customers held back", reader.fig(record, "counts.intended_holdout", "count")),
                ],
                read=[record],
            )

        measured = isinstance(reader.doc(report), dict)
        early = reader.get(report, "early_look") is True
        if early:
            analysis = reader.fig(plan, "analysis_date", "date")
            self._card(
                "early_look",
                campaign_id=cid,
                name=name,
                use_case_id=campaign.use_case_id,
                facts=[("Day the final result can be read", analysis)],
                read=[report, plan] if analysis is not None else [report],
            )

        warnings = reader.get(plan, "warnings")
        if isinstance(warnings, list) and "PLAN_UNDERPOWERED" in warnings and (not measured or early):
            self._card(
                "underpowered",
                campaign_id=cid,
                name=name,
                use_case_id=campaign.use_case_id,
                facts=[
                    ("Chance of seeing the effect planned for", reader.fig(plan, "achieved_power", "share")),
                    ("Chance planned for", reader.fig(plan, "power", "share")),
                ],
                read=[plan],
            )

        contamination = reader.get(contacts, "contamination")
        if (
            isinstance(contamination, int | float)
            and not isinstance(contamination, bool)
            and contamination >= self.rules.contamination_share
        ):
            self._card(
                "contamination",
                campaign_id=cid,
                name=name,
                use_case_id=campaign.use_case_id,
                facts=[
                    ("Held-back customers contacted anyway", reader.fig(contacts, "contamination", "share"))
                ],
                read=[contacts],
            )

        drifted = list(self._drifted_files(campaign))
        if drifted:
            self._card("drift", campaign_id=cid, name=name, use_case_id=campaign.use_case_id, read=drifted)

        for run_id in campaign.run_ids:
            if not _safe(run_id) or ("uplift_not_better_than_risk", run_id) in self.carded:
                continue
            ranking = f"runs/{run_id}/ranking_choice.json"
            if reader.get(ranking, "code") == "UPLIFT_NOT_BETTER_THAN_RISK":
                self.carded.add(("uplift_not_better_than_risk", run_id))
                fell_back = reader.get(ranking, "ranking") == "propensity_model"
                self._card(
                    "uplift_not_better_than_risk",
                    campaign_id=cid,
                    name=name,
                    use_case_id=campaign.use_case_id,
                    read=[ranking],
                    extra=_RANKED_BY_RISK if fell_back else _KEPT_UPLIFT,
                )

    def _drifted_files(self, campaign: Campaign) -> Iterator[str]:
        """The drift reports of the campaign's runs that say drifted, run by run, each run once (newest campaign)."""
        for run_id in campaign.run_ids:
            if not _safe(run_id) or ("drift", run_id) in self.carded:
                continue
            plain = f"runs/{run_id}/drift.json"
            found = False
            if self.reader.get(plain, "status") == "drifted":
                found = True
                yield plain
            moved = f"runs/{run_id}/uplift_drift.json"
            if (
                self.reader.get(moved, "features.status") == "drifted"
                or self.reader.get(moved, "treatment.status") == "outside_tolerance"
            ):
                found = True
                yield moved
            if found:
                self.carded.add(("drift", run_id))

    def _backfire(self, campaign: Campaign, name: Figure, view: ProofView) -> None:
        cid = campaign.campaign_id
        for proposal in view.proposals:
            if proposal.status != "proposed":
                continue
            stated = view.claim == "stated_random"
            self._card(
                "backfire",
                campaign_id=cid,
                name=name,
                use_case_id=campaign.use_case_id,
                facts=[
                    ("Group", proposal.segment),
                    ("Least harmful end of its range", proposal.worst_case),
                ],
                read=[f"campaigns/{cid}/segment_effects.json", f"campaigns/{cid}/incrementality_report.json"],
                title=(
                    (_STATED + _WORDS["backfire"].title[0].lower() + _WORDS["backfire"].title[1:])
                    if stated
                    else None
                ),
            )

    # --- the totals ---------------------------------------------------------------------------------------
    def _count(self, campaign: Campaign, name: Figure, view: ProofView) -> None:
        cid = campaign.campaign_id
        unit, column = _unit(view)
        benefit = _benefit_line(view)
        if view.kind == "programme" or view.claim != "proven":
            kind: Literal["stated_random", "descriptive", "programme"] = (
                "programme"
                if view.kind == "programme"
                else ("stated_random" if view.claim == "stated_random" else "descriptive")
            )
            bounds: tuple[ProvenLine, ...] = ()
            if view.claim != "descriptive" and benefit is not None and benefit.low is not None:
                bounds = (
                    ProvenLine(
                        campaign_id=cid,
                        campaign_name=name,
                        claim_label=view.claim_label,
                        lower_bound=benefit.low,
                    ),
                )
            self.apart.append(
                ApartCampaign(
                    campaign_id=cid,
                    campaign_name=name,
                    kind=kind,
                    claim_label=view.claim_label,
                    reason=_APART[kind],
                    lower_bounds=bounds,
                    unit=unit if bounds else None,
                    unit_label=_unit_words(unit, column) if bounds else None,
                )
            )
            return
        if benefit is None or benefit.low is None:
            self._exclude(cid, name, "no_lower_bound", None)
            return
        net = _net_line(view)
        self.candidates.setdefault(self._customers_of(campaign), []).append(
            _Candidate(
                rank=(str(view.measured_on.value), campaign.created_at.isoformat(), cid),
                campaign=campaign,
                name=name,
                view=view,
                bound=benefit.low,
                net=net.low if net is not None else None,
                unit=unit,
                column=column,
            )
        )

    def _customers_of(self, campaign: Campaign) -> tuple[str, ...]:
        """Who the campaign measures: its scoring runs, or an audit's assignment file; else only itself.

        Two campaigns with the same key read the same customers, so what they measured is the same effect."""
        cid = campaign.campaign_id
        if campaign.kind is CampaignKind.EXTERNAL:
            upload = self.reader.get(f"campaigns/{cid}/audit.json", "assignment_upload_id")
            if not _safe(upload) and campaign.outcomes is not None:
                upload = campaign.outcomes.upload_id
            if _safe(upload):
                return ("upload", str(upload))
        elif campaign.run_ids and all(_safe(run_id) for run_id in campaign.run_ids):
            return ("runs", *sorted(campaign.run_ids))
        return ("campaign", cid)

    def settle(self) -> None:
        """Add each set of customers once, by its latest measured campaign; list the rest apart."""
        for found in self.candidates.values():
            ordered = sorted(found, key=lambda candidate: candidate.rank, reverse=True)
            chosen = ordered[0]
            self._add(chosen.unit, chosen.campaign.campaign_id, chosen.name, chosen.view, chosen.bound)
            self.unit_columns[chosen.unit] = chosen.column
            if chosen.net is None:
                self.unpriced.append(
                    CampaignNote(
                        campaign_id=chosen.campaign.campaign_id, campaign_name=chosen.name, reason=_UNPRICED
                    )
                )
            else:
                self._add("rupees", chosen.campaign.campaign_id, chosen.name, chosen.view, chosen.net)
            # The cycle this campaign is, for the fading rule: one per set of customers, never the same effect twice.
            self._cycle(chosen.campaign, chosen.view)
            for other in ordered[1:]:
                self.apart.append(
                    ApartCampaign(
                        campaign_id=other.campaign.campaign_id,
                        campaign_name=other.name,
                        kind="same_customers",
                        claim_label=other.view.claim_label,
                        reason=_APART["same_customers"],
                        counted_as=chosen.name,
                        counted_as_id=chosen.campaign.campaign_id,
                    )
                )

    def _add(self, unit: str, cid: str, name: Figure, view: ProofView, bound: Figure) -> None:
        self.lines.setdefault(unit, []).append(
            ProvenLine(campaign_id=cid, campaign_name=name, claim_label=view.claim_label, lower_bound=bound)
        )

    # --- the fading rule -----------------------------------------------------------------------------------
    def _cycle(self, campaign: Campaign, view: ProofView) -> None:
        if campaign.use_case_id is None or view.kind == "programme":
            return
        key = f"campaigns/{campaign.campaign_id}/incrementality_report.json"
        base = "absolute_lift"
        if view.outcome_kind == "continuous":
            base = (
                "adjusted_interval"
                if isinstance(self.reader.get(key, "adjusted_interval"), dict)
                else ("mean_difference_ci")
            )
        interval = self.reader.get(key, base)
        if not isinstance(interval, dict):
            return
        value, low, high, level = (
            _number(interval.get(n)) for n in ("value", "ci_low", "ci_high", "confidence_level")
        )
        if value is None or low is None or high is None or level is None:
            return
        se = interval_se(low, high, level)
        if se is None:
            return
        oriented = value if view.outcome_is_good else -value
        group = (
            campaign.use_case_id,
            view.outcome_kind,
            str(view.outcome.value),
            base,
            str(view.outcome_is_good),
        )
        self.cycles.setdefault(group, []).append(
            _Cycle(
                campaign_id=campaign.campaign_id,
                start=campaign.treatment_start,
                report_key=key,
                base=base,
                good=view.outcome_is_good,
                effect=CycleEffect(value=oriented, se=se),
            )
        )

    def fading(self, names: dict[str, Figure]) -> None:
        rules = self.rules
        for group, found in self.cycles.items():
            ordered = sorted(found, key=lambda cycle: (cycle.start, cycle.campaign_id))[
                -rules.fading_window :
            ]
            verdict = fading_verdict(
                [cycle.effect for cycle in ordered],
                min_cycles=rules.fading_min_cycles,
                confidence=rules.fading_confidence,
            )
            if not verdict.fading:
                continue
            first, latest = ordered[0], ordered[-1]
            fmt: FigureFormat = "signed_amount" if group[1] == "continuous" else "points"
            self._card(
                "uplift_fading",
                campaign_id=latest.campaign_id,
                name=names.get(latest.campaign_id),
                facts=[
                    ("Difference in the first cycle read", self._effect(first, fmt)),
                    ("Difference in the latest cycle", self._effect(latest, fmt)),
                ],
                read=[cycle.report_key for cycle in ordered],
                use_case_id=group[0],
            )

    def _effect(self, cycle: _Cycle, fmt: FigureFormat) -> Figure | None:
        figure = self.reader.fig(cycle.report_key, f"{cycle.base}.value", fmt)
        return figure if cycle.good else derived([figure], "-s0", fmt)

    # --- waiting models ------------------------------------------------------------------------------------
    def challengers(self, use_cases: Sequence[str]) -> None:
        if self.registry is None:
            return
        from engine.contracts import ModelStatus

        for use_case_id in use_cases:
            waiting = [
                version
                for version in self.registry.list_versions(use_case_id)
                if version.status is ModelStatus.PENDING_APPROVAL
            ]
            if waiting:
                self._card(
                    "challenger_ready",
                    campaign_id=None,
                    name=None,
                    use_case_id=use_case_id,
                    read=[f"model registry: {waiting[0].model_id}"],
                )

    # --- the answer ------------------------------------------------------------------------------------------
    def totals(self) -> tuple[ProvenTotal, ...]:
        def order(unit: str) -> tuple[int, str]:
            return (_UNIT_ORDER.get(unit.partition(":")[0], len(_UNIT_ORDER)), unit)

        out: list[ProvenTotal] = []
        for unit in sorted(self.lines, key=order):
            lines = self.lines[unit]
            kind = unit.partition(":")[0]
            fmt: FigureFormat = "inr" if kind == "rupees" else "amount" if kind == "amount" else "count"
            total = sum_figures([line.lower_bound for line in lines], fmt)
            column = self.unit_columns.get(unit)
            out.append(
                ProvenTotal(
                    unit=unit,
                    unit_column=column,
                    total=total,
                    label=f"at least {total.text} {_unit_words(unit, column)}, the sum of each campaign's lower bound",
                    campaigns=tuple(lines),
                )
            )
        return tuple(out)


_UNIT_ORDER: Final[dict[str, int]] = {"outcomes": 0, "prevented": 1, "amount": 2, "rupees": 3}


def _unit_words(unit: str, column: Figure | None) -> str:
    """The unit in plain words after `at least <n>`; a yes/no outcome is named by its column."""
    kind = unit.partition(":")[0]
    name = column.text if column is not None else unit.partition(":")[2]
    if kind == "rupees":
        return "net of what contacts and offers cost"
    if kind == "outcomes":
        return f"extra {name} outcomes"
    if kind == "prevented":
        return f"{name} outcomes prevented"
    return f"in {name}"


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _unit(view: ProofView) -> tuple[str, Figure]:
    """The total a campaign adds to: one per outcome column, so two different yes/no outcomes are never summed."""
    if view.outcome_kind == "continuous":
        kind = "amount"
    else:
        kind = "outcomes" if view.outcome_is_good else "prevented"
    return f"{kind}:{view.outcome.value}", view.outcome


def _benefit_line(view: ProofView) -> ProofLine | None:
    """The gain the campaign made in its own outcome, turned so up is good: the last line of section 3."""
    section = next((s for s in view.sections if s.key == "incremental"), None)
    if section is None or section.status != "measured" or not section.lines:
        return None
    line = section.lines[-1]
    return line if line.value is not None and line.low is not None else None


def _net_line(view: ProofView) -> ProofLine | None:
    """The pack's net value in rupees (section 9), when it priced the campaign."""
    section = next((s for s in view.sections if s.key == "net_value"), None)
    if section is None or section.status != "measured":
        return None
    return next((line for line in section.lines if line.label == "Net value" and line.low is not None), None)


_UNPRINTED: Final[frozenset[str]] = frozenset(
    {
        "campaign_id",
        "use_case_id",
        "read",
        "artefacts",
        "unit",
        "code",
        "kind",
        "schema_version",
        "counted_as_id",
    }
)
"""Fields of the summary that are identifiers or artefact names, never read as words."""


def build_summary(
    storage: Storage,
    campaigns: Sequence[Campaign],
    *,
    registry: ModelRegistry | None = None,
    root: Path | None = None,
    now: datetime | None = None,
    rules: SummaryRules | None = None,
) -> CampaignSummary:
    """What wants attention and the value proven to date, over `campaigns` (newest first), every figure verified.

    Reads only aggregate artefacts (the module docstring) and the model `registry` when one is given; without
    it no challenger card can be drawn. Raises `SummaryTraceError` when a figure does not resolve.
    """
    from engine.utils.time import utc_now

    builder = _Builder(storage, registry, root, now, rules or SummaryRules())
    for campaign in campaigns:
        builder.campaign(campaign)
    builder.settle()
    names = {line.campaign_id: line.campaign_name for lines in builder.lines.values() for line in lines}
    builder.fading(names)
    builder.challengers(list(dict.fromkeys(c.use_case_id for c in campaigns if c.use_case_id is not None)))
    summary = CampaignSummary(
        cards=tuple(builder.cards),
        proven=ProvenToDate(
            rule=_RULE,
            totals=builder.totals(),
            apart=tuple(builder.apart),
            excluded=tuple(builder.excluded),
            unpriced=tuple(builder.unpriced),
        ),
        artefacts=tuple(sorted(set(builder.reader.read) | builder.view_artefacts)),
        built_at=now or utc_now(),
    )
    failures = check_figures(list(figures_of(summary)), list(free_text(summary, _UNPRINTED)), storage)
    if failures:
        raise SummaryTraceError(failures)
    return summary
