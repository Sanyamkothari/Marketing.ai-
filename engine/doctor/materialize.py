"""The one materialisation: the case's accepted plan, run once over its files, by everyone who needs the result.

Until now the preview, the plan's safety count, the AI's query trial, the prescription and the final check each
worked the plan out again for themselves, on different rows, with their own steps, order, key, outcome, table names
and as-of date. A stop could then show up only at the very end (VERIFY), after the person had approved.

:func:`materialize` is the single place that resolves all of it:

* **the steps** - the accepted ones of each file, in the engine's fixed order (:func:`plan_check.recipe_steps_of`),
  plus one `candidate` step when a caller asks what the plan would do with it;
* **the table names** - one namer (:func:`table_names`): a file is the table of its file name, and a name two files
  share is told apart;
* **the tables** - ALL of them, prepared: a query step that reads another file reads that file as the plan makes it,
  not as it was uploaded (files that make no table by a query are prepared first);
* **the key and the outcome** the guards count by (:func:`roles_of`; one helper, until the decisions model replaces
  it), **the case's as-of date**, and **the future-data check** a training file gets;
* **the rows** - :class:`Sample` is a seeded random sample, the same for every caller with the same scope;
  :class:`Full` is every row the caller loaded.

It never swallows an error: a step that cannot run, or a refusal of the guards, is a stop :class:`PlanFinding`, and
a finding's key is its code and subject only (never its count), so a yes given on a sample still matches the whole
file. Findings hold counts, codes and names only - never a cell value.
"""

from __future__ import annotations

import contextlib
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from engine.agent.config import AgentConfig
from engine.agent.contracts import ProposalState, RecipeStep, RecipeStepKind
from engine.agent.dataset_recipe import consistent_types
from engine.agent.recipe import STEP_PHASE, RecipeError
from engine.agent.sql_step import SqlStepError, sql_params
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.doctor.guards import Finding, Ledger, Receipt, run_guarded
from engine.doctor.visit_contracts import PlanFinding

if TYPE_CHECKING:
    import pandas as pd

    from engine.doctor.case import Case, CaseFile, CaseItem
    from engine.doctor.diagnose import LoadedFile

__all__ = [
    "SAMPLE_SEED",
    "Full",
    "Prepared",
    "PreparedFile",
    "Sample",
    "Scope",
    "materialize",
    "materialize_plan",
    "open_findings",
    "roles_of",
    "scoped",
    "stop_finding",
    "table_names",
]

SAMPLE_SEED: Final[int] = 20_260_101
"""The seed every sample of the plan is drawn with: the same rows for every caller of the same scope."""
_HARD_CODES: Final[frozenset[str]] = frozenset({"PLAN_STEP_CANNOT_RUN"})
"""The guards' stops no yes can lift: a step that cannot run is not a loss a person may accept."""
_SHAPING: Final[frozenset[RecipeStepKind]] = frozenset({RecipeStepKind.SQL_QUERY})


@dataclass(frozen=True)
class Sample:
    """At most `n` rows of each file, drawn at random with `seed` (never the first rows of the file)."""

    n: int
    seed: int = SAMPLE_SEED


@dataclass(frozen=True)
class Full:
    """Every row the caller loaded (the whole file, up to the profile row cap)."""


Scope = Sample | Full


@dataclass(frozen=True)
class PreparedFile:
    """What the plan did to one file: its table, the roles it was counted by, each step's receipt, its ledger."""

    upload_id: str
    file_name: str
    table: str
    key: str | None
    target: str | None
    receipts: tuple[tuple[str, Receipt], ...]
    """`(item id, receipt)` per step that ran, in order."""
    ledger: Ledger | None
    rows_total: int
    """The file's rows (the rows loaded, or the file's own count when those are a sample of it)."""
    sampled: bool = False


@dataclass(frozen=True)
class Prepared:
    """The plan's tables (prepared, by name), what each step did, and everything the guards found."""

    tables: dict[str, pd.DataFrame]
    files: tuple[PreparedFile, ...]
    receipts: tuple[tuple[str, Receipt], ...]
    findings: tuple[PlanFinding, ...]
    digest: str
    scope: Scope
    by_upload: dict[str, str] = field(default_factory=dict)
    """The table each file's upload became."""
    failed: tuple[str, ...] = ()
    """The uploads with a step that could not run: not prepared, and never to be read as if no step touched them."""

    @property
    def stopped(self) -> bool:
        return any(f.level == "stop" for f in self.findings)


# ---------------------------------------------------------------------------
# The resolutions
# ---------------------------------------------------------------------------
def table_names(files: Sequence[LoadedFile]) -> dict[str, str]:
    """The one namer: each upload's table is its file name's stem (letters, digits, underscores); a name two files
    share is told apart by a number from the second on."""
    from engine.doctor.prescription import slot_name

    taken: set[str] = set()
    names: dict[str, str] = {}
    for loaded in files:
        base = slot_name(Path(loaded.file_name).stem if loaded.file_name else loaded.upload_id)
        name, n = base, 2
        while name in taken:
            name, n = f"{base}_{n}", n + 1
        taken.add(name)
        names[loaded.upload_id] = name
    return names


def roles_of(
    items: Sequence[CaseItem], upload_id: str, columns: Collection[str]
) -> tuple[str | None, str | None]:
    """`(key, outcome)` the guards count this file by: the accepted ID and outcome items that name a column of the
    file; the outcome may come from an accepted query's table (one row per customer), whose ID then follows.
    """
    from engine.doctor.plan_check import accepted_column

    key = accepted_column(items, "key", upload_id, columns)
    target = accepted_column(items, "outcome_column", upload_id, columns)
    if target is None:
        from engine.doctor.prescription import prepared_key, query_makes

        declared = next(
            (
                str(i.payload["column"])
                for i in items
                if i.kind == "outcome_column"
                and i.state is ProposalState.ACCEPTED
                and i.upload_id in (upload_id, None)
                and isinstance(i.payload.get("column"), str)
            ),
            None,
        )
        if declared is not None and query_makes(items, declared):
            target, key = declared, prepared_key(items, declared, key)
    return key, target


def scoped(frame: pd.DataFrame, scope: Scope) -> pd.DataFrame:
    """`frame` as `scope` reads it: all of it, or a seeded random sample in the file's own order."""
    if isinstance(scope, Full) or len(frame) <= scope.n:
        return frame
    return frame.sample(n=scope.n, random_state=scope.seed).sort_index()


def _stop(code: str, message: str, order: int | None, item_id: str | None, scope: Scope) -> PlanFinding:
    return _finding(Finding(level="stop", code=code, order=order, message=message), item_id, scope, {})


def _finding(
    found: Finding,
    item_id: str | None,
    scope: Scope,
    seen: dict[tuple[str, str], int],
    *,
    hard: bool = False,
    file: str | None = None,
) -> PlanFinding:
    """A guard finding as the ledger holds it. Its subject is the step, or else the file's plan (`file`, an upload
    id): the key is the code and the subject, never the count, so a yes given to one file's stop never carries
    to another file's. Two findings of one code about one subject are told apart by their number.
    """
    subject = item_id or (f"plan:{file}" if file else "plan")
    place = (found.code, subject)
    seen[place] = seen.get(place, 0) + 1
    if seen[place] > 1:
        subject = f"{subject}#{seen[place]}"
    return PlanFinding(
        key=sha256_hex(canonical_bytes([found.code, subject]))[:16],
        level=found.level,
        code=found.code,
        text=found.message,
        item_id=item_id,
        detail=found.detail,
        count=found.count,
        scope="full" if isinstance(scope, Full) else "sample",
        hard=hard or found.code in _HARD_CODES,
    )


# ---------------------------------------------------------------------------
# The materialisation
# ---------------------------------------------------------------------------
def materialize(
    case: Case,
    items: Sequence[CaseItem],
    files: Sequence[LoadedFile],
    scope: Scope,
    *,
    candidate: tuple[str, RecipeStep] | None = None,
    twice: bool = True,
) -> Prepared:
    """The case's plan run over `files` at `scope`, with the case's as-of date. `items` are the plan's (the active
    section). `candidate` is `(upload id, step)`: a step to try as if it were accepted (a query trial)."""
    from engine.doctor.agent_review import case_as_of

    given = case_as_of(case)
    return materialize_plan(
        case.files,
        items,
        files,
        scope,
        as_of=date.fromisoformat(given) if given is not None else None,
        candidate=candidate,
        twice=twice,
    )


def materialize_plan(
    case_files: Sequence[CaseFile],
    items: Sequence[CaseItem],
    files: Sequence[LoadedFile],
    scope: Scope,
    *,
    as_of: date | None = None,
    candidate: tuple[str, RecipeStep] | None = None,
    twice: bool = True,
) -> Prepared:
    """:func:`materialize` for a caller that has the case's files and as-of date rather than the case itself."""
    from engine.doctor.plan_check import (
        _tried_before_change,
        guard_levels,
        plan_digest,
        recipe_steps_of,
    )

    digest = plan_digest(case_files, items)
    if candidate is not None:
        digest = sha256_hex(canonical_bytes([digest, candidate[0], candidate[1].model_dump(mode="json")]))[
            :32
        ]
    names = table_names(files)
    tables: dict[str, pd.DataFrame] = {
        names[f.upload_id]: consistent_types(scoped(f.frame, scope)) for f in files
    }
    agent = AgentConfig()
    done: list[PreparedFile] = []
    receipts: list[tuple[str, Receipt]] = []
    findings: list[PlanFinding] = []
    seen: dict[tuple[str, str], int] = {}
    failed: list[str] = []

    plans: list[tuple[LoadedFile, list[tuple[RecipeStep, str]]]] = []
    for loaded in files:
        mine = recipe_steps_of(items, loaded.upload_id, only_file=len(files) == 1)
        if candidate is not None and candidate[0] == loaded.upload_id:
            phased = sorted([*mine, (candidate[1], "candidate")], key=lambda pair: STEP_PHASE[pair[0].kind])
            mine = [(s.model_copy(update={"order": n}), i) for n, (s, i) in enumerate(phased, start=1)]
        if mine:
            plans.append((loaded, mine))
    # A file no query reads from is prepared first, so a query step reads the others as the plan makes them.
    plans.sort(key=lambda plan: any(s.kind in _SHAPING for s, _ in plan[1]))

    for loaded, mine in plans:
        ordered = [step for step, _ in mine]
        by_order = {step.order: item_id for step, item_id in mine}
        name = names[loaded.upload_id]
        frame = tables[name]
        key, target = roles_of(items, loaded.upload_id, {str(c) for c in frame.columns})
        file_name = loaded.file_name or "the file"
        try:
            guarded = run_guarded(
                frame,
                ordered,
                primary_key=key,
                target=target,
                tables=tables,
                upload_id=loaded.upload_id,
                source_name=loaded.file_name or None,
                levels=guard_levels(ordered),
                max_failure_pct=agent.max_conversion_failure_pct,
                twice=twice,
                as_of=as_of,
                leak_check="full",
            )
        except (RecipeError, SqlStepError, ValueError, KeyError, TypeError) as exc:
            order = getattr(exc, "order", None)
            item_id = by_order.get(order or 0)
            said = getattr(exc, "message", None) or type(exc).__name__
            if _tried_before_change(ordered, order):
                found = Finding(
                    level="stop",
                    code="PLAN_QUERY_TRIED_BEFORE_CHANGE",
                    order=order,
                    message=f"The query was tried before the fixes now in the plan for {file_name} were "
                    "settled, and it cannot run after them. Ask for the query again.",
                )
            else:
                found = Finding(
                    level="stop",
                    code="PLAN_STEP_CANNOT_RUN",
                    order=order,
                    message=f"A step could not run on {'a sample of ' if isinstance(scope, Sample) else ''}"
                    f"{file_name}: {said}",
                )
            # a step that cannot run is a rule whichever sentence says why: the file is not prepared, so no yes may
            # let the plan go on as if it were
            findings.append(_finding(found, item_id, scope, seen, hard=True, file=loaded.upload_id))
            failed.append(loaded.upload_id)
            continue
        for order in (st.order for st in ordered if _tried_before_change(ordered, st.order)):
            findings.append(  # it runs, but not on what it was tried on: the person says yes to that
                _finding(
                    Finding(
                        level="stop",
                        code="PLAN_QUERY_TRIED_BEFORE_CHANGE",
                        order=order,
                        message=f"A query was tried on {file_name} before the other steps now in the plan "
                        "were settled, and it now runs after them, so its result may differ. "
                        "Ask for the query again (name the earlier query in `replaces`).",
                    ),
                    by_order.get(order),
                    scope,
                    seen,
                    file=loaded.upload_id,
                )
            )
        made = [(by_order[r.order], r) for r in guarded.receipts]
        receipts.extend(made)
        findings.extend(
            _finding(
                f, by_order.get(f.order) if f.order is not None else None, scope, seen, file=loaded.upload_id
            )
            for f in guarded.findings
        )
        tables[name] = guarded.run.frame  # the prepared file is what every later step reads
        for step in reversed(ordered):
            if step.kind is RecipeStepKind.SQL_QUERY:
                with contextlib.suppress(SqlStepError):  # its output is also a table by the name it was given
                    tables.setdefault(sql_params(step.params).output_name, guarded.run.frame)
                break
        done.append(
            PreparedFile(
                upload_id=loaded.upload_id,
                file_name=loaded.file_name,
                table=name,
                key=key,
                target=target,
                receipts=tuple(made),
                ledger=guarded.ledger,
                rows_total=loaded.file_rows or len(loaded.frame),
                sampled=bool(loaded.file_rows) or (isinstance(scope, Sample) and len(loaded.frame) > scope.n),
            )
        )
    return Prepared(
        tables=tables,
        files=tuple(done),
        receipts=tuple(receipts),
        findings=tuple(findings),
        digest=digest,
        scope=scope,
        by_upload=names,
        failed=tuple(failed),
    )


def stop_finding(
    code: str, message: str, scope: Scope, *, hard: bool = True, subject: str | None = None
) -> PlanFinding:
    """A stop about the whole plan that is not one of the guards' (for example, nothing to prepare the file from)."""
    found = _stop(code, message, None, None, scope).model_copy(update={"hard": hard})
    if subject is None:
        return found
    return found.model_copy(update={"key": sha256_hex(canonical_bytes([code, subject]))[:16]})


def open_findings(findings: Sequence[PlanFinding], said: Sequence[Any]) -> list[PlanFinding]:
    """The stops of `findings` nobody has said yes to (:func:`plan_check.open_stops` over bare findings)."""
    from engine.doctor.plan_check import stop_is_open

    return [f for f in findings if stop_is_open(f, said)]
