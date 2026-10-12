"""The notice tools (Plan O M245, DEC-1815; the design §5.5 I and N, §7).

| # | Tool | Kind | What it does |
|---|---|---|---|
| 72 | `drift_check` | READ | a file of the case against what was approved: the case's latest prescription (the slot the file fills: its recipe, then its column contract), or another file of the case; `MISSING_COLUMN`, `MEANING_CHANGED`, `UNIT_MISMATCH` (a lakh switch), `VALUE_DRIFT` (a doubled yes-rate), `NEW_CATEGORY`, `NEW_COLUMN` |
| 84 | `propose_pattern_fix` | PROPOSE | an AI-proposed fix of a new value pattern, one of the fixed step kinds, tried on the first 1,000 rows: before -> after, the values changed and those it could not map; pending, never ticked |

`propose_pattern_fix` proposes what the AI suggested, so its output's source is `ai` and the data policy's
`data.propose_pattern_fix` rule set decides it after it ran (`engine.doctor.registry._check_output`): only the fixed
step kinds, never on the ID, the outcome or a personal-data column, grounded in a look at the column (an earlier
result of this case, cited by its evidence id), tried, decision O10's change share (a warning above 30% of the column,
refused above 60%) and never ticked. Both tools serve the rules and the screen: the AI reaches the second only through
the notice detector (`engine.doctor.notice`), which relays the AI's suggestion, so Plan G's chat prompt is unchanged.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from api.access_policy import RoutePolicy
from engine.access.roles import Role
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.config import StrictBase
from engine.doctor.notice import DriftReport
from engine.doctor.pattern_fix import PATTERN_FIX_KINDS, PatternTrial
from engine.doctor.registry import (
    BY_EXPOSURE,
    DataTool,
    DataToolContext,
    DataToolError,
    DataToolKind,
    DataToolRegistry,
    Exposure,
)

__all__ = ["M245_TOOLS", "NOTICE_TOOL_CODES", "register_notice_tools"]

M245_TOOLS: Final[tuple[str, ...]] = ("drift_check", "propose_pattern_fix")
"""Every tool this module registers, in catalogue order."""

NOTICE_TOOL_CODES: Final[frozenset[str]] = frozenset({"DRIFT_NO_BASELINE"})
"""The codes M245 adds (for `PLAN_O_CODES` and `configs/pilot/help.yaml` at integration)."""

RULES_AND_SCREEN: Final[frozenset[str]] = frozenset({"rules", "person"})
LOOK_TOOLS: Final[frozenset[str]] = frozenset(
    {"inspect_column", "value_counts", "find_values", "describe_numbers", "describe_dates", "find_format_issues"}
)
"""Plan G's look tools whose result is "a look at the column" an AI's fix must cite (Plan L M152)."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# #72 drift_check
# ---------------------------------------------------------------------------
class DriftArgs(_Args):
    upload_id: str = Field(min_length=1, max_length=128, description="The file of the case to check.")
    against_upload_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Another file of the case to compare with; the case's latest prescription when omitted.",
    )
    mode: Literal["train", "score"] = Field(
        default="train", description="`score` leaves the outcome column unread."
    )


def _file(ctx: DataToolContext, upload_id: str) -> Any:
    found = next((f for f in ctx.files if f.upload_id == upload_id), None)
    if found is None:
        raise DataToolError("TOOL_INPUT_INVALID", f"{upload_id[:40]!r} is not a file of this case.", status=404)
    return found


def _no_baseline(message: str) -> DataToolError:
    return DataToolError("DRIFT_NO_BASELINE", message, status=409)


def _drift_check(args: DriftArgs, ctx: DataToolContext) -> DriftReport:
    from engine.doctor.case import CaseError, CaseStore
    from engine.doctor.notice import _baseline, _slot_for, contract_of, drift_check

    loaded = _file(ctx, args.upload_id)
    if args.against_upload_id is not None:
        other = _file(ctx, args.against_upload_id)
        return drift_check(
            loaded.frame,
            contract_of(other.frame, primary_key=ctx.primary_key),
            upload_id=loaded.upload_id,
            mode=args.mode,
            against="upload",
            against_upload_id=other.upload_id,
        )
    if ctx.case is None or ctx.storage is None:
        raise _no_baseline("There is nothing approved to compare with: name another file of the case.")
    try:
        case = CaseStore(ctx.storage).get(ctx.case.client_id, ctx.case.case_id)
    except CaseError as exc:
        raise _no_baseline("The case cannot be read, so there is nothing approved to compare with.") from exc
    baseline = _baseline(ctx.storage, case)
    if baseline is None:
        raise _no_baseline("Nothing has been approved in this case yet, so there is nothing to compare with.")
    part = _slot_for(baseline, loaded)
    if part is None or part.contract is None:
        raise _no_baseline(
            "This file is one the preparation was approved on, or it fills no part of it: nothing to compare."
        )
    return drift_check(
        loaded.frame,
        part.contract,
        upload_id=loaded.upload_id,
        mode=args.mode,
        recipe=part.recipe,
        slot=part.slot,
        prescription_hash=baseline.prescription_hash,
    )


# ---------------------------------------------------------------------------
# #84 propose_pattern_fix
# ---------------------------------------------------------------------------
class PatternFixArgs(_Args):
    column: str = Field(min_length=1, max_length=512, description="The column the fix rewrites.")
    kind: str = Field(min_length=1, max_length=40, description="One of the fixed step kinds.")
    params: dict[str, JsonValue] = Field(default_factory=dict, description="The step's frozen parameters.")
    reason: str = Field(default="", max_length=300, description="Why, in the AI's words (shown, never run).")
    evidence_ids: tuple[str, ...] = Field(
        default=(), max_length=8, description="The look at the column it is grounded in (earlier results)."
    )


class FixStep(StrictBase):
    kind: str
    column: str
    params: dict[str, JsonValue] = Field(
        default_factory=dict, description="Its parameters; a mapping is listed as `{from, to}` pairs."
    )
    reason: str = ""


class FixEffect(StrictBase):
    """Decision O10's count: values (or rows) changed and not read, out of how many, on how many rows."""

    changed: int = Field(ge=0)
    failed: int = Field(ge=0)
    out_of: int = Field(ge=0)
    rows_tried: int = Field(ge=0)


class PatternFixOutput(StrictBase):
    """A pending, unticked fix the AI proposed, with what it did on the first rows."""

    source: Literal["ai"] = "ai"
    ticked: Literal[False] = False
    confidence: Literal["check"] = "check"
    state: Literal["pending"] = "pending"
    step: FixStep
    looked_at: bool = Field(description="Whether it cites a look at its own column.")
    trial: PatternTrial
    effect: FixEffect
    share_pct: float = Field(ge=0.0, le=100.0, description="The share of the column O10 counts, in percent.")
    warning: str | None = Field(default=None, description="O10's warning above 30%, in plain words.")


def _pairs(params: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """A mapping keyed by spellings as `{from, to}` pairs: a cell is never a key of a result (`egress`)."""
    out: dict[str, JsonValue] = {}
    for name, value in params.items():
        if name in ("mapping", "merge") and isinstance(value, dict):
            out[name] = [{"from": str(k), "to": str(v)} for k, v in value.items()]
        elif name == "units" and isinstance(value, dict):
            out[name] = [[str(k), v] for k, v in value.items()]
        else:
            out[name] = value
    return out


def _looked_at(ctx: DataToolContext, column: str, cited: tuple[str, ...]) -> bool:
    wanted = set(cited)
    return any(
        result.evidence_id in wanted and result.tool in LOOK_TOOLS and result.input.get("column") == column
        for result in ctx.evidence
    )


def _propose_pattern_fix(args: PatternFixArgs, ctx: DataToolContext) -> PatternFixOutput:
    from engine.agent.recipe import RecipeError
    from engine.doctor.change_share import ChangeShareLimits, change_share_verdict
    from engine.doctor.pattern_fix import try_pattern
    from engine.doctor.policy import effect_of, policy_or_deny

    agent = ctx.require_agent()
    column = agent.resolve(args.column)
    allowed = {kind.value: kind for kind in PATTERN_FIX_KINDS}
    kind = allowed.get(args.kind)
    if kind is None:
        raise DataToolError(
            "AGENT_STEP_NOT_ALLOWED",
            f"{args.kind[:40]!r} is not a fix that may be suggested for a value pattern. Allowed: "
            f"{', '.join(sorted(allowed))}",
            status=403,
        )
    policy = policy_or_deny(ctx.root())
    try:
        trial = try_pattern(agent.frame, column, kind, args.params, trial_rows=policy.trial_rows)
    except RecipeError as exc:
        raise DataToolError(exc.code, exc.message[:300], status=422) from exc
    step = RecipeStep(order=1, kind=kind, column=column, params=dict(args.params))
    sample = agent.frame.head(policy.trial_rows)
    effect = effect_of(step, sample, changed=trial.changed, failed=trial.failed)
    counted = effect.failed if kind in policy.change_share.retyping_kinds else effect.changed
    from engine.agent.contracts import ProposalSource

    verdict = change_share_verdict(
        changed=counted,
        out_of=effect.out_of,
        source=ProposalSource.AI,
        limits=ChangeShareLimits(warn_pct=policy.change_share.warn_pct, refuse_pct=policy.change_share.refuse_pct),
    )
    return PatternFixOutput(
        step=FixStep(kind=kind.value, column=column, params=_pairs(dict(args.params)), reason=args.reason),
        looked_at=_looked_at(ctx, column, args.evidence_ids),
        trial=trial,
        effect=FixEffect(**dataclasses.asdict(effect)),
        share_pct=round(min(100.0, verdict.share_pct), 4),
        warning=verdict.message if verdict.level == "warn" else None,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_notice_tools(registry: DataToolRegistry) -> tuple[str, ...]:
    """Register M245's tools in `registry`; their names, in catalogue order."""
    from engine.doctor.policy import policy_kind_for

    registry.register(
        DataTool(
            number=72,
            name="drift_check",
            kind=DataToolKind.READ,
            description=(
                "Check a file of the case against what was approved: the latest prescription's contract (or another "
                "file of the case). Says which columns are gone, changed meaning, moved, switched unit or gained new "
                "values, each with its one question."
            ),
            input_model=DriftArgs,
            output_model=DriftReport,
            route_policy=RoutePolicy(
                role=Role.ANALYST, action="doctor.drift_check", purpose="ask Data Doctor about the data"
            ),
            exposure=Exposure.SESSION_ONLY,
            run=_drift_check,
            callers=RULES_AND_SCREEN,
            needs=("files",),
            max_output_bytes=BY_EXPOSURE,
            wraps=(
                "engine/doctor/prescription_contract.py check_contract; engine/doctor/prescription_replay.py "
                "run_part_recipe (the recipe contract learnt at approval, [RES] D1)"
            ),
        )
    )
    registry.register(
        DataTool(
            number=84,
            name="propose_pattern_fix",
            kind=DataToolKind.PROPOSE,
            description=(
                "Try a fix the AI suggested for a new value pattern - one of the fixed step kinds, never code - on the "
                "file's first 1,000 rows, and show it pending: before and after, the values changed and those it "
                "could not map."
            ),
            input_model=PatternFixArgs,
            output_model=PatternFixOutput,
            route_policy=RoutePolicy(
                role=Role.ANALYST,
                action="doctor.propose_pattern_fix",
                purpose="ask Data Doctor for a suggestion",
            ),
            exposure=Exposure.SESSION_ONLY,
            run=_propose_pattern_fix,
            callers=RULES_AND_SCREEN,
            needs=("agent",),
            max_output_bytes=BY_EXPOSURE,
            policy_kind=policy_kind_for("propose_pattern_fix"),
            wraps=(
                "engine/agent/recipe.py run_recipe (one step on a copy of the first rows); "
                "engine/doctor/change_share.py change_share_verdict (decision O10)"
            ),
        )
    )
    return M245_TOOLS
