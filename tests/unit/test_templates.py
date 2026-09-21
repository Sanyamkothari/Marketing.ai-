"""The upload templates are a pure projection of the YAML (design §8.1, plan §4.3)."""

from __future__ import annotations

import csv
import io

import pytest

from engine.config import ColumnRole, SplitType, UseCaseConfig, list_use_case_ids, load_use_case
from engine.templates import render_template_csv, render_template_readme, template_filenames

USE_CASE_IDS: tuple[str, ...] = list_use_case_ids()

ALWAYS_PRESENT: tuple[str, ...] = ("snapshot_date", "marketing_opt_in", "last_contacted_at")


@pytest.fixture(params=USE_CASE_IDS)
def config(request: pytest.FixtureRequest) -> UseCaseConfig:
    """Each shipped use case in turn."""
    return load_use_case(str(request.param))


def _rows(config: UseCaseConfig) -> list[list[str]]:
    return list(csv.reader(io.StringIO(render_template_csv(config))))


def test_header_equals_column_names(config: UseCaseConfig) -> None:
    assert _rows(config)[0] == list(config.template.column_names)


def test_five_example_rows_each_as_wide_as_the_header(config: UseCaseConfig) -> None:
    rows = _rows(config)
    assert len(rows) == 6
    assert all(len(row) == len(rows[0]) for row in rows[1:])


def test_primary_key_is_first_and_target_is_last(config: UseCaseConfig) -> None:
    columns = config.template.columns
    assert columns[0].role is ColumnRole.PRIMARY_KEY
    assert columns[-1].role is ColumnRole.TARGET
    assert columns[-1].name == config.target.column


def test_the_shared_columns_are_present(config: UseCaseConfig) -> None:
    names = config.template.column_names
    assert set(ALWAYS_PRESENT) <= set(names)


def test_csv_is_utf8_without_a_bom_and_ends_with_a_newline(config: UseCaseConfig) -> None:
    body = render_template_csv(config)
    assert not body.startswith("﻿")
    assert body.endswith("\n")
    assert body.encode("utf-8").decode("utf-8-sig") == body


def test_two_renders_are_byte_identical(config: UseCaseConfig) -> None:
    assert render_template_csv(config) == render_template_csv(config)
    assert render_template_readme(config) == render_template_readme(config)


def test_readme_table_lists_every_column_exactly_once(config: UseCaseConfig) -> None:
    readme = render_template_readme(config)
    for column in config.template.columns:
        assert readme.count(f"| `{column.name}` |") == 1
        assert column.description.replace("|", "\\|") in readme


def test_readme_required_section_names_key_target_and_time_column(config: UseCaseConfig) -> None:
    readme = render_template_readme(config)
    required = readme.split("## Required", 1)[1].split("## Limits", 1)[0]
    primary_key = config.template.primary_key
    assert primary_key is not None
    assert f"`{primary_key.name}`" in required
    assert f"`{config.target.column}`" in required
    time_columns = config.template.by_role(ColumnRole.TIME)
    if config.split.type is SplitType.TIME_BASED:
        assert f"`{time_columns[0].name}`" in required
    else:
        assert f"`{time_columns[0].name}`" not in required


def test_readme_states_the_limits(config: UseCaseConfig) -> None:
    readme = render_template_readme(config)
    limits = config.validation
    assert f"{limits.min_rows:,} rows" in readme
    assert f"{limits.min_positive:,} positive examples" in readme
    assert f"{limits.max_file_size_mb} MB" in readme


def test_filenames_follow_the_template_stem(config: UseCaseConfig) -> None:
    assert template_filenames(config) == (
        f"{config.template_stem}_template.csv",
        f"{config.template_stem}_template_README.md",
    )


def test_a_value_containing_a_comma_is_quoted() -> None:
    """`csv.QUOTE_MINIMAL`: only the cell that needs quoting gets quotes."""
    base = load_use_case(USE_CASE_IDS[0])
    columns = list(base.template.columns)
    first = columns[0]
    columns[0] = first.model_copy(update={"examples": ("a,b", *first.examples[1:])})
    patched = base.model_copy(
        update={"template": base.template.model_copy(update={"columns": tuple(columns)})}
    )
    body = render_template_csv(patched)
    assert '"a,b"' in body
    assert body.splitlines()[1].startswith('"a,b",')


def test_an_empty_template_renders_nothing() -> None:
    base = load_use_case(USE_CASE_IDS[0])
    empty = base.model_copy(update={"template": base.template.model_copy(update={"columns": ()})})
    assert render_template_csv(empty) == ""
    assert render_template_readme(empty) == ""
