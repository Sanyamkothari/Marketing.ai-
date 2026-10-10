"""The manager demo's one-page summary: every number resolves to the journey's artefact (Plan J M111, DEC-1321).

`scripts/demo_summary.py` writes `library/hillstrom-email/DEMO_SUMMARY.md` from the committed full-file run
(`library/hillstrom-email/journey.results.json`, the file `run_report.md` is rendered from). These tests are the
acceptance of the milestone's "every number on the summary resolves to an artefact":

* the committed page and its provenance list are exactly what the artefact renders;
* every number the page prints is one a figure prints, and every figure re-reads its field of the artefact;
* a number written into the words, a value that differs from the artefact and a field the artefact lacks are each
  refused, so the check is not vacuous;
* the verdicts are computed: a result that fell the other way produces the other sentence, so the page cannot
  spin the M110 results by being typed (the list loses to the men's e-mail; uplift does not beat risk ranking).

"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts import demo_summary as demo
from scripts.demo_summary import SummaryError, build_summary, digits_of, render_markdown, verify

FOLDER = Path(__file__).resolve().parents[2] / "library" / "hillstrom-email"
RESULTS = FOLDER / "journey.results.json"
SUMMARY = FOLDER / "DEMO_SUMMARY.md"
PROVENANCE = FOLDER / "DEMO_SUMMARY.provenance.json"
SCRIPT = FOLDER.parent / "DEMO_SCRIPT.md"


@pytest.fixture(scope="module")
def results() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(RESULTS.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture
def mutable(results: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(results)


# ---------------------------------------------------------------------------
# The committed page is what the artefact renders
# ---------------------------------------------------------------------------
def test_the_committed_report_is_what_the_committed_results_render() -> None:
    """`run_report.md` is rendered from the committed results with nothing skipped: a clean checkout can check it."""
    from library.run_engine import journey_report_text

    committed = json.loads(RESULTS.read_text(encoding="utf-8"))
    assert (FOLDER / "run_report.md").read_text(encoding="utf-8") == journey_report_text(committed)


def test_the_committed_summary_and_provenance_are_what_the_artefact_renders() -> None:
    markdown, trace = demo.generate(RESULTS)
    assert SUMMARY.read_text(encoding="utf-8") == markdown
    assert PROVENANCE.read_text(encoding="utf-8") == trace
    assert demo.main(["--check"], echo=lambda _line: None) == 0


def test_check_fails_when_the_committed_page_is_edited_by_hand(tmp_path: Path) -> None:
    out = tmp_path / "DEMO_SUMMARY.md"
    markdown, trace = demo.generate(RESULTS)
    out.write_text(markdown.replace("12,862", "13,862"), encoding="utf-8")
    (tmp_path / demo.PROVENANCE_NAME).write_text(trace, encoding="utf-8")
    said: list[str] = []
    assert demo.main(["--check", "--out", str(out)], echo=said.append) == 1
    assert "stale" in said[0]


# ---------------------------------------------------------------------------
# Every number resolves to an artefact
# ---------------------------------------------------------------------------
def test_every_number_on_the_page_is_printed_by_a_figure_that_resolves_to_a_field(
    results: dict[str, Any],
) -> None:
    summary = build_summary(results)
    assert verify(summary, results) == []
    printed = {token for _, figure in demo.figures_of(summary) for token in digits_of(figure.text)}
    page = SUMMARY.read_text(encoding="utf-8")
    body = page.split("\n---\n", 1)[0]  # the footer names a file and a command, not results
    body = re.sub(
        r"(?m)^(### |\| )\d\. ", r"\1", body
    )  # the questions' own numbering is layout, not a result
    assert digits_of(body) - printed == set(), "a number on the page that no figure prints"
    assert len(printed) > 40, "the page should quote the results, not just mention them"


def test_the_provenance_list_names_the_field_of_every_figure_and_each_resolves(
    results: dict[str, Any],
) -> None:
    trace = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    assert trace["artefact"] == "journey.results.json" and trace["is_sample"] is False
    assert trace["figures"], "no figure listed"
    for entry in trace["figures"]:
        found = demo.walk(results, entry["field"])
        assert found == entry["value"], entry["id"]
        assert demo.format_value(entry["format"], entry["value"]) == entry["text"], entry["id"]


def test_the_words_around_the_numbers_hold_no_digit() -> None:
    summary = build_summary(json.loads(RESULTS.read_text(encoding="utf-8")))
    for words in demo.words_of(summary):
        assert digits_of(words) == set(), words


def test_a_number_written_into_the_words_is_refused(results: dict[str, Any]) -> None:
    summary = build_summary(results)
    rows = list(summary.rows)
    rows[0] = rows[0].model_copy(update={"verdict": rows[0].verdict + " It is worth 40 lakh."})
    problems = verify(summary.model_copy(update={"rows": tuple(rows)}), results)
    assert problems and "40" in problems[0]


def test_a_figure_that_differs_from_the_artefact_is_refused(results: dict[str, Any]) -> None:
    summary = build_summary(results)
    other = copy.deepcopy(results)
    other["steps"]["off_policy"]["estimates"]["conversion"]["chosen"]["difference_from_no_email"][
        "value"
    ] = 0.9
    problems = verify(summary, other)
    assert any("is not" in problem and "difference_from_no_email" in problem for problem in problems)


def test_a_printed_text_that_does_not_follow_from_its_value_is_refused(results: dict[str, Any]) -> None:
    summary = build_summary(results)
    claim = summary.rows[0].claims[0]
    figures = dict(claim.figures)
    figures["contacted"] = figures["contacted"].model_copy(update={"text": "13,862"})
    rows = list(summary.rows)
    rows[0] = rows[0].model_copy(
        update={"claims": (claim.model_copy(update={"figures": figures}), *rows[0].claims[1:])}
    )
    problems = verify(summary.model_copy(update={"rows": tuple(rows)}), results)
    assert any("does not print" in problem for problem in problems)


def test_a_field_the_artefact_lacks_stops_the_page_instead_of_showing_zero(mutable: dict[str, Any]) -> None:
    del mutable["steps"]["off_policy"]["estimates"]["conversion"]["chosen"]
    with pytest.raises(SummaryError, match="holds nothing at"):
        build_summary(mutable)


def test_a_non_finite_or_missing_number_is_not_defaulted(mutable: dict[str, Any]) -> None:
    mutable["steps"]["risk_model"]["test_metrics"]["roc_auc"] = None
    with pytest.raises(SummaryError, match="not a finite number"):
        build_summary(mutable)


def test_the_cli_writes_nothing_and_says_why_when_the_artefact_is_incomplete(
    tmp_path: Path, mutable: dict[str, Any]
) -> None:
    del mutable["steps"]["campaigns"]["conversion"]["report"]["p_value"]
    broken = tmp_path / "journey.results.json"
    broken.write_text(json.dumps(mutable), encoding="utf-8")
    said: list[str] = []
    assert demo.main(["--results", str(broken)], echo=said.append) == 1
    assert "No page was written" in said[0] and not (tmp_path / "DEMO_SUMMARY.md").exists()


def test_the_page_says_every_range_is_95_percent_only_if_the_artefact_does(mutable: dict[str, Any]) -> None:
    mutable["steps"]["campaigns"]["conversion"]["report"]["absolute_lift"]["confidence_level"] = 0.9
    with pytest.raises(SummaryError, match="95%"):
        build_summary(mutable)


# ---------------------------------------------------------------------------
# The verdicts are computed, so the page cannot spin the results
# ---------------------------------------------------------------------------
def _rows(summary: Any) -> dict[str, Any]:
    return {row.key: row for row in summary.rows}


def test_the_committed_results_are_told_as_they_fell(results: dict[str, Any]) -> None:
    rows = _rows(build_summary(results))
    assert rows["worth"].short == "Beats sending nothing"
    assert "beats sending no e-mail" in rows["worth"].verdict
    assert rows["simple_rule"].short == "No, it is worse"
    assert "measurably worse than sending everyone the men's e-mail" in rows["simple_rule"].verdict
    assert rows["how_we_know"].short == "Uplift does not beat risk ranking"
    assert rows["leave_alone"].short == "Named, but not established"
    assert "not calibrated" in rows["leave_alone"].verdict
    page = SUMMARY.read_text(encoding="utf-8")
    for quoted in ("+0.45 pts", "+0.60 pts", "-0.24 pts", "0.534", "p = 0.002"):
        assert quoted in page, f"{quoted} (the M110 result) is missing from the summary"


def test_if_the_list_beat_the_men_email_the_page_would_say_so(mutable: dict[str, Any]) -> None:
    gap = mutable["steps"]["off_policy"]["chosen_against_everyone"]["conversion"][
        "chosen_minus_everyone_Mens E-Mail"
    ]
    gap.update(value=0.003, ci_low=0.001, ci_high=0.005)
    rows = _rows(build_summary(mutable))
    assert rows["simple_rule"].short == "Yes, it is better"
    assert "measurably better" in rows["simple_rule"].verdict


def test_if_the_range_included_zero_the_page_would_say_not_shown(mutable: dict[str, Any]) -> None:
    gap = mutable["steps"]["off_policy"]["chosen_against_everyone"]["conversion"][
        "chosen_minus_everyone_Mens E-Mail"
    ]
    gap.update(value=-0.001, ci_low=-0.004, ci_high=0.002)
    rows = _rows(build_summary(mutable))
    assert rows["simple_rule"].short == "Not shown"


def test_if_the_list_did_not_beat_nothing_the_page_would_say_so(mutable: dict[str, Any]) -> None:
    lift = mutable["steps"]["off_policy"]["estimates"]["conversion"]["chosen"]["difference_from_no_email"]
    lift.update(value=0.001, ci_low=-0.002, ci_high=0.004)
    row = _rows(build_summary(mutable))["worth"]
    assert row.short == "Mixed: see the two measures"
    assert "Mixed" in row.verdict and "not shown to differ" in row.verdict


def test_if_uplift_beat_risk_the_page_would_say_so(mutable: dict[str, Any]) -> None:
    approval = mutable["steps"]["approval"]["checks"]
    next(c for c in approval if c["code"] == "UPLIFT_NOT_BETTER_THAN_RISK")["passed"] = True
    for offer in mutable["steps"]["beats_risk_out_of_sample"]["offers"].values():
        offer["beats_risk"] = True
    next(c for c in approval if c["code"] == "UPLIFT_MISCALIBRATED")["passed"] = True
    rows = _rows(build_summary(mutable))
    assert rows["how_we_know"].short == "Uplift beats risk ranking"
    assert "does not beat" not in rows["how_we_know"].verdict
    assert (
        "calibrated by decile" in rows["how_we_know"].verdict
        and "not calibrated" not in rows["how_we_know"].verdict
    )
    assert (
        rows["leave_alone"].short == "Named, but not confirmed"
    )  # calibrated, but the measurement shows no harm


def _sleeping_dogs_range(data: dict[str, Any]) -> dict[str, Any]:
    """The Pack's backfire-table cell holding the sleeping dogs' difference range (low and high figures)."""
    sections = data["steps"]["campaigns"]["conversion"]["proof"]["view"]["sections"]
    table = next(s for s in sections if s["key"] == "backfire")["table"]
    column = table["columns"].index("Range")
    row = next(r for r in table["rows"] if r[:2] == ["Predicted group", "Sleeping dogs"])
    cell: dict[str, Any] = row[column]
    return cell


def test_if_the_measured_sleeping_dogs_were_harmed_the_page_would_say_so(mutable: dict[str, Any]) -> None:
    cell = _sleeping_dogs_range(mutable)
    cell["low"]["value"], cell["high"]["value"] = -0.02, -0.004
    row = _rows(build_summary(mutable))["leave_alone"]
    assert row.short == "Named, and harm shown" and "made things worse" in row.verdict


def test_if_the_measured_sleeping_dogs_did_better_the_page_would_say_the_label_is_contradicted(
    mutable: dict[str, Any],
) -> None:
    cell = _sleeping_dogs_range(mutable)
    cell["low"]["value"], cell["high"]["value"] = 0.004, 0.02
    row = _rows(build_summary(mutable))["leave_alone"]
    assert "contradicts the label" in row.verdict


def test_a_mixed_uplift_result_reads_as_mixed(mutable: dict[str, Any]) -> None:
    mutable["steps"]["beats_risk_out_of_sample"]["offers"]["Womens E-Mail"]["beats_risk"] = True
    row = _rows(build_summary(mutable))["how_we_know"]
    assert row.short == "Uplift does not beat risk ranking"
    assert "does not beat risk ranking everywhere" in row.verdict
    assert "beats it for the women's" in row.verdict


# ---------------------------------------------------------------------------
# A sample run is labelled as one
# ---------------------------------------------------------------------------
def test_a_run_on_the_sample_says_so_in_its_first_lines(mutable: dict[str, Any]) -> None:
    mutable["rows"] = 6400
    summary = build_summary(mutable)
    page = render_markdown(summary)
    assert summary.is_sample and page.splitlines()[2].startswith("> **SAMPLE RUN.")
    assert "not the validated results" in page
    assert "SAMPLE RUN" not in SUMMARY.read_text(encoding="utf-8")
