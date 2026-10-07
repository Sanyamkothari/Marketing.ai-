"""Plan J M93's two check codes: accepted on a check row, kept out of the Phase 1 to 3b registry.

Plan J keeps its codes in its own set (DEC-1300 (d), `engine.decide.codes.PLAN_J_CODES`), which the
plain-language catalogue reads; `engine.measurement.codes.MEASUREMENT_CHECK_CODES` holds the two that
travel as a `ValidationCheck` row, and `ValidationCheck` accepts them through its PLAN-J hook. The
one-check contract's tables (`CHECK_CODE_TABLES`) are unchanged.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from engine.contracts import CHECK_CODE_TABLES, CHECK_CODES, Severity, ValidationCheck, check_code_table
from engine.measurement.codes import MEASUREMENT_CHECK_CODES


def test_m93_has_exactly_its_two_codes() -> None:
    assert {"LABEL_RATE_UNSTABLE", "TREATMENT_HISTORY_NOT_RANDOM"} == MEASUREMENT_CHECK_CODES


def test_they_are_in_no_table_of_the_one_check_contract() -> None:
    assert not (MEASUREMENT_CHECK_CODES & CHECK_CODES)
    assert set(CHECK_CODE_TABLES) == {"validation", "extension", "onboarding", "uplift", "agent"}
    assert all(check_code_table(code) is None for code in MEASUREMENT_CHECK_CODES)


@pytest.mark.parametrize("code", sorted(MEASUREMENT_CHECK_CODES))
def test_a_check_row_accepts_them(code: str) -> None:
    assert ValidationCheck(code=code, severity=Severity.WARNING, message="x").code == code


def test_a_code_from_nowhere_is_still_refused() -> None:
    with pytest.raises(ValidationError, match="unknown validation code"):
        ValidationCheck(code="LABEL_RATE_UNSTEADY", severity=Severity.WARNING, message="x")
