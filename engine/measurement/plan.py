"""The registered test plan (Plan J M94, DEC-1304 (g), (h)): decide how a campaign is judged before it is.

Peeking and moving goalposts turn a real effect into a disputed one: read the result every week and
stop on a good day, or change the outcome window once the numbers are in, and a 95 % interval stops
meaning 95 %. So the plan is fixed - and its hash written to the audit trail - before the outcomes are
read: the metric and its column, the outcome kind, an optional covariate (pre-registered now, used by
M102's adjusted estimate), the holdout and explore shares and the population they come from, the
outcome window, the analysis date and any secondary dates, and the detectable effect.

**Frozen.** `freeze_plan` turns what the person decided (`TestPlanInput`) and what the campaign's
assignment says (`RealisedPopulation`) into a `TestPlan` whose `plan_hash` is the SHA-256 of its
content - every field except the hash itself and who registered it when - so the same decision on the
same campaign always hashes the same. Re-registering an identical plan changes nothing; a different
one is refused (`TEST_PLAN_EXISTS`) and goes through an *amendment*: version n+1, with `amends` set to
the hash it replaces and a reason, and every version kept.

**Checked at measurement.** `plan_differences` compares what is about to be measured - the realised
holdout share and population, the outcome window, column, value and kind, the covariate - with the
plan in force; any difference refuses the measurement (`TEST_PLAN_CHANGED`) until it is amended, in
the open. The population is the one exception to an exact comparison: erasure (DEC-741) removes a
customer's row from the campaign's assignment, and that is nobody moving the goalposts. So the planned
population less a few customers - neither arm larger than planned, at most `ERASURE_ALLOWANCE_SHARE` of
it gone (and at least one customer may go) - is the planned population; past that, the head count is
compared exactly and the holdout share within `HOLDOUT_TOLERANCE`.

A read before `analysis_date` is an *early look*: the numbers are shown, labelled, with no verdict
(`is_early_look`).

**Underpowered is a warning.** When the plan gives a detectable effect and an expected rate, the
power of the planned arms is computed with M93's `engine.measurement.planner.achieved_power` - the
same two-proportion test as `engine.uplift.power.power_of_lift` (DEC-1230), which the planner agrees
with except for the far tail in very small groups - and below the plan's power the plan carries
`PLAN_UNDERPOWERED`. It never blocks: a small test honestly labelled is better than none. The same
planner computes the points behind the "Plan the test" slider (`GET /campaigns/{id}/plan-preview`),
so the card and the frozen plan never disagree; nothing here invents a point the server did not compute.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from pydantic import AwareDatetime, Field, model_validator

from engine.config import StrictBase
from engine.contracts import Artefact

if TYPE_CHECKING:
    import pandas as pd

__all__ = [
    "ERASURE_ALLOWANCE_SHARE",
    "HOLDOUT_TOLERANCE",
    "PLAN_UNDERPOWERED",
    "TEST_PLAN_CHANGED",
    "TEST_PLAN_EXISTS",
    "TEST_PLAN_INVALID",
    "TEST_PLAN_NOT_FOUND",
    "PlanDifference",
    "RealisedPopulation",
    "TestPlan",
    "TestPlanAmendment",
    "TestPlanChangedError",
    "TestPlanInput",
    "freeze_plan",
    "is_early_look",
    "plan_differences",
    "plan_hash",
    "plan_inputs",
    "population_kept",
    "realised_population",
]

TEST_PLAN_EXISTS: Final[str] = "TEST_PLAN_EXISTS"
TEST_PLAN_CHANGED: Final[str] = "TEST_PLAN_CHANGED"
TEST_PLAN_NOT_FOUND: Final[str] = "TEST_PLAN_NOT_FOUND"
TEST_PLAN_INVALID: Final[str] = "TEST_PLAN_INVALID"
PLAN_UNDERPOWERED: Final[str] = "PLAN_UNDERPOWERED"

_HASH_EXCLUDED: Final[frozenset[str]] = frozenset(
    {"plan_hash", "registered_at", "registered_by", "schema_version"}
)
_SHARE_DIGITS: Final[int] = 6
ERASURE_ALLOWANCE_SHARE: Final[float] = 0.01
"""Share of the planned population that may have gone (erased customers) and still be the plan's."""
HOLDOUT_TOLERANCE: Final[float] = 0.005
"""How far the held-back share may move (0.5 percentage points) before it counts as a change."""


class TestPlanInput(StrictBase):
    """What the person decides when registering a test plan (`POST /campaigns/{id}/plan`)."""

    __test__ = False  # not a pytest class, whatever its name says

    metric: Annotated[str, Field(min_length=1, max_length=200)] = Field(
        description='What is measured, in words: "reactivated within 90 days".'
    )
    outcome_column: Annotated[str, Field(min_length=1, max_length=200)] = Field(
        description="The outcome column of the outcomes file."
    )
    positive_label: str | None = Field(default=None, description="The value that counts as a conversion.")
    outcome_kind: Literal["binary", "continuous"] = Field(
        default="binary", description="`binary` today; `continuous` (revenue) arrives with M102."
    )
    covariate_column: str | None = Field(
        default=None, description="A pre-campaign column for the adjusted estimate (M102), registered now."
    )
    outcome_window_days: int | None = Field(
        default=None, ge=0, description="Days the outcome is counted over; the campaign's own when null."
    )
    analysis_date: date = Field(
        description="The date the result is read as final; earlier reads are early looks."
    )
    secondary_analysis_dates: tuple[date, ...] = Field(
        default=(), description="Later dates the result will also be read on (for example 60 and 90 days)."
    )
    mde_pp: float | None = Field(
        default=None,
        gt=0.0,
        le=100.0,
        description="The smallest effect worth detecting, in percentage points.",
    )
    base_rate: float | None = Field(
        default=None, ge=0.0, le=1.0, description="The outcome rate expected without the campaign, 0 to 1."
    )
    base_rate_source: str | None = Field(
        default=None, max_length=200, description="Where the expected rate comes from, in words."
    )
    alpha: float = Field(default=0.05, gt=0.0, lt=1.0, description="Two-sided significance level.")
    power: float = Field(default=0.8, gt=0.0, lt=1.0, description="The power the test is planned for.")
    expectation: str = Field(
        default="", max_length=500, description="The sponsor's written expectation, in their words."
    )

    @model_validator(mode="after")
    def _dates_in_order(self) -> TestPlanInput:
        if any(later <= self.analysis_date for later in self.secondary_analysis_dates):
            raise ValueError("Each secondary analysis date must be after the analysis date.")
        if len(set(self.secondary_analysis_dates)) != len(self.secondary_analysis_dates):
            raise ValueError("A secondary analysis date is repeated.")
        return self


class TestPlanAmendment(TestPlanInput):
    """`POST /campaigns/{id}/plan/amendments`: the whole plan as amended, and why."""

    __test__ = False

    reason: Annotated[str, Field(min_length=1, max_length=500)] = Field(
        description="Why the plan changes, in words; kept with the new version."
    )


class RealisedPopulation(StrictBase):
    """Who a campaign's assignment really compares: counts and shares, never a key."""

    population_rows: int = Field(description="Customers in the measured population.")
    n_treat: int = Field(description="Treated customers in it.")
    n_holdout: int = Field(description="Held-back customers in it.")
    n_explore: int = Field(
        default=0,
        description=(
            "Explore-slice customers of the campaign (M92). The slice lies outside the selection, so it "
            "is counted over every eligible customer, not only the measured population."
        ),
    )

    @property
    def holdout_fraction(self) -> float:
        """Held-back share of the population, rounded so equal populations compare equal."""
        return round(self.n_holdout / self.population_rows, _SHARE_DIGITS) if self.population_rows else 0.0

    @property
    def explore_fraction(self) -> float:
        """Explore-slice customers per customer of the measured population (the preview's explore share)."""
        return round(self.n_explore / self.population_rows, _SHARE_DIGITS) if self.population_rows else 0.0


class TestPlan(Artefact):
    """`campaigns/<id>/test_plan.json`: a registered, hashed test plan (one version of it)."""

    __test__ = False

    campaign_id: str = Field(description="The campaign the plan is for.")
    version: int = Field(default=1, ge=1, description="1, then one more per amendment.")
    metric: str = Field(description="What is measured, in words.")
    outcome_column: str = Field(description="The outcome column.")
    positive_label: str | None = Field(default=None, description="The value that counts as a conversion.")
    outcome_kind: Literal["binary", "continuous"] = Field(
        default="binary", description="Binary or continuous."
    )
    covariate_column: str | None = Field(default=None, description="The pre-registered covariate, if any.")
    outcome_window_days: int | None = Field(description="Days the outcome is counted over.")
    analysis_date: date = Field(description="The date the result is read as final.")
    secondary_analysis_dates: tuple[date, ...] = Field(default=(), description="Later reading dates.")
    holdout_fraction: float = Field(
        description="Held-back share of the measured population (from the assignment)."
    )
    explore_fraction: float = Field(
        default=0.0, description="Explore-slice customers per customer of the measured population (M92)."
    )
    population_rows: int = Field(description="Customers in the measured population.")
    n_treat: int = Field(description="Treated customers in it.")
    n_holdout: int = Field(description="Held-back customers in it.")
    n_explore: int = Field(
        default=0, description="Explore-slice customers of the campaign, outside the selection (M92)."
    )
    mde_pp: float | None = Field(default=None, description="The detectable effect planned for, in points.")
    base_rate: float | None = Field(default=None, description="The expected rate without the campaign.")
    base_rate_source: str | None = Field(default=None, description="Where that rate comes from.")
    alpha: float = Field(default=0.05, description="Two-sided significance level.")
    power: float = Field(default=0.8, description="The power planned for.")
    achieved_power: float | None = Field(
        default=None,
        description="The power the planned arms have for `mde_pp`; null with `power_note` when unknown.",
    )
    power_note: str | None = Field(default=None, description="Why `achieved_power` is null, in one sentence.")
    warnings: tuple[str, ...] = Field(
        default=(), description="Codes worth knowing, never blocking: PLAN_UNDERPOWERED."
    )
    expectation: str = Field(default="", description="The sponsor's written expectation.")
    amends: str | None = Field(default=None, description="`plan_hash` of the version this one replaces.")
    amendment_reason: str | None = Field(default=None, description="Why it was amended.")
    registered_at: AwareDatetime = Field(description="When this version was registered.")
    registered_by: str = Field(description="The signed-in user id that registered it.")
    plan_hash: str = Field(description="SHA-256 of the plan's content (every field but this and who/when).")


class PlanDifference(StrictBase):
    """One way what is about to be measured differs from the registered plan."""

    field: str = Field(description="The plan field that differs.")
    planned: str = Field(description="What the plan says, as text.")
    realised: str = Field(description="What the measurement would use, as text.")


class TestPlanChangedError(ValueError):
    """Raised by `measure_campaign` when the measurement would differ from the plan in force."""

    __test__ = False

    def __init__(self, differences: Sequence[PlanDifference]) -> None:
        self.differences = tuple(differences)
        listed = "; ".join(f"{d.field}: planned {d.planned}, now {d.realised}" for d in self.differences)
        super().__init__(
            f"This measurement differs from the registered test plan ({listed}). Measure as planned, or "
            f"amend the plan with a reason first."
        )


# ---------------------------------------------------------------------------
# Freezing
# ---------------------------------------------------------------------------
def plan_hash(content: dict[str, Any]) -> str:
    """SHA-256 of a plan's content: its JSON with sorted keys, the excluded fields left out."""
    kept = {key: value for key, value in content.items() if key not in _HASH_EXCLUDED}
    raw = json.dumps(kept, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def plan_inputs(plan: TestPlan) -> TestPlanInput:
    """The decision a stored plan records, as the input that would register it again."""
    fields = set(TestPlanInput.model_fields)
    return TestPlanInput.model_validate(plan.model_dump(include=fields))


def freeze_plan(
    decided: TestPlanInput,
    realised: RealisedPopulation,
    *,
    campaign_id: str,
    registered_by: str,
    registered_at: datetime,
    version: int = 1,
    amends: str | None = None,
    amendment_reason: str | None = None,
) -> TestPlan:
    """A `TestPlan` with its power check and `plan_hash`; `decided.outcome_window_days` must be resolved."""
    from engine.measurement.planner import achieved_power

    achieved: float | None = None
    note: str | None = None
    if decided.mde_pp is None or decided.base_rate is None:
        note = "Give the smallest effect worth detecting and the expected rate to see the power of this test."
    elif realised.n_treat <= 0 or realised.n_holdout <= 0:
        note = "One of the two groups is empty, so this test cannot detect any effect."
        achieved = 0.0
    elif decided.base_rate + decided.mde_pp / 100.0 > 1.0:
        note = "The expected rate plus the effect is above 100%, so the power cannot be worked out."
    else:
        estimate = achieved_power(
            realised.n_treat,
            realised.n_holdout,
            decided.base_rate,
            decided.mde_pp / 100.0,
            alpha=decided.alpha,
        )
        achieved, note = estimate.power, estimate.reason
    warnings = (PLAN_UNDERPOWERED,) if achieved is not None and achieved < decided.power else ()
    content: dict[str, Any] = {
        **decided.model_dump(include=set(TestPlanInput.model_fields)),  # an amendment's reason goes below
        "campaign_id": campaign_id,
        "version": version,
        "holdout_fraction": realised.holdout_fraction,
        "explore_fraction": realised.explore_fraction,
        "population_rows": realised.population_rows,
        "n_treat": realised.n_treat,
        "n_holdout": realised.n_holdout,
        "n_explore": realised.n_explore,
        "achieved_power": None if achieved is None else round(achieved, _SHARE_DIGITS),
        "power_note": note,
        "warnings": warnings,
        "amends": amends,
        "amendment_reason": amendment_reason,
        "registered_at": registered_at,
        "registered_by": registered_by,
    }
    draft = TestPlan.model_validate({**content, "plan_hash": "0" * 64})
    return draft.model_copy(update={"plan_hash": plan_hash(draft.model_dump(mode="json"))})


# ---------------------------------------------------------------------------
# At measurement
# ---------------------------------------------------------------------------
def realised_population(
    frame: pd.DataFrame, *, intended_column: str | None = None, bands: Sequence[str] | None = None
) -> RealisedPopulation:
    """Who a scores or assignment frame compares, by the rule `measure_incrementality` applies."""
    import pandas as pd

    from engine.measurement.campaign import as_scores_frame
    from engine.uplift.incrementality import _flag, _suppressed

    scores = as_scores_frame(frame).reset_index(drop=True)
    eligible = (
        ~_suppressed(scores["suppressed_reason"])
        if "suppressed_reason" in scores.columns
        else pd.Series(True, index=scores.index)
    )
    if intended_column is not None:
        population = eligible & _flag(scores[intended_column])
    elif bands is not None:
        wanted = {str(band) for band in bands}
        population = eligible & scores["band"].astype("string").isin(wanted).fillna(value=False)
    else:
        population = eligible
    held_out = _flag(scores["control_group"])
    explore = (
        _flag(scores["explore"]) if "explore" in scores.columns else pd.Series(False, index=scores.index)
    )
    return RealisedPopulation(
        population_rows=int(population.sum()),
        n_treat=int((population & ~held_out).sum()),
        n_holdout=int((population & held_out).sum()),
        # The explore slice is drawn outside the selection (DEC-1302 (c)), so it never lies inside an
        # uplift run's intended set or the treat bands: it is counted over every eligible customer.
        n_explore=int((eligible & explore).sum()),
    )


def plan_differences(
    plan: TestPlan,
    *,
    realised: RealisedPopulation,
    outcome_column: str,
    positive_label: str | None,
    outcome_window_days: int | None,
    covariate_column: str | None,
    outcome_kind: Literal["binary", "continuous"] = "binary",
) -> tuple[PlanDifference, ...]:
    """Every way this measurement would differ from `plan`; empty when it is measured as planned."""

    def text(value: object) -> str:
        return "none" if value is None else str(value)

    kept = population_kept(plan, realised)
    holdout_moved = abs(plan.holdout_fraction - realised.holdout_fraction) > HOLDOUT_TOLERANCE
    pairs: list[tuple[str, object, object]] = []
    if not kept and holdout_moved:
        pairs.append(("holdout_fraction", plan.holdout_fraction, realised.holdout_fraction))
    if not kept and plan.population_rows != realised.population_rows:
        pairs.append(("population_rows", plan.population_rows, realised.population_rows))
    pairs += [
        ("outcome_window_days", plan.outcome_window_days, outcome_window_days),
        ("outcome_kind", plan.outcome_kind, outcome_kind),
        ("outcome_column", plan.outcome_column, outcome_column),
        ("positive_label", _label(plan.positive_label), _label(positive_label)),
        ("covariate_column", plan.covariate_column, covariate_column),
    ]
    return tuple(
        PlanDifference(field=name, planned=text(planned), realised=text(now))
        for name, planned, now in pairs
        if planned != now
    )


def population_kept(plan: TestPlan, realised: RealisedPopulation) -> bool:
    """True when `realised` is the planned population, less at most a few customers erased since.

    Neither arm may be larger than planned, and no more than `ERASURE_ALLOWANCE_SHARE` of the planned
    population (rounded up, at least one customer) may be gone. Customers only leave an assignment by
    erasure, so a shrink that small is a privacy request honoured, not a different test.
    """
    import math

    removed = plan.population_rows - realised.population_rows
    allowance = max(1, math.ceil(plan.population_rows * ERASURE_ALLOWANCE_SHARE))
    return (
        realised.n_treat <= plan.n_treat
        and realised.n_holdout <= plan.n_holdout
        and realised.n_explore <= plan.n_explore
        and 0 <= removed <= allowance
    )


def _label(value: str | None) -> str | None:
    """A positive label compared as the measurement reads it: trimmed, case-insensitive."""
    return None if value is None else value.strip().lower()


def is_early_look(plan: TestPlan, as_of: datetime) -> bool:
    """True when `as_of` (read in UTC) falls before the plan's analysis date."""
    from datetime import UTC

    moment = as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of.astimezone(UTC)
    return moment.date() < plan.analysis_date
