"""The client-facing data request, generated from the use cases and the roles (Plan E M59, DEC-905).

A pilot's first document goes to a client's analyst who has never seen this platform: which tables
to export, which columns, how much history, in what format, how to pseudonymise the customer ID,
and what never to send. Every fact in it is read from configuration the engine itself runs on, so
the request cannot ask for less than a build needs, or for a column no feature reads:

* **Tables.** The customer table always. The table the outcome is worked out from (the use case's
  `label.role`) is *required*: without it there is nothing to learn. A table a suggested feature
  reads is *recommended*. A table `configs/pilot/data_request.yaml` lists under `extra_tables` (the
  campaign table of a use case run with a control group) takes the need and columns listed there.
* **Columns.** Per table, the standard columns in `configs/roles.yaml` and the use case's
  `standard_schema`. A column is *required* when the engine needs it to build at all (the customer
  ID, an event's date, a `standard_schema` column marked required), *needed* when a suggested
  feature or the label reads it, and *useful* otherwise.
* **History.** The minimum is `onboarding.snapshots.min_history_days + label.horizon_days`, the
  same sum `engine.onboarding.snapshots` refuses a build below (`TOO_LITTLE_HISTORY`). The
  recommendation adds the longest feature window and the snapshots a year of monthly learning
  needs, and says why.
* **What not to send.** One line per kind `engine.pii` detects, from `configs/pilot/data_request.yaml`.

The wording lives in `configs/pilot/data_request.yaml`. `render_markdown` is a pure function of the
configuration, so `docs/pilot/DATA_REQUEST.md` is committed and drift-checked like `templates/`.
"""

from __future__ import annotations

import csv
import io
import math
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from engine.config import (
    FeatureDef,
    RoleCatalogue,
    RoleKind,
    SnapshotFrequency,
    StandardColumn,
    StandardType,
    SubAggregation,
    UseCaseConfig,
    config_root,
    get_roles,
    load_use_case,
    load_yaml,
)

__all__ = [
    "DATA_REQUEST_FILENAME",
    "DataRequest",
    "DataRequestWording",
    "RequestedColumn",
    "RequestedTable",
    "build_data_request",
    "load_wording",
    "render_markdown",
    "render_role_template",
    "template_filename",
]

DATA_REQUEST_FILENAME: Final[str] = "pilot/data_request.yaml"
"""The wording file, relative to the configuration root."""

Need = Literal["required", "recommended", "optional"]
ColumnNeed = Literal["required", "needed", "useful"]

_FREQUENCY_DAYS: Final[dict[SnapshotFrequency, int]] = {
    SnapshotFrequency.MONTHLY: 30,
    SnapshotFrequency.WEEKLY: 7,
}

_KIND: Final[dict[StandardType, str]] = {
    StandardType.NUMERIC: "Number",
    StandardType.CATEGORICAL: "Code or category",
    StandardType.BOOLEAN: "Yes / no",
    StandardType.DATE: "Date (YYYY-MM-DD)",
    StandardType.TEXT: "Free text",
}

_CUSTOMER_ID: Final[str] = "customer_id"
"""The name the request asks for the key column by, in every table."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TableWording(_Strict):
    title: str
    file: str
    what: str


class ExtraTable(_Strict):
    """A table a use case needs for a reason no feature or label states (a measured campaign)."""

    role: str
    need: Literal["required", "recommended", "optional"]
    columns: tuple[str, ...] = ()
    why: str


class DataRequestWording(_Strict):
    schema_version: Literal[1]
    default_use_cases: tuple[str, ...]
    tables: dict[str, TableWording]
    do_not_send: dict[str, str]
    do_not_send_notes: tuple[str, ...] = ()
    formats: tuple[str, ...]
    pseudonymisation: tuple[str, ...]
    preflight: tuple[str, ...]
    outcomes: dict[str, str] = {}
    column_meanings: dict[str, str] = {}
    plain_phrases: dict[str, str] = {}
    extra_tables: dict[str, tuple[ExtraTable, ...]] = {}

    def plain(self, text: str) -> str:
        """`text` with every engine phrase replaced by the client's words, longest phrase first."""
        for phrase in sorted(self.plain_phrases, key=len, reverse=True):
            text = text.replace(phrase, self.plain_phrases[phrase])
            text = text.replace(phrase[:1].upper() + phrase[1:], _capital(self.plain_phrases[phrase]))
        return text


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


class RequestedColumn(_Strict):
    name: str
    kind: str
    need: ColumnNeed
    meaning: str


class RequestedTable(_Strict):
    role: str
    title: str
    file: str
    what: str
    need: Need
    why: str
    columns: tuple[RequestedColumn, ...]


class UseCaseLine(_Strict):
    id: str
    name: str
    purpose: str
    outcome: str
    history_min_days: int
    history_recommended_days: int


class DataRequest(_Strict):
    use_cases: tuple[UseCaseLine, ...]
    tables: tuple[RequestedTable, ...]
    history_min_days: int
    history_recommended_days: int
    min_customers: int
    min_outcomes: int
    do_not_send: tuple[str, ...]
    do_not_send_notes: tuple[str, ...]
    formats: tuple[str, ...]
    pseudonymisation: tuple[str, ...]
    preflight: tuple[str, ...]


@lru_cache(maxsize=8)
def _wording(path: Path, mtime_ns: int) -> DataRequestWording:
    del mtime_ns
    return DataRequestWording.model_validate(load_yaml(path))


def load_wording(root: Path | None = None) -> DataRequestWording:
    path = config_root(root) / DATA_REQUEST_FILENAME
    return _wording(path, path.stat().st_mtime_ns)


# ---------------------------------------------------------------------------
# Facts from the configuration
# ---------------------------------------------------------------------------
def _feature_columns(features: tuple[FeatureDef, ...], role: str) -> set[str]:
    """Columns of `role` a suggested feature reads, its filter and both halves of a ratio included."""
    used: set[str] = set()
    for feature in features:
        if feature.role != role:
            continue
        parts: list[FeatureDef | SubAggregation] = [feature]
        parts += [sub for sub in (feature.of, feature.over) if sub is not None]
        for part in parts:
            if part.column:
                used.add(part.column)
            if part.where is not None:
                used.add(part.where.column)
    return used


def _history(config: UseCaseConfig) -> tuple[int, int]:
    """(minimum, recommended) days of history `config` needs from the event tables."""
    snapshots = config.onboarding.snapshots
    horizon = config.label.horizon_days if config.label is not None and config.label.horizon_days else 0
    minimum = snapshots.min_history_days + horizon
    windows = [
        window for feature in config.suggested_features for window in feature.windows if window is not None
    ]
    lookback = max([snapshots.min_history_days, *windows])
    step = _FREQUENCY_DAYS[snapshots.frequency]
    recommended = lookback + horizon + (snapshots.max_snapshots - 1) * step
    return minimum, recommended


def _column(
    name: str, column: StandardColumn | None, need: ColumnNeed, *, meaning: str | None = None
) -> RequestedColumn:
    kind = _KIND[column.type] if column is not None else "Code or category"
    return RequestedColumn(
        name=name,
        kind=kind,
        need=need,
        meaning=meaning if meaning is not None else (column.description if column is not None else ""),
    )


_NEED_ORDER: Final[dict[str, int]] = {"required": 0, "needed": 1, "useful": 2}


def _entity_table(
    configs: tuple[UseCaseConfig, ...], roles: RoleCatalogue, wording: DataRequestWording
) -> RequestedTable:
    role = roles.entity_role
    columns: dict[str, RequestedColumn] = {
        _CUSTOMER_ID: _column(
            _CUSTOMER_ID,
            None,
            "required",
            meaning="The pseudonymised customer ID - the same code for the same customer in every file.",
        )
    }
    for config in configs:
        for standard in config.standard_schema.columns:
            if standard.derivable is not None and standard.name not in columns:
                need: ColumnNeed = "useful"
            else:
                need = "required" if standard.required else "needed"
            current = columns.get(standard.name)
            if current is None or _NEED_ORDER[need] < _NEED_ORDER[current.need]:
                columns[standard.name] = _column(
                    standard.name, standard, need, meaning=wording.column_meanings.get(standard.name)
                )
    spec = roles.roles[role]
    for optional in spec.optional_columns:
        if optional not in columns and optional != "as_of_date":
            columns[optional] = RequestedColumn(
                name=optional,
                kind=_optional_kind(optional),
                need="useful",
                meaning=wording.column_meanings.get(optional, ""),
            )
    words = wording.tables[role]
    return RequestedTable(
        role=role,
        title=words.title,
        file=words.file,
        what=words.what,
        need="required",
        why="Every other table is linked to a customer through this one.",
        columns=tuple(sorted(columns.values(), key=lambda c: _NEED_ORDER[c.need])),
    )


def _lower_first(text: str) -> str:
    """`"No activity..."` -> `"no activity..."` without the trailing full stop: for a clause."""
    text = text.strip().rstrip(".")
    return text[:1].lower() + text[1:]


def _sentence(text: str) -> str:
    return _lower_first(text) + "."


def _optional_kind(name: str) -> str:
    return "Date (YYYY-MM-DD)" if name.endswith("_date") else "Code or category"


def _event_table(
    role: str,
    configs: tuple[UseCaseConfig, ...],
    roles: RoleCatalogue,
    wording: DataRequestWording,
    need: Need,
    why: str,
    needed: frozenset[str] = frozenset(),
) -> RequestedTable:
    spec = roles.roles[role]
    used: set[str] = set(needed)
    for config in configs:
        used |= _feature_columns(config.suggested_features, role)
        if config.label is not None and config.label.role == role and config.label.where is not None:
            used.add(config.label.where.column)
    columns = [
        _column(
            _CUSTOMER_ID, None, "required", meaning="The pseudonymised customer ID, as in the customer file."
        ),
        RequestedColumn(
            name="event_date",
            kind="Date (YYYY-MM-DD), or date and time",
            need="required",
            meaning="When it happened.",
        ),
    ]
    for typical in spec.typical_columns:
        columns.append(
            _column(
                typical.name,
                typical,
                "needed" if typical.name in used else "useful",
                meaning=wording.column_meanings.get(typical.name),
            )
        )
    words = wording.tables[role]
    return RequestedTable(
        role=role,
        title=words.title,
        file=words.file,
        what=words.what,
        need=need,
        why=why,
        columns=tuple(sorted(columns, key=lambda c: _NEED_ORDER[c.need])),
    )


def build_data_request(use_case_ids: tuple[str, ...] | None = None, root: Path | None = None) -> DataRequest:
    """The data request for `use_case_ids` (default: the pilot's), from the configs under `root`."""
    wording = load_wording(root)
    ids = use_case_ids or wording.default_use_cases
    configs = tuple(load_use_case(use_case_id, root) for use_case_id in ids)
    roles = get_roles(root)

    rank: dict[str, int] = {"required": 0, "recommended": 1, "optional": 2}
    needs: dict[str, Need] = {}
    uses: dict[str, dict[str, list[str]]] = {}

    def want(role: str, need: Need, use_case: str, use: str) -> None:
        if role not in needs or rank[need] < rank[needs[role]]:
            needs[role] = need
        listed = uses.setdefault(role, {}).setdefault(use_case, [])
        if use not in listed:
            listed.append(use)

    extra_columns: dict[str, set[str]] = {}
    for config in configs:
        if config.label is not None and config.label.role:
            outcome = wording.outcomes.get(config.id) or wording.plain(_lower_first(config.label.description))
            want(config.label.role, "required", config.name, f"the outcome is worked out from it ({outcome})")
        for feature in config.suggested_features:
            if feature.role in roles.roles and roles.roles[feature.role].kind is RoleKind.EVENT:
                want(
                    feature.role, "recommended", config.name, wording.plain(_lower_first(feature.description))
                )
        for extra in wording.extra_tables.get(config.id, ()):
            want(extra.role, extra.need, config.name, extra.why)
            extra_columns.setdefault(extra.role, set()).update(extra.columns)

    tables = [_entity_table(configs, roles, wording)]
    for role in sorted(needs, key=lambda r: (rank[needs[r]], list(roles.roles).index(r))):
        why = " ".join(f"{use_case}: {'; '.join(items)}." for use_case, items in uses[role].items())
        needed = frozenset(extra_columns.get(role, ()))
        tables.append(_event_table(role, configs, roles, wording, needs[role], why, needed))

    lines = []
    for config in configs:
        minimum, recommended = _history(config)
        lines.append(
            UseCaseLine(
                id=config.id,
                name=config.name,
                purpose=wording.plain(config.description),
                outcome=wording.outcomes.get(config.id)
                or wording.plain(
                    config.label.description if config.label is not None else config.target.definition
                ),
                history_min_days=minimum,
                history_recommended_days=recommended,
            )
        )

    from engine.pii import DETECTORS

    missing = [d.kind for d in DETECTORS if d.kind not in wording.do_not_send]
    if missing:
        raise ValueError(f"configs/pilot/data_request.yaml do_not_send has no line for: {', '.join(missing)}")
    return DataRequest(
        use_cases=tuple(lines),
        tables=tuple(tables),
        history_min_days=max(line.history_min_days for line in lines),
        history_recommended_days=max(line.history_recommended_days for line in lines),
        min_customers=max(config.validation.min_rows for config in configs),
        min_outcomes=max(config.validation.min_positive for config in configs),
        do_not_send=tuple(wording.do_not_send[d.kind] for d in DETECTORS),
        do_not_send_notes=wording.do_not_send_notes,
        formats=wording.formats,
        pseudonymisation=wording.pseudonymisation,
        preflight=wording.preflight,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _months(days: int) -> int:
    return math.ceil(days / 30)


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


_NEED_LABEL: Final[dict[str, str]] = {
    "required": "Required",
    "recommended": "Recommended",
    "optional": "Optional",
    "needed": "Needed",
    "useful": "Useful",
}


def template_filename(role: str, wording: DataRequestWording) -> str:
    """`bills.csv` -> `bills_template.csv`."""
    return wording.tables[role].file.replace(".csv", "_template.csv")


def render_role_template(table: RequestedTable) -> str:
    """The header-only CSV template of one requested table.

    Written the way `engine.templates.render_template_csv` writes a use case's template - `csv.writer`,
    `\\n` line endings, minimal quoting, UTF-8 without a BOM - so both kinds of template open the
    same way. A role template carries no example rows: a use-case template's examples are part of
    its config, and a role has none to show, so none are invented.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([column.name for column in table.columns])
    return buffer.getvalue()


def render_markdown(request: DataRequest, wording: DataRequestWording) -> str:
    """The data request as Markdown, the committed `docs/pilot/DATA_REQUEST.md`."""
    names = " and ".join(line.name for line in request.use_cases)
    out: list[str] = [
        "<!-- Generated by `make pilot-generate` from configs/pilot/data_request.yaml, configs/roles.yaml",
        "     and the use-case configs. Do not edit by hand: tests/unit/pilot fails when it is stale. -->",
        "",
        "# Data request",
        "",
        f"This is what we need from you to run the pilot for **{names}**. Your analyst should be able to",
        "prepare every file from this page alone. If anything is unclear, the pre-flight checker described",
        "at the end will tell you before anything is sent.",
        "",
        "## What the pilot will do",
        "",
    ]
    for line in request.use_cases:
        out.append(
            f"- **{line.name}.** {line.purpose} The outcome it learns from: {line.outcome.rstrip('.').lower()}."
        )
    out += [
        "",
        "A *prediction date* is a date the platform stands at: it looks only at what happened before it,",
        "and learns from what happened after it.",
        "",
        "## How much history",
        "",
        f"- **At least {_months(request.history_min_days)} months** ({request.history_min_days} days) of history "
        "in every dated table. Below this the platform cannot learn at all: it needs some history before",
        "  each date it predicts from, and enough time after it to see what happened.",
        f"- **Better: {_months(request.history_recommended_days)} months** ({request.history_recommended_days} days). "
        "This lets the platform learn from a full year of monthly",
        "  predictions and use the longest look-back its measures need (for example complaints over the last year).",
        "- The customer table should list every customer active at any time in that period, including those who left.",
        f"- Ideally at least {request.min_customers:,} customers, of whom at least {request.min_outcomes:,} had the outcome "
        "(for example left) during the period.",
        "",
        "Per use case:",
        "",
        "| Use case | Minimum | Recommended |",
        "|---|---|---|",
    ]
    for line in request.use_cases:
        out.append(
            f"| {_cell(line.name)} | {_months(line.history_min_days)} months ({line.history_min_days} days) | "
            f"{_months(line.history_recommended_days)} months ({line.history_recommended_days} days) |"
        )
    out += ["", "## The tables", "", "| Table | File name | Need | Why |", "|---|---|---|---|"]
    for table in request.tables:
        out.append(f"| {table.title} | `{table.file}` | {_NEED_LABEL[table.need]} | {_cell(table.why)} |")
    out += [
        "",
        "**Required** tables must be sent. **Recommended** tables make the results noticeably better. Column",
        "names do not have to match ours: the platform matches your names to ours, and you confirm the match.",
        "",
    ]
    for table in request.tables:
        out += [
            f"### {table.title} (`{table.file}`)",
            "",
            f"{table.what} Template: [`{template_filename(table.role, wording)}`](templates/{template_filename(table.role, wording)}).",
            "",
            "| Column | Kind | Need | What it is |",
            "|---|---|---|---|",
        ]
        out += [
            f"| `{column.name}` | {column.kind} | {_NEED_LABEL[column.need]} | {_cell(column.meaning)} |"
            for column in table.columns
        ]
        out.append("")
    out += [
        "**Required** columns must be present. **Needed** columns feed a measure the pilot uses. **Useful**",
        "columns help if you have them.",
        "",
        "## File formats",
        "",
        *[f"- {item}" for item in request.formats],
        "",
        "## Pseudonymising the customer ID",
        "",
        *[f"- {item}" for item in request.pseudonymisation],
        "",
        "## What not to send",
        "",
        "Never include these, in any column:",
        "",
        *[f"- {item}" for item in request.do_not_send],
        "",
        *[f"- {item}" for item in request.do_not_send_notes],
        "",
        "If one of these reaches the platform anyway it is masked and never learned from, and the data",
        "readiness report will tell you which column it was so the next extract can leave it out.",
        "",
        "## Before you send: the pre-flight check",
        "",
        *[f"- {item}" for item in request.preflight],
        "",
    ]
    return "\n".join(out)
