"""One check contract: `ValidationCheck` is what `validation.json` and a build report both carry.

Phase 2 kept a twin of the Phase 1 model, `OnboardingCheck`, because the Phase 1 one sat outside its
branch (DEC-101). Plan A ruling D7 merged them, and these tests pin what the merge promised: the one
model takes a code from either table and nothing else, the Phase 1 table itself has not grown, the
onboarding-only field is optional so every `validation.json` already written still reads, a leak can
never be marked acknowledgeable whoever builds the check, and the old name is the same class rather
than a copy that could drift from it again.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from engine.contracts import (
    CHECK_CODES,
    ONBOARDING_VALIDATION_CODES,
    VALIDATION_CODES,
    Severity,
    ValidationCheck,
    ValidationReport,
)
from engine.onboarding import specs


def test_the_old_name_is_the_same_class() -> None:
    assert specs.OnboardingCheck is ValidationCheck
    assert specs.ONBOARDING_VALIDATION_CODES is ONBOARDING_VALIDATION_CODES


def test_the_build_report_carries_the_phase_one_contract() -> None:
    assert specs.BuildReport.model_fields["checks"].annotation == tuple[ValidationCheck, ...]


def test_the_known_codes_are_both_tables_and_the_phase_one_table_has_not_grown() -> None:
    assert CHECK_CODES == VALIDATION_CODES | ONBOARDING_VALIDATION_CODES
    assert len(VALIDATION_CODES) == 19
    assert not (VALIDATION_CODES & ONBOARDING_VALIDATION_CODES)


@pytest.mark.parametrize("code", sorted(CHECK_CODES))
def test_every_code_of_either_table_is_accepted(code: str) -> None:
    assert ValidationCheck(code=code, severity=Severity.WARNING, message="x").code == code


def test_a_code_from_neither_table_is_refused() -> None:
    with pytest.raises(ValidationError, match="unknown validation code"):
        ValidationCheck(code="NOT_A_CODE", severity=Severity.WARNING, message="x")


def test_a_leak_is_never_acknowledgeable_whichever_name_builds_it() -> None:
    """Phase 2 plan section 7: FUTURE_EVENTS_LEAKED is a bug, never something to wave through."""
    for model in (ValidationCheck, specs.OnboardingCheck):
        with pytest.raises(ValidationError, match="never something a user may wave through"):
            model(code="FUTURE_EVENTS_LEAKED", severity=Severity.ERROR, message="x", acknowledgeable=True)
    assert ValidationCheck(code="FUTURE_EVENTS_LEAKED", severity=Severity.ERROR, message="x")


def test_only_the_leak_is_refused_acknowledgement() -> None:
    """The rule is about one code, not about onboarding codes or errors in general."""
    for code in ("LEAKAGE_SUSPECTED", "JOIN_KEY_COVERAGE_LOW"):
        check = ValidationCheck(code=code, severity=Severity.ERROR, message="x", acknowledgeable=True)
        assert check.acknowledgeable


def test_source_id_is_optional_and_round_trips() -> None:
    plain = ValidationCheck(code="PK_NOT_UNIQUE", severity=Severity.ERROR, message="x")
    assert plain.source_id is None
    sourced = ValidationCheck(
        code="JOIN_KEY_COVERAGE_LOW", severity=Severity.WARNING, message="x", source_id="src_bills"
    )
    assert ValidationCheck.model_validate_json(sourced.model_dump_json()) == sourced


def test_a_validation_json_written_before_the_merge_still_reads() -> None:
    """The merge added a field; a file without it is exactly what every existing run directory holds."""
    written_before = {
        "run_id": "r_1",
        "upload_id": "u_1",
        "mode": "train",
        "checks": [
            {
                "code": "PK_NOT_UNIQUE",
                "severity": "error",
                "message": "2.0 rows per customer",
                "suggestion": "",
                "column": "id",
                "details": {},
                "acknowledgeable": False,
                "acknowledged": False,
            }
        ],
        "error_count": 1,
        "warning_count": 0,
        "passed": False,
        "validated_at": "2026-09-01T12:00:00Z",
    }
    report = ValidationReport.model_validate(written_before)
    assert report.checks[0].source_id is None


def test_a_build_report_check_written_before_the_merge_still_reads() -> None:
    """`OnboardingCheck` had exactly these fields, so a saved build report's rows validate unchanged."""
    written_before = {
        "code": "JOIN_KEY_COVERAGE_LOW",
        "severity": "warning",
        "message": "70% of bills rows match a customer",
        "suggestion": "Check the key format.",
        "column": "entity_key",
        "source_id": "src_bills",
        "details": {"coverage": 0.7},
        "acknowledgeable": True,
        "acknowledged": False,
    }
    check = ValidationCheck.model_validate(written_before)
    assert check.model_dump(exclude={"schema_version"}) == written_before
