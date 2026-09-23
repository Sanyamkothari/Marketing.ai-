"""The data request kit (Plan E M59): generated from the configs, committed, plain, and complete."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from engine.config import load_use_case
from engine.pii import DETECTORS
from engine.pilot.data_request import (
    build_data_request,
    load_wording,
    render_markdown,
    render_role_template,
    template_filename,
)
from engine.pilot.plain import jargon_in
from scripts.gen_data_request import rendered_files, write_kit


@pytest.fixture(scope="module")
def request_(config_root: Path):
    return build_data_request(None, config_root)


def test_the_committed_kit_is_what_the_configs_generate(repo_root: Path, config_root: Path) -> None:
    assert (
        write_kit(repo_root / "docs" / "pilot", root=config_root, check=True) == 0
    ), "docs/pilot is stale: run `make pilot-generate`"


def test_the_default_request_is_the_pilots_two_use_cases(request_) -> None:
    assert [line.id for line in request_.use_cases] == ["telco-churn", "win-back-campaign"]


def test_the_minimum_history_is_the_sum_the_build_refuses_below(config_root: Path) -> None:
    for use_case in ("telco-churn", "win-back-campaign"):
        config = load_use_case(use_case, config_root)
        request = build_data_request((use_case,), config_root)
        assert config.label is not None and config.label.horizon_days is not None
        expected = config.onboarding.snapshots.min_history_days + config.label.horizon_days
        assert request.use_cases[0].history_min_days == expected
        assert request.history_recommended_days > expected


def test_the_outcome_table_is_required_and_the_customer_table_comes_first(
    request_, config_root: Path
) -> None:
    assert request_.tables[0].role == "entity" and request_.tables[0].need == "required"
    needs = {table.role: table.need for table in request_.tables}
    for use_case in ("telco-churn", "win-back-campaign"):
        label = load_use_case(use_case, config_root).label
        assert label is not None and needs[label.role] == "required"


def test_a_table_a_suggested_feature_reads_is_asked_for(request_, config_root: Path) -> None:
    roles = {table.role for table in request_.tables}
    for use_case in ("telco-churn", "win-back-campaign"):
        for feature in load_use_case(use_case, config_root).suggested_features:
            assert feature.role in roles, feature.name


def test_a_column_a_suggested_feature_reads_is_needed(request_) -> None:
    bills = next(table for table in request_.tables if table.role == "bills")
    assert {c.name: c.need for c in bills.columns}["amount"] == "needed"


def test_the_campaign_table_carries_who_was_held_back(request_) -> None:
    campaign = next(table for table in request_.tables if table.role == "campaign_events")
    needs = {c.name: c.need for c in campaign.columns}
    assert campaign.need == "required"
    assert needs["treatment"] == "needed" and needs["campaign_id"] == "needed"


def test_every_kind_of_personal_detail_the_engine_detects_is_on_the_do_not_send_list(
    request_, config_root: Path
) -> None:
    wording = load_wording(config_root)
    for detector in DETECTORS:
        assert wording.do_not_send[detector.kind] in request_.do_not_send
    text = " ".join(request_.do_not_send).lower()
    for named in ("names", "phone", "e-mail", "aadhaar", "pan"):
        assert named in text


def test_the_default_request_uses_no_model_jargon(request_, config_root: Path) -> None:
    markdown = render_markdown(request_, load_wording(config_root))
    offenders = [line for line in markdown.splitlines() if jargon_in(line)]
    assert offenders == []


def test_the_request_explains_pseudonymisation_formats_and_the_preflight(request_, config_root: Path) -> None:
    markdown = render_markdown(request_, load_wording(config_root))
    for heading in (
        "## How much history",
        "## The tables",
        "## File formats",
        "## Pseudonymising",
        "## What not to send",
        "## Before you send",
    ):
        assert heading in markdown
    assert "HMAC-SHA256" in markdown and "scripts.preflight" in markdown


def test_a_template_is_a_header_row_with_the_customer_id_first(request_) -> None:
    for table in request_.tables:
        rows = list(csv.reader(io.StringIO(render_role_template(table))))
        assert len(rows) == 1, "a role template carries no invented example rows"
        assert rows[0][0] == "customer_id"
        assert rows[0] == [column.name for column in table.columns]


def test_every_requested_table_has_a_template_file(tmp_path: Path, config_root: Path, request_) -> None:
    names = {path.name for path, _ in rendered_files(tmp_path, root=config_root)}
    wording = load_wording(config_root)
    assert {template_filename(t.role, wording) for t in request_.tables} <= names


def test_a_single_use_case_request_is_narrower(config_root: Path) -> None:
    churn = build_data_request(("telco-churn",), config_root)
    assert "campaign_events" not in {table.role for table in churn.tables}
