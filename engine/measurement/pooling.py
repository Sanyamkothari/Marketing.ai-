"""Local empirical-Bayes pooling across comparable campaigns (Plan K M136, DEC-1402).

A small campaign's measured difference is mostly noise when the effect it looks for (half a point to three points)
is about the size of its own range, and the best-looking of several such campaigns is the luckiest, not the best
(the "winner's curse"). Drawing each campaign's difference towards the average of **comparable** campaigns, by as
much as its own noise warrants, corrects both. This module computes that **Estimated** figure from the campaigns'
stored reports, writes it beside each report, and never changes the measured result.

**Comparable campaigns (the cell).** Campaigns are pooled only with campaigns that share every one of:

==================  ============================================================================================
`use_case_id`       the campaign record (`campaign.json`); a campaign with no use case (an audit, a programme)
                    is never pooled
metric              the report's `outcome_column` and `outcome_window_days`, and the outcomes file's
                    `positive_label` (the record): the same outcome, counted the same way over the same window.
                    The test plan's `metric` is free words a person typed and a campaign may have no plan, so the
                    columns the measurement actually read decide
`outcome_kind`      the report (`binary` when absent, as every report before Plan J M102)
channel             the scoring run's `catalogue_stamp.json` (Plan J M99): every channel of the catalogue actions the
                    run names; not recorded (no catalogue) is a value of its own, pooled only with itself
offer family        the same stamp: the catalogue action ids the run names
the holdout         the record's `holdout_scope`, `holdout_scope_key` and `holdout_epoch` (Plan J M92): a campaign
                    held back by a persistent holdout pools only with campaigns of the same epoch, so nothing is
                    pooled across epochs of a changed holdout; per-run draws pool with each other
generated data      `run.json` `synthetic` or the outcomes upload's `synthetic`: a planted effect never moves
                    a real campaign's estimate
client              the first scoring run's `run.json` `client_id` (Plan D M51), the rule the monthly loop's
                    steps use: one client's estimate is never drawn towards another client's results
==================  ============================================================================================

**Who enters.** A campaign enters its cell only with a final result the engine can stand behind: measured (not an
early look, every outcome window closed), its control group drawn at random by the engine or verified random
(`causal_basis` `engine_random` or `verified_random`), not a programme readout. Its estimate is the **registered
primary estimate**, read from the stored report, never re-measured: the adjusted difference whenever the plan
registered an adjustment (`adjusted_interval`, Plan J M102, for an amount or a yes/no outcome alike; Plan K M135's
estimate arrives through the same field), read for an amount by `engine.measurement.measure.headline_estimate`, the
rule its verdict uses; otherwise, on a yes/no outcome, the difference in rates (`absolute_lift`; every offer
together, `offers_combined.absolute_lift`, for a campaign of several offers, and none for one measured before offers
were added together), and on an amount the plain difference (`mean_difference_ci`). Its standard error is read from the width of the stored 95% range,
`(high - low) / (2 z)`, the rule the effect-fading card already uses. Campaigns that measure the same customers (the
same scoring runs) are one campaign: only the latest measured enters, exactly as value to date counts them once.

**The model (per cell, design §9.2).** `τ̂_j | τ_j ~ N(τ_j, s_j²)`, `τ_j ~ N(μ, σ²)`. `σ²` is estimated by restricted
maximum likelihood (REML: the root of the restricted score, found by bisection, truncated at zero); when that does
not converge the closed-form DerSimonian-Laird estimate is used and the method recorded. `μ̂` is the weighted mean
with weights `w_j = 1 / (s_j² + σ̂²)`. Each campaign's estimate is `τ̃_j = B_j μ̂ + (1 - B_j) τ̂_j`, where `B_j` is
the share of its own figure that is noise, with Morris's correction below.

**The correction (Morris 1983), on the estimate and on its range.** Plugging `μ̂` and `σ̂²` in as if they were
known draws too hard and gives a range that is too narrow. Morris's parametric empirical-Bayes estimator corrects
both, for `k` campaigns and one estimated mean (Morris 1983, JASA 78, §4, with `r = 1`):

* the share is `B_j = ((k - 3) / (k - 1)) s_j² / (s_j² + σ̂²)`: the factor makes the estimate of the share unbiased;
* the variance is Morris's own, `V_j = s_j² (1 - ((k - 1) / k) B_j) + (2 / (k - 3)) B_j² (τ̂_j - μ̂)²`: the first term
  is the posterior variance with the uncertainty of `μ̂` added (`(k - 1)/k` rather than one), the second the
  uncertainty of `B_j` itself;
* the range is `τ̃_j ± 1.96 √V_j`.

**A deviation from design §9.2 (DEC-1402).** The design puts Morris's correction on the range only and gives the
point estimate the plug-in share `s_j² / (s_j² + σ̂²)`, so `σ̂² = 0` would draw every campaign fully to the average.
That was tried: with the plug-in share and Morris's range the nightly two-group world covers 93.0% at eight and at
twelve campaigns, on the lower edge of the band (93.05%), so the corrected share is used for the estimate too, as
Morris's estimator does. Two consequences follow and are shown, not hidden:

* with three comparable campaigns the factor is zero, so nothing would move: three campaigns are treated as too few
  (`POOL_TOO_FEW`, with its own reason), and the first campaign drawn towards the others is the fourth;
* `σ̂² = 0` draws each campaign `(k - 3) / (k - 1)` of the way to the average (a third at four campaigns, a half at
  five), as far as the method allows, never fully.

**Guards.**

* Fewer than four comparable campaigns (or fewer than `min_campaigns`): nothing is pooled, and the file says why
  (`POOL_TOO_FEW`).
* `σ̂² = 0`: every campaign is drawn as far towards the cell's average as the method allows. That is shown, with a
  warning (`POOL_NO_SPREAD`), because it usually means too little data to see the campaigns differ, not that they
  are equal.
* The holdout, client and duplicate rules above.

**Never a measured result.** The file is labelled **Estimated**. "Proven value to date" stays the sum of the raw
measured lower bounds (decision K7); the summary and the Value Proof Pack show the estimate *beside* the measured
difference. The file is written only when `pooling.enabled` is true in `configs/measurement.yaml` (default false),
and read only then, so with the default every summary and Proof Pack is byte for byte what it was.

**Where it lives.** `campaigns/<id>/pooled_estimate.json`, one per campaign in a cell, rewritten for every campaign of
the use case each time one of its campaigns is measured (`refresh_pooled_estimates`, called by the one measurement
path, `engine.measurement.cycle.measure_recorded`, whether a person or the monthly schedule measures). It holds
aggregates only (estimates, ranges, campaign ids), never a customer id.

**Current or stale.** Each file records the `computed_at` of every member's report (`member_reports`) and the
settings it was fitted under. A reader shows it only while its own report, every member's report and the settings
are unchanged (`is_current`); otherwise the file counts as absent. Campaigns measured while pooling was off, or a
change to `min_campaigns`, would leave the files behind: building the campaign summary with pooling on compares
every use case's files with what would be written now and rewrites the use cases that differ
(`refresh_stale_pooled_estimates`), so turning pooling on fills in every campaign measured before. Pure Python;
nothing heavy is imported.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import AwareDatetime, Field

from engine.config import StrictBase
from engine.contracts import Artefact
from engine.uplift.contracts import ConfidenceValue
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from engine.measurement.campaign import Campaign, CampaignStore
    from engine.storage import Storage
    from engine.uplift.contracts import IncrementalityReport

__all__ = [
    "ESTIMATED_LABEL",
    "FIRST_DRAWN",
    "MEASUREMENT_SETTINGS_FILENAME",
    "MIN_CAMPAIGNS",
    "POOLED_FILENAME",
    "POOLING_CODES",
    "POOL_NO_SPREAD",
    "POOL_TOO_FEW",
    "CellEntry",
    "CellFit",
    "CellMember",
    "EstimateField",
    "MemberFit",
    "PooledEstimate",
    "PooledPrior",
    "PoolingCell",
    "PoolingSettings",
    "cell_entry",
    "dl_between_variance",
    "fit_cell",
    "is_current",
    "load_pooling_settings",
    "pool_entries",
    "pooled_key",
    "pooled_prior",
    "primary_estimate",
    "read_pooled_estimate",
    "refresh_pooled_estimates",
    "refresh_stale_pooled_estimates",
    "reml_between_variance",
    "se_from_interval",
]

_LOGGER = get_logger(__name__)

# ---------------------------------------------------------------------------
# Codes (DEC-1402): each joins Plan K's code list with a plain-language help entry at integration.
# ---------------------------------------------------------------------------
POOL_TOO_FEW: Final[str] = "POOL_TOO_FEW"
"""Fewer than three comparable campaigns have a final result: nothing is pooled, and the reason is shown."""
POOL_NO_SPREAD: Final[str] = "POOL_NO_SPREAD"
"""A warning, never blocking: the comparable campaigns differ by no more than noise, so each is drawn as far towards
their average as the method allows; that usually means too little data, not equal effects."""
POOLING_CODES: Final[frozenset[str]] = frozenset({POOL_TOO_FEW, POOL_NO_SPREAD})

POOLED_FILENAME: Final[str] = "pooled_estimate.json"
"""`campaigns/<id>/pooled_estimate.json`: the campaign's Estimated figure, beside its report."""
MEASUREMENT_SETTINGS_FILENAME: Final[str] = "measurement.yaml"
"""`configs/measurement.yaml`: installation-wide measurement options; absent means every default."""
MIN_CAMPAIGNS: Final[int] = 3
"""The smallest `min_campaigns` a setting may name (design §9.2: fewer than three are never pooled)."""
FIRST_DRAWN: Final[int] = 4
"""The fewest comparable campaigns that draw anything: Morris's factor `(k - 3) / (k - 1)` is zero at three."""
ESTIMATED_LABEL: Final[str] = "Estimated"
"""How every pooled figure is labelled wherever it is shown: never as a measured result (decision K7)."""
CONFIDENCE: Final[float] = 0.95
_Z: Final[float] = NormalDist().inv_cdf(0.5 + CONFIDENCE / 2.0)
_REML_ITERATIONS: Final[int] = 200
_REML_TOLERANCE: Final[float] = 1e-12

Method = Literal["reml", "dl"]


# ---------------------------------------------------------------------------
# Settings: `configs/measurement.yaml`, section `pooling`
# ---------------------------------------------------------------------------
class PoolingSettings(StrictBase):
    """`measurement.pooling` (`configs/measurement.yaml`). Off by default: nothing is written or read."""

    enabled: bool = Field(
        default=False, description="Write and show the Estimated figure of comparable campaigns."
    )
    min_campaigns: int = Field(
        default=MIN_CAMPAIGNS,
        ge=MIN_CAMPAIGNS,
        description="Comparable campaigns a cell needs before anything is pooled; never fewer than three.",
    )


def load_pooling_settings(root: Path | None = None) -> PoolingSettings:
    """`pooling` of `configs/measurement.yaml` under the configuration root; every default when absent.

    Raises `engine.config.ConfigError`, the coded error every route already answers with its code and a plain
    message (HTTP 422), never a bare 500: `CONFIG_YAML_ERROR` or `CONFIG_NOT_A_MAPPING` for a file that is not a
    mapping of settings, `CONFIG_INVALID` for a section that names an unknown key or a value out of range, so a typo
    cannot silently leave pooling off.
    """
    import yaml
    from pydantic import ValidationError

    from engine.config import ConfigError, config_root

    path = config_root(root) / MEASUREMENT_SETTINGS_FILENAME
    if not path.is_file():
        return PoolingSettings()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(
            "CONFIG_YAML_ERROR", f"{MEASUREMENT_SETTINGS_FILENAME} is not valid YAML: {exc}.", path=str(path)
        ) from exc
    if document is None:  # an empty file, or comments only: every default
        return PoolingSettings()
    if not isinstance(document, dict):
        raise ConfigError(
            "CONFIG_NOT_A_MAPPING",
            f"{MEASUREMENT_SETTINGS_FILENAME} must contain a mapping of settings, not {type(document).__name__}.",
            path=str(path),
        )
    section = document.get("pooling")
    if section is not None and not isinstance(section, dict):
        raise ConfigError(
            "CONFIG_INVALID",
            f"{MEASUREMENT_SETTINGS_FILENAME}: `pooling` must be a mapping of settings.",
            path="pooling",
        )
    try:
        return PoolingSettings.model_validate(section or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        dotted = ".".join(["pooling", *(str(part) for part in first["loc"])])
        raise ConfigError(
            "CONFIG_INVALID", f"{MEASUREMENT_SETTINGS_FILENAME} {dotted}: {first['msg']}.", path=dotted
        ) from exc


# ---------------------------------------------------------------------------
# The statistics (pure)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CellMember:
    """One campaign's registered estimate and its standard error."""

    campaign_id: str
    effect: float
    se: float


@dataclass(frozen=True)
class MemberFit:
    """One campaign's Estimated figure: its own estimate drawn towards the cell's average."""

    campaign_id: str
    effect: float
    se: float
    shrinkage: float
    """`B_j`: the share of the campaign's own figure that is noise, and so the weight on the cell's average."""
    pooled_effect: float
    pooled_se: float

    def interval(self, z: float = _Z) -> tuple[float, float]:
        return self.pooled_effect - z * self.pooled_se, self.pooled_effect + z * self.pooled_se


@dataclass(frozen=True)
class CellFit:
    """The cell's fitted average and spread, and every member's Estimated figure."""

    method: Method
    mean: float
    mean_se: float
    between_variance: float
    members: tuple[MemberFit, ...]

    @property
    def no_spread(self) -> bool:
        return self.between_variance <= 0.0


def _weights(ses: Sequence[float], tau2: float) -> list[float]:
    return [1.0 / (s * s + tau2) for s in ses]


def _weighted_mean(effects: Sequence[float], weights: Sequence[float]) -> float:
    return sum(w * y for w, y in zip(weights, effects, strict=True)) / sum(weights)


def _reml_score(effects: Sequence[float], ses: Sequence[float], tau2: float) -> float:
    """The restricted log-likelihood's derivative in `σ²` (times two): `Σw²(y-μ̂)² - Σw + Σw²/Σw`."""
    weights = _weights(ses, tau2)
    total = sum(weights)
    mean = _weighted_mean(effects, weights)
    squares = sum(w * w for w in weights)
    return (
        sum(w * w * (y - mean) ** 2 for w, y in zip(weights, effects, strict=True)) - total + squares / total
    )


def reml_between_variance(effects: Sequence[float], ses: Sequence[float]) -> float | None:
    """The REML estimate of `σ²`, truncated at zero; None when no root of the score could be bracketed.

    The restricted score is positive at zero exactly when the campaigns spread more than their noise; then its
    root is bracketed by doubling an upper end and found by bisection to a relative tolerance of 1e-12.
    """
    if len(effects) < 2:
        return None
    try:
        at_zero = _reml_score(effects, ses, 0.0)
    except (ZeroDivisionError, OverflowError):
        return None
    if not math.isfinite(at_zero):
        return None
    if at_zero <= 0.0:
        return 0.0
    mean = sum(effects) / len(effects)
    spread = sum((y - mean) ** 2 for y in effects) / (len(effects) - 1)
    high = max(spread, max(s * s for s in ses), 1e-300)
    for _ in range(_REML_ITERATIONS):
        score = _reml_score(effects, ses, high)
        if not math.isfinite(score):
            return None
        if score < 0.0:
            break
        high *= 2.0
    else:
        return None
    low = 0.0
    for _ in range(_REML_ITERATIONS):
        middle = (low + high) / 2.0
        if _reml_score(effects, ses, middle) > 0.0:
            low = middle
        else:
            high = middle
        if high - low <= _REML_TOLERANCE * high:
            return (low + high) / 2.0
    return None


def dl_between_variance(effects: Sequence[float], ses: Sequence[float]) -> float:
    """The DerSimonian-Laird moment estimate of `σ²`: `max(0, (Q - (k - 1)) / (Σw - Σw²/Σw))`, `w = 1/s²`."""
    weights = _weights(ses, 0.0)
    total = sum(weights)
    mean = _weighted_mean(effects, weights)
    q = sum(w * (y - mean) ** 2 for w, y in zip(weights, effects, strict=True))
    denominator = total - sum(w * w for w in weights) / total
    if denominator <= 0.0:
        return 0.0
    return max(0.0, (q - (len(effects) - 1)) / denominator)


def fit_cell(members: Sequence[CellMember], *, method: Method = "reml") -> CellFit:
    """Fit the cell's normal-normal model and every member's Estimated figure (the module docstring).

    `method="reml"` falls back to DerSimonian-Laird (and records `dl`) when REML does not converge. Raises
    `ValueError` with fewer than two members or a standard error that is not a positive finite number.
    """
    if len(members) < 2:
        raise ValueError("A cell needs at least two campaigns to estimate how much they differ.")
    if any(not (math.isfinite(m.se) and m.se > 0.0 and math.isfinite(m.effect)) for m in members):
        raise ValueError("Every campaign needs a finite estimate and a positive, finite standard error.")
    effects = [m.effect for m in members]
    ses = [m.se for m in members]
    used: Method = method
    tau2: float | None = reml_between_variance(effects, ses) if method == "reml" else None
    if tau2 is None:
        tau2, used = dl_between_variance(effects, ses), "dl"
    weights = _weights(ses, tau2)
    total = sum(weights)
    mean = _weighted_mean(effects, weights)
    mean_variance = 1.0 / total
    k = len(members)
    # Morris (1983), r = 1 estimated mean: the share is estimated without bias by (k - 3) / (k - 1) times the
    # plug-in share; the variance is s²(1 - ((k - 1)/k) B) + (2/(k - 3)) B² (y - μ̂)². Three campaigns draw nothing.
    correction = (k - 3) / (k - 1) if k > 3 else 0.0
    spread_term = 2.0 / (k - 3) if k > 3 else 0.0
    fitted: list[MemberFit] = []
    for member in members:
        noise = member.se * member.se
        shrinkage = correction * noise / (noise + tau2)
        pooled = shrinkage * mean + (1.0 - shrinkage) * member.effect
        variance = (
            noise * (1.0 - ((k - 1) / k) * shrinkage)
            + spread_term * shrinkage * shrinkage * (member.effect - mean) ** 2
        )
        fitted.append(
            MemberFit(
                campaign_id=member.campaign_id,
                effect=member.effect,
                se=member.se,
                shrinkage=shrinkage,
                pooled_effect=pooled,
                pooled_se=math.sqrt(variance),
            )
        )
    return CellFit(
        method=used,
        mean=mean,
        mean_se=math.sqrt(mean_variance),
        between_variance=tau2,
        members=tuple(fitted),
    )


# ---------------------------------------------------------------------------
# Reading a stored report
# ---------------------------------------------------------------------------
EstimateField = Literal[
    "absolute_lift", "offers_combined.absolute_lift", "adjusted_interval", "mean_difference_ci"
]


def se_from_interval(interval: ConfidenceValue) -> float | None:
    """The standard error behind a stored range, `(high - low) / (2 z)`; None when the range cannot be read."""
    low, high, level = interval.ci_low, interval.ci_high, interval.confidence_level
    if low is None or high is None or not (0.0 < level < 1.0):
        return None
    if not (math.isfinite(low) and math.isfinite(high) and math.isfinite(interval.value)) or high <= low:
        return None
    return (high - low) / (2.0 * NormalDist().inv_cdf(0.5 + level / 2.0))


def primary_estimate(report: IncrementalityReport) -> tuple[EstimateField, ConfidenceValue] | None:
    """The registered primary estimate of a stored report, and the field it is read from; None when it has none.

    The adjusted difference whenever the plan registered an adjustment (it is the planned analysis), for either
    kind of outcome. Otherwise an amount's plain difference (both read by `measure.headline_estimate`, the rule an
    amount's verdict uses); a yes/no outcome's difference in rates; for several offers, every offer together
    (`offers_combined`), and none for a report of several offers measured before they were added together, whose
    own fields are the first offer's alone.
    """
    if report.adjusted_interval is not None:
        return "adjusted_interval", report.adjusted_interval
    if report.outcome_kind == "continuous":
        from engine.measurement.measure import headline_estimate

        estimate, adjusted = headline_estimate(report)
        if estimate is None:
            return None
        return ("adjusted_interval" if adjusted else "mean_difference_ci"), estimate
    if report.arms is not None and len(report.arms) >= 2:
        combined = report.offers_combined
        if combined is not None and combined.absolute_lift is not None:
            return "offers_combined.absolute_lift", combined.absolute_lift
        return None
    if report.absolute_lift is not None:
        return "absolute_lift", report.absolute_lift
    return None


# ---------------------------------------------------------------------------
# The artefact
# ---------------------------------------------------------------------------
class PoolingCell(StrictBase):
    """What makes campaigns comparable (the module docstring's table); equal cells pool together."""

    use_case_id: str
    outcome_column: str
    positive_label: str | None = None
    outcome_window_days: int | None = None
    outcome_kind: Literal["binary", "continuous"]
    channels: tuple[str, ...] | None = Field(
        default=None, description="Every channel of the catalogue actions the run named; null: no catalogue."
    )
    offer_family: tuple[str, ...] | None = Field(
        default=None, description="The catalogue action ids the run named; null: no catalogue."
    )
    holdout: str = Field(description="`run`, or `<scope>:<key>:epoch-<n>` for a persistent holdout's epoch.")
    synthetic: bool = Field(description="Measured on generated data; pooled only with other generated data.")
    client_id: str | None = Field(
        default=None,
        description="The client of the campaign's first scoring run (`run.json`); campaigns pool within one client.",
    )

    @property
    def cell_id(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return "cell_" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


class PooledEstimate(Artefact):
    """`campaigns/<id>/pooled_estimate.json`: the campaign's Estimated figure, beside its measured one."""

    campaign_id: str
    label: Literal["Estimated"] = Field(
        default="Estimated", description="Never a measured result (decision K7)."
    )
    status: Literal["pooled", "not_pooled"]
    code: str | None = Field(
        default=None, description="`POOL_TOO_FEW` when nothing is pooled; null otherwise."
    )
    reason: str | None = Field(default=None, description="Why nothing is pooled, in one plain sentence.")
    cell_id: str
    cell: PoolingCell
    estimate_field: EstimateField = Field(description="The report field the measured estimate was read from.")
    measured_effect: float = Field(
        description="The campaign's own registered estimate, as the report states it."
    )
    measured_se: float = Field(description="Its standard error, read from the width of its stored range.")
    pooled_effect: float | None = Field(
        default=None, description="The Estimated figure; null when not pooled."
    )
    pooled_interval: ConfidenceValue | None = Field(
        default=None, description="pooled_effect with its 95% range (Morris 1983); null when not pooled."
    )
    pooled_from: tuple[str, ...] = Field(
        description="Every comparable campaign of the cell, this one included."
    )
    campaigns_compared: int = Field(description="How many campaigns `pooled_from` names.")
    shrinkage: float | None = Field(
        default=None,
        description="The weight on the cell's average (Morris's corrected B); null when not pooled.",
    )
    cell_mean: float | None = Field(default=None, description="The cell's weighted average estimate.")
    cell_mean_se: float | None = Field(default=None, description="Its standard error.")
    between_variance: float | None = Field(
        default=None,
        description="How much the campaigns' true effects differ (σ̂², REML or DerSimonian-Laird).",
    )
    method: Literal["reml", "dl"] | None = Field(default=None, description="How σ̂² was estimated.")
    warnings: tuple[str, ...] = Field(default=(), description="`POOL_NO_SPREAD` when σ̂² is zero.")
    report_computed_at: AwareDatetime = Field(
        description="`computed_at` of the report the estimate was read from."
    )
    member_reports: dict[str, AwareDatetime] = Field(
        description="`computed_at` of every member's report, this one's included: a re-measured member makes it stale."
    )
    settings: PoolingSettings = Field(
        description="The settings it was fitted under: changed settings make it stale."
    )
    computed_at: AwareDatetime


class PooledPrior(Artefact):
    """The cell's average and spread, offered to the next model's learning as information only (Plan J M106)."""

    label: Literal["Estimated"] = "Estimated"
    cell_id: str
    estimate_field: EstimateField
    mean: float = Field(description="The comparable campaigns' weighted average effect.")
    mean_se: float = Field(description="Its standard error.")
    between_variance: float = Field(description="How much their true effects differ (σ̂²).")
    campaigns: int = Field(description="Comparable campaigns it is drawn from.")
    method: Literal["reml", "dl"]
    note: str = Field(description="What it is, and that it changes nothing about how the model is chosen.")


_TOO_FEW_REASON: Final[str] = (
    "Fewer comparable campaigns have a final result than pooling needs, so this campaign is shown only as measured."
)
_THREE_REASON: Final[str] = (
    "Three comparable campaigns have a final result: the estimate can be drawn towards them only from the fourth "
    "on, so this campaign is shown only as measured."
)
_PRIOR_NOTE: Final[str] = (
    "The average and spread of comparable campaigns' measured effects, estimated, shown for information. It does "
    "not change how the next model is chosen or approved."
)
_POOLABLE_BASES: Final[frozenset[str]] = frozenset({"engine_random", "verified_random"})


def pooled_key(campaign_id: str) -> str:
    """`campaigns/<id>/pooled_estimate.json`."""
    return f"campaigns/{campaign_id}/{POOLED_FILENAME}"


@dataclass(frozen=True)
class CellEntry:
    """A campaign as a cell member: its cell, who it measures, and its registered estimate."""

    campaign_id: str
    cell: PoolingCell
    customers: tuple[str, ...]
    rank: tuple[str, str, str]
    estimate_field: EstimateField
    effect: float
    se: float
    report_computed_at: datetime


def _json(storage: Storage, key: str) -> Any:
    from engine.storage import StorageError

    try:
        return json.loads(storage.read_bytes(key)) if storage.exists(key) else None
    except (StorageError, ValueError):
        return None


def _field(document: Any, name: str) -> Any:
    return document.get(name) if isinstance(document, dict) else None


def _synthetic(storage: Storage, campaign: Campaign) -> bool:
    """Generated data anywhere behind the campaign, read the way the Value Proof Pack reads it."""
    from engine.pilot.proof import safe_identifier

    uploads: list[object] = [campaign.outcomes.upload_id] if campaign.outcomes is not None else []
    contacts = _json(storage, f"campaigns/{campaign.campaign_id}/contact_readout.json")
    if _field(contacts, "synthetic") is True:
        return True
    uploads.append(_field(contacts, "contact_upload_id"))
    for upload_id in uploads:
        if safe_identifier(upload_id) and _field(
            _json(storage, f"uploads/{upload_id}/upload.json"), "synthetic"
        ):
            return True
    return any(
        safe_identifier(run_id) and _field(_json(storage, f"runs/{run_id}/run.json"), "synthetic") is True
        for run_id in campaign.run_ids
    )


def _catalogue(storage: Storage, campaign: Campaign) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None]:
    """`(channels, offer family)` from the runs' `catalogue_stamp.json`; `(None, None)` with no catalogue."""
    from engine.pilot.proof import safe_identifier

    channels: set[str] = set()
    actions: set[str] = set()
    stamped = False
    for run_id in campaign.run_ids:
        if not safe_identifier(run_id):
            continue
        planned = _field(_json(storage, f"runs/{run_id}/catalogue_stamp.json"), "planned_channels")
        if not isinstance(planned, dict):
            continue
        stamped = True
        for action_id, listed in planned.items():
            actions.add(str(action_id))
            channels.update(str(channel) for channel in listed or ())
    if not stamped:
        return None, None
    return tuple(sorted(channels)), tuple(sorted(actions))


def _client(storage: Storage, campaign: Campaign) -> str | None:
    """The client of the campaign's first scoring run, read as the monthly loop reads it (`cycle._client_matches`)."""
    from engine.pilot.proof import safe_identifier

    if not campaign.run_ids or not safe_identifier(campaign.run_ids[0]):
        return None
    client = _field(_json(storage, f"runs/{campaign.run_ids[0]}/run.json"), "client_id")
    return client if isinstance(client, str) else None


def _holdout(campaign: Campaign) -> str:
    from engine.holdout.spec import PERSISTENT_SCOPES

    if campaign.holdout_scope in PERSISTENT_SCOPES:
        return f"{campaign.holdout_scope}:{campaign.holdout_scope_key}:epoch-{campaign.holdout_epoch}"
    return "run"


def cell_entry(storage: Storage, campaign: Campaign) -> CellEntry | None:
    """The campaign as a cell member, or None when it may not enter one (the module docstring's rule)."""
    from pydantic import ValidationError

    from engine.measurement.campaign import REPORT_FILENAME, CampaignKind, CampaignStatus, campaign_key
    from engine.pilot.proof import safe_identifier
    from engine.storage import StorageError
    from engine.uplift.contracts import IncrementalityReport, IncrementalityStatus

    if (
        campaign.use_case_id is None
        or campaign.kind is CampaignKind.PROGRAMME
        or campaign.status is not CampaignStatus.MEASURED
        or campaign.causal_basis not in _POOLABLE_BASES
        or not safe_identifier(campaign.campaign_id)
    ):
        return None
    try:
        report = storage.read_model(campaign_key(campaign.campaign_id, REPORT_FILENAME), IncrementalityReport)
    except (StorageError, ValidationError, ValueError):
        return None
    if report.early_look or not report.causal or report.status is not IncrementalityStatus.MATURE:
        return None
    found = primary_estimate(report)
    if found is None:
        return None
    field_name, interval = found
    se = se_from_interval(interval)
    if se is None:
        return None
    channels, family = _catalogue(storage, campaign)
    cell = PoolingCell(
        use_case_id=campaign.use_case_id,
        outcome_column=report.outcome_column,
        positive_label=campaign.outcomes.positive_label if campaign.outcomes is not None else None,
        outcome_window_days=report.outcome_window_days,
        outcome_kind=report.outcome_kind or "binary",
        channels=channels,
        offer_family=family,
        holdout=_holdout(campaign),
        synthetic=_synthetic(storage, campaign),
        client_id=_client(storage, campaign),
    )
    runs = campaign.run_ids
    customers = ("runs", *sorted(runs)) if runs else ("campaign", campaign.campaign_id)
    return CellEntry(
        campaign_id=campaign.campaign_id,
        cell=cell,
        customers=customers,
        rank=(report.as_of.isoformat(), campaign.created_at.isoformat(), campaign.campaign_id),
        estimate_field=field_name,
        effect=interval.value,
        se=se,
        report_computed_at=report.computed_at,
    )


def _estimate(
    entry: CellEntry, members: Sequence[CellEntry], settings: PoolingSettings, now: datetime, **fields: Any
) -> PooledEstimate:
    ids = tuple(member.campaign_id for member in members)
    return PooledEstimate(
        campaign_id=entry.campaign_id,
        cell_id=entry.cell.cell_id,
        cell=entry.cell,
        estimate_field=entry.estimate_field,
        measured_effect=entry.effect,
        measured_se=entry.se,
        pooled_from=ids,
        campaigns_compared=len(ids),
        report_computed_at=entry.report_computed_at,
        member_reports={member.campaign_id: member.report_computed_at for member in members},
        settings=settings,
        computed_at=now,
        **fields,
    )


def pool_entries(
    entries: Sequence[CellEntry], settings: PoolingSettings, now: datetime
) -> list[PooledEstimate]:
    """Every member's figure: one member per set of customers (the latest measured), grouped by cell, then fitted."""
    latest: dict[tuple[str, ...], CellEntry] = {}
    for entry in entries:
        held = latest.get(entry.customers)
        if held is None or entry.rank > held.rank:
            latest[entry.customers] = entry
    cells: dict[str, list[CellEntry]] = {}
    for entry in latest.values():
        cells.setdefault(entry.cell.cell_id, []).append(entry)
    out: list[PooledEstimate] = []
    for members in cells.values():
        members.sort(key=lambda entry: entry.campaign_id)
        if len(members) < max(settings.min_campaigns, FIRST_DRAWN):
            reason = _TOO_FEW_REASON if len(members) < settings.min_campaigns else _THREE_REASON
            out += [
                _estimate(
                    entry, members, settings, now, status="not_pooled", code=POOL_TOO_FEW, reason=reason
                )
                for entry in members
            ]
            continue
        fit = fit_cell([CellMember(entry.campaign_id, entry.effect, entry.se) for entry in members])
        warnings = (POOL_NO_SPREAD,) if fit.no_spread else ()
        for entry, member in zip(members, fit.members, strict=True):
            low, high = member.interval()
            out.append(
                _estimate(
                    entry,
                    members,
                    settings,
                    now,
                    status="pooled",
                    pooled_effect=member.pooled_effect,
                    pooled_interval=ConfidenceValue(
                        value=member.pooled_effect, ci_low=low, ci_high=high, confidence_level=CONFIDENCE
                    ),
                    shrinkage=member.shrinkage,
                    cell_mean=fit.mean,
                    cell_mean_se=fit.mean_se,
                    between_variance=fit.between_variance,
                    method=fit.method,
                    warnings=warnings,
                )
            )
    return out


def _write_use_case(
    storage: Storage, campaigns: Sequence[Campaign], settings: PoolingSettings, now: datetime
) -> tuple[PooledEstimate, ...]:
    """Rewrite every file of one use case's `campaigns`; a campaign that no longer enters a cell loses its file."""
    entries = [entry for item in campaigns if (entry := cell_entry(storage, item)) is not None]
    written = pool_entries(entries, settings, now)
    for estimate in written:
        storage.write_model(pooled_key(estimate.campaign_id), estimate)
    kept = {estimate.campaign_id for estimate in written}
    for item in campaigns:
        key = pooled_key(item.campaign_id)
        if item.campaign_id not in kept and storage.exists(key):
            storage.delete(key)
    return tuple(written)


def refresh_pooled_estimates(
    storage: Storage,
    store: CampaignStore,
    campaign: Campaign,
    *,
    root: Path | None = None,
    settings: PoolingSettings | None = None,
    now: datetime,
) -> tuple[PooledEstimate, ...]:
    """Rewrite `pooled_estimate.json` of every campaign of `campaign`'s use case; nothing when pooling is off.

    Called once a measurement is stored. Reads only the stored reports, records, run stamps and upload records
    (aggregate files); a campaign that no longer enters a cell loses its file, so no stale figure stays behind.
    `settings` are the ones already read from `root` (`load_pooling_settings`), when the caller has them.
    """
    settings = settings if settings is not None else load_pooling_settings(root)
    if not settings.enabled or campaign.use_case_id is None:
        return ()
    written = _write_use_case(
        storage, store.list(use_case_id=campaign.use_case_id, limit=None), settings, now
    )
    pooled = sum(estimate.status == "pooled" for estimate in written)
    _LOGGER.info(
        "pooling.refresh use_case=%s campaigns=%d pooled=%d", campaign.use_case_id, len(written), pooled
    )
    return written


def _same(document: Any, expected: PooledEstimate) -> bool:
    """Whether a stored file says what `expected` says, apart from when it was written."""
    from pydantic import ValidationError

    if not isinstance(document, dict):
        return False
    try:
        stored = PooledEstimate.model_validate(document)
    except ValidationError:
        return False
    return stored.model_copy(update={"computed_at": expected.computed_at}) == expected


def refresh_stale_pooled_estimates(
    storage: Storage,
    store: CampaignStore,
    use_case_ids: Iterable[str],
    *,
    settings: PoolingSettings,
    now: datetime,
) -> tuple[str, ...]:
    """Rewrite the files of every use case named whose files differ from what would be written now.

    The way pooling catches up when it is turned on, or its settings change: campaigns measured while it was off
    have no file, and a member re-measured since, a campaign that joined a cell or a changed `min_campaigns` leaves
    the others' files behind. Every campaign of each use case is read from `store`. Returns the use cases
    rewritten; nothing when pooling is off.
    """
    if not settings.enabled:
        return ()
    rewritten: list[str] = []
    for use_case_id in sorted(set(use_case_ids)):
        members = store.list(use_case_id=use_case_id, limit=None)
        entries = [entry for item in members if (entry := cell_entry(storage, item)) is not None]
        expected = {estimate.campaign_id: estimate for estimate in pool_entries(entries, settings, now)}
        current = all(
            (
                _same(_json(storage, pooled_key(item.campaign_id)), expected[item.campaign_id])
                if item.campaign_id in expected
                else not storage.exists(pooled_key(item.campaign_id))
            )
            for item in members
        )
        if not current:
            _write_use_case(storage, members, settings, now)
            rewritten.append(use_case_id)
    if rewritten:
        _LOGGER.info("pooling.catch_up use_cases=%d", len(rewritten))
    return tuple(rewritten)


def _same_time(stamp: Any, computed: Any) -> bool:
    from datetime import datetime

    if not isinstance(stamp, str) or not isinstance(computed, str):
        return False
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")) == datetime.fromisoformat(
            computed.replace("Z", "+00:00")
        )
    except ValueError:
        return False


def is_current(
    pooled: Any,
    report: Any,
    *,
    settings: PoolingSettings | None = None,
    report_of: Callable[[str], Any] | None = None,
) -> bool:
    """Whether a `pooled_estimate.json` document is still what pooling says now.

    It must have been read from this `incrementality_report.json` document; with `settings`, fitted under them;
    with `report_of` (a campaign id to its stored report document), every member's report must be the one it read.
    """
    if not _same_time(_field(pooled, "report_computed_at"), _field(report, "computed_at")):
        return False
    if settings is not None and _field(pooled, "settings") != settings.model_dump(mode="json"):
        return False
    if report_of is not None:
        members = _field(pooled, "member_reports")
        if not isinstance(members, dict) or not members:
            return False
        for member_id, stamp in members.items():
            if not _same_time(stamp, _field(report_of(str(member_id)), "computed_at")):
                return False
    return True


def read_pooled_estimate(
    storage: Storage, campaign_id: str, *, settings: PoolingSettings | None = None
) -> PooledEstimate | None:
    """The campaign's current Estimated figure, or None: no file, a stale file (`is_current`), or unreadable."""
    from pydantic import ValidationError

    from engine.measurement.campaign import REPORT_FILENAME, campaign_key
    from engine.pilot.proof import safe_identifier

    def report_of(member_id: str) -> Any:
        return (
            _json(storage, campaign_key(member_id, REPORT_FILENAME)) if safe_identifier(member_id) else None
        )

    document = _json(storage, pooled_key(campaign_id))
    if not is_current(document, report_of(campaign_id), settings=settings, report_of=report_of):
        return None
    try:
        return PooledEstimate.model_validate(document)
    except ValidationError:
        return None


def pooled_prior(storage: Storage, campaign_id: str, *, root: Path | None = None) -> PooledPrior | None:
    """The campaign's cell average and spread as a prior for the next model, when pooling is on and it pooled."""
    settings = load_pooling_settings(root)
    if not settings.enabled:
        return None
    estimate = read_pooled_estimate(storage, campaign_id, settings=settings)
    if (
        estimate is None
        or estimate.status != "pooled"
        or estimate.cell_mean is None
        or estimate.cell_mean_se is None
        or estimate.between_variance is None
        or estimate.method is None
    ):
        return None
    return PooledPrior(
        cell_id=estimate.cell_id,
        estimate_field=estimate.estimate_field,
        mean=estimate.cell_mean,
        mean_se=estimate.cell_mean_se,
        between_variance=estimate.between_variance,
        campaigns=estimate.campaigns_compared,
        method=estimate.method,
        note=_PRIOR_NOTE,
    )
