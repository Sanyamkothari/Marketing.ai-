"""Combining files, the doing half: what the person approved becomes one table (no AI).

`engine.doctor.combining` proposes a join or a stack; once it is approved, :func:`combine_item` builds it from the
whole files. The files are only read: the table comes out as a new one.

* **A join** (`build_dataset` item) plans again on the whole files and refuses (`COMBINE_CHANGED`) if the rules
  are no longer the ones that were approved. It then uses the build driver's own preparation
  (`engine.doctor.build_driver.prepare_sources`: stacks counted once, each log's customer IDs written as the
  customer list writes them) and puts one row per customer in the table: the customer list as it was, plus, for
  each kind of log, how many entries it has in the last 90 days and how many days ago the last one was. They are
  counted up to the day the logs run to, so no entry after that day is read (the builder's point-in-time rule).
* **A stack** (`stack_files` item) runs the approved `StackSpec`: a row that two exports both hold is kept once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pandas as pd
from pydantic import ValidationError

from engine.agent.tools import AgentToolError
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.config import AggFunction
from engine.doctor.build_driver import BuildDriveError, BuildPlan, plan_build, prepare_sources
from engine.doctor.guards import Finding, Receipt, build_ledger, make_receipt, plan_findings, receipt_findings
from engine.doctor.stack_files import StackError, StackSpec, run_stack

if TYPE_CHECKING:
    from engine.doctor.case import CaseItem
    from engine.doctor.contracts import Diagnosis

__all__ = [
    "JOIN_KIND",
    "STACK_KIND",
    "Combined",
    "combine_item",
    "guard_join",
    "guard_stack",
    "plan_of",
    "rules_digest",
    "upload_ids_of",
]

JOIN_KIND: Final[str] = "build_dataset"
STACK_KIND: Final[str] = "stack_files"
"""The two item kinds (the protocol's APPROVE-class list names both)."""

_TABLE: Final[str] = "data-doctor"
"""The plan's use case: the table is made before any goal is chosen."""


@dataclass(frozen=True)
class Combined:
    """The table an approved item made, and which files it stands in for."""

    frame: pd.DataFrame
    file_name: str
    upload_ids: tuple[str, ...]
    summary: str
    receipt: Receipt | None = None
    findings: tuple[Finding, ...] = ()


def guard_join(
    plan: BuildPlan, files: Mapping[str, Sequence[pd.DataFrame]], table: pd.DataFrame
) -> tuple[Receipt, tuple[Finding, ...]]:
    """The receipt and findings for a join: the customer list and each log read, the table made, counted by code."""
    prepared = prepare_sources(plan, files).sources
    (entity, base), *logs = prepared
    inputs: dict[str, pd.DataFrame] = {entity.source_name: base}
    inputs.update({source.source_name: frame for source, frame in logs})
    receipt = make_receipt(1, JOIN_KIND, inputs, table, key=entity.key_column)
    return receipt, tuple(receipt_findings(receipt) + plan_findings(build_ledger([receipt])))


def guard_stack(
    frames: Sequence[pd.DataFrame], table: pd.DataFrame, key: str | None
) -> tuple[Receipt, tuple[Finding, ...]]:
    """The receipt and findings for a stack: every file read, the table made. A customer may be on many rows."""
    inputs = {f"file {i}": frame for i, frame in enumerate(frames, 1)}
    receipt = make_receipt(1, STACK_KIND, inputs, table, key=key, stacking=True)
    return receipt, tuple(receipt_findings(receipt, one_row_per_key=False))


def _stopped(findings: Sequence[Finding]) -> None:
    stops = [f for f in findings if f.level == "stop" and f.code in _AUTOMATIC]
    if stops:
        raise AgentToolError("COMBINE_GUARD_STOP", f"{stops[0].message} Untick the change to go on.")


_AUTOMATIC: Final[frozenset[str]] = frozenset(
    {"STEP_ZERO_ROWS", "STEP_EMPTY_COLUMN", "JOIN_REPEATS_CUSTOMERS"}
)
"""The stops that need no question: the table would be empty, hold an empty column, or count customers twice."""


def plan_of(diagnosis: Diagnosis, frames: Mapping[str, pd.DataFrame]) -> BuildPlan:
    """The build driver's plan for `frames` (the files, by upload id): no outcome, the driver's own features."""
    return plan_build(diagnosis, frames, use_case=_TABLE, label_spec=None)


def rules_digest(plan: BuildPlan) -> str:
    """What the person approves in a join, apart from the counts: which files, which ID columns and ID rules."""
    parts = [
        [source.upload_ids, source.key_column, source.date_column, source.key_rule.params()]
        for source in (plan.entity, *plan.events)
    ]
    return sha256_hex(canonical_bytes(parts))


def upload_ids_of(item: CaseItem) -> tuple[str, ...]:
    """The files a join or stack item is made from."""
    given = item.payload.get("upload_ids")
    return tuple(str(u) for u in given) if isinstance(given, list) else ()


def combine_item(
    item: CaseItem, diagnosis: Diagnosis, frames: Mapping[str, pd.DataFrame], names: Mapping[str, str]
) -> Combined:
    """The table the approved `item` makes from `frames` (the whole files, by upload id); `names` are file names."""
    ids = upload_ids_of(item)
    missing = [u for u in ids if u not in frames]
    if not ids or missing:
        raise AgentToolError("COMBINE_FILES_MISSING", "A file of this change is no longer in the case.")
    stem = Path(names.get(ids[0], "table")).stem
    try:
        if item.kind == STACK_KIND:
            spec = StackSpec.model_validate(item.payload.get("spec"))
            if not {f.upload_id for f in spec.files} <= set(ids):
                raise AgentToolError(
                    "COMBINE_FILES_MISSING", "A file of this change is no longer in the case."
                )
            stacked = [frames[f.upload_id] for f in spec.files]
            run = run_stack(stacked, spec)
            receipt, findings = guard_stack(stacked, run.frame, spec.key_column)
            _stopped(findings)
            return Combined(
                run.frame, f"{stem} stacked.csv", ids, f"{len(run.frame):,} rows", receipt, findings
            )
        plan = plan_of(diagnosis, {u: frames[u] for u in ids})
        if rules_digest(plan) != item.payload.get("rules"):
            raise AgentToolError(
                "COMBINE_CHANGED",
                "The files no longer fit together the way you approved. Untick the change to go on.",
            )
        files = {s.source_name: [frames[u] for u in s.upload_ids] for s in (plan.entity, *plan.events)}
        table = _one_row_per_customer(plan, files)
        receipt, findings = guard_join(plan, files, table)
        _stopped(findings)
    except (BuildDriveError, StackError) as exc:
        raise AgentToolError(exc.code, exc.message) from exc
    except ValidationError as exc:
        raise AgentToolError(
            "COMBINE_FAILED", "This change can no longer be read. Untick it to go on."
        ) from exc
    logs = ", ".join(dict.fromkeys(Path(names.get(u, "")).stem for s in plan.events for u in s.upload_ids))
    return Combined(table, f"{stem} with {logs}.csv", ids, f"{len(table):,} customers", receipt, findings)


def _name(frame: pd.DataFrame, column: str) -> Any:
    return next(c for c in frame.columns if str(c) == column)


def _one_row_per_customer(plan: BuildPlan, files: Mapping[str, Sequence[pd.DataFrame]]) -> pd.DataFrame:
    (entity, base), *logs = prepare_sources(plan, files).sources
    keys = base[_name(base, entity.key_column)]
    if keys.isna().any() or keys.duplicated().any():
        raise AgentToolError(
            "COMBINE_KEY_REPEATS", f"{entity.source_name} does not have one row per customer."
        )
    text = keys.astype(str)
    by_role: dict[str, list[pd.DataFrame]] = {}
    for source, frame in logs:
        if source.date_column is None:
            continue
        when = pd.to_datetime(frame[_name(frame, source.date_column)], errors="coerce", utc=True)
        who = frame[_name(frame, source.key_column)].astype(str)
        by_role.setdefault(source.role, []).append(pd.DataFrame({"who": who, "when": when}))
    events = {role: pd.concat(parts, ignore_index=True).dropna() for role, parts in by_role.items()}
    ends = [e["when"].max() for e in events.values() if len(e)]
    if not ends:
        raise AgentToolError("COMBINE_NO_DATES", "None of the dates in the logs could be read.")
    today = max(ends)  # point in time: nothing after the day the logs run to is read
    table = base.copy()
    for feature in plan.features:
        log = events.get(feature.role or "")
        if log is None:
            continue
        name = feature.name if feature.name not in table.columns else f"{feature.name}_events"
        if feature.function is AggFunction.COUNT and feature.window_days:
            recent = log[log["when"] > today - pd.Timedelta(days=feature.window_days)]
            table[name] = text.map(recent.groupby("who").size()).fillna(0).astype("Int64")
        elif feature.function is AggFunction.DAYS_SINCE_LAST:
            last = text.map(log.groupby("who")["when"].max())
            table[name] = (today - last).dt.days.astype("Int64")
    return table
