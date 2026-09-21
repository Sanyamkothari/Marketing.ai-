"""The industry file and its cross-file rules (design section 3.7)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from engine.config import (
    DEFAULT_CONFIG_ROOT,
    AiType,
    ConfigError,
    IndustryConfig,
    UseCaseStatus,
    load_industry,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "configs"


def _root_with_industry(tmp_path: Path, fixture: str) -> Path:
    """A copy of `configs/` whose `industries/telecom.yaml` is the named broken fixture."""
    root = tmp_path / "configs"
    shutil.copytree(DEFAULT_CONFIG_ROOT, root)
    shutil.copy(FIXTURES / fixture, root / "industries" / "telecom.yaml")
    return root


@pytest.mark.parametrize(
    ("fixture", "code"),
    [
        ("industry_unknown_available.yaml", "INDUSTRY_USE_CASE_MISSING"),
        ("industry_planned_but_present.yaml", "INDUSTRY_PLANNED_BUT_PRESENT"),
        ("industry_planned_needs_name.yaml", "INDUSTRY_PLANNED_NEEDS_NAME"),
        ("industry_stage_mismatch.yaml", "INDUSTRY_STAGE_MISMATCH"),
        ("industry_duplicate_use_case.yaml", "INDUSTRY_DUPLICATE_USE_CASE"),
        ("industry_name_on_available.yaml", "INDUSTRY_NAME_ON_AVAILABLE"),
    ],
)
def test_broken_industry_files_raise_their_code(tmp_path: Path, fixture: str, code: str) -> None:
    root = _root_with_industry(tmp_path, fixture)
    with pytest.raises(ConfigError) as error:
        load_industry("telecom", root)
    assert error.value.code == code


def test_duplicate_stage_ids_are_rejected() -> None:
    document = {
        "schema_version": 1,
        "id": "telecom",
        "name": "Telecom",
        "stages": [
            {"id": "awareness", "name": "Awareness", "ai_type": "predictive", "use_cases": []},
            {"id": "awareness", "name": "Awareness again", "ai_type": "predictive", "use_cases": []},
        ],
    }
    with pytest.raises(ConfigError) as error:
        IndustryConfig.model_validate(document)
    assert error.value.code == "INDUSTRY_DUPLICATE_STAGE"


def test_a_missing_industry_file_is_reported() -> None:
    with pytest.raises(ConfigError) as error:
        load_industry("no-such-industry")
    assert error.value.code == "CONFIG_NOT_FOUND"


def test_all_refs_walks_stages_in_order() -> None:
    industry = load_industry("telecom")
    refs = industry.all_refs()
    assert [ref.id for _, ref in refs] == [
        "targeted-advertisement",
        "ai-onboarding-assistant",
        "order-fulfillment",
        "fault-prediction",
        "payment-propensity",
        "rca",
        "win-back-campaign",
    ]
    stage, planned = refs[1]
    assert stage.ai_type is AiType.GENERATIVE
    assert planned.status is UseCaseStatus.PLANNED
    assert planned.name == "AI Onboarding Assistant"
    assert planned.description and planned.description.endswith(".")
