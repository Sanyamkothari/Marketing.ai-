"""Section 9 of a journey's `run_report.md`: Plan K's measurement upgrades, read from `plan_k.results.json` (DEC-1413).

`library/journey.py` (`run_journey(plan_k=True)`) writes `plan_k.results.json`; this module turns it into the report's
last section. The method is the manager-demo summary's (`scripts/demo_summary.py`, M111, DEC-1321), kept apart so the
library owns no import of `scripts`:

* every number the section prints is a :class:`Figure`: its value, the text printed, the format that makes the text
  from the value, and the dotted path of the field of `plan_k.results.json` it was read from;
* the words around the numbers hold **no digit**: a sentence is a template with named holes and a hole is filled by a
  figure's text, so a number cannot be typed into a sentence;
* every verdict ("narrower", "safe", "not applicable") is computed from the figures' values, so a result that fell the
  other way produces the other sentence;
* :func:`verify` reads the document again, recomputes every figure and every printed text and checks the words for
  digits; :func:`build_section` refuses to return a section that does not verify, and a field the document lacks stops
  the section (`PlanKReportError`) instead of being shown as zero or left out.

Pure Python: nothing heavy is imported.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from typing import Any, Final, Literal

from pydantic import Field

from engine.config import StrictBase

__all__ = [
    "ARTEFACT",
    "Block",
    "Figure",
    "PlanKReportError",
    "Section",
    "build_section",
    "digits_of",
    "provenance",
    "render_plan_k",
    "verify",
]

ARTEFACT: Final = "plan_k.results.json"
"""How the provenance names the document: every field path is inside this one file."""
DIGITS: Final = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?")
"""A number as a reader sees it; the words of the section may hold none that no figure prints."""
SECTION_NUMBER: Final = "9"
MILESTONE: Final = re.compile(r"\bM\d{3}\b")
"""A milestone's name ("M135") in the words is a reference to the plan, not a result: it is allowed among the words."""

Format = Literal[
    "count",
    "points",
    "signed_points",
    "percent",
    "percent2",
    "scientific",
    "signed_conversions",
    "conversions",
    "inr",
    "seconds",
    "text",
]


class PlanKReportError(Exception):
    """The document does not hold something the section needs, or the section does not trace back to it."""


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------
def _zero_free(value: float) -> float:
    return 0.0 if value == 0 else value


def _signed(text: str) -> str:
    return text if text.startswith("-") else f"+{text}"


def format_value(fmt: Format, value: float | str) -> str:
    """The text printed for `value` in format `fmt`: the one way a value becomes text in the section."""
    if fmt == "text":
        return str(value)
    number = float(value)
    if fmt == "count":
        return f"{round(number):,}"
    if fmt == "points":
        return f"{_zero_free(round(number * 100.0, 2)):.2f} pts"
    if fmt == "signed_points":
        return _signed(f"{_zero_free(round(number * 100.0, 2)):.2f}") + " pts"
    if fmt == "percent":
        return f"{_zero_free(round(number * 100.0, 0)):.0f} %"
    if fmt == "percent2":
        return f"{_zero_free(round(number * 100.0, 2)):.2f} %"
    if fmt == "scientific":
        return f"{number:.2e}"
    if fmt == "conversions":
        return f"{_zero_free(round(number, 1)):,.1f}"
    if fmt == "signed_conversions":
        return _signed(f"{_zero_free(round(number, 1)):,.1f}")
    if fmt == "seconds":
        return f"{_zero_free(round(number, 1)):,.1f} s"
    sign = "-" if number < 0 else ""  # inr
    return f"{sign}₹{abs(number):,.0f}"


class Figure(StrictBase):
    """One number the section prints (or one piece of artefact text), with the field it was read from."""

    value: float | str
    text: str
    format: Format
    field: str


class Block(StrictBase):
    """A line of the section: literal markup, or a sentence with named holes and the figure that fills each."""

    kind: Literal["heading", "literal", "claim", "row"]
    template: str
    figures: dict[str, Figure] = Field(default_factory=dict)

    def words(self) -> str:
        return self.template.format(**{name: figure.text for name, figure in self.figures.items()})


class Section(StrictBase):
    blocks: tuple[Block, ...]


def walk(document: Any, path: str) -> Any:
    """The value at dotted `path` of a JSON document; raises `PlanKReportError` when it is not there."""
    current = document
    for part in path.split("."):
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise PlanKReportError(f"{ARTEFACT} holds nothing at {path!r}")
    return current


class _Reader:
    """Makes figures from the document; a missing or non-numeric field stops the section."""

    def __init__(self, results: Mapping[str, Any]) -> None:
        self.results = results

    def raw(self, path: str) -> Any:
        return walk(self.results, path)

    def has(self, path: str) -> bool:
        """Whether the field exists and is not null (a null is a fact the section words, not a gap)."""
        try:
            return walk(self.results, path) is not None
        except PlanKReportError:
            return False

    def number(self, path: str) -> float:
        value = self.raw(path)
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise PlanKReportError(f"{path!r} of {ARTEFACT} is not a finite number: {value!r}")
        return float(value)

    def flag(self, path: str) -> bool:
        value = self.raw(path)
        if not isinstance(value, bool):
            raise PlanKReportError(f"{path!r} of {ARTEFACT} is not true or false: {value!r}")
        return value

    def fig(self, path: str, fmt: Format) -> Figure:
        value: float | str
        if fmt == "text":
            raw = self.raw(path)
            if not isinstance(raw, str):
                raise PlanKReportError(f"{path!r} of {ARTEFACT} is not text: {raw!r}")
            value = raw
        else:
            value = self.number(path)
        return Figure(value=value, text=format_value(fmt, value), format=fmt, field=path)


def _side(low: float, high: float) -> Literal["above", "below", "includes"]:
    """Which side of zero a range lies: the verdict of every interval in the section."""
    if low > 0:
        return "above"
    if high < 0:
        return "below"
    return "includes"


_SIDE: Final = {
    "above": "the whole range is above zero",
    "below": "the whole range is below zero",
    "includes": "the range includes zero",
}
NEGLIGIBLE: Final = 0.00005
"""A variance reduction smaller than this (five thousandths of a percent) is read as none: the section prints three
decimals of a percent, and a reduction that rounds to zero must not be worded as a narrowing."""


# ---------------------------------------------------------------------------
# Building the section
# ---------------------------------------------------------------------------
def build_section(results: Mapping[str, Any]) -> Section:
    """The section for a Plan K readout's `results`, every figure verified against them (module docstring)."""
    section = _build(results)
    problems = verify(section, results)
    if problems:
        raise PlanKReportError("the section does not trace back to the document: " + "; ".join(problems))
    return section


def _build(results: Mapping[str, Any]) -> Section:
    r = _Reader(results)
    blocks: list[Block] = []

    def head(text: str) -> None:
        blocks.append(Block(kind="heading", template=text))

    def lit(text: str = "") -> None:
        blocks.append(Block(kind="literal", template=text))

    def say(template: str, **figures: Figure) -> None:
        blocks.append(Block(kind="claim", template=template, figures=figures))

    def row(template: str, **figures: Figure) -> None:
        blocks.append(Block(kind="row", template=template, figures=figures))

    base = "steps.plan_k."
    head(f"## {SECTION_NUMBER}. Plan K's measurement upgrades on the same rows")
    lit()
    say(
        "The default journey above is unchanged. This section reruns it with Plan K's measurement upgrades switched on "
        "and reads what each one says, whichever way it falls. Label: {label}. {rows} customers, the same split, "
        "campaign-effect run {run}, scoring run {scoring}, {seconds} for the whole journey. Every range is a "
        "{confidence} range.",
        label=r.fig("label", "text"),
        rows=r.fig("rows", "count"),
        run=r.fig("campaign_effect_run_id", "text"),
        scoring=r.fig("scoring_run_id", "text"),
        seconds=r.fig("wall_clock_seconds", "seconds"),
        confidence=r.fig(f"{base}covariate_run_score.unadjusted.confidence_level", "percent"),
    )
    lit()
    _reproduction(r, say, lit)
    _covariate(r, say, lit, head, row, base + "covariate_run_score")
    _rollout(r, say, lit, head, base + "rollout")
    _pooling(r, say, lit, head, row, base + "pooling")
    _harm(r, say, lit, head, row, base + "harm_alarm")
    return Section(blocks=tuple(blocks))


def _reproduction(r: _Reader, say: Any, lit: Any) -> None:
    if not r.has("reproduction"):
        say("The default journey's committed results were not given, so this run is not compared with them.")
        lit()
        return
    identical = r.raw("reproduction.identical")
    differing = sorted(group for group, same in identical.items() if not same)
    for group in identical:
        r.flag(f"reproduction.identical.{group}")  # each must be true or false
    if not r.flag("reproduction.csv_sha256_equal"):
        say(
            "This run read a different file from the default journey, so its numbers are not compared with it."
        )
    elif not differing:
        say(
            "**Reproduced.** The split, the risk model's test scores, the campaign-effect model's AUUC, the offers given, "
            "the off-policy values and the replayed campaigns' differences are exactly the default journey's: the extra "
            "settings changed nothing it reports."
        )
    else:
        say(
            "**Not reproduced.** These groups of the default journey's numbers differ in this run: "
            + ", ".join(f"`{group}`" for group in differing)
            + "."
        )
    lit()


def _covariate(r: _Reader, say: Any, lit: Any, head: Any, row: Any, at: str) -> None:
    head(f"### {SECTION_NUMBER}.1 The conversion, adjusted by the scoring run's own prediction (M135)")
    lit()
    say(
        "The same replayed campaign as the conversion campaign above, planned again with `covariate_source` {source}: "
        "the plan, registered before any evaluation outcome was read, adjusts by the chance of buying without an e-mail "
        "that the scoring run predicted for each customer before the campaign. {treated} contacted and {control} held "
        "back customers, {tc} and {cc} conversions.",
        source=r.fig(f"{at}.covariate_source", "text"),
        treated=r.fig(f"{at}.treated_rows", "count"),
        control=r.fig(f"{at}.control_rows", "count"),
        tc=r.fig(f"{at}.treated_conversions", "count"),
        cc=r.fig(f"{at}.control_conversions", "count"),
    )
    lit()
    same = r.raw(f"{at}.plain_campaign.same_customers_and_outcomes")
    for key in same:
        r.flag(f"{at}.plain_campaign.same_customers_and_outcomes.{key}")
    say(
        "The plain campaign measured "
        + (
            "exactly the same customers and outcomes (rows, conversions and difference), so the two readings differ "
            "only in how the difference is estimated."
            if all(same.values())
            else "different customers or outcomes in at least one respect, so the two readings are not directly comparable."
        )
    )
    lit()
    lit("| Reading | Difference | Range |")
    lit("|---|---|---|")
    un = f"{at}.unadjusted"
    row(
        "| Unadjusted (the plain difference in rates) | {v} | {low} to {high} |",
        v=r.fig(f"{un}.value", "signed_points"),
        low=r.fig(f"{un}.ci_low", "signed_points"),
        high=r.fig(f"{un}.ci_high", "signed_points"),
    )
    adjusted = r.has(f"{at}.adjusted")
    if adjusted:
        ad = f"{at}.adjusted"
        row(
            "| Adjusted by the prediction | {v} | {low} to {high} |",
            v=r.fig(f"{ad}.value", "signed_points"),
            low=r.fig(f"{ad}.ci_low", "signed_points"),
            high=r.fig(f"{ad}.ci_high", "signed_points"),
        )
    lit()
    if not adjusted:
        say(
            "**Not adjusted.** The engine gave no adjusted estimate: {note}",
            note=r.fig(f"{at}.adjustment_note", "text"),
        )
    else:
        reduction = r.number(f"{at}.variance_reduction")
        if abs(reduction) < NEGLIGIBLE:
            effect = "left the range essentially as it was"
        elif reduction > 0:
            effect = "narrowed the range"
        else:
            effect = "widened the range"
        before = _side(r.number(f"{un}.ci_low"), r.number(f"{un}.ci_high"))
        after = _side(r.number(f"{at}.adjusted.ci_low"), r.number(f"{at}.adjusted.ci_high"))
        sides = (
            f"In both readings {_SIDE[before]}."
            if before == after
            else f"Unadjusted, {_SIDE[before]}; adjusted, {_SIDE[after]}."
        )
        say(
            f"**Measured variance reduction {{reduction}}** (a share of one). The prediction {effect}: that share of the "
            "difference's chance variation is the engine's own `variance_reduction`, reported whichever sign it has. The "
            "two ranges are made by different methods (the plain difference in rates, Newcombe's; the adjusted "
            "estimate, Welch's on the adjusted values), so their widths alone do not measure the adjustment; the "
            "variance reduction does. "
            f"{sides} {{missing}} measured customers had no prediction.",
            reduction=r.fig(f"{at}.variance_reduction", "scientific"),
            missing=r.fig(f"{at}.rows_covariate_missing", "count"),
        )
        lit()
        say("The engine's own sentence: {summary}", summary=r.fig(f"{at}.report.summary", "text"))
    lit()


def _rollout(r: _Reader, say: Any, lit: Any, head: Any, at: str) -> None:
    head(f"### {SECTION_NUMBER}.2 Safe to roll out? (M137)")
    lit()
    if not r.flag(f"{at}.written") or not r.has(f"{at}.rollout"):
        say(
            "The campaign-effect run wrote no rollout verdict (`risk_comparison.json` is absent or carries no `rollout` "
            "key), so there is nothing to report."
        )
        lit()
        return
    v = f"{at}.rollout"
    outcome = r.fig(f"{v}.outcome", "text")
    meaning = {
        "safe": "the lower bound is above zero: ranking by the campaign-effect model is safe to roll out against risk ranking",
        "not_safe": "the upper bound is below zero: ranking by the campaign-effect model is worse than risk ranking",
        "cannot_tell": "the bounds include zero: this data cannot tell the two rankings apart",
    }.get(str(outcome.value))
    if meaning is None:
        raise PlanKReportError(
            f"{v}.outcome is {outcome.value!r}, which is none of safe, not_safe, cannot_tell"
        )
    say(
        f"The campaign-effect run's equal-budget comparison of the offer {{offer}} against {{against}} (the first offer "
        f"only) was read as a one-sided verdict. **{{outcome}}**: {meaning}.",
        offer=r.fig(f"{at}.offer", "text"),
        against=r.fig(f"{at}.against", "text"),
        outcome=outcome,
    )
    lit()
    say("The comparison itself: {summary}", summary=r.fig(f"{at}.comparison.summary", "text"))
    lit()
    say(
        "At one-sided confidence {confidence}, uplift ranking minus risk ranking at equal budget is between {low} and "
        "{high} conversions, over {rows} cross-fitted rows.",
        confidence=r.fig(f"{v}.confidence", "percent"),
        low=r.fig(f"{v}.lower_bound_conversions", "signed_conversions"),
        high=r.fig(f"{v}.upper_bound_conversions", "signed_conversions"),
        rows=r.fig(f"{at}.comparison.rows", "count"),
    )
    lit()
    if r.has(f"{v}.lower_bound_inr") and r.has(f"{v}.upper_bound_inr"):
        say(
            "In rupees on the use case's value inputs (`{basis}`): between {low} and {high}; {missing} rows had no value "
            "and counted at zero.",
            basis=r.fig(f"{v}.value_basis", "text"),
            low=r.fig(f"{v}.lower_bound_inr", "inr"),
            high=r.fig(f"{v}.upper_bound_inr", "inr"),
            missing=r.fig(f"{v}.rows_without_value", "count"),
        )
    elif r.has(f"{v}.inr_unavailable_reason"):
        say(
            "No rupee bounds, because {reason}",
            reason=r.fig(f"{v}.inr_unavailable_reason", "text"),
        )
    else:
        say("No rupee bounds: the comparison had no value inputs, so nothing is priced.")
    lit()
    if r.has(f"{v}.rows_needed"):
        say(
            "If the gain seen here is real, about {needed} comparison rows would put the lower bound above zero. That is "
            "not a promise: the gain seen here may not be real.",
            needed=r.fig(f"{v}.rows_needed", "count"),
        )
        lit()
    say("The engine's own sentence: {summary}", summary=r.fig(f"{v}.summary", "text"))
    lit()


def _pooling(r: _Reader, say: Any, lit: Any, head: Any, row: Any, at: str) -> None:
    head(f"### {SECTION_NUMBER}.3 Pooling comparable campaigns (M136)")
    lit()
    say(
        "The journey measured {campaigns} campaigns that can enter a pooling cell; a campaign that measures the same "
        "customers as another (the same scoring run) counts once, whatever outcome it measures: the pooling module keeps "
        "the latest measured. The pooling module needs at least {first} comparable "
        "campaigns in a cell before it moves any figure. The installation setting was not switched on: the module was "
        "asked what it would do with these campaigns as they are.",
        campaigns=r.fig(f"{at}.campaigns_entering_a_cell", "count"),
        first=r.fig(f"{at}.first_drawn", "count"),
    )
    lit()
    cells = r.raw(f"{at}.cells")
    if cells:
        lit("| Outcome | Kind | Campaigns measured | Campaigns compared | Answer |")
        lit("|---|---|---|---|---|")
    for index in range(len(cells)):
        cell = f"{at}.cells.{index}"
        estimate = f"{cell}.estimates.0"
        extra: dict[str, Figure] = {}
        if not r.has(f"{cell}.estimates"):
            answer = "not counted: it measures the same customers as a later campaign, which counts instead"
        elif r.raw(f"{estimate}.status") == "pooled":
            answer = "pooled"
        elif r.raw(f"{estimate}.code") == "POOL_TOO_FEW":
            answer = "not pooled: fewer than {first} comparable campaigns ({code})"
            extra["first"] = r.fig(f"{at}.first_drawn", "count")
            extra["code"] = r.fig(f"{estimate}.code", "text")
        else:
            answer = "not pooled ({code})"
            extra["code"] = r.fig(f"{estimate}.code", "text")
        row(
            "| {outcome} | {kind} | {measured} | {compared} | " + answer + " |",
            outcome=r.fig(f"{cell}.outcome_column", "text"),
            kind=r.fig(f"{cell}.outcome_kind", "text"),
            measured=r.fig(f"{cell}.measured", "count"),
            compared=r.fig(f"{cell}.campaigns_compared", "count"),
            **extra,
        )
    lit()
    if r.flag(f"{at}.pooled"):
        for index in range(len(cells)):
            estimate = f"{at}.cells.{index}.estimates.0"
            if r.has(f"{at}.cells.{index}.estimates") and r.raw(f"{estimate}.status") == "pooled":
                say(
                    "Cell {outcome}: measured {measured}, estimated after pooling {pooled}.",
                    outcome=r.fig(f"{at}.cells.{index}.outcome_column", "text"),
                    measured=r.fig(f"{estimate}.measured_effect", "signed_points"),
                    pooled=r.fig(f"{estimate}.pooled_effect", "signed_points"),
                )
                lit()
    else:
        say(
            "**Not pooled.** No cell reaches {first} comparable campaigns, so there is no pooled figure to set beside the "
            "raw one and none is shown. The journey scores one list, so all its campaigns share one scoring run and at "
            "most one of them counts.",
            first=r.fig(f"{at}.first_drawn", "count"),
        )
        lit()


def _harm(r: _Reader, say: Any, lit: Any, head: Any, row: Any, at: str) -> None:
    head(f"### {SECTION_NUMBER}.4 The harm alarm (M138)")
    lit()
    applicable = r.flag(f"{at}.applicable")
    readings = r.raw(f"{at}.readings")
    lit(
        "| Campaign | Outcome | Status | Early look | Customers still inside their window | Interim reading | Alarm file written |"
    )
    lit("|---|---|---|---|---|---|---|")
    for name in readings:
        reading = f"{at}.readings.{name}"
        row(
            "| "
            + name
            + " | {outcome} | {status} | "
            + ("yes" if r.flag(f"{reading}.early_look") else "no")
            + " | {immature} | "
            + ("yes" if r.flag(f"{reading}.interim") else "no")
            + " | "
            + ("yes" if r.flag(f"{reading}.harm_alarm_file_written") else "no")
            + " |",
            outcome=r.fig(f"{reading}.outcome_column", "text"),
            status=r.fig(f"{reading}.status", "text"),
            immature=r.fig(f"{reading}.rows_immature", "count"),
        )
    lit()
    if applicable:
        say(
            "**Applicable.** At least one reading is interim, which is what the alarm watches. The journey did not switch "
            "the alarm on, so nothing was checked."
        )
    else:
        say(
            "**Not applicable.** The alarm watches interim readings: an early look, or a reading with customers still "
            "inside their outcome window. Every reading here is a retrospective replay of a finished test, measured with "
            "every outcome in and read as a final result, so the engine's own test of a reading (`is_interim`) says none is "
            "interim and no alarm file exists. A final result's verdict says whether the campaign did harm."
        )
    lit()


# ---------------------------------------------------------------------------
# Verifying, provenance and rendering
# ---------------------------------------------------------------------------
def figures_of(section: Section) -> Iterator[tuple[str, Figure]]:
    for position, block in enumerate(section.blocks):
        for name, figure in block.figures.items():
            yield f"{position}.{name}", figure


def verify(section: Section, results: Mapping[str, Any]) -> list[str]:
    """What fails to trace to `results`; empty when every number resolves.

    Re-reads every figure's field, compares the value, recomputes the printed text and checks that the words hold no
    digit (a number written into a sentence would reach the report untraced).
    """
    failures: list[str] = []
    for block in section.blocks:
        words = MILESTONE.sub("", block.template)
        if block.kind == "heading":
            words = re.sub(rf"^#+ {SECTION_NUMBER}(\.\d*)? ", "", words)
        stray = DIGITS.findall(words)
        if stray:
            failures.append(
                f"the words {block.template[:60]!r} hold {', '.join(stray)}, which no figure prints"
            )
        holes = set(re.findall(r"\{(\w+)\}", block.template))
        if holes != set(block.figures):
            failures.append(f"holes {sorted(holes)} do not match figures {sorted(block.figures)}")
    for name, figure in figures_of(section):
        try:
            found = walk(results, figure.field)
        except PlanKReportError:
            failures.append(f"{name}: {figure.field} cannot be read")
            continue
        if isinstance(figure.value, str) or isinstance(found, str):
            same = found == figure.value
        else:
            same = (
                isinstance(found, int | float)
                and not isinstance(found, bool)
                and math.isclose(float(found), float(figure.value), rel_tol=1e-9, abs_tol=1e-12)
            )
        if not same:
            failures.append(f"{name}: {figure.value!r} is not {found!r} at {figure.field}")
        elif figure.text != format_value(figure.format, figure.value):
            failures.append(f"{name}: the text {figure.text!r} does not print {figure.value!r}")
    return failures


def digits_of(text: str) -> set[str]:
    """The numbers in `text`, as the section prints them."""
    return set(DIGITS.findall(text))


def provenance(section: Section) -> dict[str, Any]:
    """Every figure of the section: its id, printed text, value, format and the field it was read from."""
    return {
        "artefact": ARTEFACT,
        "figures": [
            {
                "id": name,
                "text": figure.text,
                "value": figure.value,
                "format": figure.format,
                "field": figure.field,
            }
            for name, figure in figures_of(section)
        ],
    }


def render_plan_k(results: Mapping[str, Any], *, results_path: str, command: str) -> str:
    """Section 9 as Markdown, verified; `results_path` and `command` are printed for reproduction."""
    section = build_section(results)
    lines = [block.words() for block in section.blocks]
    lines += [
        "",
        f"*Section {SECTION_NUMBER} is generated from [`{results_path}`]({results_path}); every number in it is a field "
        "of that file, the words around the numbers contain no digits and each verdict is computed from the figures. "
        "To rebuild it:*",
        "",
        "```bash",
        command,
        "```",
        "",
    ]
    return "\n".join(lines)
