"""Enough data (Plan L M158, DEC-1580 … DEC-1584): when a use case's data is short, what to do instead.

Data Doctor never just stops. When Guided setup's advisor stops on a file (`readiness` says `too_small`,
or `missing` for a stop no fix can mend), `enough_data` says three things, each from a number the
engine measured or a threshold it is configured with, never from a guess:

* **What is short and by how much.** The Run button's own checks (`engine.stages.validate`'s
  `ROWS_TOO_FEW` and `TARGET_TOO_FEW_POSITIVES`, through `params_from_config`, so the thresholds are
  the use case's `validation.min_rows` and `validation.min_positive`) are run on the rows the advisor
  read; their measured counts are the shortfall. When the advisor read fewer rows than the file has,
  the sentence says so.
* **What you can do now.** Only what the product can do: another use case on Home that the files do
  power (added across use cases by `with_alternatives`, from the same verdicts), and, for a use case
  whose actions contact customers (`actions.contacts_customers`), collecting the missing outcomes with
  the next campaign, run with this use case's control group (`effective_holdout_fraction`) and sized by
  the test planner (`engine.measurement.planner.mde_two_proportions`, at `configs/pilot/readiness.yaml`'s
  confidence and power, in the direction the use case's campaigns aim for) on the customers and the
  outcome rate measured in the file. "Audit a campaign" on the Results page reads such a campaign. No date window
  or snapshot window is applied before a readiness verdict today, so "use a longer history" is never
  offered: there is nothing shorter to undo.
* **What to collect, and from whom.** How many more rows (at the file's own outcome rate, how many
  customers it takes to reach `validation.min_positive`), where the outcome usually comes from
  (`target.label_source`), and the pilot's data request for the use case
  (`engine.pilot.data_request.build_data_request`: the tables it asks for and the history it needs),
  which the screen offers as a download (`GET /pilot/data-request?use_case=<id>`).

Every sentence is plain language (`engine.pilot.plain.jargon_in` finds nothing in it; the tests check).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import Field

from engine.config import StrictBase
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    import pandas as pd

    from engine.config import UseCaseConfig
    from engine.doctor.contracts import UseCaseReadiness

__all__ = [
    "NextOption",
    "NextSteps",
    "Shortfall",
    "enough_data",
    "with_alternatives",
]

_LOG = get_logger(__name__)

ShortfallCode = Literal["ROWS_TOO_FEW", "TARGET_TOO_FEW_POSITIVES"]
OptionKind = Literal["other_use_case", "collect_with_control_group"]

_AIM_WORDS: Final[dict[str, str]] = {"down": "fall", "up": "rise", "either": "change"}
_DAYS_PER_MONTH: Final[int] = 30
"""As the pilot's data request turns days of history into months (`engine.pilot.data_request._months`)."""


class Shortfall(StrictBase):
    """One threshold the file does not reach: the measured count, the configured minimum, the gap."""

    code: ShortfallCode = Field(description="The Run button's check that decides it.")
    measured: int = Field(description="What the file has, counted.")
    needed: int = Field(description="The use case's configured minimum.")
    short_by: int = Field(description="`needed - measured`.")
    setting: str = Field(
        description="The setting the minimum comes from: `validation.min_rows` or `validation.min_positive`."
    )
    rows_counted: int = Field(description="Rows the count was made on.")
    file_rows: int = Field(description="Rows in the whole file.")
    sentence: str = Field(description="The shortfall in one plain sentence.")


class NextOption(StrictBase):
    """Something the person can do now, which the product can do."""

    kind: OptionKind = Field(
        description=(
            "`other_use_case`: a use case the same files power today; `collect_with_control_group`: run the "
            "next campaign with a control group so its outcomes become history."
        )
    )
    title: str = Field(description="What to do, in a few words.")
    detail: str = Field(description="Why and how, in plain sentences, with the measured numbers.")
    use_case_id: str | None = Field(default=None, description="For `other_use_case`: the use case to start.")
    upload_id: str | None = Field(
        default=None, description="For `other_use_case`: the file to start it from."
    )
    control_group: int | None = Field(
        default=None, description="For `collect_with_control_group`: customers to keep back at random."
    )
    contacted: int | None = Field(
        default=None, description="For `collect_with_control_group`: customers to contact."
    )
    smallest_change_points: float | None = Field(
        default=None,
        description=(
            "For `collect_with_control_group`: the smallest change in the outcome rate, in percentage points, "
            "such a campaign can show (the test planner); null with the reason in `detail`."
        ),
    )


class NextSteps(StrictBase):
    """What to do when the data is not enough: the shortfall, the options, what to collect (M158)."""

    shortfalls: tuple[Shortfall, ...] = Field(default=(), description="What is short, and by how much.")
    options: tuple[NextOption, ...] = Field(default=(), description="What can be done now, best first.")
    collect: tuple[str, ...] = Field(
        default=(), description="What to collect and from whom, in plain sentences."
    )
    data_request_use_case: str | None = Field(
        default=None,
        description="The use case whose data request (`GET /pilot/data-request?use_case=`) lists what to ask for.",
    )


def enough_data(
    frame: pd.DataFrame,
    *,
    outcome: str,
    config: UseCaseConfig,
    root: Path,
    file_name: str,
    file_rows: int | None = None,
) -> NextSteps:
    """What is short in `frame` for `config`, what can be done now and what to collect.

    `frame` is what the advisor read (perhaps the first rows of a longer file: `file_rows` is the whole
    file's count). Never raises: a part that cannot be worked out is left out and logged.
    """
    counted = len(frame)
    whole = max(file_rows if file_rows is not None else counted, counted)
    shortfalls, counts = _shortfalls(frame, outcome, config, file_name, counted, whole)
    options: list[NextOption] = []
    if config.actions.contacts_customers and counts is not None:
        option = _collect_with_control_group(config, root, counts, counted, file_name)
        if option is not None:
            options.append(option)
    return NextSteps(
        shortfalls=tuple(shortfalls),
        options=tuple(options),
        collect=_collect(config, root, shortfalls, counts, counted, file_name),
        data_request_use_case=config.id,
    )


def with_alternatives(verdicts: Sequence[UseCaseReadiness]) -> tuple[UseCaseReadiness, ...]:
    """`verdicts` with, on each one that is not ready, the use cases the same files do power today."""
    powered = [
        v for v in verdicts if v.status in {"ready", "check"} and v.upload_id is not None and v.outcome_column
    ]
    out: list[UseCaseReadiness] = []
    for verdict in verdicts:
        if verdict.status in {"ready", "check"}:
            out.append(verdict)
            continue
        others = tuple(
            NextOption(
                kind="other_use_case",
                title=f"Start {other.name} instead",
                detail=f"Your files can power {other.name} today. {other.summary}",
                use_case_id=other.use_case_id,
                upload_id=other.upload_id,
            )
            for other in powered
            if other.use_case_id != verdict.use_case_id
        )
        current: NextSteps | None = getattr(verdict, "next_steps", None)
        if current is None:
            current = NextSteps(data_request_use_case=verdict.use_case_id)
        updated = current.model_copy(update={"options": (*others, *current.options)})
        out.append(verdict.model_copy(update={"next_steps": updated}))
    return tuple(out)


# ---------------------------------------------------------------------------
# What is short
# ---------------------------------------------------------------------------
class _Counts:
    """The outcome counted the way the Run button counts it."""

    def __init__(self, label: str, positives: int, negatives: int) -> None:
        self.label = label
        self.positives = positives
        self.negatives = negatives

    @property
    def rate(self) -> float | None:
        total = self.positives + self.negatives
        return self.positives / total if total else None


def _shortfalls(
    frame: pd.DataFrame, outcome: str, config: UseCaseConfig, file_name: str, counted: int, whole: int
) -> tuple[list[Shortfall], _Counts | None]:
    from engine.stages.validate import (
        check_rows_too_few,
        check_target_too_few_positives,
        params_from_config,
        resolve_positive_label,
    )

    params = params_from_config(config, target=outcome, row_count=counted)
    entity = _entity(config)
    tail = f" Counted on the first {counted:,} of its {whole:,} rows." if whole > counted else ""
    found: list[Shortfall] = []
    counts: _Counts | None = None
    try:
        label, positives, negatives = resolve_positive_label(frame, params)
        if label is not None:
            counts = _Counts(str(label), positives, negatives)
    except Exception:  # a count that cannot be made is left out, never guessed
        _LOG.info("doctor.sufficiency.count_skipped use_case=%s", config.id)
    for result, setting in (
        (check_rows_too_few(frame, params), "validation.min_rows"),
        (check_target_too_few_positives(frame, params), "validation.min_positive"),
    ):
        for finding in result.findings:
            details: dict[str, Any] = dict(finding.details)
            if finding.code == "ROWS_TOO_FEW":
                measured, needed = int(details["rows"]), int(details["min_rows"])
                sentence = (
                    f"{file_name} has {measured:,} {entity}s; the model needs at least {needed:,}, "
                    f"so {needed - measured:,} more.{tail}"
                )
            else:
                measured, needed = int(details["positive_count"]), int(details["min_positive"])
                label_text = details.get("positive_label")
                sentence = (
                    f"{file_name} has {measured:,} {entity}s with the outcome ({outcome} = {label_text}); "
                    f"the model needs at least {needed:,}, so {needed - measured:,} more.{tail}"
                )
            found.append(
                Shortfall(
                    code=finding.code,  # type: ignore[arg-type]  # one of the two codes run above
                    measured=measured,
                    needed=needed,
                    short_by=needed - measured,
                    setting=setting,
                    rows_counted=counted,
                    file_rows=whole,
                    sentence=sentence,
                )
            )
    return found, counts


def _entity(config: UseCaseConfig) -> str:
    return (config.entity or "customer").strip() or "customer"


# ---------------------------------------------------------------------------
# What can be done now
# ---------------------------------------------------------------------------
def _collect_with_control_group(
    config: UseCaseConfig, root: Path, counts: _Counts, eligible: int, file_name: str
) -> NextOption | None:
    """The next campaign, run with this use case's control group, sized by the test planner."""
    from engine.holdout.spec import effective_holdout_fraction
    from engine.measurement.planner import arm_sizes, mde_two_proportions
    from engine.pilot.readiness import campaign_aim, load_readiness_settings

    rate = counts.rate
    if rate is None or eligible <= 0:
        return None
    try:
        share = effective_holdout_fraction(config.actions)
        settings = load_readiness_settings(root)
        aim = campaign_aim(config.id, root)
        contacted, kept_back = arm_sizes(eligible, share)
        mde = mde_two_proportions(contacted, kept_back, rate, settings.alpha, settings.power, direction=aim)
    except Exception:  # the option is a courtesy; a planner that cannot answer leaves it out
        _LOG.info("doctor.sufficiency.planner_skipped use_case=%s", config.id)
        return None
    entity = _entity(config)
    confidence = (1.0 - settings.alpha) * 100.0
    if mde.points is not None:
        seen = (
            f"With the outcome rate in your file ({rate:.1%}), such a campaign can show a "
            f"{_AIM_WORDS[aim]} of {mde.points:.1f} points or more ({settings.power:.0%} chance, "
            f"{confidence:g}% confidence)."
        )
    else:
        seen = f"How small a change it could show is not known: {mde.reason}"
    detail = (
        f"Run your next campaign to the {eligible:,} {entity}s in {file_name}, keeping {kept_back:,} "
        f"({share:.0%}, the control group this use case keeps) back at random. {seen} Record who was "
        'contacted, who was kept back and what happened: "Audit a campaign" on the Results page reads it, and the '
        f"outcomes you record are more history for {config.name}."
    )
    return NextOption(
        kind="collect_with_control_group",
        title="Collect the outcome with your next campaign",
        detail=detail,
        control_group=kept_back,
        contacted=contacted,
        smallest_change_points=round(mde.points, 2) if mde.points is not None else None,
    )


# ---------------------------------------------------------------------------
# What to collect, and from whom
# ---------------------------------------------------------------------------
def _collect(
    config: UseCaseConfig,
    root: Path,
    shortfalls: Sequence[Shortfall],
    counts: _Counts | None,
    rows: int,
    file_name: str,
) -> tuple[str, ...]:
    entity = _entity(config)
    lines: list[str] = []
    by_code = {shortfall.code: shortfall for shortfall in shortfalls}
    short_rows = by_code.get("ROWS_TOO_FEW")
    if short_rows is not None:
        lines.append(
            f"At least {short_rows.short_by:,} more {entity}s like the ones in {file_name}, each with its outcome."
        )
    short_outcomes = by_code.get("TARGET_TOO_FEW_POSITIVES")
    if short_outcomes is not None and counts is not None and counts.rate:
        total = math.ceil(short_outcomes.needed / counts.rate - 1e-9)
        more = max(0, total - rows)
        lines.append(
            f"At the outcome rate in your file ({counts.rate:.1%}), {short_outcomes.needed:,} {entity}s with "
            f"the outcome means about {total:,} {entity}s in all, {more:,} more than {file_name} has."
        )
    source = config.target.label_source.strip().rstrip(".")
    if source:
        lines.append(f"The outcome usually comes from: {source}.")
    request = _data_request_line(config, root)
    if request:
        lines.append(request)
    return tuple(lines)


def _data_request_line(config: UseCaseConfig, root: Path) -> str | None:
    """What the pilot's data request asks for, for this use case: its tables and the history it needs."""
    from engine.pilot.data_request import build_data_request

    try:
        request = build_data_request((config.id,), root)
    except Exception:  # a use case the request cannot describe gets no line, never a made-up one
        _LOG.info("doctor.sufficiency.data_request_skipped use_case=%s", config.id)
        return None
    tables = [f"{table.title} ({table.file})" for table in request.tables if table.need == "required"]
    if not tables:
        return None
    minimum = math.ceil(request.history_min_days / _DAYS_PER_MONTH)
    recommended = math.ceil(request.history_recommended_days / _DAYS_PER_MONTH)
    listed = tables[0] if len(tables) == 1 else f"{', '.join(tables[:-1])} and {tables[-1]}"
    return (
        f"The data request for {config.name} lists what to ask for: {listed}, with at least {minimum} "
        f"months of history ({recommended} recommended)."
    )
