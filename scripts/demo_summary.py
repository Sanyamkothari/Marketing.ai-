"""The one-page results summary of the manager demo, generated from the journey's artefact (Plan J M111, DEC-1321).

    python -m scripts.demo_summary [--results PATH] [--out PATH] [--check]

The page answers the five questions of the demo (who to contact, with which offer, who to leave alone, what it is
worth, how we know) from the Hillstrom journey's `journey.results.json` (M110, DEC-1320), whichever way the
results fell. **Nothing is typed in.** The method is the Value Proof Pack's (M104, DEC-1314):

* every number on the page is a :class:`Figure`: its value, the text printed, how that text is made from the
  value, and the field of the artefact it was read from;
* the words around the numbers carry **no digit**: a sentence is a template with named holes, and a hole is
  filled by a figure's text, so a number cannot be written into a sentence by hand;
* the verdicts ("above zero", "worse", "not shown") are computed from the figures' values, never written, so a
  result that fell the other way produces the other sentence;
* :func:`verify` reads the artefact again, recomputes every figure and every printed text, and checks the words
  for digits; :func:`build_summary` refuses to return a summary that does not verify, and a number the artefact
  does not hold stops the page (`SummaryError`) rather than being shown as zero.

The page is written next to the dataset (`library/hillstrom-email/DEMO_SUMMARY.md`) with a machine-readable list
of every figure and its source (`DEMO_SUMMARY.provenance.json`). `--check` re-renders both from the committed
artefact and fails when either differs, which is what `tests/unit/test_demo_summary.py` runs.

Run on the committed sample (what `scripts.seed_validated` does on a machine without the full file) the page says,
in its first lines, that it is a sample run whose numbers are not the validated ones.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import Field

from engine.config import StrictBase
from engine.pilot.roi import format_inr

__all__ = [
    "ARTEFACT",
    "FULL_FILE_ROWS",
    "Claim",
    "DemoSummary",
    "Figure",
    "Row",
    "SummaryError",
    "build_summary",
    "digits_of",
    "main",
    "provenance",
    "render_markdown",
    "verify",
]

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
FOLDER: Final = REPO_ROOT / "library" / "hillstrom-email"
RESULTS_DEFAULT: Final = FOLDER / "journey.results.json"
SUMMARY_DEFAULT: Final = FOLDER / "DEMO_SUMMARY.md"
PROVENANCE_NAME: Final = "DEMO_SUMMARY.provenance.json"
ARTEFACT: Final = "journey.results.json"
"""How the provenance names the artefact: every field path is inside this one file."""
FULL_FILE_ROWS: Final = 64_000
"""Rows of the validated file (`library/hillstrom-email/fetch.py`'s `ROWS`); a run on any other count is a sample."""
COMMAND: Final = "python -m scripts.demo_summary"

DIGITS: Final = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?")
"""A number as a reader sees it; the words of the page may hold none that no figure prints."""

Format = Literal[
    "count",
    "signed_count",
    "rate",
    "points",
    "signed_points",
    "p_value",
    "auuc",
    "score",
    "usd",
    "inr",
    "percent",
    "text",
]


class SummaryError(Exception):
    """The artefact does not hold something the page needs, or the page does not trace back to it."""


# ---------------------------------------------------------------------------
# Figures: a number, the text printed for it, and where it was read
# ---------------------------------------------------------------------------
def _zero_free(value: float) -> float:
    """-0.0 prints as 0.0, so a rounded-away negative never shows a minus sign."""
    return 0.0 if value == 0 else value


def _signed(text: str) -> str:
    return text if text.startswith("-") else f"+{text}"


def format_value(fmt: Format, value: float | str) -> str:
    """The text printed for `value` in format `fmt`: the one way a value becomes text on the page."""
    if fmt == "text":
        return str(value)
    number = float(value)
    if fmt == "count":
        return f"{round(number):,}"
    if fmt == "signed_count":
        return _signed(f"{round(number):,}")
    if fmt == "rate":
        return f"{_zero_free(round(number * 100.0, 2)):.2f}%"
    if fmt == "points":
        return f"{_zero_free(round(number * 100.0, 2)):.2f} pts"
    if fmt == "signed_points":
        return _signed(f"{_zero_free(round(number * 100.0, 2)):.2f}") + " pts"
    if fmt == "p_value":
        return f"{_zero_free(round(number, 3)):.3f}"
    if fmt == "auuc":
        return _signed(f"{_zero_free(round(number, 4)):.4f}")
    if fmt == "score":
        return f"{_zero_free(round(number, 3)):.3f}"
    if fmt == "usd":
        text = f"{abs(_zero_free(round(number, 3))):.3f}"
        return f"-${text}" if number < 0 and float(text) != 0 else f"${text}"
    if fmt == "inr":
        return format_inr(number)
    return f"{_zero_free(round(number * 100.0, 0)):.0f}%"  # percent


class Figure(StrictBase):
    """One number the page prints (or one piece of artefact text), with the field it was read from."""

    value: float | str = Field(description="The artefact's own value at `field`.")
    text: str = Field(description="What the page prints: `format_value(format, value)`.")
    format: Format
    field: str = Field(description="Dotted path in `journey.results.json`; a list index is a number.")


class Claim(StrictBase):
    """A sentence: words with named holes (no digit among the words) and the figure that fills each hole."""

    template: str
    figures: dict[str, Figure]

    def words(self) -> str:
        return self.template.format(**{name: figure.text for name, figure in self.figures.items()})


class Row(StrictBase):
    """One question of the demo: the finding in figures, and a verdict computed from them."""

    key: str
    question: str
    short: str = Field(description="The answer in a few words; computed, no digit.")
    claims: tuple[Claim, ...]
    verdict: str = Field(description="Computed from the figures' values; no digit.")


class DemoSummary(StrictBase):
    """The page: a heading, what the data is, one row per question, the limits."""

    title: str
    is_sample: bool
    sample_notice: str | None = None
    about: Claim
    rows: tuple[Row, ...]
    limits: tuple[Claim, ...]
    artefact: str = ARTEFACT


def walk(document: Any, path: str) -> Any:
    """The value at dotted `path` of a JSON document; raises `SummaryError` when it is not there."""
    current = document
    for part in path.split("."):
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise SummaryError(f"{ARTEFACT} holds nothing at {path!r}")
    return current


class _Reader:
    """Makes figures from a results document; a missing or non-numeric field stops the page."""

    def __init__(self, results: Mapping[str, Any]) -> None:
        self.results = results

    def raw(self, path: str) -> Any:
        return walk(self.results, path)

    def number(self, path: str) -> float:
        value = self.raw(path)
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise SummaryError(f"{path!r} of {ARTEFACT} is not a finite number: {value!r}")
        return float(value)

    def fig(self, path: str, fmt: Format) -> Figure:
        value: float | str
        if fmt == "text":
            raw = self.raw(path)
            if not isinstance(raw, str):
                raise SummaryError(f"{path!r} of {ARTEFACT} is not text: {raw!r}")
            value = raw
        else:
            value = self.number(path)
        return Figure(value=value, text=format_value(fmt, value), format=fmt, field=path)

    def flag(self, path: str) -> bool:
        value = self.raw(path)
        if not isinstance(value, bool):
            raise SummaryError(f"{path!r} of {ARTEFACT} is not true or false: {value!r}")
        return value

    def index_of(self, list_path: str, key: str, wanted: str) -> int:
        """The position in the list at `list_path` of the element whose `key` is `wanted`."""
        elements = self.raw(list_path)
        if isinstance(elements, list):
            for position, element in enumerate(elements):
                if isinstance(element, dict) and element.get(key) == wanted:
                    return position
        raise SummaryError(f"{ARTEFACT} has no element with {key} = {wanted!r} at {list_path!r}")


def _side(low: float, high: float) -> Literal["above", "below", "includes"]:
    """Which side of zero a 95% range lies: the verdict of every interval on the page."""
    if low > 0:
        return "above"
    if high < 0:
        return "below"
    return "includes"


_SIDE_WORDS: Final = {
    "above": "its whole range is above zero",
    "below": "its whole range is below zero",
    "includes": "its range includes zero, so it is not shown",
}


# ---------------------------------------------------------------------------
# Building the page
# ---------------------------------------------------------------------------
def build_summary(results: Mapping[str, Any]) -> DemoSummary:
    """The page for a journey's `results`, every figure verified against them (see the module docstring).

    Raises `SummaryError` when the artefact lacks a field the page needs or a figure does not verify.
    """
    summary = _build(results)
    problems = verify(summary, results)
    if problems:
        raise SummaryError("the page does not trace back to the artefact: " + "; ".join(problems))
    return summary


def _build(results: Mapping[str, Any]) -> DemoSummary:
    r = _Reader(results)
    is_sample = int(r.number("rows")) != FULL_FILE_ROWS
    steps = "steps."
    confidence_at = f"{steps}campaigns.conversion.report.absolute_lift.confidence_level"
    _confirm_confidence(r, confidence_at, f"{steps}off_policy.estimator")

    about = Claim(
        template=(
            "{rows} customers of a retailer, from a public e-mail test in which each was sent, at random, the "
            "men's e-mail, the women's e-mail or nothing. Half of them ({training}) trained every model and "
            "setting; the other half ({evaluation}) was never seen by a model, and every result below is "
            "measured on it. Every range is a {confidence} range. This is a retrospective reading of a public "
            "file, not a client's own campaign, and the product sent nothing."
        ),
        figures={
            "rows": r.fig("rows", "count"),
            "training": r.fig("split.training.rows", "count"),
            "evaluation": r.fig("split.evaluation.rows", "count"),
            "confidence": r.fig(confidence_at, "percent"),
        },
    )

    # What the checks say, decided first because several verdicts depend on them.
    calibrated = _check_passed(r, "UPLIFT_MISCALIBRATED")
    stable = _check_passed(r, "UPLIFT_UNSTABLE_ACROSS_FOLDS")
    oos = f"{steps}beats_risk_out_of_sample.offers"
    holdout_beats = _check_passed(r, "UPLIFT_NOT_BETTER_THAN_RISK")
    mens_beats = r.flag(f"{oos}.Mens E-Mail.beats_risk")
    womens_beats = r.flag(f"{oos}.Womens E-Mail.beats_risk")
    beats = holdout_beats and mens_beats and womens_beats

    # -- 1. who to contact, and with which e-mail ----------------------------------------------------------
    arms = f"{steps}treat_list.offer_choice.arms"
    mens_at = r.index_of(arms, "level", "Mens E-Mail")
    womens_at = r.index_of(arms, "level", "Womens E-Mail")
    contact = Claim(
        template=(
            "The list gives an e-mail to {contacted} of {rows} customers: the men's e-mail to {mens} and the "
            "women's e-mail to {womens}. The engine's own random control group is held back from the rest."
        ),
        figures={
            "contacted": r.fig(f"{steps}treat_list.treat_rows", "count"),
            "rows": r.fig(f"{steps}treat_list.treat_list_rows", "count"),
            "mens": r.fig(f"{arms}.{mens_at}.offered_rows", "count"),
            "womens": r.fig(f"{arms}.{womens_at}.offered_rows", "count"),
        },
    )
    as_policy = Claim(
        template=(
            "Counted as a policy over all {rows} evaluation customers, it means the men's e-mail for {mens}, "
            "the women's e-mail for {womens} and no e-mail for {none}."
        ),
        figures={
            "rows": r.fig(f"{steps}off_policy.rows", "count"),
            "mens": r.fig(f"{steps}off_policy.policy_shares.Mens E-Mail", "rate"),
            "womens": r.fig(f"{steps}off_policy.policy_shares.Womens E-Mail", "rate"),
            "none": r.fig(f"{steps}off_policy.policy_shares.No E-Mail", "rate"),
        },
    )
    row_contact = Row(
        key="contact",
        question="Who to contact, and with which e-mail?",
        short="A list, with an e-mail chosen on every row",
        claims=(contact, as_policy),
        verdict=(
            "A list was made, with an offer on every row. Which e-mail each customer gets comes from the "
            "campaign-effect model, "
            + (
                "which beats plain risk ranking (question five)."
                if beats
                else "which does not beat plain risk ranking on this file (question five)."
            )
        ),
    )

    # -- 2. who to leave alone -----------------------------------------------------------------------------
    alone = Claim(
        template=(
            "{sleeping} customers are left alone because contacting them looks harmful (the model's "
            '"sleeping dogs"), and {below_cost} more because the expected gain is below the cost of the '
            "e-mail."
        ),
        figures={
            "sleeping": r.fig(f"{steps}treat_list.offer_choice.reasons.sleeping_dog", "count"),
            "below_cost": r.fig(f"{steps}treat_list.offer_choice.reasons.below_cost", "count"),
        },
    )
    dogs = _table_row(
        r,
        f"{steps}campaigns.conversion.proof.view.sections",
        "backfire",
        ("Predicted group", "Sleeping dogs"),
    )
    col = _column_reader(r, f"{steps}campaigns.conversion.proof.view.sections", "backfire")
    dogs_side = _side(
        r.number(f"{dogs}.{col('Range')}.low.value"), r.number(f"{dogs}.{col('Range')}.high.value")
    )
    measured_dogs = Claim(
        template=(
            "In the replay campaign, {contacted} customers the scoring run labelled sleeping dogs were e-mailed "
            "anyway (the offer choice and the segment labels come from different steps) and {held} like them "
            "were held back. E-mailing them changed the conversion rate by {diff} (range {low} to {high})."
        ),
        figures={
            "contacted": r.fig(f"{dogs}.{col('Contacted')}.value", "count"),
            "held": r.fig(f"{dogs}.{col('Held back')}.value", "count"),
            "diff": r.fig(f"{dogs}.{col('Difference')}.value", "signed_points"),
            "low": r.fig(f"{dogs}.{col('Range')}.low.value", "signed_points"),
            "high": r.fig(f"{dogs}.{col('Range')}.high.value", "signed_points"),
        },
    )
    row_alone = Row(
        key="leave_alone",
        question="Who to leave alone?",
        short=_alone_short(calibrated, dogs_side),
        claims=(alone, measured_dogs),
        verdict=_alone_verdict(calibrated, dogs_side),
    )

    # -- 3. what it is worth -------------------------------------------------------------------------------
    chosen = f"{steps}off_policy.estimates.conversion.chosen.difference_from_no_email"
    off_side = _side(r.number(f"{chosen}.ci_low"), r.number(f"{chosen}.ci_high"))
    off_policy = Claim(
        template=(
            "On the evaluation customers, by inverse-probability weighting of the e-mail the file sent at "
            "random, the list raises the conversion rate by {lift} over sending no e-mail (range {low} to "
            "{high})."
        ),
        figures={
            "lift": r.fig(f"{chosen}.value", "signed_points"),
            "low": r.fig(f"{chosen}.ci_low", "signed_points"),
            "high": r.fig(f"{chosen}.ci_high", "signed_points"),
        },
    )
    campaign = f"{steps}campaigns.conversion.report"
    replay_side = _side(
        r.number(f"{campaign}.absolute_lift.ci_low"), r.number(f"{campaign}.absolute_lift.ci_high")
    )
    replay = Claim(
        template=(
            "Measured as a campaign against the engine's own random control group, with a third of each group "
            "kept (the replay), the lift is {lift} (range {low} to {high}; p = {p}), about {extra} extra "
            "conversions (range {extra_low} to {extra_high})."
        ),
        figures={
            "lift": r.fig(f"{campaign}.absolute_lift.value", "signed_points"),
            "low": r.fig(f"{campaign}.absolute_lift.ci_low", "signed_points"),
            "high": r.fig(f"{campaign}.absolute_lift.ci_high", "signed_points"),
            "p": r.fig(f"{campaign}.p_value", "p_value"),
            "extra": r.fig(f"{campaign}.incremental_conversions.value", "count"),
            "extra_low": r.fig(f"{campaign}.incremental_conversions.ci_low", "count"),
            "extra_high": r.fig(f"{campaign}.incremental_conversions.ci_high", "count"),
        },
    )
    money = f"{steps}off_policy.money_on_evaluation_rows.chosen"
    pack = f"{steps}campaigns.conversion.proof.view.sections"
    net_section = f"{pack}.{r.index_of(pack, 'key', 'net_value')}.lines"
    net_line = f"{net_section}.{r.index_of(net_section, 'label', 'Net value')}"
    rupees = Claim(
        template=(
            "In rupees (revenue before margin, at the stated exchange rate) the list nets {net} on the "
            "evaluation customers after the cost of the e-mails, range {low} to {high}. The campaign's Value "
            "Proof Pack, built only on the replay's measured outcomes, shows a net value of {pack_low} to "
            "{pack_high}; it credits the outcomes of only a third of each group while costing every e-mail, "
            "so it understates the list."
        ),
        figures={
            "net": r.fig(f"{money}.net_inr", "inr"),
            "low": r.fig(f"{money}.net_ci_low_inr", "inr"),
            "high": r.fig(f"{money}.net_ci_high_inr", "inr"),
            "pack_low": r.fig(f"{net_line}.low.value", "inr"),
            "pack_high": r.fig(f"{net_line}.high.value", "inr"),
        },
    )
    row_worth = Row(
        key="worth",
        question="What is it worth?",
        short=_worth_short(off_side, replay_side),
        claims=(off_policy, replay, rupees),
        verdict=_worth_verdict(off_side, replay_side),
    )

    # -- 4. against the obvious alternative ----------------------------------------------------------------
    versus = f"{steps}off_policy.chosen_against_everyone.conversion.chosen_minus_everyone_Mens E-Mail"
    versus_w = f"{steps}off_policy.chosen_against_everyone.conversion.chosen_minus_everyone_Womens E-Mail"
    everyone = f"{steps}off_policy.estimates.conversion.everyone_Mens E-Mail.difference_from_no_email"
    everyone_money = f"{steps}off_policy.money_on_evaluation_rows.everyone_Mens E-Mail"
    simple = Claim(
        template=(
            "Sending everyone the men's e-mail raises the conversion rate by {mens_lift} over no e-mail. "
            "The list against that, on the same customers, is {gap} (range {low} to {high}). In rupees, "
            "sending everyone the men's e-mail nets {mens_net} (range {mens_low} to {mens_high}). Against "
            "sending everyone the women's e-mail the list is {w_gap} (range {w_low} to {w_high})."
        ),
        figures={
            "mens_lift": r.fig(f"{everyone}.value", "signed_points"),
            "gap": r.fig(f"{versus}.value", "signed_points"),
            "low": r.fig(f"{versus}.ci_low", "signed_points"),
            "high": r.fig(f"{versus}.ci_high", "signed_points"),
            "mens_net": r.fig(f"{everyone_money}.net_inr", "inr"),
            "mens_low": r.fig(f"{everyone_money}.net_ci_low_inr", "inr"),
            "mens_high": r.fig(f"{everyone_money}.net_ci_high_inr", "inr"),
            "w_gap": r.fig(f"{versus_w}.value", "signed_points"),
            "w_low": r.fig(f"{versus_w}.ci_low", "signed_points"),
            "w_high": r.fig(f"{versus_w}.ci_high", "signed_points"),
        },
    )
    gap_side = _side(r.number(f"{versus}.ci_low"), r.number(f"{versus}.ci_high"))
    w_side = _side(r.number(f"{versus_w}.ci_low"), r.number(f"{versus_w}.ci_high"))
    row_simple = Row(
        key="simple_rule",
        question="Does it beat the obvious alternative, the men's e-mail to everyone?",
        short={"below": "No, it is worse", "includes": "Not shown", "above": "Yes, it is better"}[gap_side],
        claims=(simple,),
        verdict={
            "below": "No. The list is measurably worse than sending everyone the men's e-mail: on this file "
            "the simple rule wins.",
            "includes": "Not shown. The list cannot be told apart from sending everyone the men's e-mail.",
            "above": "Yes. The list is measurably better than sending everyone the men's e-mail.",
        }[gap_side]
        + " Against the women's e-mail to everyone: "
        + {
            "below": "the list is measurably worse.",
            "includes": "not shown either way.",
            "above": "the list is measurably better.",
        }[w_side],
    )

    # -- 5. how we know ------------------------------------------------------------------------------------
    roc_at = r.index_of(f"{steps}risk_model.baseline_rows", "id", "roc_auc")
    risk = Claim(
        template=(
            "The risk model's ROC-AUC is {roc}, against {baseline} for its plain logistic-regression baseline."
        ),
        figures={
            "roc": r.fig(f"{steps}risk_model.test_metrics.roc_auc", "score"),
            "baseline": r.fig(f"{steps}risk_model.baseline_rows.{roc_at}.baseline_value", "score"),
        },
    )
    mens_cmp = _baseline_difference(r, f"{oos}.Mens E-Mail", "propensity_model")
    womens_cmp = _baseline_difference(r, f"{oos}.Womens E-Mail", "propensity_model")
    uplift_vs_risk = Claim(
        template=(
            "Does the campaign-effect (uplift) model beat plain risk ranking? Judged by the area under the "
            "uplift curve, in the engine's own check on its hold-out the difference is {gap} (range {low} to "
            "{high}). Repeated on the evaluation customers, where neither model saw a row, the difference is "
            "{m_gap} (range {m_low} to {m_high}) for the men's e-mail and {w_gap} (range {w_low} to {w_high}) "
            "for the women's."
        ),
        figures={
            "gap": _holdout_difference(r, "value"),
            "low": _holdout_difference(r, "ci_low"),
            "high": _holdout_difference(r, "ci_high"),
            "m_gap": r.fig(f"{mens_cmp}.value", "auuc"),
            "m_low": r.fig(f"{mens_cmp}.ci_low", "auuc"),
            "m_high": r.fig(f"{mens_cmp}.ci_high", "auuc"),
            "w_gap": r.fig(f"{womens_cmp}.value", "auuc"),
            "w_low": r.fig(f"{womens_cmp}.ci_low", "auuc"),
            "w_high": r.fig(f"{womens_cmp}.ci_high", "auuc"),
        },
    )
    quality = {
        (True, True): "stable across the folds of the data and calibrated by decile",
        (True, False): "stable across the folds of the data but not calibrated by decile",
        (False, True): "calibrated by decile but not stable across the folds of the data",
        (False, False): "neither stable across the folds of the data nor calibrated by decile",
    }[(stable, calibrated)]
    row_how = Row(
        key="how_we_know",
        question="How do we know?",
        short="Uplift beats risk ranking" if beats else "Uplift does not beat risk ranking",
        claims=(risk, uplift_vs_risk),
        verdict=_how_verdict(beats, holdout_beats, mens_beats, womens_beats)
        + " The model is "
        + quality
        + ".",
    )

    notice = (
        "This page was generated from a run on the committed sample, a small part of the file. Its numbers are "
        "not the validated results: they differ from the committed page, and the ranges are wide. The "
        "validated results are on the full file."
        if is_sample
        else None
    )
    return DemoSummary(
        title="Hillstrom e-mail test: what the product found",
        is_sample=is_sample,
        sample_notice=notice,
        about=about,
        rows=(row_contact, row_alone, row_worth, row_simple, row_how),
        limits=_limits(r),
    )


def _how_verdict(beats: bool, holdout: bool, mens: bool, womens: bool) -> str:
    """The uplift-against-risk verdict, spelled out check by check so a mixed result reads as mixed."""
    if beats:
        return "Uplift beats risk ranking, in the engine's check on its hold-out and out of sample for each e-mail."
    if not (holdout or mens or womens):
        detail = (
            "Uplift does not beat risk ranking: not in the engine's check on its hold-out, and not out of "
            "sample, where neither model saw a row, for either e-mail."
        )
    else:

        def says(passed: bool) -> str:
            return "beats" if passed else "does not beat"

        detail = (
            f"Uplift does not beat risk ranking everywhere. In the engine's check on its hold-out it "
            f"{says(holdout)} it; out of sample it {says(mens)} it for the men's e-mail and {says(womens)} it "
            "for the women's."
        )
    return (
        detail
        + " Whatever the list earns, it does not earn it because uplift modelling added to plain risk ranking."
    )


def _alone_short(calibrated: bool, dogs: str) -> str:
    if dogs == "below":
        return "Named, and harm shown"
    return "Named, but not confirmed" if calibrated else "Named, but not established"


def _alone_verdict(calibrated: bool, dogs: str) -> str:
    first = (
        "The predicted effects match the measured ones by decile."
        if calibrated
        else "The predicted effects do not match what was measured, by decile: the model is not calibrated, "
        "so these names are its best guess."
    )
    second = {
        "below": "The measurement agrees: e-mailing the customers called sleeping dogs made things worse.",
        "includes": "The measurement does not confirm the label: for the customers called sleeping dogs the "
        "range includes zero, so no harm is shown from e-mailing them.",
        "above": "The measurement contradicts the label: the customers called sleeping dogs did better when "
        "e-mailed.",
    }[dogs]
    return f"{first} {second}"


def _column_reader(r: _Reader, sections: str, key: str) -> Callable[[str], int]:
    """A function from a column title of a Pack table to its position in the table's rows."""
    section = f"{sections}.{r.index_of(sections, 'key', key)}"
    columns = r.raw(f"{section}.table.columns")
    if not isinstance(columns, list):
        raise SummaryError(f"the {key} section of the Pack has no table")

    def column(title: str) -> int:
        if title not in columns:
            raise SummaryError(f"the {key} table of the Pack has no column {title!r}")
        return int(columns.index(title))

    return column


def _table_row(r: _Reader, sections: str, key: str, lead: tuple[str, str]) -> str:
    """The path of the row of a Pack table whose first two cells are `lead` (a kind and a group)."""
    section = f"{sections}.{r.index_of(sections, 'key', key)}"
    rows = r.raw(f"{section}.table.rows")
    if isinstance(rows, list):
        for position, row in enumerate(rows):
            if isinstance(row, list) and tuple(row[:2]) == lead:
                return f"{section}.table.rows.{position}"
    raise SummaryError(f"the {key} table of the Pack has no row for {lead[1]!r}")


def _worth_short(off: str, replay: str) -> str:
    if off == replay == "above":
        return "Beats sending nothing"
    if off == replay == "below":
        return "Worse than sending nothing"
    if off == replay == "includes":
        return "Not shown to beat sending nothing"
    return "Mixed: see the two measures"


_VERDICT_WORDS: Final = {
    "above": "the list beats sending no e-mail",
    "below": "the list is worse than sending no e-mail",
    "includes": "the list is not shown to differ from sending no e-mail",
}


def _worth_verdict(off: str, replay: str) -> str:
    if off == replay:
        return {
            "above": "Yes. The list beats sending no e-mail, both off the file and as measured by the replay "
            "campaign.",
            "below": "No. The list is worse than sending no e-mail, both off the file and as measured by the "
            "replay campaign.",
            "includes": "Not shown. Neither the file nor the replay campaign separates the list from sending "
            "no e-mail.",
        }[off]
    return (
        f"Mixed. Off the file, {_VERDICT_WORDS[off]}; as measured by the replay campaign, "
        f"{_VERDICT_WORDS[replay]}."
    )


def _check_passed(r: _Reader, code: str) -> bool:
    at = r.index_of("steps.approval.checks", "code", code)
    return r.flag(f"steps.approval.checks.{at}.passed")


def _holdout_difference(r: _Reader, part: str) -> Figure:
    """The engine's own check on the hold-out: uplift minus the approved propensity model's score."""
    comparison = "steps.uplift_model.evaluation.baseline_comparison"
    baselines = f"{comparison}.baselines"
    at = r.index_of(baselines, "baseline", "propensity_model")
    return r.fig(f"{baselines}.{at}.difference.{part}", "auuc")


def _baseline_difference(r: _Reader, offer_path: str, baseline: str) -> str:
    at = r.index_of(f"{offer_path}.baselines", "baseline", baseline)
    return f"{offer_path}.baselines.{at}.difference"


def _confirm_confidence(r: _Reader, path: str, estimator: str) -> None:
    """The page says every range is 95%: the report's level and the off-policy estimator's words must agree."""
    if not math.isclose(r.number(path), 0.95):
        raise SummaryError(f"{path!r} of {ARTEFACT} is not a 95% range; the page says 95%")
    words = r.raw(estimator)
    if not isinstance(words, str) or "95%" not in words:
        raise SummaryError(f"{estimator!r} of {ARTEFACT} does not say its intervals are 95%")


def _limits(r: _Reader) -> tuple[Claim, ...]:
    fx = r.fig("value.fx_inr_per_usd", "count")
    cost = r.raw("contact_cost_inr")
    if not isinstance(cost, int | float):
        raise SummaryError("contact_cost_inr of the artefact is not a number")
    return (
        Claim(
            template=(
                "Public data and a retrospective reading. The file is an old public e-mail test; nothing was "
                "sent by the product, and the licence of the file is not stated, so it is for internal "
                "validation. A client's own campaign is the next proof."
            ),
            figures={},
        ),
        Claim(
            template=(
                "The replay keeps a third of each group, counts the rest as having no outcome, and uses an "
                "outcome window of {window} days, because the product refuses to measure a window that has "
                "not passed; the file's own outcomes cover two weeks."
            ),
            figures={"window": r.fig("steps.campaigns.conversion.report.outcome_window_days", "count")},
        ),
        Claim(
            template=(
                "Rupee figures turn the file's dollars into rupees at {fx} rupees to the dollar, an assumed "
                "rate, count revenue before margin, and take an e-mail to cost {cost} rupee. Change the rate "
                "and every rupee figure moves with it."
            ),
            figures={
                "fx": fx,
                "cost": Figure(
                    value=float(cost),
                    text=f"{float(cost):.2f}",
                    format="text",
                    field="contact_cost_inr",
                ),
            },
        ),
        Claim(
            template=(
                "The amount spent is skewed: a few large purchases dominate it, so the ranges on money may be "
                "too narrow. The other public datasets considered, Criteo among them, were not run: not reachable from "
                "here, or not licensed for this use."
            ),
            figures={},
        ),
    )


# ---------------------------------------------------------------------------
# Verifying
# ---------------------------------------------------------------------------
def figures_of(summary: DemoSummary) -> Iterator[tuple[str, Figure]]:
    """Every figure of the page with its id (`row.claim.name`), in page order."""
    for index, claim in enumerate([summary.about, *summary.limits]):
        scope = "about" if index == 0 else f"limit{index}"
        for name, figure in claim.figures.items():
            yield f"{scope}.{name}", figure
    for row in summary.rows:
        for number, claim in enumerate(row.claims, start=1):
            for name, figure in claim.figures.items():
                yield f"{row.key}.{number}.{name}", figure


def words_of(summary: DemoSummary) -> Iterator[str]:
    """Every string of the page outside a figure: templates, questions, verdicts, notices, the title."""
    yield summary.title
    if summary.sample_notice:
        yield summary.sample_notice
    claims: list[Claim] = [summary.about, *summary.limits]
    for row in summary.rows:
        yield row.question
        yield row.verdict
        claims.extend(row.claims)
    for claim in claims:
        yield claim.template


def verify(summary: DemoSummary, results: Mapping[str, Any]) -> list[str]:
    """What fails to trace to `results`; empty when every number resolves.

    Re-reads every figure's field, compares the value, recomputes the printed text, and checks that the words of
    the page hold no digit (a number written into a sentence would reach the page untraced).
    """
    failures: list[str] = []
    for words in words_of(summary):
        stray = DIGITS.findall(words)
        if stray:
            failures.append(f"the words {words[:60]!r} hold {', '.join(stray)}, which no figure prints")
    for name, figure in figures_of(summary):
        try:
            found = walk(results, figure.field)
        except SummaryError:
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
    for row in summary.rows:
        for claim in row.claims:
            holes = set(claim.figures)
            used = set(re.findall(r"\{(\w+)\}", claim.template))
            if holes != used:
                failures.append(f"{row.key}: holes {sorted(used)} do not match figures {sorted(holes)}")
    return failures


def digits_of(text: str) -> set[str]:
    """The numbers in `text`, as the page prints them."""
    return set(DIGITS.findall(text))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_markdown(summary: DemoSummary, *, results_name: str = ARTEFACT) -> str:
    """The page as Markdown: what it is, one block per question, the limits, where the numbers come from."""
    lines = [f"# {summary.title}", ""]
    if summary.sample_notice:
        lines += [f"> **SAMPLE RUN. {summary.sample_notice}**", ""]
    lines += [
        "**Public dataset, retrospective.** " + summary.about.words(),
        "",
        "## The answers in short",
        "",
        "| Question | Answer |",
        "|---|---|",
    ]
    lines += [
        f"| {number}. {row.question} | **{row.short}** |" for number, row in enumerate(summary.rows, start=1)
    ]
    lines += ["", "## The five questions, in full", ""]
    for number, row in enumerate(summary.rows, start=1):
        lines.append(f"### {number}. {row.question}")
        lines.append("")
        for claim in row.claims:
            lines.append(claim.words())
            lines.append("")
        lines.append(f"**Verdict.** {row.verdict}")
        lines.append("")
    lines += ["## The limits, said plainly", ""]
    lines += [f"- {claim.words()}" for claim in summary.limits]
    lines += [
        "",
        "---",
        "",
        f"*Generated by `{COMMAND}` from `{results_name}`; do not edit by hand. Every number above is a field of "
        f"that file: `{PROVENANCE_NAME}` lists each one with the field it was read from, and "
        f"`{COMMAND} --check` reads the file again and fails if a number, a printed text or a verdict no longer "
        "follows from it. The words around the numbers contain no digits, and the verdicts are computed.*",
        "",
    ]
    return "\n".join(lines)


def provenance(summary: DemoSummary, *, results_name: str = ARTEFACT) -> dict[str, Any]:
    """Every figure of the page: its id, printed text, value, format and the field of the artefact it came from."""
    return {
        "artefact": results_name,
        "generated_by": COMMAND,
        "is_sample": summary.is_sample,
        "figures": [
            {
                "id": name,
                "text": figure.text,
                "value": figure.value,
                "format": figure.format,
                "field": figure.field,
            }
            for name, figure in figures_of(summary)
        ],
    }


def _provenance_text(summary: DemoSummary, *, results_name: str = ARTEFACT) -> str:
    return json.dumps(provenance(summary, results_name=results_name), indent=2, ensure_ascii=False) + "\n"


def generate(results_path: Path) -> tuple[str, str]:
    """`(markdown, provenance json)` for the journey results at `results_path`, verified."""
    results = json.loads(results_path.read_text(encoding="utf-8"))
    summary = build_summary(results)
    return render_markdown(summary), _provenance_text(summary)


def main(argv: Sequence[str] | None = None, *, echo: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(
        prog=COMMAND, description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--results", type=Path, default=RESULTS_DEFAULT, help="default: the committed full-file run"
    )
    parser.add_argument("--out", type=Path, default=None, help="default: DEMO_SUMMARY.md beside the dataset")
    parser.add_argument(
        "--check", action="store_true", help="fail if the committed page differs from a fresh render"
    )
    args = parser.parse_args(argv)
    out: Path = args.out or (
        SUMMARY_DEFAULT if args.results == RESULTS_DEFAULT else args.results.parent / "DEMO_SUMMARY.md"
    )
    provenance_path = out.with_name(PROVENANCE_NAME)
    try:
        markdown, trace = generate(args.results)
    except FileNotFoundError:
        echo(f"{args.results} is missing: run python -m scripts.seed_validated, or use the committed results")
        return 2
    except SummaryError as error:
        echo(f"No page was written: {error}")
        return 1
    if args.check:
        stale = [
            str(path)
            for path, text in ((out, markdown), (provenance_path, trace))
            if not path.is_file() or path.read_text(encoding="utf-8") != text
        ]
        if stale:
            echo(f"stale: {', '.join(stale)}; run {COMMAND} and commit the result")
            return 1
        echo("the demo summary is what the artefact renders")
        return 0
    out.write_text(markdown, encoding="utf-8")
    provenance_path.write_text(trace, encoding="utf-8")
    echo(f"summary     {out}")
    echo(f"provenance  {provenance_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
