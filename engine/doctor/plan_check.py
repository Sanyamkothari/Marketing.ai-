"""The plan's safety count: the accepted steps run once on the case's sample under the guards.

One small module. PREVIEW calls :func:`check_plan` and stores the answer on the case (`CaseVisit.plan_check`), so it
is counted once per plan, not on every poll. The screen reads it as `view.ledger` (:func:`ledger_view`, the field
names `ui/modules/datadoctor/plan.js` documents); `approve` refuses while :func:`open_stops` is not empty. Every
number comes from `engine.doctor.guards` (our code, never the AI). No cell value is stored: counts, codes and column
names only.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import JsonValue

from engine.agent.config import AgentConfig, AgentLevel
from engine.agent.contracts import ProposalState, RecipeStep, RecipeStepKind
from engine.agent.recipe import STEP_PHASE
from engine.agent.sql_step import SqlStepError, sql_params
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.doctor.case import CaseFile, CaseItem
from engine.doctor.diagnose import LoadedFile
from engine.doctor.guards import Receipt, holds, names_a_log_row
from engine.doctor.materialize import Sample, materialize_plan
from engine.doctor.prescription import slot_name
from engine.doctor.visit_contracts import Acknowledgement, CaseVisit, PlanCheck, PlanFinding

if TYPE_CHECKING:
    from engine.doctor.materialize import Prepared

__all__ = [
    "SAMPLE_ROWS",
    "accepted_column",
    "acknowledged_codes",
    "case_tables",
    "check_plan",
    "guard_levels",
    "ledger_view",
    "open_stops",
    "plan_check_of",
    "plan_digest",
    "readiness_acknowledged",
    "recipe_steps_of",
    "steps_digest",
    "stop_is_open",
    "trial_steps_of",
    "with_full_findings",
]

SAMPLE_ROWS: Final[int] = 1_000
"""Rows of each file the plan is tried on."""
_TOTALS_SHOWN: Final[int] = 10
_ORDER: Final[dict[str, int]] = {"stop": 0, "flag": 1, "note": 2}


def plan_digest(files: Sequence[CaseFile], items: Sequence[CaseItem]) -> str:
    """What the plan is: the files and every accepted item as it stands. A change to either is another plan."""
    accepted = sorted((i.item_id, i.payload_digest) for i in items if i.state is ProposalState.ACCEPTED)
    return sha256_hex(canonical_bytes([[f.upload_id for f in files], accepted]))[:32]


def recipe_steps_of(
    items: Sequence[CaseItem], upload_id: str, *, only_file: bool, pending: bool = False
) -> list[tuple[RecipeStep, str]]:
    """The accepted recipe steps for one file with their item ids, in the engine's fixed order, numbered 1..n.

    `pending` also takes the steps still waiting for a yes: the plan as it will stand if they are all accepted.
    """
    found: list[tuple[RecipeStep, str]] = []
    states = (ProposalState.ACCEPTED, ProposalState.PENDING) if pending else (ProposalState.ACCEPTED,)
    for item in items:
        if item.state not in states or not isinstance(item.payload.get("step"), dict):
            continue
        if item.upload_id != upload_id and not (item.upload_id is None and only_file):
            continue
        try:
            found.append((RecipeStep.model_validate(item.payload["step"]), item.item_id))
        except ValueError:
            continue
    found.sort(key=lambda pair: STEP_PHASE[pair[0].kind])
    return [
        (step.model_copy(update={"order": n}), item_id) for n, (step, item_id) in enumerate(found, start=1)
    ]


_SHAPING: Final[frozenset[RecipeStepKind]] = frozenset(
    {
        RecipeStepKind.SQL_QUERY,
        RecipeStepKind.COMBINE_ROWS,
        RecipeStepKind.UNPIVOT,
        RecipeStepKind.KEEP_LATEST_PER_ID,
    }
)
"""The steps that make other rows or another table: a query's trial never applies one that is only proposed."""


def trial_steps_of(
    items: Sequence[CaseItem], upload_id: str, *, only_file: bool, replacing: Collection[str] = ()
) -> list[tuple[RecipeStep, str]]:
    """What a new query is tried after: the accepted steps and the waiting per-column fixes (dates parsed, numbers
    read), never a waiting query, combine or reshape, and never an item named in `replacing` (the chat swaps or
    takes it back at the end of its turn). Numbered 1..n."""
    state = {i.item_id: i.state for i in items}
    everything = recipe_steps_of(items, upload_id, only_file=only_file, pending=True)
    left_out = [
        STEP_PHASE[step.kind]
        for step, item_id in everything
        if step.kind in _SHAPING and (item_id in replacing or state[item_id] is not ProposalState.ACCEPTED)
    ]
    cut = min(left_out, default=None)  # a waiting fix that reads what a left-out step makes would not run
    kept = [
        (step, item_id)
        for step, item_id in everything
        if item_id not in replacing
        and (state[item_id] is ProposalState.ACCEPTED or step.kind not in _SHAPING)
        and (state[item_id] is ProposalState.ACCEPTED or cut is None or STEP_PHASE[step.kind] <= cut)
    ]
    return [
        (step.model_copy(update={"order": n}), item_id) for n, (step, item_id) in enumerate(kept, start=1)
    ]


def steps_digest(steps: Sequence[RecipeStep]) -> str:
    """A short digest of what steps do (kind, columns, parameters), not of their number or who proposed them: how a
    query step remembers the fixes it was tried after."""
    shape = [[s.kind.value, s.column, s.new_column, s.params] for s in steps]
    return sha256_hex(canonical_bytes(shape))[:16]


def accepted_column(
    items: Sequence[CaseItem], kind: str, upload_id: str, columns: Collection[str]
) -> str | None:
    """The column an accepted item of `kind` names for this file (its ID, its outcome), if the file has it."""
    for item in items:
        column = item.payload.get("column")
        if (
            item.kind == kind
            and item.state is ProposalState.ACCEPTED
            and item.upload_id in (upload_id, None)
            and isinstance(column, str)
            and column in columns
        ):
            return column
    return None


def guard_levels(steps: Sequence[RecipeStep]) -> tuple[AgentLevel, ...]:
    """The levels a plan's steps may run at: the default ones, and reshaping when a step is a query."""
    levels = tuple(AgentConfig().levels)
    if any(s.kind is RecipeStepKind.SQL_QUERY for s in steps) and AgentLevel.RESHAPE not in levels:
        levels = (*levels, AgentLevel.RESHAPE)
    return levels


def case_tables(files: Sequence[LoadedFile]) -> dict[str, Any]:
    """Each file's first `SAMPLE_ROWS` rows by table name: what a query step may read besides its own table."""
    return {slot_name(Path(f.file_name).stem): f.frame.head(SAMPLE_ROWS) for f in files}


def _step_row(receipt: Receipt, item_id: str) -> dict[str, JsonValue]:
    counted = receipt.keys_in is not None
    return {
        "item_id": item_id,
        "rows_in": next(iter(receipt.rows_in.values()), 0),
        "rows_out": receipt.rows_out,
        "customers_in": receipt.keys_in if counted else None,
        "customers_out": receipt.keys_out if counted else None,
        "customers_lost": receipt.keys_lost if counted else None,
        "customers_duplicated": receipt.keys_repeated if counted else None,
        "totals": [
            {"column": t.column, "before": t.before, "after": t.after} for t in receipt.totals[:_TOTALS_SHOWN]
        ],
    }


def _tried_before_change(ordered: Sequence[RecipeStep], order: int | None) -> bool:
    """Whether the step that failed is a query tried on the file after other fixes than those it now runs after."""
    step = next((s for s in ordered if s.order == order), None)
    if step is None or step.kind is not RecipeStepKind.SQL_QUERY:
        return False
    try:
        tried = sql_params(step.params).tried_after
    except SqlStepError:
        return False
    return tried is not None and tried != steps_digest([s for s in ordered if s.order < step.order])


def plan_check_of(prepared: Prepared, *, basis: str | None = None) -> PlanCheck:
    """What the guards counted when the plan was materialised, as the case keeps it (the ledger and the findings).
    `basis` is set when every row of the files was counted (see :attr:`PlanCheck.basis`).

    A plan with no steps has an empty ledger. A step that cannot run is a stop in the findings, not a silence.
    """
    steps = [_step_row(receipt, item_id) for item_id, receipt in prepared.receipts]
    if not steps:
        return PlanCheck(digest=prepared.digest, findings=prepared.findings, basis=basis)
    totals = {"rows_in": 0, "rows_out": 0, "customers_in": 0, "customers_out": 0}
    counted = True
    customer_tables = 0  # a log's rows (orders, payments) are never added to the customers
    rates: tuple[float | None, float | None] = (None, None)
    sampled = 0
    for done in prepared.files:
        ledger = done.ledger
        if ledger is None:
            continue
        totals["rows_in"] += next(iter(done.receipts[0][1].rows_in.values()), 0)
        totals["rows_out"] += done.receipts[-1][1].rows_out
        if not names_a_log_row(done.key):
            customer_tables += 1
            counted = counted and ledger.unit == "customers"
            totals["customers_in"] += ledger.start
            totals["customers_out"] += ledger.end
        if rates == (None, None):
            rates = (ledger.rate_start, ledger.rate_end)
        # Only files counted in `rows_in` count here, so "the first N of M rows" compares like with like.
        sampled += done.rows_total
    counted = counted and customer_tables > 0
    view: dict[str, JsonValue] = {
        "rows_in": totals["rows_in"],
        "rows_out": totals["rows_out"],
        "customers_in": totals["customers_in"] if counted else None,
        "customers_out": totals["customers_out"] if counted else None,
        "positive_rate_in": rates[0],
        "positive_rate_out": rates[1],
        "sampled_from": sampled if sampled > totals["rows_in"] else None,
        "steps": list(steps),
    }
    return PlanCheck(digest=prepared.digest, ledger=view, findings=prepared.findings, basis=basis)


def check_plan(
    files: Sequence[LoadedFile],
    case_files: Sequence[CaseFile],
    items: Sequence[CaseItem],
    *,
    as_of: date | None = None,
) -> PlanCheck:
    """Run the accepted steps on a seeded sample of each file under the guards (run twice) and keep what was
    counted: :func:`materialize_plan` at :class:`Sample` of `SAMPLE_ROWS`, as a stored check."""
    return plan_check_of(materialize_plan(case_files, items, files, Sample(SAMPLE_ROWS), as_of=as_of))


def stop_is_open(found: PlanFinding, acknowledged: Sequence[Acknowledgement]) -> bool:
    """Whether a finding is a stop nobody has said yes to. A hard stop is never lifted; a yes holds while the count
    stays at or below the one said yes to (a bigger loss needs a new yes)."""
    if found.level != "stop":
        return False
    if found.hard:
        return True
    return not any(a.key == found.key and holds(a.count, found.count) for a in acknowledged)


def with_full_findings(check: PlanCheck, found: Sequence[PlanFinding]) -> PlanCheck:
    """The check with what VERIFY found on the whole file taken over the earlier findings of the same key (the
    whole file is what counts) and in place of the last check's readiness findings; the guards' other findings stay. It is marked `verified`: the screen reads those findings as the reasons the data is not ready.
    """
    keys = {f.key for f in found}
    # what the last check said (readiness) goes; the guards' findings stay unless the new ones say it again
    kept = tuple(f for f in check.findings if f.key not in keys and f.source != "readiness")
    return check.model_copy(
        update={"findings": (*kept, *found), "basis": check.basis or check.digest, "verified": True}
    )


def readiness_acknowledged(visit: CaseVisit | None) -> tuple[str, ...]:
    """The readiness findings a person said yes to, as `validation.acknowledged` entries (`CODE` or `CODE:column`),
    for the plan as counted: the readiness check then holds them as acknowledged."""
    if visit is None or visit.plan_check is None:
        return ()
    return tuple(
        dict.fromkeys(
            f.code if f.column is None else f"{f.code}:{f.column}"
            for f in visit.plan_check.findings
            if f.source == "readiness"
            and f.level == "stop"
            and not f.hard
            and not stop_is_open(f, visit.acknowledged)
        )
    )


def open_stops(check: PlanCheck | None, acknowledged: Sequence[Acknowledgement]) -> list[PlanFinding]:
    """The stops nobody has said yes to (:func:`stop_is_open`)."""
    if check is None:
        return []
    return [f for f in check.findings if stop_is_open(f, acknowledged)]


def acknowledged_codes(visit: CaseVisit | None) -> tuple[str, ...] | None:
    """The kinds of stop a person said yes to for the plan as counted: what an approved recipe may replay with.
    None when the plan was never counted (approved before the guards), so its recipe replays as it always did.
    """
    if visit is None or visit.plan_check is None:
        return None
    return tuple(
        sorted(
            {
                f.code
                for f in visit.plan_check.findings
                if f.level == "stop" and not f.hard and not stop_is_open(f, visit.acknowledged)
            }
        )
    )


def ledger_view(check: PlanCheck | None, acknowledged: Sequence[Acknowledgement]) -> dict[str, Any] | None:
    """`view.ledger` as the plan screen reads it; None until the plan has been counted. A stop a person said yes
    to is shown as a flag that says so, and carries `acknowledged`; every finding carries its `key`."""
    if check is None:
        return None
    shown: list[dict[str, Any]] = []
    for found in sorted(check.findings, key=lambda f: _ORDER[f.level]):
        ack = found.level == "stop" and not found.hard and not stop_is_open(found, acknowledged)
        shown.append(
            {
                "level": "flag" if ack else found.level,
                "kind": "guard" if found.source == "guard" else "readiness",
                "key": found.key,
                "text": " ".join(
                    filter(None, [found.text, found.detail, "(confirmed by a person)" if ack else ""])
                ),
                "acknowledged": ack,
                "item_id": found.item_id,
                "code": found.code,
                "count": found.count,
                "scope": found.scope,
                "hard": found.hard,
                "acknowledgeable": found.level == "stop" and not found.hard,
            }
        )
    worst: dict[str, str] = {}  # the worst level said about each step, for its receipt
    for entry in shown:
        item_id = entry["item_id"]
        if item_id and _ORDER[entry["level"]] < _ORDER.get(worst.get(item_id, ""), 3):
            worst[item_id] = entry["level"]
    rows = cast("list[dict[str, Any]]", check.ledger.get("steps") or [])
    steps = [{**row, **({"level": worst[row["item_id"]]} if row["item_id"] in worst else {})} for row in rows]
    return {**check.ledger, "steps": steps, "findings": shown}
