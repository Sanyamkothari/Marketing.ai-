"""Regression tests for Claude Code's review of M98 (`docs/handoff/M98_REVIEW.md`).

One test or group per finding. Each fails on the partner's commit `3d8a71a` and passes on the fix:

1. reasons that state false things (the unit tests in `tests/unit/decide/test_treat_list.py`, and the
   treat list's own reasons here);
2. the holdout flag matched by position, a missing customer read as `True`, composite keys joined on the
   first column only;
3. the treat flag recomputed instead of read from M92's `treated` column;
4. expected gross value written as `net_value`;
5. today's use case file read instead of the run's `run_config.json`;
6. (hand-off evidence: `tests/unit/decide/test_m98_handoff.py`);
7. the default run's `scores.csv` byte-identical, the builder timed (`tests/unit/decide/test_treat_list_scale.py`);
and the should-fix items: a failed build answered with a plain reason, `reasons.yaml` found under the
configuration root, no swallowed exception, reasons joined by key.
"""

from __future__ import annotations

import io
import logging
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.config import ResolvedConfig, RunMode, load_use_case
from engine.contracts import RunRecord, RunState
from engine.decide.reasons import BusinessReasonDictionary, load_reasons_dictionary
from engine.decide.treat_list import (
    TREAT_LIST_CSV,
    TREAT_LIST_PARQUET,
    TreatListError,
    build_treat_list,
)
from engine.holdout.assign import (
    HOLDOUT_ASSIGNMENT_FILENAME,
    HOLDOUT_MEMBER_COLUMN,
    TREATED_COLUMN,
    selection_masks,
)
from engine.runs import RUN_CONFIG_FILENAME, RUN_FILENAME
from engine.stages import export
from engine.storage import LocalStorage, run_key, upload_key
from tests.fixtures.decide.treat_runs import USE_CASE, WrittenRun, write_run

pytestmark = pytest.mark.integration


def _csv(storage: LocalStorage, run_id: str) -> pd.DataFrame:
    text = storage.read_bytes(run_key(run_id, TREAT_LIST_CSV)).decode("utf-8")
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


def _flag(series: pd.Series) -> pd.Series:
    return series.map({True: "1", False: "0"})


# ---------------------------------------------------------------------------
# Finding 2: the holdout and explore flags are joined on every key column
# ---------------------------------------------------------------------------
def test_flags_follow_the_customer_when_the_assignment_is_in_another_order(tmp_path: Path) -> None:
    """Same number of rows, other order: matched by position, every customer got someone else's flag."""
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c000001", rows=60, holdout=True, shuffle_assignment=True)
    assert run.assignment is not None
    assert not run.assignment["customer_id"].equals(run.scores["customer_id"]), "the file really is shuffled"
    build_treat_list(storage, run.run_id)
    out = _csv(storage, run.run_id).set_index("customer_id")
    by_key = run.assignment.set_index("customer_id").reindex(out.index)
    assert (out["holdout"] == _flag(by_key[HOLDOUT_MEMBER_COLUMN])).all()
    assert (out["explore"] == _flag(by_key["explore"])).all()
    assert by_key[HOLDOUT_MEMBER_COLUMN].any() and not by_key[HOLDOUT_MEMBER_COLUMN].all()


def test_a_composite_key_run_is_joined_on_both_key_columns(tmp_path: Path) -> None:
    """Two snapshots per customer: joined on the customer alone, the second snapshot overwrote the first."""
    storage = LocalStorage(tmp_path)
    run = write_run(
        storage, "r_20261001_0c000002", rows=80, composite=True, holdout=True, shuffle_assignment=True
    )
    assert run.assignment is not None
    keys = ["customer_id", "snapshot_date"]
    assert not run.assignment.duplicated(keys).any() and run.assignment["customer_id"].duplicated().any()
    build_treat_list(storage, run.run_id)
    out = _csv(storage, run.run_id)
    assert list(out.columns[:2]) == keys
    assigned = run.assignment.rename(columns={"explore": "assigned_explore"})
    merged = out.merge(assigned, on=keys, how="left", validate="one_to_one")
    assert merged[HOLDOUT_MEMBER_COLUMN].notna().all(), "every (customer, snapshot) is in the file"
    assert (merged["holdout"] == _flag(merged[HOLDOUT_MEMBER_COLUMN])).all()
    assert (merged["treat"] == _flag(merged[TREATED_COLUMN])).all()
    assert (merged["explore"] == _flag(merged["assigned_explore"])).all()
    # Membership is per customer, so both snapshots of a customer agree; the snapshots still differ in treat.
    assert out.groupby("customer_id")["holdout"].nunique().max() == 1


def test_a_customer_the_assignment_does_not_cover_is_null_not_true(tmp_path: Path) -> None:
    """`NaN.astype(bool)` is True: a customer missing from the file used to be reported as held out."""
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c000003", rows=40, holdout=True, drop_from_assignment=3)
    assert run.assignment is not None and len(run.assignment) == 37
    summary = build_treat_list(storage, run.run_id)
    out = _csv(storage, run.run_id).set_index("customer_id")
    missing = sorted(set(run.scores["customer_id"]) - set(run.assignment["customer_id"]))
    assert len(missing) == 3
    assert (out.loc[missing, "holdout"] == "").all() and (out.loc[missing, "explore"] == "").all()
    covered = run.assignment.set_index("customer_id")
    assert (out.drop(index=missing)["holdout"] == _flag(covered[HOLDOUT_MEMBER_COLUMN])).all()
    assert summary.holdout_rows == int(covered[HOLDOUT_MEMBER_COLUMN].sum())
    assert summary.holdout_note is not None and "3 of 40" in summary.holdout_note
    frame = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run.run_id, TREAT_LIST_PARQUET))))
    assert int(frame["holdout"].isna().sum()) == 3
    # A customer whose holdout is unknown is still not treated when the scores put them in the control group.
    unknown = out.loc[missing]
    scores = run.scores.set_index("customer_id")
    assert not ((unknown["treat"] == "1") & scores.loc[missing, "control_group"].astype(bool)).any()


def test_an_assignment_that_repeats_a_key_is_not_guessed_at(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c000004", rows=30, holdout=True)
    assert run.assignment is not None
    twice = pd.concat([run.assignment, run.assignment.iloc[:1]])
    buffer = io.BytesIO()
    twice.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(run.run_id, HOLDOUT_ASSIGNMENT_FILENAME), buffer.getvalue())
    summary = build_treat_list(storage, run.run_id)
    assert (_csv(storage, run.run_id)["holdout"] == "").all()
    assert summary.holdout_rows is None and summary.holdout_note


def test_a_run_with_no_assignment_leaves_both_flag_columns_and_both_counts_blank(tmp_path: Path) -> None:
    """The file and its summary agree: unknown is null in both, and the note does not call the control group unknown."""
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c000018", rows=30)
    summary = build_treat_list(storage, run.run_id)
    out = _csv(storage, run.run_id)
    assert (out["holdout"] == "").all() and (out["explore"] == "").all()
    assert summary.holdout_rows is None and summary.explore_rows is None
    assert summary.holdout_note is not None
    assert "control_group" in summary.holdout_note and "never treated" in summary.holdout_note
    # A control customer reads as not treated.
    controls = run.scores.set_index("customer_id").loc[lambda f: f["control_group"].astype(bool)].index
    assert len(controls) > 0
    assert (out.set_index("customer_id").loc[controls, "treat"] == "0").all()


# ---------------------------------------------------------------------------
# Finding 3: the treat flag is M92's, read, and where M92 has none, M92's own function
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["propensity", "uplift"])
def test_treat_equals_the_assignments_treated_column_by_key(kind: str, tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    run = write_run(
        storage,
        "r_20261001_0c000005",
        kind=kind,  # type: ignore[arg-type]
        rows=90,
        holdout=True,
        shuffle_assignment=True,
        value=kind == "uplift",
    )
    assert run.assignment is not None
    build_treat_list(storage, run.run_id)
    out = _csv(storage, run.run_id).set_index("customer_id")
    assigned = run.assignment.set_index("customer_id").reindex(out.index)
    assert (out["treat"] == _flag(assigned[TREATED_COLUMN])).all()
    assert int((out["treat"] == "1").sum()) > 5, "the fixture treats someone"
    scores = run.scores.set_index("customer_id").reindex(out.index)
    treat = out["treat"] == "1"
    assert not (treat & scores["suppressed_reason"].notna()).any()
    assert not (treat & (out["holdout"] == "1")).any()
    assert not (treat & scores["control_group"].astype(bool)).any()
    if kind == "uplift":
        assert not (treat & (scores["segment"] == "sleeping_dog")).any()
    assert (treat & (out["explore"] == "1")).any(), "an explored customer is treated (M92)"


def test_without_a_treated_column_the_selection_is_m92s_own_function(tmp_path: Path) -> None:
    """An assignment from before the `treated` column: the same flags, derived with `selection_masks`."""
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c000006", kind="uplift", rows=70, holdout=True)
    assert run.assignment is not None
    build_treat_list(storage, run.run_id)
    with_column = _csv(storage, run.run_id)["treat"].copy()

    older = run.assignment.drop(columns=[TREATED_COLUMN, "treatment_probability"])
    buffer = io.BytesIO()
    older.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(run.run_id, HOLDOUT_ASSIGNMENT_FILENAME), buffer.getvalue())
    storage.delete(run_key(run.run_id, "treat_list_summary.json"))
    build_treat_list(storage, run.run_id)
    assert (_csv(storage, run.run_id)["treat"] == with_column).all()

    selected, sleeping = selection_masks(run.scores, run.config)
    assert not (selected & sleeping).any(), "the function the builder reuses never selects a sleeping dog"


def test_selection_masks_agree_with_the_assignment_for_both_run_kinds(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    for kind, run_id in (("propensity", "r_20261001_0c000007"), ("uplift", "r_20261001_0c000008")):
        run = write_run(storage, run_id, kind=kind, rows=50, holdout=True)  # type: ignore[arg-type]
        assert run.assignment is not None
        selected, sleeping = selection_masks(run.scores, run.config)
        eligible = run.scores["suppressed_reason"].isna().to_numpy()
        control = run.scores["control_group"].to_numpy(dtype=bool)
        explore = run.assignment["explore"].to_numpy(dtype=bool)
        derived = (eligible & selected & ~control) | explore
        assert (derived == run.assignment[TREATED_COLUMN].to_numpy(dtype=bool)).all()
        assert (sleeping.any()) == (kind == "uplift")


# ---------------------------------------------------------------------------
# Finding 4: gross value has its own column; net value stays null on a propensity run
# ---------------------------------------------------------------------------
def test_a_propensity_run_never_puts_gross_value_in_net_value(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c000009", rows=50, value=True)
    summary = build_treat_list(storage, run.run_id)
    out = _csv(storage, run.run_id)
    assert (out["net_value"] == "").all()
    assert (out["expected_gross_value"] != "").sum() > 20
    assert summary.net_value_total is None and summary.net_value_unit is None
    assert summary.expected_gross_value_total is not None
    assert (
        summary.expected_gross_value_note is not None
        and "not incremental" in summary.expected_gross_value_note
    )


def test_gross_value_that_cannot_be_worked_out_is_null_with_a_reason_and_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The partner's `except Exception: pass` hid every failure; now the reason is in the summary and the log."""
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c00000a", rows=30, value=True)
    storage.delete(upload_key(f"u_{run.run_id[-8:]}", "source.csv"))
    with caplog.at_level(logging.WARNING, logger="engine.decide.treat_list"):
        summary = build_treat_list(storage, run.run_id)
    assert (_csv(storage, run.run_id)["expected_gross_value"] == "").all()
    assert summary.expected_gross_value_total is None
    assert (
        summary.expected_gross_value_note and "could not be worked out" in summary.expected_gross_value_note
    )
    assert any("expected gross value not worked out" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# Finding 5: the run's own settings
# ---------------------------------------------------------------------------
def test_the_run_config_decides_which_band_is_the_floor_not_todays_use_case(tmp_path: Path) -> None:
    """A run scored with a lowest band called 'Rest' keeps treating nobody there, whatever the file says today."""
    storage = LocalStorage(tmp_path)
    document = load_use_case(USE_CASE).model_dump(mode="json")
    document["actions"]["bands"][-1]["name"] = "Rest"
    custom = ResolvedConfig(
        schema_version=1,
        use_case_id=USE_CASE,
        resolved_at=pd.Timestamp("2026-10-01", tz="UTC").to_pydatetime(),
        config=type(load_use_case(USE_CASE)).model_validate(document),
        overrides_applied={},
        sources={},
    )
    run = write_run(storage, "r_20261001_0c00000b", rows=40, config=custom)
    assert (run.scores["band"] == "Rest").any() and load_use_case(USE_CASE).actions.bands[-1].name == "Low"
    build_treat_list(storage, run.run_id)
    out = _csv(storage, run.run_id)
    assert not ((out["band"] == "Rest") & (out["treat"] == "1")).any()
    assert ((out["band"] != "Rest") & (out["suppression_reason"] == "") & (out["treat"] == "1")).any()


def test_a_run_without_saved_settings_says_so(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c00000c", rows=20)
    storage.delete(run_key(run.run_id, RUN_CONFIG_FILENAME))
    with pytest.raises(TreatListError) as caught:
        build_treat_list(storage, run.run_id)
    assert caught.value.code == "RUN_NOT_SCORED" and "run_config.json" in caught.value.message


# ---------------------------------------------------------------------------
# Reasons: configuration root, join by key
# ---------------------------------------------------------------------------
def test_reasons_are_read_from_the_config_root_given(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    run = write_run(storage, "r_20261001_0c00000d", rows=30)
    root = tmp_path / "configs"
    (root / "decide").mkdir(parents=True)
    (root / "decide" / "reasons.yaml").write_text(
        "schema_version: 2\nfeatures:\n  months_since_churn:\n"
        '    up: "Away for {value} months raises the score"\n'
        '    down: "Away for {value} months lowers the score"\n'
        '    none: "Away for {value} months matters overall"\n',
        encoding="utf-8",
    )
    build_treat_list(storage, run.run_id, config_root=root)
    custom = _csv(storage, run.run_id)
    reasons = pd.concat([custom["reason_1"], custom["reason_2"], custom["reason_3"]])
    assert reasons.str.startswith("Away for ").any()
    assert not reasons.str.contains(
        "Monthly spend of"
    ).any(), "the shipped file is not read when a root is given"


def _expected_reasons(run: WrittenRun) -> dict[str, list[str | None]]:
    """Each customer's own reasons in business words, computed per customer from the explanations."""
    dictionary = load_reasons_dictionary()
    out: dict[str, list[str | None]] = {}
    for explanation in run.explanations:
        phrases: list[str | None] = [
            dictionary.render(r.feature, r.direction.value, r.value, r.text) for r in explanation.reasons[:3]
        ]
        out[explanation.primary_key] = phrases + [None] * (3 - len(phrases))
    return out


def test_reasons_are_the_right_customers_when_the_explanations_are_in_another_order(tmp_path: Path) -> None:
    """Same row count, other order: the partner's builder took reasons by position."""
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c00000e", rows=48, shuffle_explanations=True)
    build_treat_list(storage, run.run_id)
    out = _csv(storage, run.run_id).set_index("customer_id")
    expected = _expected_reasons(run)
    for customer, phrases in expected.items():
        got = [out.loc[customer, f"reason_{slot}"] or None for slot in (1, 2, 3)]
        assert got == phrases, customer


def test_a_customer_without_explanations_keeps_the_text_the_scores_already_hold(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c00000f", rows=24, drop_explanations=4)
    build_treat_list(storage, run.run_id)
    out = _csv(storage, run.run_id).set_index("customer_id")
    scores = run.scores.set_index("customer_id")
    last = scores.index[-4:]
    for slot in (1, 2, 3):
        column = f"reason_{slot}"
        assert (out.loc[last, column] == scores.loc[last, column].fillna("")).all()


def test_no_explanations_at_all_keeps_todays_reason_text(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c000010", rows=20, explanations=False)
    build_treat_list(storage, run.run_id)
    out = _csv(storage, run.run_id)
    assert (out["reason_1"] == "").all() and (out["reason_3"] == "").all(), "nothing invented"


def test_fewer_than_three_reasons_leave_null_not_empty_text_or_repeats(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c000011", rows=40)
    build_treat_list(storage, run.run_id)
    frame = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run.run_id, TREAT_LIST_PARQUET))))
    counts = {e.primary_key: len(e.reasons) for e in run.explanations}
    for customer, count in counts.items():
        row = frame[frame["customer_id"] == customer].iloc[0]
        filled = [row[f"reason_{slot}"] for slot in (1, 2, 3)]
        assert sum(value is not None for value in filled) == min(count, 3)
        assert all(value is None or value.strip() for value in filled)
        assert len({v for v in filled if v is not None}) == sum(v is not None for v in filled)


# ---------------------------------------------------------------------------
# Should fix: a plain reason when a treat list cannot be built
# ---------------------------------------------------------------------------
def _client(tmp_path: Path, config_root: Path) -> TestClient:
    return TestClient(create_app(config_root=config_root, data_dir=tmp_path))


def test_the_route_answers_a_plain_reason_when_the_build_fails(tmp_path: Path, config_root: Path) -> None:
    """Swallowed at debug level, the user saw 'There is no artefact called treat_list.csv'."""
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c000012", rows=20)
    storage.delete(run_key(run.run_id, RUN_CONFIG_FILENAME))
    with _client(tmp_path, config_root) as client:
        for path in (
            f"/runs/{run.run_id}/treat_list.csv",
            f"/runs/{run.run_id}/artefacts/treat_list_summary.json",
        ):
            response = client.get(path)
            assert response.status_code == 409, response.text
            detail = response.json()["detail"]
            assert detail["code"] == "RUN_NOT_SCORED"
            assert "run_config.json" in detail["message"] and "no artefact" not in detail["message"]


def test_a_download_after_retention_removed_the_rows_answers_a_plain_reason(
    tmp_path: Path, config_root: Path
) -> None:
    """Retention deletes `treat_list.*` and `scores.*` but keeps the summary: the card must not offer a 404."""
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c000016", rows=20)
    with _client(tmp_path, config_root) as client:
        assert client.get(f"/runs/{run.run_id}/artefacts/treat_list_summary.json").status_code == 200
        for name in (TREAT_LIST_CSV, TREAT_LIST_PARQUET, export.SCORES_PARQUET, export.SCORES_CSV):
            storage.delete(run_key(run.run_id, name))
        for path in (
            f"/runs/{run.run_id}/treat_list.csv",
            f"/runs/{run.run_id}/artefacts/treat_list.parquet",
        ):
            response = client.get(path)
            assert response.status_code == 409, response.text
            detail = response.json()["detail"]
            assert detail["code"] == "RUN_NOT_SCORED" and "no scores file" in detail["message"]
        # The summary is still served as it was.
        assert client.get(f"/runs/{run.run_id}/artefacts/treat_list_summary.json").status_code == 200


def test_a_removed_treat_list_is_rebuilt_while_the_scores_are_still_there(
    tmp_path: Path, config_root: Path
) -> None:
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c000017", rows=20)
    with _client(tmp_path, config_root) as client:
        first = client.get(f"/runs/{run.run_id}/treat_list.csv")
        storage.delete(run_key(run.run_id, TREAT_LIST_CSV))
        again = client.get(f"/runs/{run.run_id}/treat_list.csv")
    assert first.status_code == 200 and again.status_code == 200
    assert again.content == first.content


def test_the_route_says_when_the_run_is_not_finished_and_when_it_trained_a_model(
    tmp_path: Path, config_root: Path
) -> None:
    storage = LocalStorage(tmp_path)
    running = write_run(storage, "r_20261001_0c000013", rows=20)
    record = storage.read_model(run_key(running.run_id, RUN_FILENAME), RunRecord)
    storage.write_model(
        run_key(running.run_id, RUN_FILENAME), record.model_copy(update={"state": RunState.RUNNING})
    )
    trained = write_run(storage, "r_20261001_0c000014", rows=20)
    record = storage.read_model(run_key(trained.run_id, RUN_FILENAME), RunRecord)
    storage.write_model(
        run_key(trained.run_id, RUN_FILENAME), record.model_copy(update={"mode": RunMode.TRAIN})
    )
    with _client(tmp_path, config_root) as client:
        waiting = client.get(f"/runs/{running.run_id}/treat_list.csv")
        assert waiting.status_code == 409 and "running" in waiting.json()["detail"]["message"]
        other = client.get(f"/runs/{trained.run_id}/treat_list.csv")
        assert other.status_code == 409 and "trained a model" in other.json()["detail"]["message"]


def test_the_route_serves_the_builders_bytes_and_scores_csv_is_another_file(
    tmp_path: Path, config_root: Path
) -> None:
    """'Download contact list' is `scores.csv`; 'Download treat list' is `treat_list.csv`: different files."""
    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c000015", kind="uplift", rows=30, holdout=True)
    with _client(tmp_path, config_root) as client:
        treat = client.get(f"/runs/{run.run_id}/treat_list.csv")
        scores = client.get(f"/runs/{run.run_id}/scores.csv")
    assert treat.status_code == 200 and scores.status_code == 200
    treat_header = treat.text.splitlines()[0].split(",")
    scores_header = scores.text.splitlines()[0].split(",")
    assert {"treat", "holdout", "explore"} <= set(treat_header)
    assert not {"treat", "holdout", "explore"} & set(scores_header)
    assert {"action", "control_group"} <= set(scores_header) and "action" not in treat_header
    assert treat.content != scores.content


# ---------------------------------------------------------------------------
# Finding 7: a default run is byte-identical to main
# ---------------------------------------------------------------------------
def test_building_the_treat_list_leaves_the_runs_other_files_byte_identical(tmp_path: Path) -> None:
    """`scores.csv`, `scores.parquet` and `scores_csv_columns` are the same with the treat list code path run."""
    from engine.contracts import scores_csv_columns

    storage = LocalStorage(tmp_path)
    run = write_run(storage, "r_20261001_0c000016", rows=60)
    before = {key: storage.read_bytes(key) for key in storage.list_keys(f"runs/{run.run_id}/")}
    header_before = before[run_key(run.run_id, export.SCORES_CSV)].decode().splitlines()[0]
    assert tuple(header_before.split(",")) == scores_csv_columns(run.config, "customer_id")

    build_treat_list(storage, run.run_id)

    after = {key: storage.read_bytes(key) for key in storage.list_keys(f"runs/{run.run_id}/")}
    added = sorted(set(after) - set(before))
    assert [Path(key).name for key in added] == [
        "treat_list.csv",
        "treat_list.parquet",
        "treat_list_summary.json",
    ]
    assert {key: after[key] for key in before} == before, "no existing file changed by a byte"

    # And a fresh write of the same scores, on a path that never touched the treat list, is the same bytes.
    other = LocalStorage(tmp_path / "other")
    again = write_run(other, run.run_id, rows=60)
    assert (
        other.read_bytes(run_key(run.run_id, export.SCORES_CSV))
        == before[run_key(run.run_id, export.SCORES_CSV)]
    )
    assert again.scores.equals(run.scores)


def test_the_pipeline_and_the_stages_do_not_import_the_treat_list() -> None:
    """The builder is never imported by the pipeline's stages and adds no method rebind."""
    code = (
        "import sys, engine.pipeline, engine.stages.actions, engine.stages.export, engine.stages.explain;"
        "print('engine.decide.treat_list' in sys.modules, 'engine.decide.reasons' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout.split() == ["False", "False"], result.stdout + result.stderr
    pipeline = Path("engine/pipeline.py").read_text(encoding="utf-8")
    assert "treat_list" not in pipeline and "decide.reasons" not in pipeline


def test_the_builder_has_no_row_loops() -> None:
    """The review found per-row `.apply` and list comprehensions in the builder and the CSV writer."""
    import inspect

    from engine.decide import reasons, treat_list

    for module in (treat_list, reasons):
        source = inspect.getsource(module)
        for banned in (".apply(", "iterrows", "itertuples", ".iterrows"):
            assert banned not in source, f"{module.__name__} uses {banned}"
    assert "[None] * n_rows" not in inspect.getsource(treat_list)
    assert isinstance(load_reasons_dictionary(), BusinessReasonDictionary)
    assert np.__name__ == "numpy"
