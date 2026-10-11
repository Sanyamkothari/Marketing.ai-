"""An early alarm when a campaign is doing harm, and never an early win (Plan K M138, DEC-1403).

M94 refuses a verdict on an early look, which is right for "did it work?": read the result every week and stop
on a good day, and a 95% interval stops meaning 95%. But an offer that is *hurting* customers should not run on
until the planned day just because the plan said so. This module watches for harm only, with a test that may be
read at every data refresh without raising the false-alarm rate: the normal-mixture sequential probability ratio
test (mSPRT; Johari, Pekelis and Walsh), one-sided.

**The statistic** (design §9.4). At each refresh, for the difference `d` (contacted minus held back) read from
the campaign's report and its variance `V = s_T^2 / n_T + s_C^2 / n_C`, with the mixing variance `tau^2`:

    L = sqrt(V / (V + tau^2)) * exp(tau^2 * d^2 / (2 * V * (V + tau^2)))

`tau` is the registered test plan's smallest effect worth detecting, in the outcome's own unit (`mde_pp / 100`
for a yes/no outcome, `mde_value` for an amount), so the mixture concentrates where harm worth catching lies.
With no plan, or a plan with no such effect, nothing is checked and the file says why.

**One-sided, for harm only.** The alarm fires when `L >= 1 / alpha` **and** `d` lies on the harmful side: below
zero for an outcome the campaign wants more of, above zero for one it exists to prevent (a churn rate). The
direction is decided as the Value Proof Pack decides it (`engine.pilot.proof`): the value inputs, else the
audit's or programme's own readout, else the use case's rule. A campaign of several offers is checked offer by
offer against the shared control, each at `alpha / m` (Bonferroni), so the chance of any false alarm stays at
most `alpha`. `L` is a likelihood ratio whose expectation is one when nothing is wrong, so by Ville's inequality
the chance it *ever* reaches `1 / alpha`, however often it is read, is at most `alpha` (the always-valid
property; `tests/statistical/test_harm_alarm.py` checks it over many refresh schedules).

**Never a win.** The same statistic is never read the other way. A benefit, however large, raises nothing, and
nothing here writes a verdict: M94's early-look rule stands, and the result is read on the planned day only.
`tests/integration/measurement/test_harm_alarm_loop.py` greps every output path for win wording on an early look.

**Sticky, and nothing stops.** An alarm, once fired under a plan, stands until the final result is read (an
mSPRT is a stopping rule: the evidence that crossed the line is not withdrawn because a later refresh looks
calmer). It raises a warning card on the campaigns' summary (`HARM_ALARM`, `engine.measurement.summary`) and
one alert when it first fires (`harm_alarm`, the scheduling alert path). Nothing is paused or stopped: a person
decides.

**Where it runs.** On every interim reading of a campaign, in two places.

* The monthly loop's measure step (`engine.measurement.cycle.measure_step`). While a live campaign's outcome
  window is still open, each firing reads the outcomes recorded so far (`[treatment start, now]`, from the same
  saved connection) and builds an interim reading in memory (`engine.measurement.cycle.interim_reading`): every
  customer counted, each outcome as it stands now, so both groups are compared over exactly the same days. That
  reading is checked and then dropped: no report, no outcomes copy and no change to the campaign are stored, as
  for any reading refused with `CAMPAIGN_NOT_MATURED`. Once the window has closed the step measures the
  campaign as before, and every reading before the plan's analysis date (an early look) is checked too.
* On demand, `POST /campaigns/{id}/measure`, for a reading that is an early look or still has customers inside
  their outcome window (per-row treatment dates).

It reads the report the measurement just computed (never a second computation of the difference) and writes
`campaigns/<id>/harm_alarm.json` (`HarmCheck`, aggregate: no customer id). A final result is never checked: its
verdict says whether the campaign did harm.

**Opt-in.** `harm_alarm.enabled` in `configs/measurement.yaml` (installation-wide, read by
`load_harm_alarm_settings`) is false by default: then nothing is checked or written, no card is shown (even for a
`harm_alarm.json` left from a time the setting was on) and every output is byte for byte what it was. The file is
read before anything is measured, so a file that cannot be read refuses the measurement up front with a
`ConfigError` (HTTP 422, or a failed firing with the error's code) and writes nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, TypeVar

from pydantic import AwareDatetime, Field

from engine.config import StrictBase
from engine.contracts import Artefact

if TYPE_CHECKING:
    from pathlib import Path

    from engine.measurement.campaign import Campaign
    from engine.measurement.plan import TestPlan
    from engine.scheduling.alerts import Alert
    from engine.storage import Storage
    from engine.uplift.contracts import IncrementalityReport

__all__ = [
    "HARM_ALARM",
    "HARM_ALARM_FILENAME",
    "HARM_CODES",
    "MEASUREMENT_SETTINGS_FILENAME",
    "WHOLE_CAMPAIGN",
    "HarmAlarm",
    "HarmAlarmSettings",
    "HarmCheck",
    "HarmLook",
    "HarmOutcome",
    "Judgement",
    "campaign_client_id",
    "check_campaign_harm",
    "harm_alert",
    "harm_alert_message",
    "harm_looks",
    "is_interim",
    "judge",
    "load_harm_alarm_settings",
    "mixture_log_statistic",
    "outcome_is_good_for",
    "rate_difference",
]

HARM_ALARM: Final[str] = "HARM_ALARM"
"""The warning card's code (and the alarm's): the campaign may be doing harm. Joins `PLAN_K_CODES` at integration."""
HARM_CODES: Final[frozenset[str]] = frozenset({HARM_ALARM})
HARM_ALARM_FILENAME: Final[str] = "harm_alarm.json"
"""`campaigns/<id>/harm_alarm.json`: the latest harm check of a campaign (`HarmCheck`)."""
MEASUREMENT_SETTINGS_FILENAME: Final[str] = "measurement.yaml"
"""`configs/measurement.yaml`: installation-wide measurement options; absent means every default."""
WHOLE_CAMPAIGN: Final[str] = "treated"
"""The `arm` of a campaign with one treated group: every contacted customer against the held-back ones."""
SCHEMA_VERSION: Final[int] = 1
_LARGEST_EXPONENT: Final[float] = 700.0
"""Past `exp(700)` a float overflows: the statistic is then stored by its logarithm only (`statistic` null)."""

_M = TypeVar("_M", bound=StrictBase)
CheckStatus = Literal["alarm", "no_alarm", "not_checked"]
NotCheckedReason = Literal["no_plan", "no_effect_planned", "not_random"]


# ---------------------------------------------------------------------------
# Settings: `configs/measurement.yaml`, section `harm_alarm`
# ---------------------------------------------------------------------------
class HarmAlarmSettings(StrictBase):
    """`measurement.harm_alarm` (`configs/measurement.yaml`). Off by default: nothing is read or written."""

    enabled: bool = Field(default=False, description="Check every interim reading of a campaign for harm.")
    alpha: float = Field(
        default=0.05,
        gt=0.0,
        le=0.1,
        description="The most often the alarm may fire on a campaign that does no harm, however often it is read.",
    )
    min_rows_per_arm: int = Field(
        default=100,
        ge=30,
        description="Customers each group needs with an outcome counted before a reading is judged at all.",
    )


def load_harm_alarm_settings(root: Path | None = None) -> HarmAlarmSettings:
    """`harm_alarm` of `configs/measurement.yaml` under the configuration root; every default when absent.

    Raises `engine.config.ConfigError`, the coded error every route already answers with its code and a plain
    message (HTTP 422), never a bare 500, as M136's `load_pooling_settings` does for the same file:
    `CONFIG_YAML_ERROR` or `CONFIG_NOT_A_MAPPING` for a file that is not a mapping of settings, `CONFIG_INVALID`
    for a section that is not a mapping, names an unknown key or holds a value out of range, so a typo cannot
    silently leave the alarm off. Other sections of the file (M136's `pooling`) are not validated here.
    """
    import yaml
    from pydantic import ValidationError

    from engine.config import ConfigError, config_root

    path = config_root(root) / MEASUREMENT_SETTINGS_FILENAME
    if not path.is_file():
        return HarmAlarmSettings()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(
            "CONFIG_YAML_ERROR", f"{MEASUREMENT_SETTINGS_FILENAME} is not valid YAML: {exc}.", path=str(path)
        ) from exc
    if document is None:  # an empty file, or comments only: every default
        return HarmAlarmSettings()
    if not isinstance(document, dict):
        raise ConfigError(
            "CONFIG_NOT_A_MAPPING",
            f"{MEASUREMENT_SETTINGS_FILENAME} must contain a mapping of settings, not {type(document).__name__}.",
            path=str(path),
        )
    section = document.get("harm_alarm")
    if section is not None and not isinstance(section, dict):
        raise ConfigError(
            "CONFIG_INVALID",
            f"{MEASUREMENT_SETTINGS_FILENAME}: `harm_alarm` must be a mapping of settings.",
            path="harm_alarm",
        )
    try:
        return HarmAlarmSettings.model_validate(section or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        dotted = ".".join(["harm_alarm", *(str(part) for part in first["loc"])])
        raise ConfigError(
            "CONFIG_INVALID", f"{MEASUREMENT_SETTINGS_FILENAME} {dotted}: {first['msg']}.", path=dotted
        ) from exc


# ---------------------------------------------------------------------------
# The statistic (pure)
# ---------------------------------------------------------------------------
def mixture_log_statistic(difference: float, variance: float, mixing_variance: float) -> float:
    """`log L` of the module docstring: the normal-mixture likelihood ratio of a difference with variance `variance`.

    `variance` and `mixing_variance` must be above zero (`ValueError` otherwise).
    """
    if not (variance > 0.0 and mixing_variance > 0.0) or not math.isfinite(difference):
        raise ValueError("The mixture statistic needs a finite difference and two variances above zero.")
    total = variance + mixing_variance
    return 0.5 * math.log(variance / total) + mixing_variance * difference**2 / (2.0 * variance * total)


@dataclass(frozen=True)
class Judgement:
    """One reading judged: the statistic's logarithm, whether the difference is harmful, whether the alarm fires."""

    log_statistic: float
    harmful: bool
    fired: bool


def judge(
    difference: float, variance: float, *, mixing_sd: float, alpha: float, outcome_is_good: bool
) -> Judgement:
    """The one-sided rule: fire when `L >= 1 / alpha` and the difference lies on the harmful side.

    The harmful side is below zero when the outcome is one to have more of, above zero when the campaign exists
    to prevent it. A difference on the other side never fires, however large: the statistic is never a win.
    """
    log_statistic = mixture_log_statistic(difference, variance, mixing_sd**2)
    harmful = difference < 0.0 if outcome_is_good else difference > 0.0
    return Judgement(log_statistic, harmful, harmful and log_statistic >= math.log(1.0 / alpha))


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------
class HarmLook(StrictBase):
    """One treated group against the held-back customers at this reading."""

    arm: str = Field(description="`treated` for the whole campaign, else the offer, as configured.")
    treated_rows: int = Field(
        description="Contacted customers of this group whose outcome is counted so far."
    )
    control_rows: int = Field(description="Held-back customers whose outcome is counted so far.")
    difference: float | None = Field(
        default=None,
        description="Contacted minus held back, in the outcome's unit (a share for a yes/no outcome); null: unread.",
    )
    variance: float | None = Field(default=None, description="The variance of `difference`; null: unread.")
    log_statistic: float | None = Field(default=None, description="Natural logarithm of the statistic.")
    statistic: float | None = Field(
        default=None,
        description="The statistic `L`; null when unread or too large to hold (see `log_statistic`).",
    )
    threshold: float = Field(description="`1 / alpha` for this group (alpha split across the offers).")
    harmful: bool | None = Field(
        default=None, description="True when the difference lies on the harmful side; null: unread."
    )
    fired: bool = Field(default=False, description="The alarm fires for this group at this reading.")
    reason: str | None = Field(default=None, description="Why the group was not judged, in plain words.")


class HarmAlarm(StrictBase):
    """The shared contract (design §5): a campaign's group crossed the harm line, and when."""

    campaign_id: str
    arm: str = Field(description="`treated` for the whole campaign, else the offer.")
    fired_at: AwareDatetime = Field(description="The reading at which the alarm first fired (UTC).")
    statistic: float | None = Field(description="`L` when it fired; null when too large to hold.")
    log_statistic: float = Field(description="Natural logarithm of `L` when it fired.")
    threshold: float = Field(description="The level it crossed, `1 / alpha` for the group.")
    difference: float = Field(description="Contacted minus held back when it fired, in the outcome's unit.")
    treated_rows: int = Field(description="Contacted customers counted when it fired.")
    control_rows: int = Field(description="Held-back customers counted when it fired.")
    as_of: AwareDatetime = Field(description="The moment the outcomes were read as of.")
    test_plan_hash: str = Field(description="The registered test plan the alarm was raised under.")


class HarmCheck(Artefact):
    """`campaigns/<id>/harm_alarm.json`: the latest harm check of a campaign, and every alarm raised under its plan."""

    schema_version: int = SCHEMA_VERSION
    campaign_id: str
    checked_at: AwareDatetime = Field(description="When the check ran (UTC).")
    as_of: AwareDatetime = Field(description="The moment the report read the outcomes as of.")
    status: CheckStatus = Field(
        description="`alarm` while any alarm stands, `no_alarm`, or `not_checked` with the reason."
    )
    reason: str = Field(description="What the check found, or why nothing was checked, in plain words.")
    not_checked: NotCheckedReason | None = Field(
        default=None, description="Why nothing was checked (a slug)."
    )
    alpha: float = Field(description="The false-alarm limit the check holds to across every reading.")
    mixing_sd: float | None = Field(
        default=None, description="`tau`: the plan's smallest effect worth detecting, in the outcome's unit."
    )
    outcome_kind: Literal["binary", "continuous"] = Field(description="Yes/no outcome or an amount.")
    outcome_is_good: bool | None = Field(
        default=None,
        description="False when the campaign exists to make the outcome rarer; null: not checked.",
    )
    test_plan_hash: str | None = Field(default=None, description="The plan the check was read under.")
    analysis_date: date | None = Field(default=None, description="The plan's day for the final reading.")
    early_look: bool = Field(description="True when read before the plan's analysis date.")
    looks: tuple[HarmLook, ...] = Field(default=(), description="Each treated group at this reading.")
    alarms: tuple[HarmAlarm, ...] = Field(
        default=(),
        description="Every alarm raised under this plan, oldest first; they stand until the final result.",
    )


@dataclass(frozen=True)
class HarmOutcome:
    """A check written, and the alarms that fired for the first time at it (what an alert is raised for)."""

    check: HarmCheck
    new_alarms: tuple[HarmAlarm, ...]


# ---------------------------------------------------------------------------
# Reading a report
# ---------------------------------------------------------------------------
def is_interim(report: IncrementalityReport) -> bool:
    """True for a reading that is not the final result: an early look, or one with outcomes still to come in."""
    from engine.uplift.contracts import IncrementalityStatus

    return bool(report.early_look or report.rows_immature or report.status is IncrementalityStatus.IMMATURE)


def _mixing_sd(plan: TestPlan) -> float | None:
    """`tau`: the plan's smallest effect worth detecting, in the outcome's own unit."""
    if plan.outcome_kind == "continuous":
        return plan.mde_value
    return plan.mde_pp / 100.0 if plan.mde_pp is not None else None


@dataclass(frozen=True)
class _Group:
    arm: str
    treated_rows: int
    control_rows: int
    difference: float | None
    variance: float | None


def rate_difference(
    treated_rows: int, control_rows: int, treated_rate: float | None, control_rate: float | None
) -> tuple[float | None, float | None]:
    """A yes/no outcome's difference in rates and its variance `p_T (1 - p_T) / n_T + p_C (1 - p_C) / n_C`.

    `(None, None)` when a group is empty or a rate unread.
    """
    if treated_rate is None or control_rate is None or treated_rows <= 0 or control_rows <= 0:
        return None, None
    variance = (
        treated_rate * (1.0 - treated_rate) / treated_rows
        + control_rate * (1.0 - control_rate) / control_rows
    )
    return treated_rate - control_rate, variance


def _rate_group(
    arm: str, treated_rows: int, control_rows: int, treated: float | None, control: float | None
) -> _Group:
    difference, variance = rate_difference(treated_rows, control_rows, treated, control)
    return _Group(arm, treated_rows, control_rows, difference, variance)


def _groups(report: IncrementalityReport) -> tuple[_Group, ...]:
    """Each treated group's difference and variance, read from the report the measurement just computed."""
    if report.outcome_kind == "continuous":
        from engine.measurement.measure import headline_estimate
        from engine.measurement.summary import interval_se

        # The planned analysis: the adjusted estimate when the plan named a covariate, else Welch's difference.
        estimate, _ = headline_estimate(report)
        variance: float | None = None
        if estimate is not None and estimate.ci_low is not None and estimate.ci_high is not None:
            # Welch's interval is a t interval: its width over 2 z is at least the standard error, so the
            # variance read back is never too small and the alarm errs towards silence.
            se = interval_se(estimate.ci_low, estimate.ci_high, estimate.confidence_level)
            variance = se**2 if se is not None else None
        difference = estimate.value if estimate is not None and variance is not None else None
        return (_Group(WHOLE_CAMPAIGN, report.treated_rows, report.control_rows, difference, variance),)
    if report.arms:
        return tuple(
            _rate_group(
                arm.arm,
                arm.treated_rows or 0,
                arm.control_rows or 0,
                arm.treated_rate,
                arm.control_rate,
            )
            for arm in report.arms
        )
    return (
        _rate_group(
            WHOLE_CAMPAIGN, report.treated_rows, report.control_rows, report.treated_rate, report.control_rate
        ),
    )


def harm_looks(
    report: IncrementalityReport, *, mixing_sd: float, outcome_is_good: bool, settings: HarmAlarmSettings
) -> tuple[HarmLook, ...]:
    """Each treated group of `report` judged against the held-back customers, alpha split across the groups."""
    groups = _groups(report)
    alpha = settings.alpha / max(
        len(groups), 1
    )  # Bonferroni across the offers: any false alarm stays at most alpha
    return tuple(
        _look(group, settings=settings, alpha=alpha, tau=mixing_sd, good=outcome_is_good) for group in groups
    )


def _look(group: _Group, *, settings: HarmAlarmSettings, alpha: float, tau: float, good: bool) -> HarmLook:
    threshold = 1.0 / alpha
    base = HarmLook(
        arm=group.arm, treated_rows=group.treated_rows, control_rows=group.control_rows, threshold=threshold
    )
    if min(group.treated_rows, group.control_rows) < settings.min_rows_per_arm:
        return base.model_copy(
            update={
                "reason": "Too few customers in one of the groups have their outcome counted yet to judge the difference."
            }
        )
    if group.difference is None or group.variance is None or not group.variance > 0.0:
        return base.model_copy(
            update={
                "difference": group.difference,
                "reason": "The outcome does not vary within the groups yet, so the difference cannot be judged.",
            }
        )
    judged = judge(group.difference, group.variance, mixing_sd=tau, alpha=alpha, outcome_is_good=good)
    return base.model_copy(
        update={
            "difference": group.difference,
            "variance": group.variance,
            "log_statistic": judged.log_statistic,
            "statistic": _statistic(judged.log_statistic),
            "harmful": judged.harmful,
            "fired": judged.fired,
        }
    )


def _statistic(log_statistic: float) -> float | None:
    return math.exp(log_statistic) if log_statistic <= _LARGEST_EXPONENT else None


# ---------------------------------------------------------------------------
# Which way round the outcome counts
# ---------------------------------------------------------------------------
def outcome_is_good_for(
    storage: Storage, campaign: Campaign, report: IncrementalityReport, root: Path | None
) -> bool:
    """Which way round the outcome counts, decided as the Value Proof Pack decides it (`engine.pilot.proof`).

    The value inputs (the campaign's own, else its scoring run's), else the audit's or programme's readout, else
    the use case's rule (`outcome_is_good_by_default`) for the column the campaign page judges. A campaign with
    no outcomes recorded yet (the loop's interim reading of an open window) is judged on the column `report` read.
    """
    from engine.config import load_use_case
    from engine.measurement.audit import AUDIT_FILENAME
    from engine.measurement.campaign import campaign_key
    from engine.measurement.programme import PROGRAMME_FILENAME
    from engine.pilot.roi import ROI_INPUTS_FILENAME, outcome_is_good_by_default
    from engine.storage import run_key

    keys = [campaign_key(campaign.campaign_id, ROI_INPUTS_FILENAME)]
    keys += [run_key(run_id, ROI_INPUTS_FILENAME) for run_id in campaign.run_ids[:1]]
    for key in keys:
        found = _field(storage, key, "outcome_is_good")
        if found is not None:
            return found
    for name in (AUDIT_FILENAME, PROGRAMME_FILENAME):
        found = _field(storage, campaign_key(campaign.campaign_id, name), "outcome_is_good")
        if found is not None:
            return found
    if campaign.use_case_id is None:
        return True
    column = report.outcome_column
    if campaign.outcomes is not None and not campaign.outcomes.outcome_named:
        try:
            column = load_use_case(campaign.use_case_id, root).target.column or "outcome"
        except (
            Exception
        ):  # the use case has gone: judge the column by its own name, as the campaign page does
            column = report.outcome_column
    return outcome_is_good_by_default(campaign.use_case_id, column, root)


def _field(storage: Storage, key: str, name: str) -> bool | None:
    """A boolean field of a JSON artefact, or None when the file or a boolean there is missing."""
    import json

    from engine.storage import StorageError

    try:
        if not storage.exists(key):
            return None
        document = json.loads(storage.read_bytes(key))
    except (StorageError, ValueError):
        return None
    value = document.get(name) if isinstance(document, dict) else None
    return value if isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
_NOT_CHECKED: Final[dict[NotCheckedReason, str]] = {
    "no_plan": (
        "No test plan is registered, so there is no smallest effect worth detecting to watch for harm against; "
        "nothing was checked."
    ),
    "no_effect_planned": (
        "The test plan gives no smallest effect worth detecting, so there is no size of harm to watch for; "
        "nothing was checked."
    ),
    "not_random": (
        "The groups were not drawn at random, so a difference between them cannot be read as harm; nothing was "
        "checked."
    ),
}
_ALARM_TEXT: Final[str] = (
    "Customers contacted are doing worse than the customers held back, by more than chance explains even though "
    "the campaign is checked at every reading. This is an alarm, not a result: nothing has been stopped."
)
_QUIET_TEXT: Final[str] = (
    "The check found no harm to act on at this reading. This is not a result: the result is read only on the "
    "planned day."
)


def check_campaign_harm(
    storage: Storage,
    campaign: Campaign,
    report: IncrementalityReport,
    *,
    root: Path | None,
    now: datetime,
    settings: HarmAlarmSettings | None = None,
    interim: bool = False,
) -> HarmOutcome | None:
    """Check an interim reading of `campaign` for harm and write `harm_alarm.json`; None when nothing was done.

    Nothing is read or written when the alarm is off (`configs/measurement.yaml` under `root`) or `report` is a
    final result. `report` is the one the measurement just computed for this campaign; `interim` marks a reading
    the caller knows is not final although the report alone does not say so (the loop's reading of an open
    window, `engine.measurement.cycle.interim_reading`).
    """
    from engine.measurement.campaign import TEST_PLAN_FILENAME, campaign_key
    from engine.measurement.plan import TestPlan

    settings = settings if settings is not None else load_harm_alarm_settings(root)
    if not settings.enabled or not (interim or is_interim(report)):
        return None
    cid = campaign.campaign_id
    plan = _stored(storage, campaign_key(cid, TEST_PLAN_FILENAME), TestPlan)
    head: dict[str, Any] = {
        "campaign_id": cid,
        "checked_at": now,
        "as_of": report.as_of,
        "alpha": settings.alpha,
        "outcome_kind": report.outcome_kind or "binary",
        "early_look": report.early_look,
        "test_plan_hash": plan.plan_hash if plan is not None else None,
        "analysis_date": plan.analysis_date if plan is not None else None,
    }
    tau = _mixing_sd(plan) if plan is not None else None
    skipped: NotCheckedReason | None = None
    if plan is None:
        skipped = "no_plan"
    elif tau is None or not tau > 0.0:
        skipped = "no_effect_planned"
    elif not report.causal:
        skipped = "not_random"
    if skipped is not None or plan is None or tau is None:
        reason: NotCheckedReason = skipped or "no_plan"
        check = HarmCheck(**head, status="not_checked", reason=_NOT_CHECKED[reason], not_checked=reason)
        storage.write_model(campaign_key(cid, HARM_ALARM_FILENAME), check)
        return HarmOutcome(check, ())
    good = outcome_is_good_for(storage, campaign, report, root)
    looks = harm_looks(report, mixing_sd=tau, outcome_is_good=good, settings=settings)
    previous = _stored(storage, campaign_key(cid, HARM_ALARM_FILENAME), HarmCheck)
    standing = previous.alarms if previous is not None and previous.test_plan_hash == plan.plan_hash else ()
    alarmed = {alarm.arm for alarm in standing}
    new = tuple(
        HarmAlarm(
            campaign_id=cid,
            arm=look.arm,
            fired_at=now,
            statistic=look.statistic,
            log_statistic=float(look.log_statistic or 0.0),
            threshold=look.threshold,
            difference=float(look.difference or 0.0),
            treated_rows=look.treated_rows,
            control_rows=look.control_rows,
            as_of=report.as_of,
            test_plan_hash=plan.plan_hash,
        )
        for look in looks
        if look.fired and look.arm not in alarmed
    )
    alarms = (*standing, *new)
    check = HarmCheck(
        **head,
        status="alarm" if alarms else "no_alarm",
        reason=_ALARM_TEXT if alarms else _QUIET_TEXT,
        mixing_sd=tau,
        outcome_is_good=good,
        looks=looks,
        alarms=alarms,
    )
    storage.write_model(campaign_key(cid, HARM_ALARM_FILENAME), check)
    return HarmOutcome(check, new)


def _stored(storage: Storage, key: str, model: type[_M]) -> _M | None:
    from engine.storage import StorageError

    try:
        return storage.read_model(key, model) if storage.exists(key) else None
    except (StorageError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The alert
# ---------------------------------------------------------------------------
def campaign_client_id(storage: Storage, campaign: Campaign) -> str | None:
    """The client of the campaign's scoring run (`run.json`), for the alert's scope; None when it has none."""
    from engine.contracts import RunRecord
    from engine.storage import run_key

    if not campaign.run_ids:
        return None
    record = _stored(storage, run_key(campaign.run_ids[0], "run.json"), RunRecord)
    return record.client_id if record is not None else None


def harm_alert_message(outcome: HarmOutcome, *, use_case_name: str | None) -> str:
    """The alert's words: ids, a date and what to do; never a customer, a value or a verdict (DEC-770)."""
    check = outcome.check
    whose = f" for {use_case_name}" if use_case_name else ""
    offers = sorted({alarm.arm for alarm in outcome.new_alarms} - {WHOLE_CAMPAIGN})
    which = f" (offer {', '.join(offers)})" if offers else ""
    if check.early_look and check.analysis_date is not None:
        when = f" The result is still read only on {check.analysis_date.isoformat()}."
    else:  # outcomes still to come in, perhaps after the planned day: the result waits for every one of them
        when = " The final result is read once every customer's outcome is in."
    return (
        f"The campaign {check.campaign_id}{whose} may be doing harm{which}: at this reading the customers "
        f"contacted are doing worse than those held back, by more than chance explains. This is an alarm, not a "
        f"result, and nothing has been stopped: open the campaign and decide whether to pause it.{when}"
    )


def harm_alert(
    outcome: HarmOutcome,
    *,
    use_case_id: str,
    use_case_name: str | None,
    client_id: str | None,
    schedule_id: str | None,
    run_id: str | None,
    now: datetime,
) -> Alert | None:
    """The `harm_alarm` alert for the alarms that fired for the first time; None when none did."""
    from engine.scheduling.alerts import AlertKind, new_alert

    if not outcome.new_alarms:
        return None
    return new_alert(
        AlertKind.HARM_ALARM,
        use_case_id=use_case_id,
        client_id=client_id,
        schedule_id=schedule_id,
        run_id=run_id,
        message=harm_alert_message(outcome, use_case_name=use_case_name),
        now=now,
    )
