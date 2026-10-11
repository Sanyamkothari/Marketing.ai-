"""Plan K's measurement upgrades on Hillstrom's randomised e-mail test (DEC-1413).

Nothing here downloads anything. The fast tests read the committed results (`plan_k.results.json`, aggregates only)
and the committed report; they are the acceptance of the milestone's "never write a number the run did not
produce":

* every figure of the report's section 9 resolves to a field of `plan_k.results.json`, and the words hold no digit
  (M111's demo-summary pattern); a number typed into a sentence, a figure that differs from the file and a field the
  file lacks are each refused, and each verdict is computed, so a result that fell the other way is worded the
  other way;
* the committed report is exactly what `library.run_engine` renders from the committed results, and the sections
  before section 9 are the report as it was;
* the default journey is unchanged: no extra setting, campaign or key without the opt-in.

The journey test runs the opt-in journey and the default one on the committed sample, with smaller arm floors, and
checks that each Plan K figure equals the artefact the engine stored. The full file's numbers are the committed ones;
the one test that re-derives them skips, saying why, when the full file is absent.
"""

from __future__ import annotations

import copy
import inspect
import json
import re
from pathlib import Path
from typing import Any

import library.plan_k_report as pk
import pytest
from library.plan_k_report import (
    PlanKReportError,
    build_section,
    digits_of,
    provenance,
    render_plan_k,
    verify,
)

from conftest import CONFIGS, LIBRARY

FOLDER = LIBRARY / "hillstrom-email"
SAMPLE = FOLDER / "sample.csv"
PREPARED = FOLDER / "data" / "prepared.csv"
REPORT = FOLDER / "run_report.md"
PLAN_K = FOLDER / "plan_k.results.json"
JOURNEY = FOLDER / "journey.results.json"
AUDIT = FOLDER / "audit.results.json"

SAMPLE_FLOORS: dict[str, Any] = {
    "validation.min_positive": 20,
    "uplift.min_arm_positives": 5,
    "uplift.min_arm_rows": 500,
    "uplift.bootstrap_samples": 50,
}


@pytest.fixture(scope="module")
def results() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(PLAN_K.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture
def mutable(results: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(results)


def _at(document: dict[str, Any], path: str) -> Any:
    return pk.walk(document, path)


def _set(document: dict[str, Any], path: str, value: Any) -> None:
    *head, last = path.split(".")
    node: Any = document
    for part in head:
        node = node[int(part)] if isinstance(node, list) else node[part]
    node[int(last) if isinstance(node, list) else last] = value


BASE = "steps.plan_k."
RUN_SCORE = BASE + "covariate_run_score"
ROLLOUT = BASE + "rollout"
POOLING = BASE + "pooling"
HARM = BASE + "harm_alarm"


# ---------------------------------------------------------------------------
# The default journey is unchanged
# ---------------------------------------------------------------------------
def test_the_plan_k_journey_is_opt_in_and_the_default_adds_nothing() -> None:
    import library.journey as journey

    parameter = inspect.signature(journey.run_journey).parameters["plan_k"]
    assert parameter.default is False
    assert not set(journey.PLAN_K_UPLIFT_OVERRIDES) & set(journey.HILLSTROM.uplift_overrides)
    assert not journey.HILLSTROM.risk_overrides.keys() & journey.PLAN_K_UPLIFT_OVERRIDES.keys()
    committed = json.loads(JOURNEY.read_text(encoding="utf-8"))
    assert "plan_k" not in committed and "plan_k" not in committed["steps"]
    assert sorted(committed["steps"]["campaigns"]) == ["conversion", "spend"]
    assert not any(key.startswith("uplift.evidence") for key in committed["uplift_overrides"])


def test_the_opt_in_turns_on_the_comparison_the_rollout_verdict_needs() -> None:
    import library.journey as journey

    from engine.config import resolve_config

    merged = {
        "problem_type": "uplift",
        "uplift.treatment_column": "segment",
        **journey.HILLSTROM.uplift_overrides,
        **journey.PLAN_K_UPLIFT_OVERRIDES,
    }
    evidence = resolve_config("hillstrom-email", merged, root=CONFIGS).config.uplift.evidence
    assert evidence.risk_comparison and evidence.rollout_verdict
    default = {
        "problem_type": "uplift",
        "uplift.treatment_column": "segment",
        **journey.HILLSTROM.uplift_overrides,
    }
    plain = resolve_config("hillstrom-email", default, root=CONFIGS).config.uplift.evidence
    assert not plain.risk_comparison and not plain.rollout_verdict


def _stub_journey_results(committed: dict[str, Any]) -> dict[str, Any]:
    results = copy.deepcopy(committed)
    results["plan_k"] = {"uplift_overrides": {}}
    results["steps"]["plan_k"] = {}
    return results


def test_the_readout_says_which_default_numbers_the_run_reproduced() -> None:
    from library.journey import REPRODUCED, plan_k_results

    committed = json.loads(JOURNEY.read_text(encoding="utf-8"))
    same = plan_k_results(_stub_journey_results(committed), committed)["reproduction"]
    assert same["csv_sha256_equal"] is True
    assert same["identical"] == dict.fromkeys(REPRODUCED, True)
    moved = _stub_journey_results(committed)
    moved["steps"]["off_policy"]["estimates"]["conversion"]["chosen"]["value"]["value"] += 1e-6
    moved["steps"]["uplift_model"]["evaluation"]["auuc"]["value"] += 1e-6
    different = plan_k_results(moved, committed)["reproduction"]["identical"]
    assert different["off_policy"] is False and different["uplift_evaluation"] is False
    assert different["campaigns"] is True and different["split"] is True
    assert "reproduction" not in plan_k_results(moved)


# ---------------------------------------------------------------------------
# The committed results are only what the engine's artefacts said
# ---------------------------------------------------------------------------
def test_the_results_file_holds_aggregates_only(results: dict[str, Any]) -> None:
    text = PLAN_K.read_text(encoding="utf-8")
    assert PLAN_K.stat().st_size < 200_000
    assert '"customer_id"' not in text and '"primary_key"' not in text
    assert set(results["steps"]["plan_k"]) == {
        "plans_registered_before_outcomes",
        "covariate_run_score",
        "rollout",
        "pooling",
        "harm_alarm",
    }


def test_the_run_score_plan_was_registered_for_the_scoring_runs_prediction(results: dict[str, Any]) -> None:
    run = _at(results, RUN_SCORE)
    registered = _at(results, BASE + "plans_registered_before_outcomes")
    assert registered["covariate_source"] == "run_score" == run["plan"]["covariate_source"]
    assert registered["plan_hash"] == run["plan"]["plan_hash"] == run["report"]["test_plan_hash"]
    assert run["plan"]["covariate_column"] is None
    assert run["covariate_source"] == run["report"]["covariate_source"] == "run_score"


def test_the_adjusted_campaign_measured_the_plain_campaigns_customers(results: dict[str, Any]) -> None:
    run = _at(results, RUN_SCORE)
    same = run["plain_campaign"]["same_customers_and_outcomes"]
    assert all(same.values()), same
    assert run["unadjusted"] == run["plain_campaign"]["unadjusted"] == run["report"]["absolute_lift"]
    assert run["adjusted"] == run["report"]["adjusted_interval"]
    assert run["variance_reduction"] == run["report"]["variance_reduction"]
    assert run["rows_covariate_missing"] == run["report"]["rows_covariate_missing"]


def test_pooling_is_recorded_as_the_modules_own_answer(results: dict[str, Any]) -> None:
    pooling = _at(results, POOLING)
    assert pooling["setting_enabled_by_the_journey"] is False
    assert pooling["first_drawn"] == 4
    for cell in pooling["cells"]:
        for estimate in cell.get("estimates", []):
            if estimate["status"] == "not_pooled":
                assert estimate["code"] == "POOL_TOO_FEW"
                assert estimate["pooled_effect"] is None and estimate["pooled_interval"] is None
            assert estimate["campaigns_compared"] == cell["campaigns_compared"]
    assert pooling["pooled"] is False and pooling["largest_cell"] < pooling["first_drawn"]


def test_the_harm_alarm_is_not_applicable_because_every_reading_is_final(results: dict[str, Any]) -> None:
    harm = _at(results, HARM)
    assert harm["applicable"] is False and harm["setting_enabled_by_the_journey"] is False
    assert harm["readings"], "every measured campaign is recorded"
    for reading in harm["readings"].values():
        assert reading["interim"] is False and reading["early_look"] is False
        assert reading["rows_immature"] == 0 and reading["status"] == "mature"
        assert reading["harm_alarm_file_written"] is False


def test_the_rollout_verdict_is_the_engines_and_priced_only_on_value_inputs(results: dict[str, Any]) -> None:
    rollout = _at(results, ROLLOUT)
    assert rollout["written"] is True and rollout["artefact"] == "risk_comparison.json"
    verdict = rollout["rollout"]
    assert verdict == rollout["comparison"]["rollout"]
    assert verdict["outcome"] in {"safe", "not_safe", "cannot_tell"}
    assert (verdict["value_basis"] == "net_inr") == (verdict["lower_bound_inr"] is not None)
    if (
        verdict["outcome"] == "cannot_tell"
        and verdict["lower_bound_conversions"] < 0 < verdict["upper_bound_conversions"]
    ):
        assert verdict["rows_needed"] is None or verdict["rows_needed"] > 0
    assert verdict["lower_bound_conversions"] <= verdict["upper_bound_conversions"]


# ---------------------------------------------------------------------------
# Section 9 resolves to the file, and its verdicts are computed
# ---------------------------------------------------------------------------
def test_every_number_of_section_nine_is_printed_by_a_figure_that_resolves(results: dict[str, Any]) -> None:
    section = build_section(results)
    assert verify(section, results) == []
    text = render_plan_k(results, results_path="plan_k.results.json", command="x")
    body = text.split("*Section 9 is generated", 1)[0]
    printed = {text for _, figure in pk.figures_of(section) for text in [figure.text]}
    allowed = set().union(*(digits_of(text) for text in printed))
    stray = digits_of(re.sub(r"\bM\d{3}\b", "", body)) - allowed - {"9", "9.1", "9.2", "9.3", "9.4"}
    assert not stray, f"numbers printed that no figure prints: {sorted(stray)}"


def test_the_provenance_names_the_field_of_every_figure_and_each_resolves(results: dict[str, Any]) -> None:
    trace = provenance(build_section(results))
    assert trace["artefact"] == "plan_k.results.json" and trace["figures"]
    for figure in trace["figures"]:
        found = pk.walk(results, figure["field"])
        assert found == figure["value"] or (
            isinstance(found, float) and abs(found - figure["value"]) < 1e-12
        ), figure
        assert figure["text"] == pk.format_value(figure["format"], figure["value"])


def test_the_words_around_the_numbers_hold_no_digit(results: dict[str, Any]) -> None:
    for block in build_section(results).blocks:
        words = pk.MILESTONE.sub("", block.template)
        if block.kind == "heading":
            words = re.sub(r"^#+ 9(\.\d*)? ", "", words)
        assert not digits_of(words), block.template


def test_a_number_written_into_the_words_is_refused(results: dict[str, Any]) -> None:
    section = build_section(results)
    blocks = list(section.blocks)
    at = next(i for i, b in enumerate(blocks) if b.kind == "claim")
    blocks[at] = blocks[at].model_copy(update={"template": blocks[at].template + " Roughly 12 more."})
    problems = verify(type(section)(blocks=tuple(blocks)), results)
    assert any("12" in problem for problem in problems)


def test_a_figure_that_differs_from_the_file_is_refused(results: dict[str, Any]) -> None:
    section = build_section(results)
    changed = copy.deepcopy(results)
    _set(changed, RUN_SCORE + ".treated_rows", _at(results, RUN_SCORE + ".treated_rows") + 1)
    assert any("treated_rows" in problem for problem in verify(section, changed))


def test_a_printed_text_that_does_not_follow_from_its_value_is_refused(results: dict[str, Any]) -> None:
    section = build_section(results)
    blocks = []
    for block in section.blocks:
        figures = {
            name: (
                figure.model_copy(update={"text": "123.45 pts"})
                if figure.format == "signed_points"
                else figure
            )
            for name, figure in block.figures.items()
        }
        blocks.append(block.model_copy(update={"figures": figures}))
    assert any("does not print" in p for p in verify(type(section)(blocks=tuple(blocks)), results))


def test_a_field_the_file_lacks_stops_the_section_instead_of_showing_zero(mutable: dict[str, Any]) -> None:
    del mutable["steps"]["plan_k"]["covariate_run_score"]["rows_covariate_missing"]
    with pytest.raises(PlanKReportError, match="rows_covariate_missing"):
        build_section(mutable)


def test_a_non_finite_number_is_not_defaulted(mutable: dict[str, Any]) -> None:
    _set(mutable, RUN_SCORE + ".treated_rows", float("nan"))
    with pytest.raises(PlanKReportError, match="not a finite number"):
        build_section(mutable)


def _text(document: dict[str, Any]) -> str:
    return render_plan_k(document, results_path="plan_k.results.json", command="x")


def test_a_negative_variance_reduction_is_worded_as_a_widening(mutable: dict[str, Any]) -> None:
    _set(mutable, RUN_SCORE + ".variance_reduction", -0.01)
    text = _text(mutable)
    assert "The prediction widened the range" in text and "-1.00e-02" in text
    assert "narrowed the range" not in text


def test_a_positive_variance_reduction_is_worded_as_a_narrowing(mutable: dict[str, Any]) -> None:
    _set(mutable, RUN_SCORE + ".variance_reduction", 0.02)
    assert "The prediction narrowed the range" in _text(mutable)


def test_a_reduction_that_rounds_to_zero_is_not_worded_as_a_narrowing(mutable: dict[str, Any]) -> None:
    _set(mutable, RUN_SCORE + ".variance_reduction", 1e-8)
    text = _text(mutable)
    assert "essentially as it was" in text and "narrowed the range" not in text


def test_a_campaign_with_no_adjusted_estimate_says_why(mutable: dict[str, Any]) -> None:
    run = _at(mutable, RUN_SCORE)
    run["adjusted"], run["variance_reduction"] = None, None
    run["adjustment_note"] = "The scoring run predicted the same for every customer."
    text = _text(mutable)
    assert "**Not adjusted.**" in text and "predicted the same for every customer" in text
    assert "Adjusted by the prediction" not in text


def test_the_side_of_zero_each_range_lies_on_is_computed(mutable: dict[str, Any]) -> None:
    for key, low, high in (("unadjusted", 0.001, 0.02), ("adjusted", -0.03, -0.001)):
        _set(mutable, f"{RUN_SCORE}.{key}.ci_low", low)
        _set(mutable, f"{RUN_SCORE}.{key}.ci_high", high)
    text = _text(mutable)
    assert "Unadjusted, the whole range is above zero; adjusted, the whole range is below zero" in text


@pytest.mark.parametrize(
    ("outcome", "words"),
    [
        ("safe", "is safe to roll out against risk ranking"),
        ("not_safe", "is worse than risk ranking"),
        ("cannot_tell", "this data cannot tell the two rankings apart"),
    ],
)
def test_the_rollout_sentence_follows_the_verdict(mutable: dict[str, Any], outcome: str, words: str) -> None:
    _set(mutable, ROLLOUT + ".rollout.outcome", outcome)
    assert words in _text(mutable)


def test_an_unknown_rollout_outcome_stops_the_section(mutable: dict[str, Any]) -> None:
    _set(mutable, ROLLOUT + ".rollout.outcome", "go")
    with pytest.raises(PlanKReportError, match="none of safe"):
        build_section(mutable)


def test_a_rollout_without_value_inputs_prints_no_rupee(mutable: dict[str, Any]) -> None:
    verdict = _at(mutable, ROLLOUT + ".rollout")
    verdict.update(
        value_basis="conversions", lower_bound_inr=None, upper_bound_inr=None, rows_without_value=None
    )
    verdict["inr_unavailable_reason"] = None
    text = _text(mutable)
    assert "₹" not in text and "No rupee bounds: the comparison had no value inputs" in text


def test_a_rollout_the_run_did_not_write_is_said_not_invented(mutable: dict[str, Any]) -> None:
    rollout = _at(mutable, ROLLOUT)
    rollout["written"], rollout["rollout"] = False, None
    rollout.pop("comparison", None)
    assert "wrote no rollout verdict" in _text(mutable)


def test_four_comparable_campaigns_would_be_pooled_and_said_so(mutable: dict[str, Any]) -> None:
    pooling = _at(mutable, POOLING)
    cell = pooling["cells"][0]
    cell["campaigns_compared"] = 4
    estimate = cell["estimates"][0]
    estimate.update(
        status="pooled",
        code=None,
        reason=None,
        campaigns_compared=4,
        pooled_effect=0.004,
        pooled_interval={"value": 0.004, "ci_low": 0.001, "ci_high": 0.007, "confidence_level": 0.95},
    )
    pooling["pooled"], pooling["largest_cell"] = True, 4
    text = _text(mutable)
    assert "**Not pooled.**" not in text and "| pooled |" in text and "estimated after pooling" in text
    assert "measured +0.60 pts, estimated after pooling +0.40 pts, range +0.10 pts to +0.70 pts." in text


def test_a_pooled_continuous_cell_is_printed_in_its_own_unit_not_in_points(mutable: dict[str, Any]) -> None:
    pooling = _at(mutable, POOLING)
    cell = next(c for c in pooling["cells"] if c["outcome_kind"] == "continuous")
    cell["campaigns_compared"] = 4
    cell["estimates"] = [
        {
            "campaign_id": cell["campaigns_measured"][0],
            "status": "pooled",
            "code": None,
            "reason": None,
            "campaigns_compared": 4,
            "estimate_field": "adjusted_interval",
            "measured_effect": 0.5,
            "measured_se": 0.1,
            "pooled_effect": 0.4,
            "pooled_interval": {"value": 0.4, "ci_low": 0.1, "ci_high": 0.7, "confidence_level": 0.95},
        }
    ]
    pooling["pooled"], pooling["largest_cell"] = True, 4
    sentence = next(line for line in _text(mutable).splitlines() if line.startswith("Cell spend:"))
    assert "pts" not in sentence
    assert (
        "measured +0.50, estimated after pooling +0.40, range +0.10 to +0.70 (in the outcome's own unit)."
        in sentence
    )


def test_the_shared_scoring_run_clause_is_printed_only_when_the_file_shows_one_run(
    results: dict[str, Any], mutable: dict[str, Any]
) -> None:
    run = _at(results, POOLING + ".cells.0.scoring_runs.0")
    assert f"measured on the one scoring run {run}, so within a cell at most one of them counts" in _text(
        results
    )
    cells = _at(mutable, POOLING + ".cells")
    cells[1]["scoring_runs"] = ["r_20261010_99999999"]
    text = _text(mutable)
    assert "**Not pooled.**" in text and "scoring run" not in text.split("**Not pooled.**")[1].split("\n")[0]


def test_the_pooling_verdict_is_worded_from_the_count_not_typed(results: dict[str, Any]) -> None:
    text = _text(results)
    first = _at(results, POOLING + ".first_drawn")
    assert f"not pooled: fewer than {first} comparable campaigns (POOL_TOO_FEW)" in text


def test_a_harm_alarm_on_an_interim_reading_would_be_said_to_apply(mutable: dict[str, Any]) -> None:
    harm = _at(mutable, HARM)
    reading = next(iter(harm["readings"].values()))
    reading.update(interim=True, early_look=True)
    harm["applicable"] = True
    text = _text(mutable)
    assert "**Applicable.**" in text and "**Not applicable.**" not in text


def test_a_run_that_did_not_reproduce_the_default_journey_says_which_group(mutable: dict[str, Any]) -> None:
    _set(mutable, "reproduction.identical.off_policy", False)
    text = _text(mutable)
    assert "**Not reproduced.**" in text and "`off_policy`" in text and "**Reproduced.**" not in text


def test_a_run_on_another_file_is_not_compared_with_the_committed_default(mutable: dict[str, Any]) -> None:
    _set(mutable, "reproduction.csv_sha256_equal", False)
    assert "read a different file from the default journey" in _text(mutable)


# ---------------------------------------------------------------------------
# The committed report
# ---------------------------------------------------------------------------
def test_the_committed_report_is_the_committed_journey_audit_and_plan_k_results() -> None:
    from library.run_engine import journey_report_text

    journey = json.loads(JOURNEY.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    plan_k = json.loads(PLAN_K.read_text(encoding="utf-8"))
    expected = journey_report_text(journey, audit, plan_k=plan_k)
    assert REPORT.read_text(encoding="utf-8") == expected
    assert journey_report_text(journey) == expected, "the committed Plan K results are the default"
    before = journey_report_text(journey, committed_plan_k=False)
    assert expected.startswith(before.rstrip("\n")), "sections 1 to 8 are the report as it was"
    assert "## 9. Plan K's measurement upgrades on the same rows" in expected
    assert "## 9." not in before
    assert "python -m library.run_engine journey --dataset hillstrom-email --plan-k" in expected


def test_the_committed_plan_k_is_not_appended_to_a_journey_of_another_file_or_run() -> None:
    from library.run_engine import journey_report_text

    journey = json.loads(JOURNEY.read_text(encoding="utf-8"))
    assert "**Reproduced.**" in journey_report_text(journey)
    other_file = copy.deepcopy(journey)
    other_file["csv_sha256"] = "0" * 64
    text = journey_report_text(other_file)
    assert "**Reproduced.**" not in text and "Plan K's measurement upgrades" not in text
    other_run = copy.deepcopy(journey)
    other_run["steps"]["uplift_model"]["run_id"] = "r_20261010_99999999"
    assert "Plan K's measurement upgrades" not in journey_report_text(other_run)


def test_a_report_without_plan_k_results_is_what_it_was_before() -> None:
    from library.run_engine import journey_report_text

    journey = json.loads(JOURNEY.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    bare = journey_report_text(journey, audit, committed_plan_k=False)
    assert bare == journey_report_text(journey, audit, plan_k=None, committed_plan_k=False)
    assert "Plan K's measurement upgrades" not in bare
    alone = journey_report_text(journey, committed_audit=False)
    assert "Plan K's measurement upgrades" not in alone and "## 8." not in alone, "the journey alone"


def _library_section() -> str:
    text = (LIBRARY.parent / "docs" / "LIBRARY.md").read_text(encoding="utf-8")
    start = text.index("## 10. Plan K: the measurement upgrades")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def test_the_figures_in_docs_library_are_the_committed_results(results: dict[str, Any]) -> None:
    """No download: every key number hand-copied into LIBRARY.md section 10 is read back from `plan_k.results.json`."""
    section = _library_section()
    run = _at(results, RUN_SCORE)

    def pts(interval: dict[str, Any]) -> list[str]:
        return [f"{interval[k] * 100:+.2f}" for k in ("value", "ci_low", "ci_high")]

    value, low, high = pts(run["unadjusted"])
    expected = [f"{value} pts ({low} to {high})"]
    if run["adjusted"] is not None:
        value, low, high = pts(run["adjusted"])
        expected.append(f"{value} pts ({low} to {high})")
        expected.append(f"{run['variance_reduction']:.2e}")
    expected.append(f"{run['rows_covariate_missing']:,} measured customers had no prediction")
    verdict = _at(results, ROLLOUT)["rollout"]
    expected.append(f"**{verdict['outcome']}**")
    expected.append(f"{verdict['lower_bound_conversions']:+.1f}")
    expected.append(f"{verdict['upper_bound_conversions']:+.1f}")
    expected.append(f"{_at(results, ROLLOUT + '.comparison.rows'):,}")
    expected.append(f"{_at(results, ROLLOUT + '.comparison.difference.value'):+.1f} conversions")
    if verdict["lower_bound_inr"] is not None:
        expected += [
            (
                f"-₹{abs(verdict['lower_bound_inr']):,.0f}"
                if verdict["lower_bound_inr"] < 0
                else f"₹{verdict['lower_bound_inr']:,.0f}"
            ),
            f"₹{verdict['upper_bound_inr']:,.0f}",
        ]
    pooling = _at(results, POOLING)
    expected.append("POOL_TOO_FEW" if not pooling["pooled"] else "pooled")
    expected.append(f"fewer than {pooling['first_drawn']} comparable campaigns")
    expected.append("not applicable" if not _at(results, HARM + ".applicable") else "applicable")
    expected.append("python -m library.run_engine journey --dataset hillstrom-email --plan-k")
    missing = [e for e in expected if e not in section]
    assert not missing, f"docs/LIBRARY.md section 10 no longer matches plan_k.results.json: {missing}"


# ---------------------------------------------------------------------------
# The committed results are only ever written from the dataset's own prepared file
# ---------------------------------------------------------------------------
def test_a_plan_k_journey_on_another_file_leaves_the_committed_results_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`journey --plan-k --csv sample.csv` runs, but leaves the committed full-file results and report alone."""
    import library.journey as journey_module
    import library.run_engine as engine

    committed = json.loads(JOURNEY.read_text(encoding="utf-8"))

    class Outcome:
        def __init__(self) -> None:
            self.results = _stub_journey_results(committed)
            self.directory = tmp_path

    def stub(*_args: Any, **kwargs: Any) -> Outcome:
        assert kwargs["plan_k"] is True
        return Outcome()

    monkeypatch.setattr(journey_module, "run_journey", stub)
    before = (PLAN_K.read_bytes(), REPORT.read_bytes(), JOURNEY.read_bytes())
    code = engine.journey_main(["--dataset", "hillstrom-email", "--csv", str(SAMPLE), "--plan-k"])
    assert code == 0
    assert (PLAN_K.read_bytes(), REPORT.read_bytes(), JOURNEY.read_bytes()) == before
    assert "not committed" in capsys.readouterr().err
    readout = json.loads((tmp_path / "plan_k.results.json").read_text(encoding="utf-8"))
    assert readout["reproduction"]["identical"]["split"] is True


# ---------------------------------------------------------------------------
# The opt-in journey, on the sample
# ---------------------------------------------------------------------------
_CALLS: list[tuple[str, str]] = []


@pytest.fixture(scope="module")
def sample_runs(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The default journey and the Plan K one on the committed sample (smaller floors), in one temporary store."""
    import library.journey as module

    def spy(name: str, original: Any) -> Any:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            _CALLS.append(
                (name, str(args[3]) if name == "_uplift_checks_on" else str(kwargs.get("covariate_source")))
            )
            return original(*args, **kwargs)

        return wrapped

    runs = tmp_path_factory.mktemp("runs")
    common: dict[str, Any] = {
        "csv_path": SAMPLE,
        "config_root": CONFIGS,
        "runs_dir": runs,
        "extra_risk_overrides": {"validation.min_positive": 20},
        "extra_uplift_overrides": SAMPLE_FLOORS,
    }
    default = module.run_journey(module.HILLSTROM, **common)
    _CALLS.clear()
    with pytest.MonkeyPatch.context() as patch:
        for name in ("_open_campaign", "_uplift_checks_on"):
            patch.setattr(module, name, spy(name, getattr(module, name)))
        opted_in = module.run_journey(module.HILLSTROM, plan_k=True, **common)
    return default, opted_in


@pytest.mark.slow
@pytest.mark.integration
def test_the_default_journey_of_the_same_run_has_no_plan_k_step(sample_runs: Any) -> None:
    default, opted_in = sample_runs
    assert "plan_k" not in default.results and "plan_k" not in default.results["steps"]
    assert sorted(default.results["steps"]["campaigns"]) == ["conversion", "spend"]
    assert default.directory.name == "journey" and opted_in.directory.name == "journey_plan_k"
    assert not any(k.startswith("uplift.evidence") for k in default.results["uplift_overrides"])
    assert opted_in.results["uplift_overrides"]["uplift.evidence.rollout_verdict"] is True
    assert sorted(opted_in.results["steps"]["campaigns"]) == ["conversion", "spend"]


@pytest.mark.slow
@pytest.mark.integration
def test_the_extra_settings_change_nothing_the_default_journey_reports(sample_runs: Any) -> None:
    from library.journey import REPRODUCED, plan_k_results

    default, opted_in = sample_runs
    readout = plan_k_results(opted_in.results, json.loads(json.dumps(default.results, default=str)))
    assert readout["reproduction"]["csv_sha256_equal"] is True
    assert readout["reproduction"]["identical"] == dict.fromkeys(REPRODUCED, True), readout["reproduction"]


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.usefixtures("sample_runs")
def test_the_run_score_plan_is_registered_before_any_evaluation_outcome_is_read() -> None:
    names = [(name, what) for name, what in _CALLS]
    opens = [i for i, (name, what) in enumerate(names) if name == "_open_campaign" and what == "run_score"]
    assert len(opens) == 1
    evaluation_reads = [
        i
        for i, (name, what) in enumerate(names)
        if name == "_uplift_checks_on" and what == "u_evaluation_rows"
    ]
    assert evaluation_reads and opens[0] < min(evaluation_reads)


@pytest.mark.slow
@pytest.mark.integration
def test_the_recorded_figures_are_the_artefacts_the_engine_stored(sample_runs: Any) -> None:
    from engine.measurement.campaign import REPORT_FILENAME, campaign_key
    from engine.storage import LocalStorage, run_key

    _default, opted_in = sample_runs
    storage = LocalStorage(opted_in.directory / "data")
    step = opted_in.results["steps"]["plan_k"]

    run = step["covariate_run_score"]
    stored = json.loads(storage.read_bytes(campaign_key(run["campaign_id"], REPORT_FILENAME)))
    assert run["report"] == stored
    assert run["covariate_source"] == "run_score" and stored["covariate_source"] == "run_score"
    assert run["unadjusted"] == stored["absolute_lift"] and run["adjusted"] == stored["adjusted_interval"]
    assert run["variance_reduction"] == stored["variance_reduction"]
    assert run["rows_covariate_missing"] == stored["rows_covariate_missing"]
    assert all(run["plain_campaign"]["same_customers_and_outcomes"].values())

    uplift_id = opted_in.results["steps"]["uplift_model"]["run_id"]
    comparison = json.loads(storage.read_bytes(run_key(uplift_id, "risk_comparison.json")))
    assert step["rollout"]["comparison"] == comparison and step["rollout"]["rollout"] == comparison["rollout"]
    assert comparison["rollout"]["outcome"] in {"safe", "not_safe", "cannot_tell"}

    pooling = step["pooling"]
    assert pooling["pooled"] is False
    assert {e["code"] for cell in pooling["cells"] for e in cell.get("estimates", [])} == {"POOL_TOO_FEW"}
    assert not any(
        storage.exists(f"campaigns/{c['campaign_id']}/pooled_estimate.json")
        for c in step["harm_alarm"]["readings"].values()
    )

    harm = step["harm_alarm"]
    assert harm["applicable"] is False
    assert not any(
        storage.exists(f"campaigns/{c['campaign_id']}/harm_alarm.json") for c in harm["readings"].values()
    )


@pytest.mark.slow
@pytest.mark.integration
def test_the_section_is_rendered_from_the_sample_runs_results_alone(sample_runs: Any) -> None:
    from library.journey import plan_k_results

    default, opted_in = sample_runs
    readout = json.loads(json.dumps(plan_k_results(opted_in.results, default.results), default=str))
    text = render_plan_k(readout, results_path="plan_k.results.json", command="x")
    assert verify(build_section(readout), readout) == []
    assert "### 9.1" in text and "### 9.4" in text


@pytest.mark.slow
@pytest.mark.integration
def test_a_fresh_run_on_the_full_file_reproduces_the_committed_plan_k_numbers(tmp_path: Path) -> None:
    if not PREPARED.is_file():
        pytest.skip("needs the full file (fetch.py); it is never in git")
    from library.journey import HILLSTROM, plan_k_results, run_journey

    fresh = run_journey(HILLSTROM, csv_path=PREPARED, config_root=CONFIGS, runs_dir=tmp_path, plan_k=True)
    committed = json.loads(PLAN_K.read_text(encoding="utf-8"))
    readout = json.loads(json.dumps(plan_k_results(fresh.results), default=str))

    def numbers(document: dict[str, Any]) -> dict[str, Any]:
        step = document["steps"]["plan_k"]
        run = step["covariate_run_score"]
        return {
            "adjusted": run["adjusted"],
            "unadjusted": run["unadjusted"],
            "variance_reduction": run["variance_reduction"],
            "missing": run["rows_covariate_missing"],
            "rollout": step["rollout"]["rollout"],
            "pooling": [
                [(e["status"], e["code"], e["campaigns_compared"]) for e in cell.get("estimates", [])]
                for cell in step["pooling"]["cells"]
            ],
            "harm": {k: v["interim"] for k, v in step["harm_alarm"]["readings"].items()},
        }

    assert numbers(readout) == numbers(committed)
