"""The data request kit (Plan E M59): generated from the configs, committed, plain, and complete."""

from __future__ import annotations

import csv
import io
import re
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

TELECOM_PILOT: tuple[str, ...] = ("telco-churn", "win-back-campaign")
"""Plan E's first pilot, requested by name since Plan J M93 made the default neutral. The tests below
that read telco-churn's own tables and features run on this request, exactly as they ran on the old
default; the default itself is pinned by `test_the_default_request_is_neutral_not_telecom`."""


@pytest.fixture(scope="module")
def request_(config_root: Path):
    return build_data_request(TELECOM_PILOT, config_root)


@pytest.fixture(scope="module")
def default_request(config_root: Path):
    return build_data_request(None, config_root)


def test_the_committed_kit_is_what_the_configs_generate(repo_root: Path, config_root: Path) -> None:
    assert (
        write_kit(repo_root / "docs" / "pilot", root=config_root, check=True) == 0
    ), "docs/pilot is stale: run `make pilot-generate`"


def test_the_named_request_is_the_pilots_two_use_cases(request_) -> None:
    assert [line.id for line in request_.use_cases] == ["telco-churn", "win-back-campaign"]


def test_the_default_request_is_neutral_not_telecom(default_request, config_root: Path) -> None:
    """Plan J M93: the kit is sold to any B2C business, so the default assumes no industry."""
    assert [line.id for line in default_request.use_cases] == ["win-back-campaign"]
    assert not [line.id for line in default_request.use_cases if line.id.startswith("telco")]
    wording = load_wording(config_root)
    assert "telco" not in " ".join(wording.preflight).lower()
    assert "telecom" not in " ".join(wording.preflight).lower()


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


def test_the_default_request_uses_no_model_jargon(request_, default_request, config_root: Path) -> None:
    for request in (request_, default_request):
        markdown = render_markdown(request, load_wording(config_root))
        offenders = [line for line in markdown.splitlines() if jargon_in(line)]
        assert offenders == []


def test_the_request_explains_pseudonymisation_formats_and_the_preflight(request_, config_root: Path) -> None:
    markdown = render_markdown(request_, load_wording(config_root))
    for heading in (
        "## How much history",
        "## The tables",
        "## Send what you have",
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


def test_every_requested_table_has_a_template_file(
    tmp_path: Path, config_root: Path, request_, default_request
) -> None:
    wording = load_wording(config_root)
    for use_cases, request in ((None, default_request), (TELECOM_PILOT, request_)):
        names = {path.name for path, _ in rendered_files(tmp_path, use_cases=use_cases, root=config_root)}
        assert {template_filename(t.role, wording) for t in request.tables} <= names


def test_a_single_use_case_request_is_narrower(config_root: Path) -> None:
    churn = build_data_request(("telco-churn",), config_root)
    assert "campaign_events" not in {table.role for table in churn.tables}


# ---------------------------------------------------------------------------
# Send what you have (Plan L follow-up, DEC-1598): ask only for what Data Doctor cannot read past
# ---------------------------------------------------------------------------
def test_the_request_says_plainly_what_no_longer_matters(
    request_, default_request, config_root: Path
) -> None:
    wording = load_wording(config_root)
    for request in (request_, default_request):
        markdown = render_markdown(request, wording)
        assert "What you do not need to fix first:" in markdown
        assert "Excel is fine." in markdown and "Titles and totals are fine." in markdown
        assert request.not_needed == wording.not_needed and request.not_needed
        # The old asks are gone: no UTF-8-only CSV, no one fixed date format.
        assert "UTF-8" not in markdown
        assert "YYYY-MM-DD" not in markdown


def test_the_request_keeps_asking_for_what_still_matters(request_, config_root: Path) -> None:
    markdown = render_markdown(request_, load_wording(config_root))
    still = " ".join(request_.formats)
    assert "column names the same from one month's export to the next" in still
    assert "same customer in every file" in still
    assert "date on every row" in still
    assert "put the year in the column names" in still
    assert "## How much history" in markdown and "## What not to send" in markdown
    assert any("Notes and remarks" in note for note in request_.do_not_send_notes)


def test_what_no_longer_matters_is_what_intake_reads(config_root: Path) -> None:
    """Every shape the request calls fine is one `configs/intake.yaml` and the workbook reader handle."""
    from engine.intake.config import load_intake_config
    from engine.intake.excel import EXCEL_SUFFIXES, LEGACY_EXCEL_SUFFIXES

    intake = load_intake_config(config_root)
    not_needed = " ".join(load_wording(config_root).not_needed)
    for word in ("-", "unknown", "not known", "#DIV/0!"):
        assert word in not_needed and word.casefold() in intake.missing_words
    for word in ("Grand Total", "Sub-total"):
        assert word in not_needed and word.casefold() in intake.total_words
    for suffix in EXCEL_SUFFIXES:
        assert suffix in not_needed
    still = " ".join(load_wording(config_root).formats)
    for suffix in LEGACY_EXCEL_SUFFIXES:  # ".xls" is a prefix of ".xlsx": match the whole suffix
        whole = re.compile(re.escape(suffix) + r"(?![a-z])")
        assert whole.search(still) and not whole.search(not_needed)
    assert "password-protected workbooks cannot be read" in still
    assert "Scanned PDFs and photos of tables cannot be read" in still


def test_the_preflight_is_optional_and_says_it_reads_no_workbook(config_root: Path) -> None:
    from engine.pilot.preflight import READABLE_SUFFIXES

    preflight = load_wording(config_root).preflight
    assert preflight[0].startswith("Optional.")
    assert "CSV and Parquet files only, not Excel workbooks" in " ".join(preflight)
    assert set(READABLE_SUFFIXES.values()) == {
        "csv",
        "parquet",
    }, "the pre-flight sentence names what it reads"
