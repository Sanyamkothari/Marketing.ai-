"""The readiness report's planning sections (Plan J M93), from artefacts written by hand.

"Can we measure it?" gives the smallest change a test is sure to see at 3, 5, 10 and 15% control
groups, from the customers at the latest prediction date and the dataset's own base rate, and says
where the base rate came from. "The outcome, checked" gives the definition in words, the share per
month, the count, the future-data check's verdict, and LABEL_RATE_UNSTABLE when the share jumps
beyond the configured tolerance. "Past campaigns" appears only when a treatment column is named.
None of them changes the verdict, and none uses a word `engine.pilot.plain` calls jargon.
"""

from __future__ import annotations

import io
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.clients import LocalClientStore
from engine.contracts import RunState, Severity, ValidationCheck
from engine.measurement.planner import arm_sizes, mde_two_proportions
from engine.onboarding.datasets import (
    DATASET_FRAME_FILENAME,
    DATASET_MANIFEST_FILENAME,
    DATASET_REPORT_FILENAME,
    DATASET_STATUS_FILENAME,
    dataset_fingerprint_of,
    dataset_key,
)
from engine.onboarding.specs import (
    BuildReport,
    BuildStatus,
    DatasetColumn,
    DatasetManifest,
    LabelSpec,
    LabelType,
    LeakCheckRecord,
    SnapshotMode,
    SnapshotStat,
    StandardType,
)
from engine.pilot.plain import jargon_in
from engine.pilot.readiness import (
    ReadinessFacts,
    ReadinessSettings,
    collect_readiness,
    label_in_words,
    label_rate_checks,
    load_readiness_settings,
    readiness_document,
)
from engine.storage import LocalStorage

NOW = datetime(2026, 10, 7, tzinfo=UTC)
LAPSE = LabelSpec(
    name="lapsed",
    type=LabelType.EVENT_ABSENCE,
    role="activity",
    horizon_days=30,
    grace_days=7,
    exclude_roles=("other_event",),
)


def stat(day: str, customers: int, positives: int | None, **extra: Any) -> SnapshotStat:
    return SnapshotStat(
        date=date.fromisoformat(day),
        entities=customers,
        positives=positives,
        positive_rate=None if positives is None else round(positives / customers, 4),
        **extra,
    )


STEADY = (
    stat("2024-01-31", 10_000, 400),
    stat("2024-02-29", 10_200, 410),
    stat("2024-03-31", 10_400, 420),
    stat("2024-04-30", 10_500, 0, censored=True),
)


def report(
    snapshots: tuple[SnapshotStat, ...] = STEADY, checks: tuple[ValidationCheck, ...] = ()
) -> BuildReport:
    return BuildReport(
        dataset_id="ds_1",
        checks=checks,
        sources=(),
        snapshots=snapshots,
        features=(),
        rows_out=sum(s.entities for s in snapshots),
        entities_out=10_400,
        duration_s=1.0,
        error_count=sum(1 for c in checks if c.severity is Severity.ERROR),
        warning_count=sum(1 for c in checks if c.severity is Severity.WARNING),
        passed=not any(c.severity is Severity.ERROR for c in checks),
        built_at=NOW,
        leak_check=LeakCheckRecord(
            scope="full",
            reason="first_build_of_recipe",
            recipe_hash="sha256:v1:r",
            rows_total=30_600,
            rows_probed=30_600,
            summary="Full future-data check.",
        ),
    )


def facts(build: BuildReport | None, label: LabelSpec | None = LAPSE, **extra: Any) -> ReadinessFacts:
    fields: dict[str, Any] = {
        "dataset_id": "ds_1",
        "client_name": "Demo Company",
        "use_case_id": "win-back-campaign",
        "use_case_name": "Win-back Campaign",
        "state": "done",
        "status_error": None,
        "report": build,
        "manifest": None,
        "sources": (),
        "history_needed_days": 127,
        "min_history_days": 90,
        "horizon_days": 37,
        "min_outcomes": 200,
        "outcome_description": "",
        "built_at": NOW,
        "label": label,
    }
    fields.update(extra)
    return ReadinessFacts(**fields)


def section(document_blocks: tuple[Any, ...], heading: str) -> list[Any]:
    """The blocks under `heading`, up to the next heading."""
    blocks = list(document_blocks)
    start = next(i for i, b in enumerate(blocks) if b.kind == "heading" and b.text == heading)
    end = next((i for i in range(start + 1, len(blocks)) if blocks[i].kind == "heading"), len(blocks))
    return blocks[start + 1 : end]


def texts(blocks: list[Any]) -> list[str]:
    """Every string a reader sees in `blocks`."""
    found: list[str] = []
    for block in blocks:
        dumped = block.model_dump()
        for key in ("text", "title", "caption", "empty_text"):
            if isinstance(dumped.get(key), str):
                found.append(dumped[key])
        for key in ("rows", "items", "columns"):
            for row in dumped.get(key) or ():
                found.extend(
                    cell
                    for cell in (row if isinstance(row, (list, tuple)) else (row,))
                    if isinstance(cell, str)
                )
    return found


# ---------------------------------------------------------------------------
# Can we measure it?
# ---------------------------------------------------------------------------
def test_can_we_measure_it_gives_the_planners_numbers_at_each_share() -> None:
    document = readiness_document(facts(report()), now=NOW)
    blocks = section(document.blocks, "Can we measure it?")
    (table,) = [b for b in blocks if b.kind == "table"]
    assert [row[0] for row in table.rows] == ["3%", "5%", "10%", "15%"]
    base_rate = (400 + 410 + 420) / (10_000 + 10_200 + 10_400)
    for row, share in zip(table.rows, (0.03, 0.05, 0.10, 0.15), strict=True):
        n_treat, n_control = arm_sizes(10_500, share)
        mde = mde_two_proportions(n_treat, n_control, base_rate)
        assert row[1:3] == (f"{n_control:,}", f"{n_treat:,}")
        assert row[3] == f"{mde.points:.1f} points ({mde.relative:.0%} of the base rate)"
    intro = " ".join(texts(blocks))
    assert "From this dataset: 1,230 of 30,600 customers had the outcome" in intro
    assert "10,500 customers at the latest prediction date" in intro


def test_an_unknown_base_rate_is_not_measured_with_its_reason() -> None:
    nothing_kept = (stat("2024-04-30", 10_500, 0, censored=True),)
    blocks = section(readiness_document(facts(report(nothing_kept)), now=NOW).blocks, "Can we measure it?")
    (table,) = [b for b in blocks if b.kind == "table"]
    assert {row[3] for row in table.rows} == {"not measured"}
    said = " ".join(texts(blocks))
    assert "Not measured: this dataset has no prediction date with a known outcome" in said
    assert "base rate is not known" in said


def test_the_planning_sections_never_change_the_verdict() -> None:
    jumpy = (stat("2024-01-31", 10_000, 400), stat("2024-02-29", 10_000, 900))
    document = readiness_document(facts(report(jumpy)), now=NOW)
    assert document.blocks[0].state == "ready"


# ---------------------------------------------------------------------------
# The outcome, checked
# ---------------------------------------------------------------------------
def test_the_label_section_says_the_definition_the_months_the_count_and_the_leak_verdict() -> None:
    blocks = section(readiness_document(facts(report()), now=NOW).blocks, "The outcome, checked")
    (pairs,) = [b for b in blocks if b.kind == "key_values"]
    rows = dict(pairs.rows)
    assert rows["Definition"] == (
        "A customer has the outcome when there is no activity record in the 30 days after the prediction "
        "date, plus 7 days' grace (37 days in all). A customer with any other events record in that time is "
        "left out altogether."
    )
    assert rows["Customers with the outcome"] == "1,230 of 30,600 across the 3 prediction date(s) used (4.0%)"
    assert rows["Future-data check"].startswith("Passed: 30,600 of 30,600 rows")
    (months,) = [b for b in blocks if b.kind == "table"]
    assert [row[0] for row in months.rows] == ["Jan 2024", "Feb 2024", "Mar 2024"]
    assert months.rows[0][1:] == ("10,000", "400", "4.0%")
    assert not [b for b in blocks if b.kind == "callout"], "a steady rate raised LABEL_RATE_UNSTABLE"


def test_a_leak_reads_as_failed() -> None:
    leaked = ValidationCheck(code="FUTURE_EVENTS_LEAKED", severity=Severity.ERROR, message="x")
    blocks = section(
        readiness_document(facts(report(checks=(leaked,))), now=NOW).blocks, "The outcome, checked"
    )
    (pairs,) = [b for b in blocks if b.kind == "key_values"]
    assert dict(pairs.rows)["Future-data check"].startswith("Failed:")


def test_a_jump_beyond_the_tolerance_is_label_rate_unstable() -> None:
    jumpy = (
        stat("2024-01-31", 10_000, 400),
        stat("2024-02-29", 10_000, 400),
        stat("2024-03-31", 10_000, 700),
    )
    (check,) = label_rate_checks(jumpy, tolerance=0.5, min_z=3.0)
    assert check.code == "LABEL_RATE_UNSTABLE" and check.severity is Severity.WARNING
    assert check.details["from_date"] == "2024-02-29" and check.details["to_date"] == "2024-03-31"
    blocks = section(readiness_document(facts(report(jumpy)), now=NOW).blocks, "The outcome, checked")
    (callout,) = [b for b in blocks if b.kind == "callout"]
    assert callout.tone == "warning" and "4.0% at 2024-02-29 to 7.0% at 2024-03-31" in callout.text


@pytest.mark.parametrize(
    ("after", "flagged"),
    [(590, False), (610, True)],  # 4.0% -> 5.9% is a 47.5% move; 4.0% -> 6.1% is 52.5%
)
def test_the_tolerance_is_where_it_switches(after: int, flagged: bool) -> None:
    pair = (stat("2024-01-31", 10_000, 400), stat("2024-02-29", 10_000, after))
    assert bool(label_rate_checks(pair, tolerance=0.5, min_z=3.0)) is flagged


def test_a_small_extracts_noise_is_not_called_unstable() -> None:
    small = (stat("2024-01-31", 100, 4), stat("2024-02-29", 100, 8))  # doubled, but 4 customers more
    assert label_rate_checks(small, tolerance=0.5, min_z=3.0) == ()


def test_the_tolerance_is_read_from_configs_pilot_readiness_yaml(config_root: Path, tmp_path: Path) -> None:
    assert load_readiness_settings(config_root) == ReadinessSettings()
    root = tmp_path / "configs"
    shutil.copytree(config_root, root)
    (root / "pilot" / "readiness.yaml").write_text(
        "schema_version: 1\ncontrol_group_shares: [0.2]\nlabel_rate_tolerance: 0.9\nlabel_rate_min_z: 3.0\n",
        encoding="utf-8",
    )
    jumpy = (stat("2024-01-31", 10_000, 400), stat("2024-02-29", 10_000, 700))  # a 75% move
    document = readiness_document(facts(report(jumpy)), root=root, now=NOW)
    assert not [b for b in section(document.blocks, "The outcome, checked") if b.kind == "callout"]
    (table,) = [b for b in section(document.blocks, "Can we measure it?") if b.kind == "table"]
    assert [row[0] for row in table.rows] == ["20%"]
    (root / "pilot" / "readiness.yaml").unlink()
    assert load_readiness_settings(root) == ReadinessSettings()


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (
            LabelSpec(name="churned", type=LabelType.EVENT_ABSENCE, role="activity", horizon_days=60),
            "A customer has the outcome when there is no activity record in the 60 days after the prediction date.",
        ),
        (
            LabelSpec(name="came_back", type=LabelType.EVENT_PRESENCE, role="orders", horizon_days=90),
            "A customer has the outcome when there is at least one orders record in the 90 days after the "
            "prediction date.",
        ),
        (
            LabelSpec(name="flag", type=LabelType.COLUMN, column="churn_flag"),
            "A customer has the outcome when the column 'churn_flag' of the customer table says so.",
        ),
    ],
)
def test_the_definition_in_words(label: LabelSpec, expected: str) -> None:
    assert label_in_words(label) == expected


# ---------------------------------------------------------------------------
# Past campaigns: through collect_readiness, on a dataset written to storage
# ---------------------------------------------------------------------------
def _write_dataset(tmp_path: Path, frame: pd.DataFrame) -> LocalStorage:
    storage = LocalStorage(tmp_path)
    storage.write_model(
        dataset_key("ds_1", DATASET_STATUS_FILENAME),
        BuildStatus(
            dataset_id="ds_1",
            client_id="c_1",
            spec_id="spec_1",
            state=RunState.DONE,
            updated_at=NOW,
            stages=(),
            progress_pct=100,
        ),
    )
    storage.write_model(
        dataset_key("ds_1", DATASET_REPORT_FILENAME),
        report((stat("2024-01-31", len(frame), int(frame["reactivated_90d"].sum())),)),
    )
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    storage.write_bytes(dataset_key("ds_1", DATASET_FRAME_FILENAME), buffer.getvalue())
    columns = tuple(
        DatasetColumn(
            name=str(name),
            type=(
                StandardType.NUMERIC
                if pd.api.types.is_numeric_dtype(frame[name])
                else StandardType.CATEGORICAL
            ),
            origin="key" if name == "customer_id" else ("label" if name == "reactivated_90d" else "mapped"),
        )
        for name in frame.columns
    )
    storage.write_model(
        dataset_key("ds_1", DATASET_MANIFEST_FILENAME),
        DatasetManifest(
            dataset_id="ds_1",
            client_id="c_1",
            use_case="win-back-campaign",
            spec_id="spec_1",
            spec_hash="sha256:x",
            feature_spec_hash="sha256:y",
            snapshot_mode=SnapshotMode.SINGLE,
            primary_key=("customer_id",),
            target="reactivated_90d",
            columns=columns,
            n_rows=len(frame),
            n_entities=len(frame),
            snapshot_dates=(date(2024, 1, 31),),
            fingerprint=dataset_fingerprint_of(frame),
            built_at=NOW,
            engine_version="test",
        ),
    )
    return storage


@pytest.fixture(scope="module")
def campaign_frame() -> pd.DataFrame:
    from tests.fixtures.make_uplift_data import make_uplift_data

    frame = make_uplift_data(3000, seed=21).frame.copy()
    rng = np.random.default_rng(5)
    score = 0.3 * frame["visits_30d"] + 0.04 * frame["tenure_months"] + rng.normal(scale=0.3, size=len(frame))
    frame["chosen_by_model"] = (score >= score.quantile(0.6)).astype(int)
    return frame


def _past(tmp_path: Path, frame: pd.DataFrame, column: str | None) -> list[Any]:
    storage = _write_dataset(tmp_path, frame)
    found = collect_readiness(
        storage, LocalClientStore(tmp_path / "clients.db"), "ds_1", treatment_column=column
    )
    document = readiness_document(found, now=NOW)
    headings = [b.text for b in document.blocks if b.kind == "heading"]
    if "Past campaigns" not in headings:
        return []
    return section(document.blocks, "Past campaigns")


def test_no_treatment_column_means_no_past_campaigns_section(
    tmp_path: Path, campaign_frame: pd.DataFrame
) -> None:
    assert _past(tmp_path, campaign_frame, None) == []


def test_a_model_selected_history_is_shown_as_not_random(
    tmp_path: Path, campaign_frame: pd.DataFrame
) -> None:
    blocks = _past(tmp_path, campaign_frame, "chosen_by_model")
    (pairs,) = [b for b in blocks if b.kind == "key_values"]
    assert dict(pairs.rows)["How customers were chosen"] == "Not at random: by a model or a rule"
    callouts = [b for b in blocks if b.kind == "callout"]
    assert callouts[0].tone == "warning" and "was not random" in callouts[0].text
    assert callouts[-1].title == "Learn who each campaign changes: next cycle"


def test_a_randomised_history_is_shown_as_random(tmp_path: Path, campaign_frame: pd.DataFrame) -> None:
    blocks = _past(tmp_path, campaign_frame, "treatment")
    (pairs,) = [b for b in blocks if b.kind == "key_values"]
    assert dict(pairs.rows)["How customers were chosen"] == "At random"
    assert dict(pairs.rows)["Contacted"] == f"{int(campaign_frame['treatment'].sum()):,}"


def test_a_column_not_in_the_dataset_is_not_known(tmp_path: Path, campaign_frame: pd.DataFrame) -> None:
    blocks = _past(tmp_path, campaign_frame, "no_such_column")
    (pairs,) = [b for b in blocks if b.kind == "key_values"]
    assert dict(pairs.rows)["How customers were chosen"] == "Not known"


def test_a_dataset_that_was_not_built_says_so(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    storage.write_model(
        dataset_key("ds_1", DATASET_STATUS_FILENAME),
        BuildStatus(
            dataset_id="ds_1",
            client_id="c_1",
            spec_id="s",
            state=RunState.FAILED,
            updated_at=NOW,
            stages=(),
            progress_pct=0,
        ),
    )
    found = collect_readiness(
        storage, LocalClientStore(tmp_path / "clients.db"), "ds_1", treatment_column="treatment"
    )
    assert found.treatment is None and found.treatment_note and "not built" in found.treatment_note


# ---------------------------------------------------------------------------
# Plain language, everywhere the planning sections write
# ---------------------------------------------------------------------------
def test_jargon_in_finds_nothing_in_any_planning_message(
    tmp_path: Path, campaign_frame: pd.DataFrame
) -> None:
    jumpy = (stat("2024-01-31", 10_000, 400), stat("2024-02-29", 10_000, 900))
    nothing_kept = (stat("2024-04-30", 10_500, 0, censored=True),)
    leaked = ValidationCheck(code="FUTURE_EVENTS_LEAKED", severity=Severity.ERROR, message="x")
    said: list[str] = []
    for build in (report(), report(jumpy), report(nothing_kept), report(checks=(leaked,)), None):
        document = readiness_document(facts(build), now=NOW)
        for heading in ("The outcome, checked", "Can we measure it?"):
            if any(b.kind == "heading" and b.text == heading for b in document.blocks):
                said += [heading, *texts(section(document.blocks, heading))]
    for column in ("chosen_by_model", "treatment", "no_such_column", "plan"):
        said += ["Past campaigns", *texts(_past(tmp_path / column, campaign_frame, column))]
    said += [
        check.message + " " + check.suggestion for check in label_rate_checks(jumpy, tolerance=0.5, min_z=3.0)
    ]
    assert len(said) > 40
    assert [text for text in said if jargon_in(text)] == []
