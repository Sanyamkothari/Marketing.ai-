"""The generated observability files: they do not drift, and they cannot outlive their metrics.

`tests/unit/test_generated_files.py` already runs `--check` for the templates and the API docs;
this file does the same for the dashboard and the alarms and then goes further, because a dashboard
is a generated file with a second failure mode the templates do not have: it can be *up to date*
and still address a metric nothing emits, or miss a metric something does. So the assertions here
are about the relationship between `infra/observability/*.json` and `engine.aws.metrics`, not only
about whether the bytes match.

The thresholds get their own test. Every alarm in this repository was chosen before the product
served a single request, and two of the three thresholds are deliberately not numbers at all -
they are substitutions, because "how long should a stage take" and "how much should a day cost" are
a measurement and a business decision that nobody here has made (plan section 13.3). A test asserts
that every alarm says which of the two kinds its threshold is, and says out loud that it is policy.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import pytest

from engine.aws.metrics import COARSE_DIMENSIONS, METRICS, NAMESPACE
from scripts import gen_dashboard

if TYPE_CHECKING:
    from pathlib import Path

PLACEHOLDER = re.compile(r"\$\{[A-Za-z0-9_]+\}")


def dashboard(repo_root: Path) -> dict[str, Any]:
    path = repo_root / gen_dashboard.DEFAULT_OUT / gen_dashboard.DASHBOARD_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def alarms(repo_root: Path) -> dict[str, Any]:
    path = repo_root / gen_dashboard.DEFAULT_OUT / gen_dashboard.ALARMS_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def metric_widgets(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [widget for widget in body["widgets"] if widget["type"] == "metric"]


# ---------------------------------------------------------------------------
# Drift, the same way every other generated file is checked
# ---------------------------------------------------------------------------
def test_the_committed_files_are_up_to_date(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo_root)
    assert gen_dashboard.main(["--check"]) == 0


def test_generating_into_an_empty_directory_reproduces_the_committed_bytes(
    repo_root: Path, tmp_path: Path
) -> None:
    assert gen_dashboard.main(["--out", str(tmp_path)]) == 0

    for name in (gen_dashboard.DASHBOARD_FILENAME, gen_dashboard.ALARMS_FILENAME):
        generated = (tmp_path / name).read_text(encoding="utf-8")
        committed = (repo_root / gen_dashboard.DEFAULT_OUT / name).read_text(encoding="utf-8")
        assert generated == committed


def test_check_reports_drift_and_shows_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert gen_dashboard.main(["--out", str(tmp_path)]) == 0
    target = tmp_path / gen_dashboard.DASHBOARD_FILENAME
    target.write_text(target.read_text(encoding="utf-8").replace("RunsFailed", "RunsBroken"), "utf-8")

    assert gen_dashboard.main(["--out", str(tmp_path), "--check"]) == 1

    printed = capsys.readouterr().out
    assert "RunsBroken" in printed and "RunsFailed" in printed, "the diff must show what differs"


def test_check_reports_a_missing_file_rather_than_passing(tmp_path: Path) -> None:
    assert gen_dashboard.main(["--out", str(tmp_path), "--check"]) == 1


# ---------------------------------------------------------------------------
# The dashboard cannot outlive the metric registry
# ---------------------------------------------------------------------------
def test_every_metric_has_exactly_one_widget(repo_root: Path) -> None:
    titled = [widget["properties"]["title"] for widget in metric_widgets(dashboard(repo_root))]
    named = [title.split(" ")[0] for title in titled]
    assert named == [
        spec.name for spec in METRICS
    ], "a metric added to engine.aws.metrics without running `make generate` would show up here"


def test_every_widget_addresses_this_product_s_namespace_and_the_coarse_dimensions(
    repo_root: Path,
) -> None:
    for widget in metric_widgets(dashboard(repo_root)):
        (reference,) = widget["properties"]["metrics"]
        namespace, name, *pairs = reference
        assert namespace == NAMESPACE
        assert name in {spec.name for spec in METRICS}
        assert tuple(pairs[0::2]) == COARSE_DIMENSIONS, (
            "the coarse set is the one every datapoint is published under; a widget on the detail "
            "set would have to name every use case, stage and backend by hand"
        )


def test_the_dashboard_body_carries_nothing_put_dashboard_would_reject(repo_root: Path) -> None:
    """`PutDashboard` validates the document, so provenance goes in a text widget, not a new key."""
    body = dashboard(repo_root)
    assert set(body) == {"widgets"}
    provenance = [widget for widget in body["widgets"] if widget["type"] == "text"]
    assert len(provenance) == 1
    assert gen_dashboard.COMMAND in provenance[0]["properties"]["markdown"]


def test_no_two_widgets_overlap(repo_root: Path) -> None:
    occupied: set[tuple[int, int]] = set()
    for widget in dashboard(repo_root)["widgets"]:
        cells = {
            (widget["x"] + column, widget["y"] + row)
            for column in range(widget["width"])
            for row in range(widget["height"])
        }
        assert not cells & occupied, f"{widget.get('properties', {}).get('title')} sits on another widget"
        assert widget["x"] + widget["width"] <= 24, "a CloudWatch dashboard is 24 columns wide"
        occupied |= cells


# ---------------------------------------------------------------------------
# The alarms, and the fact that their thresholds are policy
# ---------------------------------------------------------------------------
def test_every_alarm_names_a_metric_that_exists_and_is_alarmable(repo_root: Path) -> None:
    alarmable = {spec.name for spec in METRICS if spec.alarmable}
    reserved = {spec.name for spec in METRICS if not spec.alarmable}
    named = {alarm["metric_name"] for alarm in alarms(repo_root)["alarms"]}

    assert named <= alarmable
    assert not named & reserved, (
        "an alarm on a metric nothing emits is either permanently INSUFFICIENT_DATA or, with the "
        "wrong missing-data treatment, a permanent false alarm"
    )


def test_the_generator_refuses_an_alarm_on_a_metric_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invented() -> list[dict[str, Any]]:
        return [{"metric_name": "RunsImagined", "name": "x"}]

    monkeypatch.setattr(gen_dashboard, "alarm_definitions", invented)

    with pytest.raises(ValueError, match="RunsImagined"):
        gen_dashboard.render_alarms()


def test_every_alarm_says_its_thresholds_are_policy_and_not_a_measurement(repo_root: Path) -> None:
    for alarm in alarms(repo_root)["alarms"]:
        rationale = alarm["rationale"]
        assert alarm["threshold_source"] in {"literal", "substitution"}
        assert (
            "POLICY" in rationale or "measurement" in rationale
        ), f"{alarm['name']} must say beside its threshold that nobody has measured it"
        assert len(rationale) > 120, f"{alarm['name']}'s rationale is too short to be a reason"


def test_a_threshold_nobody_has_measured_is_a_substitution_rather_than_a_number(
    repo_root: Path,
) -> None:
    by_metric = {alarm["metric_name"]: alarm for alarm in alarms(repo_root)["alarms"]}

    assert by_metric["RunsFailed"]["threshold"] == 1, "one failure is an event, and countable"
    for metric_name in ("StageDurationSeconds", "JobCostUsd"):
        alarm = by_metric[metric_name]
        assert alarm["threshold_source"] == "substitution"
        assert PLACEHOLDER.fullmatch(str(alarm["threshold"])), (
            f"{metric_name}'s threshold must stay a substitution: committing a number for it would "
            "be inventing a measurement this repository has never taken (plan section 13.3)"
        )


def test_every_placeholder_used_anywhere_is_declared(repo_root: Path) -> None:
    """`Fn.sub` fails on a token it was given no value for, so the declaration has to be complete."""
    declared = set(alarms(repo_root)["placeholders"])
    path = repo_root / gen_dashboard.DEFAULT_OUT
    used: set[str] = set()
    for name in (gen_dashboard.DASHBOARD_FILENAME, gen_dashboard.ALARMS_FILENAME):
        used |= set(PLACEHOLDER.findall((path / name).read_text(encoding="utf-8")))

    assert used - declared == set(), f"undeclared placeholder(s): {sorted(used - declared)}"
    assert declared - used == set(), f"declared but unused: {sorted(declared - used)}"


def test_every_declared_placeholder_says_what_a_deployment_must_put_in_it(repo_root: Path) -> None:
    for token, explanation in alarms(repo_root)["placeholders"].items():
        assert explanation.strip(), f"{token} is declared with no explanation"


def test_the_alarms_document_carries_its_own_provenance(repo_root: Path) -> None:
    body = alarms(repo_root)
    assert body["generated_by"] == gen_dashboard.COMMAND
    assert body["namespace"] == NAMESPACE
