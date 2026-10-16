"""The plan's safety count: the accepted steps run once on the case's sample under the guards.

One small module. PREVIEW calls :func:`check_plan` and stores the answer on the case (`CaseVisit.plan_check`), so it
is counted once per plan, not on every poll. The screen reads it as `view.ledger` (:func:`ledger_view`, the field
names `ui/modules/datadoctor/plan.js` documents); `approve` refuses while :func:`open_stops` is not empty. Every
number comes from `engine.doctor.guards` (our code, never the AI). No cell value is stored: counts, codes and column
names only.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any, Final, cast

from pydantic import JsonValue

from engine.agent.config import AgentConfig, AgentLevel
from engine.agent.contracts import ProposalState, RecipeStep, RecipeStepKind
from engine.agent.dataset_recipe import consistent_types
from engine.agent.recipe import STEP_PHASE, RecipeError
from engine.agent.sql_step import SqlStepError
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.doctor.case import CaseFile, CaseItem
from engine.doctor.diagnose import LoadedFile
from engine.doctor.guards import Finding, Receipt, names_a_log_row, run_guarded
from engine.doctor.prescription import slot_name
from engine.doctor.visit_contracts import Acknowledgement, CaseVisit, PlanCheck, PlanFinding

__all__ = ["acknowledged_codes", "check_plan", "ledger_view", "open_stops", "plan_digest", "recipe_steps_of"]

SAMPLE_ROWS: Final[int] = 1_000
"""Rows of each file the plan is tried on."""
_TOTALS_SHOWN: Final[int] = 10
_ORDER: Final[dict[str, int]] = {"stop": 0, "flag": 1, "note": 2}


def plan_digest(files: Sequence[CaseFile], items: Sequence[CaseItem]) -> str:
    """What the plan is: the files and every accepted item as it stands. A change to either is another plan."""
    accepted = sorted((i.item_id, i.payload_digest) for i in items if i.state is ProposalState.ACCEPTED)
    return sha256_hex(canonical_bytes([[f.upload_id for f in files], accepted]))[:32]


def recipe_steps_of(
    items: Sequence[CaseItem], upload_id: str, *, only_file: bool
) -> list[tuple[RecipeStep, str]]:
    """The accepted recipe steps for one file with their item ids, in the engine's fixed order, numbered 1..n."""
    found: list[tuple[RecipeStep, str]] = []
    for item in items:
        if item.state is not ProposalState.ACCEPTED or not isinstance(item.payload.get("step"), dict):
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


def _column(items: Sequence[CaseItem], kind: str, upload_id: str, columns: Collection[str]) -> str | None:
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


def _finding(found: Finding, item_id: str | None) -> PlanFinding:
    return PlanFinding(
        key=sha256_hex(canonical_bytes([found.key, item_id]))[:16],
        level=found.level,
        code=found.code,
        text=found.message,
        item_id=item_id,
    )


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


def check_plan(
    files: Sequence[LoadedFile], case_files: Sequence[CaseFile], items: Sequence[CaseItem]
) -> PlanCheck:
    """Run the accepted steps on each file's sample under the guards (run twice) and keep what was counted.

    A plan with no steps has an empty ledger. A step that cannot run on the sample is a stop, not a silence.
    """
    digest = plan_digest(case_files, items)
    tables = {slot_name(Path(f.file_name).stem): f.frame.head(SAMPLE_ROWS) for f in files}
    steps: list[dict[str, JsonValue]] = []
    findings: list[PlanFinding] = []
    totals = {"rows_in": 0, "rows_out": 0, "customers_in": 0, "customers_out": 0}
    counted = True
    customer_tables = 0  # a log's rows (orders, payments) are never added to the customers
    rates: tuple[float | None, float | None] = (None, None)
    sampled = 0
    for loaded in files:
        mine = recipe_steps_of(items, loaded.upload_id, only_file=len(files) == 1)
        if not mine:
            continue
        ordered = [step for step, _ in mine]
        by_order = {step.order: item_id for step, item_id in mine}
        frame = consistent_types(loaded.frame.head(SAMPLE_ROWS))
        columns = {str(c) for c in frame.columns}
        key = _column(items, "key", loaded.upload_id, columns)
        agent = AgentConfig()
        levels = tuple(agent.levels)
        if any(s.kind is RecipeStepKind.SQL_QUERY for s in ordered) and AgentLevel.RESHAPE not in levels:
            levels = (*levels, AgentLevel.RESHAPE)
        try:
            guarded = run_guarded(
                frame,
                ordered,
                primary_key=key,
                target=_column(items, "outcome_column", loaded.upload_id, columns),
                tables=tables,
                upload_id=loaded.upload_id,
                source_name=loaded.file_name or None,
                levels=levels,
                max_failure_pct=agent.max_conversion_failure_pct,
            )
        except (RecipeError, SqlStepError, ValueError, KeyError, TypeError) as exc:
            item_id = by_order.get(getattr(exc, "order", None) or 0)
            failed = Finding(
                level="stop",
                code="PLAN_STEP_CANNOT_RUN",
                order=getattr(exc, "order", None),
                message=f"A step could not run on a sample of {loaded.file_name or 'the file'}: "
                f"{getattr(exc, 'message', None) or type(exc).__name__}",
            )
            findings.append(_finding(failed, item_id))
            continue
        for receipt in guarded.receipts:
            steps.append(_step_row(receipt, by_order[receipt.order]))
        findings.extend(
            _finding(f, by_order.get(f.order) if f.order is not None else None) for f in guarded.findings
        )
        ledger = guarded.ledger
        if ledger is not None:
            totals["rows_in"] += next(iter(guarded.receipts[0].rows_in.values()), 0)
            totals["rows_out"] += guarded.receipts[-1].rows_out
            if not names_a_log_row(key):
                customer_tables += 1
                counted = counted and ledger.unit == "customers"
                totals["customers_in"] += ledger.start
                totals["customers_out"] += ledger.end
            if rates == (None, None):
                rates = (ledger.rate_start, ledger.rate_end)
            # Only files counted in `rows_in` count here, so "the first N of M rows" compares like with like.
            sampled += loaded.file_rows or len(loaded.frame)
    if not steps:
        return PlanCheck(digest=digest, findings=tuple(findings))
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
    return PlanCheck(digest=digest, ledger=view, findings=tuple(findings))


def open_stops(check: PlanCheck | None, acknowledged: Sequence[Acknowledgement]) -> list[PlanFinding]:
    """The stops nobody has said yes to."""
    if check is None:
        return []
    said = {a.key for a in acknowledged}
    return [f for f in check.findings if f.level == "stop" and f.key not in said]


def acknowledged_codes(visit: CaseVisit | None) -> tuple[str, ...] | None:
    """The kinds of stop a person said yes to for the plan as counted: what an approved recipe may replay with.
    None when the plan was never counted (approved before the guards), so its recipe replays as it always did.
    """
    if visit is None or visit.plan_check is None:
        return None
    said = {a.key for a in visit.acknowledged}
    return tuple(sorted({f.code for f in visit.plan_check.findings if f.level == "stop" and f.key in said}))


def ledger_view(check: PlanCheck | None, acknowledged: Sequence[Acknowledgement]) -> dict[str, Any] | None:
    """`view.ledger` as the plan screen reads it; None until the plan has been counted. A stop a person said yes
    to is shown as a flag that says so, and carries `acknowledged`; every finding carries its `key`."""
    if check is None:
        return None
    said = {a.key for a in acknowledged}
    shown: list[dict[str, Any]] = []
    for found in sorted(check.findings, key=lambda f: _ORDER[f.level]):
        ack = found.level == "stop" and found.key in said
        shown.append(
            {
                "level": "flag" if ack else found.level,
                "kind": "guard",
                "key": found.key,
                "text": f"{found.text} (confirmed by a person)" if ack else found.text,
                "acknowledged": ack,
                "item_id": found.item_id,
                "code": found.code,
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
