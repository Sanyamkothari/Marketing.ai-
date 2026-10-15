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
from engine.doctor.stack_files import StackError, StackSpec, run_stack
from engine.privacy.working_key import (
    WorkingKeyError,
    key_vault,
    new_salt,
    pii_key_kind,
    refuse_aadhaar,
    working_key,
)

if TYPE_CHECKING:
    from engine.doctor.case import CaseItem
    from engine.doctor.contracts import Diagnosis

__all__ = ["JOIN_KIND", "STACK_KIND", "Combined", "combine_item", "plan_of", "rules_digest", "upload_ids_of"]

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
    key_vault: pd.DataFrame | None = None
    """When the customers' key is personal data (a phone, a PAN), the table holds salted hashes of it and this holds
    the lookup back to the real value, for the contact-list export only (`engine.privacy.working_key`)."""


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
    item: CaseItem,
    diagnosis: Diagnosis,
    frames: Mapping[str, pd.DataFrame],
    names: Mapping[str, str],
    *,
    key_salt: str | None = None,
) -> Combined:
    """The table the approved `item` makes from `frames` (the whole files, by upload id); `names` are file names.

    `key_salt` is the case's salt for a key that is personal data (G24); without one a fresh salt is drawn.
    """
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
            run = run_stack([frames[f.upload_id] for f in spec.files], spec)
            return Combined(run.frame, f"{stem} stacked.csv", ids, f"{len(run.frame):,} rows")
        plan = plan_of(diagnosis, {u: frames[u] for u in ids})
        if rules_digest(plan) != item.payload.get("rules"):
            raise AgentToolError(
                "COMBINE_CHANGED",
                "The files no longer fit together the way you approved. Untick the change to go on.",
            )
        table, vault = _one_row_per_customer(
            plan,
            {s.source_name: [frames[u] for u in s.upload_ids] for s in (plan.entity, *plan.events)},
            key_salt or new_salt(),
        )
    except (BuildDriveError, StackError, WorkingKeyError) as exc:
        raise AgentToolError(exc.code, exc.message) from exc
    except ValidationError as exc:
        raise AgentToolError(
            "COMBINE_FAILED", "This change can no longer be read. Untick it to go on."
        ) from exc
    logs = ", ".join(dict.fromkeys(Path(names.get(u, "")).stem for s in plan.events for u in s.upload_ids))
    return Combined(table, f"{stem} with {logs}.csv", ids, f"{len(table):,} customers", key_vault=vault)


def _name(frame: pd.DataFrame, column: str) -> Any:
    return next(c for c in frame.columns if str(c) == column)


def _one_row_per_customer(
    plan: BuildPlan, files: Mapping[str, Sequence[pd.DataFrame]], salt: str
) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """The table with one row per customer, and the key vault when the key is personal data (phone, PAN, e-mail).

    The customers and their logs meet on salted hashes of such a key, and the table keeps the hashes (G24). An
    Aadhaar number is refused: it is never stored.
    """
    (entity, base), *logs = prepare_sources(plan, files).sources
    key_column = _name(base, entity.key_column)
    keys = base[key_column]
    if keys.isna().any() or keys.duplicated().any():
        raise AgentToolError(
            "COMBINE_KEY_REPEATS", f"{entity.source_name} does not have one row per customer."
        )
    kind = pii_key_kind(keys, str(key_column))
    refuse_aadhaar(kind)
    hashed = kind is not None

    def written(values: pd.Series[Any]) -> pd.Series[Any]:
        return working_key(values, salt) if hashed else values.astype(str)

    text = written(keys)
    by_role: dict[str, list[pd.DataFrame]] = {}
    for source, frame in logs:
        if source.date_column is None:
            continue
        when = pd.to_datetime(frame[_name(frame, source.date_column)], errors="coerce", utc=True)
        who = written(frame[_name(frame, source.key_column)])
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
    if not hashed:
        return table, None
    vault = key_vault(keys, text)
    table[key_column] = text
    return table, vault
