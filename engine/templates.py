"""Upload-template rendering: one CSV and one README per use case, from the YAML alone.

Both renderers are pure functions of a `UseCaseConfig`: no timestamps, no sorting, no dict-order
dependence, so `scripts/gen_templates.py` and the `GET /use-cases/{id}/template.csv` route produce
byte-identical output (plan §4.3).
"""

from __future__ import annotations

import csv
import io

from engine.config import ColumnRole, SplitType, TemplateColumn, UseCaseConfig

EXAMPLE_ROWS: int = 5
"""Number of example rows every template carries (plan §4.3: five example rows)."""


def render_template_csv(config: UseCaseConfig) -> str:
    """The upload template of `config` as CSV text.

    Header = `template.columns` names in config order; then five rows built from `examples[i]`.
    `csv.writer` with ``lineterminator="\\n"`` and `QUOTE_MINIMAL`, UTF-8, no BOM, trailing newline.
    Returns ``""`` when the use case declares no template columns.
    """
    columns = config.template.columns
    if not columns:
        return ""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([column.name for column in columns])
    for index in range(EXAMPLE_ROWS):
        writer.writerow([column.examples[index] for column in columns])
    return buffer.getvalue()


def render_template_readme(config: UseCaseConfig) -> str:
    """The upload template's README of `config` as Markdown text.

    Title, one intro paragraph, one table row per template column, a Required section derived from
    the column roles and a Limits line from `validation`. Returns ``""`` when the use case declares
    no template columns.
    """
    columns = config.template.columns
    if not columns:
        return ""
    lines: list[str] = [
        f"# {config.name} — upload template",
        "",
        f"One row per {config.entity}. Keep the header row exactly as it is.",
        "",
        "## Columns",
        "",
        "| Column | Role | Type | Description |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| `{column.name}` | {_role_label(column)} | {column.type.value} | {_cell(column.description)} |"
        for column in columns
    ]
    lines += ["", "## Required", ""]
    lines += [f"- {item}" for item in _required_items(config)]
    limits = config.validation
    lines += [
        "",
        "## Limits",
        "",
        f"At least {limits.min_rows:,} rows and {limits.min_positive:,} positive examples; "
        f"maximum file size {limits.max_file_size_mb} MB.",
        "",
    ]
    return "\n".join(lines)


def template_filenames(config: UseCaseConfig) -> tuple[str, str]:
    """`(csv filename, README filename)` of `config`, both derived from `template_stem`."""
    stem = config.template_stem
    return f"{stem}_template.csv", f"{stem}_template_README.md"


def _role_label(column: TemplateColumn) -> str:
    """`primary_key` -> `primary key`; the role id is never a display string elsewhere."""
    return column.role.value.replace("_", " ")


def _cell(text: str) -> str:
    """Escape the one character a Markdown table cell cannot carry."""
    return text.replace("|", "\\|")


def _required_items(config: UseCaseConfig) -> tuple[str, ...]:
    """The Required bullets, in column order of the roles that make a column mandatory."""
    items: list[str] = []
    primary_key = config.template.primary_key
    if primary_key is not None:
        items.append(
            f"`{primary_key.name}` — the primary key: it identifies each row and is never used as a feature."
        )
    targets = config.template.by_role(ColumnRole.TARGET)
    if targets:
        items.append(f"`{targets[0].name}` — the target: required for training, leave blank when scoring.")
    if config.split.type is SplitType.TIME_BASED:
        time_columns = config.template.by_role(ColumnRole.TIME)
        if time_columns:
            items.append(
                f"`{time_columns[0].name}` — the time column: this use case splits the data by time, "
                "so every row needs it."
            )
    return tuple(items)
