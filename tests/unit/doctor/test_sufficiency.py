"""Enough data (Plan L M158, DEC-1580 … DEC-1584): what is short, what to do now, what to collect.

Every threshold below is read from the shipped configs (`configs/engine.yaml`'s `validation.min_rows` and
`validation.min_positive`, the use case's control group, `configs/pilot/readiness.yaml`), and every count
is the file's own, counted here independently of the code under test.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from engine.config import load_use_case
from engine.doctor.contracts import UseCaseReadiness
from engine.doctor.diagnose import LoadedFile, Offer, readiness
from engine.doctor.sufficiency import NextSteps, enough_data, with_alternatives
from engine.holdout.spec import effective_holdout_fraction
from engine.measurement.planner import arm_sizes, mde_two_proportions
from engine.pilot.data_request import build_data_request
from engine.pilot.plain import jargon_in
from engine.pilot.readiness import campaign_aim, load_readiness_settings
from engine.stages.ingest import profile_dataset
from tests.fixtures.make_data import GenerationSpec, generate

AD = "targeted-advertisement"
ORDERS = "order-fulfillment"


def frame_for(use_case: str, rows: int) -> pd.DataFrame:
    return generate(GenerationSpec(use_case_id=use_case, rows=rows, variant="clean"))


def positives(frame: pd.DataFrame, column: str) -> int:
    return int((frame[column].astype(str) == "1").sum())


def sentences(steps: NextSteps) -> list[str]:
    return [
        *(s.sentence for s in steps.shortfalls),
        *(o.title for o in steps.options),
        *(o.detail for o in steps.options),
        *steps.collect,
    ]


def test_too_few_outcomes_is_measured_against_the_shipped_minimum(config_root: Path) -> None:
    config = load_use_case(AD, config_root)
    frame = frame_for(AD, 1500)
    found = positives(frame, "converted_30d")
    assert found < config.validation.min_positive, "the fixture must be short of the shipped minimum"

    steps = enough_data(
        frame, outcome="converted_30d", config=config, root=config_root, file_name="history.csv"
    )

    (short,) = steps.shortfalls  # 1,500 rows reach min_rows (1,000): only the outcomes are short
    assert short.code == "TARGET_TOO_FEW_POSITIVES"
    assert (short.measured, short.needed) == (found, config.validation.min_positive)
    assert config.validation.min_positive == 200, "engine.yaml's shipped minimum"
    assert short.short_by == config.validation.min_positive - found
    assert short.setting == "validation.min_positive"
    assert f"{found:,} customers with the outcome" in short.sentence
    assert f"at least {config.validation.min_positive:,}" in short.sentence
    assert steps.data_request_use_case == AD


def test_a_tiny_file_is_short_of_rows_and_outcomes(config_root: Path) -> None:
    config = load_use_case(AD, config_root)
    frame = frame_for(AD, 60)
    steps = enough_data(frame, outcome="converted_30d", config=config, root=config_root, file_name="tiny.csv")
    by_code = {s.code: s for s in steps.shortfalls}
    assert set(by_code) == {"ROWS_TOO_FEW", "TARGET_TOO_FEW_POSITIVES"}
    rows = by_code["ROWS_TOO_FEW"]
    assert (rows.measured, rows.needed, rows.short_by) == (
        60,
        config.validation.min_rows,
        config.validation.min_rows - 60,
    )
    assert config.validation.min_rows == 1000
    assert f"At least {config.validation.min_rows - 60:,} more customers" in steps.collect[0]


def test_the_next_campaign_is_sized_by_the_test_planner(config_root: Path) -> None:
    config = load_use_case(AD, config_root)
    frame = frame_for(AD, 1500)
    found = positives(frame, "converted_30d")
    steps = enough_data(
        frame, outcome="converted_30d", config=config, root=config_root, file_name="history.csv"
    )

    (option,) = [o for o in steps.options if o.kind == "collect_with_control_group"]
    share = effective_holdout_fraction(config.actions)
    contacted, kept = arm_sizes(len(frame), share)
    settings = load_readiness_settings(config_root)
    mde = mde_two_proportions(
        contacted,
        kept,
        found / len(frame),
        settings.alpha,
        settings.power,
        direction=campaign_aim(AD, config_root),
    )
    assert config.actions.contacts_customers
    assert (option.contacted, option.control_group) == (contacted, kept)
    assert mde.points is not None and option.smallest_change_points == round(mde.points, 2)
    assert f"{kept:,} ({share:.0%}" in option.detail
    assert f"{mde.points:.1f} points" in option.detail
    assert "Audit a campaign" in option.detail


def test_a_use_case_that_contacts_nobody_is_not_offered_a_campaign(config_root: Path) -> None:
    config = load_use_case(ORDERS, config_root)
    assert not config.actions.contacts_customers
    frame = frame_for(ORDERS, 300)
    steps = enough_data(
        frame, outcome="delayed_beyond_sla", config=config, root=config_root, file_name="orders.csv"
    )
    assert steps.shortfalls, "300 orders are short of the shipped minimums"
    assert not any(o.kind == "collect_with_control_group" for o in steps.options)


def test_what_to_collect_names_the_rate_the_source_and_the_data_request(config_root: Path) -> None:
    config = load_use_case(AD, config_root)
    frame = frame_for(AD, 1500)
    found = positives(frame, "converted_30d")
    steps = enough_data(
        frame, outcome="converted_30d", config=config, root=config_root, file_name="history.csv"
    )

    rate = found / len(frame)
    total = math.ceil(config.validation.min_positive / rate - 1e-9)
    assert any(
        f"about {total:,} customers in all, {total - len(frame):,} more" in line for line in steps.collect
    )
    assert f"The outcome usually comes from: {config.target.label_source}." in steps.collect
    request = build_data_request((AD,), config_root)
    required = [t for t in request.tables if t.need == "required"]
    (line,) = [line for line in steps.collect if line.startswith("The data request")]
    assert all(f"{t.title} ({t.file})" in line for t in required)
    assert f"at least {math.ceil(request.history_min_days / 30)} months" in line


def test_counting_the_first_rows_of_a_longer_file_says_so(config_root: Path) -> None:
    config = load_use_case(AD, config_root)
    frame = frame_for(AD, 1500)
    steps = enough_data(
        frame,
        outcome="converted_30d",
        config=config,
        root=config_root,
        file_name="big.csv",
        file_rows=250_000,
    )
    (short,) = steps.shortfalls
    assert (short.rows_counted, short.file_rows) == (1500, 250_000)
    assert "Counted on the first 1,500 of its 250,000 rows." in short.sentence


def test_a_file_with_enough_has_no_shortfall(config_root: Path) -> None:
    config = load_use_case(AD, config_root)
    frame = frame_for(AD, 3000)
    assert positives(frame, "converted_30d") >= config.validation.min_positive
    steps = enough_data(frame, outcome="converted_30d", config=config, root=config_root, file_name="good.csv")
    assert steps.shortfalls == ()
    assert not any("more customers" in line for line in steps.collect)


@pytest.mark.parametrize("rows", [60, 1500])
def test_every_sentence_is_plain(config_root: Path, rows: int) -> None:
    config = load_use_case(AD, config_root)
    steps = enough_data(
        frame_for(AD, rows), outcome="converted_30d", config=config, root=config_root, file_name="f.csv"
    )
    for text in sentences(steps):
        assert jargon_in(text) == (), text


def verdict(use_case: str, status: str, upload_id: str | None) -> UseCaseReadiness:
    return UseCaseReadiness(
        use_case_id=use_case,
        name=use_case.title(),
        stage="",
        status=status,  # type: ignore[arg-type]
        upload_id=upload_id,
        summary=f"{use_case} summary.",
    )


def test_a_use_case_the_files_power_is_offered_instead() -> None:
    ready, small, missing = (
        verdict("a", "ready", "u_1"),
        verdict("b", "too_small", "u_2"),
        verdict("c", "missing", None),
    )
    out = {v.use_case_id: v for v in with_alternatives([ready, small, missing])}
    assert out["a"].next_steps is None, "a ready use case needs no next steps"
    for use_case in ("b", "c"):
        steps = out[use_case].next_steps
        assert steps is not None
        (option,) = steps.options
        assert (option.kind, option.use_case_id, option.upload_id) == ("other_use_case", "a", "u_1")
        assert option.title == "Start A instead"


def test_readiness_carries_next_steps_end_to_end(config_root: Path) -> None:
    def loaded(frame: pd.DataFrame, use_case: str, name: str, upload_id: str) -> LoadedFile:
        profile = profile_dataset(
            frame,
            load_use_case(use_case, config_root),
            upload_id=upload_id,
            file_name=name,
            file_format="csv",
            file_size_bytes=0,
            delimiter=",",
            encoding="utf-8",
            row_count=len(frame),
        )
        return LoadedFile(upload_id=upload_id, file_name=name, frame=frame, profile=profile)

    tiny = loaded(frame_for(AD, 60), AD, "tiny.csv", "u_tiny")
    bills = loaded(frame_for("payment-propensity", 3000), "payment-propensity", "bills.csv", "u_bills")
    offers = [
        Offer(config=load_use_case(AD, config_root), stage="Awareness"),
        Offer(config=load_use_case("payment-propensity", config_root), stage="Service"),
    ]
    by_id = {v.use_case_id: v for v in readiness([tiny, bills], offers, config_root)}
    assert by_id["payment-propensity"].status in {"ready", "check"}
    small = by_id[AD]
    assert small.status == "too_small" and small.next_steps is not None
    kinds = [o.kind for o in small.next_steps.options]
    assert kinds[0] == "other_use_case", "what the files power today comes first"
    assert small.next_steps.options[0].upload_id == "u_bills"
    assert "collect_with_control_group" in kinds
    assert {s.code for s in small.next_steps.shortfalls} == {"ROWS_TOO_FEW", "TARGET_TOO_FEW_POSITIVES"}
