"""The three-form KPI grammar (DEC-007): parsed at load, never evaluated in M1."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine.config import ConfigError, KpiFormula, list_use_case_ids, load_all_use_cases

ENGINE_CONFIG_SOURCE = Path(__file__).resolve().parents[2] / "engine" / "config.py"


@pytest.mark.parametrize("use_case_id", list_use_case_ids())
def test_every_shipped_formula_parses(use_case_id: str) -> None:
    kpi = load_all_use_cases()[use_case_id].output.kpi
    parsed = kpi.parsed
    assert parsed.fn in ("count_rows", "count_where_band_in", "sum_where_band_in")
    assert kpi.label
    band_names = {band.name for band in load_all_use_cases()[use_case_id].actions.bands}
    assert set(parsed.bands) <= band_names


def test_the_three_forms() -> None:
    assert KpiFormula.parse("count_rows()") == KpiFormula(fn="count_rows")
    assert KpiFormula.parse('count_where_band_in(["High","Medium"])') == KpiFormula(
        fn="count_where_band_in", bands=("High", "Medium")
    )
    assert KpiFormula.parse('sum_where_band_in("avg_monthly_spend", ["High"])') == KpiFormula(
        fn="sum_where_band_in", column="avg_monthly_spend", bands=("High",)
    )


@pytest.mark.parametrize(
    "formula",
    [
        "count_everything()",
        "count_rows",
        "count_rows(1)",
        'count_where_band_in("High")',
        "count_where_band_in([High])",
        "count_where_band_in([])",
        "count_where_band_in([1, 2])",
        'sum_where_band_in("col")',
        "__import__('os').system('ls')",
        "",
    ],
)
def test_unparseable_formulas_raise(formula: str) -> None:
    with pytest.raises(ConfigError) as error:
        KpiFormula.parse(formula)
    assert error.value.code == "KPI_FORMULA_UNPARSEABLE"


def test_a_formula_is_kept_verbatim_in_the_config() -> None:
    config = load_all_use_cases()["targeted-advertisement"]
    assert config.output.kpi.formula == 'count_where_band_in(["High","Medium"])'
    assert config.output.kpi.parsed.bands == ("High", "Medium")


def test_the_config_module_never_evaluates_anything() -> None:
    source = ENGINE_CONFIG_SOURCE.read_text(encoding="utf-8")
    assert re.search(r"(?<![\w.])eval\s*\(", source) is None
    assert re.search(r"(?<![\w.])exec\s*\(", source) is None
    assert "compile(" not in source.replace("re.compile(", "")
