"""The one findings ledger: every stop the visit knows, in one contract (root-cause plan, F3).

A stop used to live in six places, each in its own shape: the guards' and readiness' findings in the plan's check, a
query trial's findings inside the item's payload, a detector's doubt, the refusal that stopped the visit (`VisitStop`).
Only the first could be said yes to, and the screen, the approval gate and the AI each read a different subset.

:class:`Finding` is the one contract: `code`, `subject`, `level` (`hard`: a rule nobody may say yes to; `stop`: a
person may say yes; `flag`: shown, never blocks), `scope`, `count`, the measured numbers, and the `resolutions` the
person (or the AI) has: acknowledge, answer, fix, ask the AI to fix it. :func:`findings_of` reads them all for a case;
:func:`blocking` is the one gate (`approve`, the screen and the ready card read it); :func:`screen_ledger` is
`view.ledger` with every finding in it. Nothing is stored twice: a yes is still an `Acknowledgement` in the visit, by
the finding's key, and holds while the count stays at or below the one said yes to.

Names, counts and codes only; no cell value, no provider text.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import Field

from engine.agent.contracts import ProposalState
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.config import StrictBase
from engine.doctor.plan_check import ledger_view, plan_digest, stop_is_open
from engine.doctor.visit_contracts import Acknowledgement, PlanCheck, PlanFinding

if TYPE_CHECKING:
    from engine.doctor.case import Case, CaseItem

__all__ = [
    "Finding",
    "Resolution",
    "blocking",
    "find",
    "findings_of",
    "screen_ledger",
]

Level = Literal["hard", "stop", "flag"]
Source = Literal["guard", "readiness", "sql_trial", "doubt", "visit"]
_ORDER: Final[dict[str, int]] = {"hard": 0, "stop": 1, "flag": 2}
_DOUBTS_KEPT: Final[int] = 20


class Resolution(StrictBase):
    """One way to clear a finding: say yes to it, answer a question, change a step, or ask the AI to."""

    kind: Literal["acknowledge", "answer", "fix", "ai_may_fix", "retry"]
    label: str
    item_ids: tuple[str, ...] = Field(default=(), description="The steps a fix would change.")
    question_id: str | None = None


class Finding(StrictBase):
    """A stop, flag or hard rule about one subject, whoever found it."""

    key: str = Field(
        description="What a yes is tied to: this code about this subject, never its count (a plan finding keeps "
        "the key the guards gave it)."
    )
    code: str
    subject: str = Field(description="What it is about: a step (item id), a column, `plan`, or `visit`.")
    level: Level
    source: Source
    scope: Literal["sample", "full"] = "sample"
    count: int | None = None
    measured: dict[str, int | float] = Field(default_factory=dict, description="The numbers, by name.")
    text: str = Field(description="Plain words with counts; never a cell value.")
    detail: str | None = None
    item_id: str | None = None
    column: str | None = None
    acknowledged: bool = False
    resolutions: tuple[Resolution, ...] = ()

    @property
    def acknowledgeable(self) -> bool:
        return self.level == "stop" and any(r.kind == "acknowledge" for r in self.resolutions)

    @property
    def blocks(self) -> bool:
        """Whether approval waits for it: a hard rule always, a stop until someone said yes."""
        return self.level == "hard" or (self.level == "stop" and not self.acknowledged)


def _key(code: str, subject: str) -> str:
    return sha256_hex(canonical_bytes([code, subject]))[:16]


def _measured(count: int | None) -> dict[str, int | float]:
    return {} if count is None else {"count": count}


def _resolutions(*, hard: bool, item_id: str | None, ai: bool = True) -> tuple[Resolution, ...]:
    out: list[Resolution] = []
    if not hard:
        out.append(Resolution(kind="acknowledge", label="Accept this and go on"))
    ids = (item_id,) if item_id else ()
    out.append(Resolution(kind="fix", label="Change the plan or the file", item_ids=ids))
    if ai:
        out.append(Resolution(kind="ai_may_fix", label="Ask Data Doctor to fix it", item_ids=ids))
    return tuple(out)


def _from_plan(found: PlanFinding, said: Sequence[Acknowledgement]) -> Finding:
    level: Level = "flag" if found.level != "stop" else ("hard" if found.hard else "stop")
    return Finding(
        key=found.key,
        code=found.code,
        subject=found.item_id or found.column or "plan",
        level=level,
        source="readiness" if found.source == "readiness" else "guard",
        scope=found.scope,
        count=found.count,
        measured=_measured(found.count),
        text=found.text,
        detail=found.detail,
        item_id=found.item_id,
        column=found.column,
        acknowledged=level == "stop" and not stop_is_open(found, said),
        resolutions=_resolutions(hard=level == "hard", item_id=found.item_id) if level != "flag" else (),
    )


def _holds(said: Sequence[Acknowledgement], key: str, count: int | None) -> bool:
    from engine.doctor.guards import holds

    return any(a.key == key and holds(a.count, count) for a in said)


def _from_trials(
    items: Sequence[CaseItem], said: Sequence[Acknowledgement], told: set[tuple[str, str | None]]
) -> list[Finding]:
    """The stops a query's trial wrote into its item's payload (`tools_sql.SqlStepProposal.payload`): the plan's own
    count of the same code about the same step says it better and takes its place."""
    out: list[Finding] = []
    for item in items:
        if item.state is not ProposalState.ACCEPTED:
            continue
        raw = item.payload.get("findings")
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict) or entry.get("level") != "stop":
                continue
            code, text = entry.get("code"), entry.get("message")
            if not isinstance(code, str) or not isinstance(text, str) or (code, item.item_id) in told:
                continue
            count = entry.get("count")
            number = count if isinstance(count, int) and not isinstance(count, bool) else None
            key = _key(code, item.item_id)
            detail = entry.get("detail")
            out.append(
                Finding(
                    key=key,
                    code=code,
                    subject=item.item_id,
                    level="stop",
                    source="sql_trial",
                    count=number,
                    measured=_measured(number),
                    text=text,
                    detail=detail if isinstance(detail, str) else None,
                    item_id=item.item_id,
                    acknowledged=_holds(said, key, number),
                    resolutions=_resolutions(hard=False, item_id=item.item_id),
                )
            )
    return out


def findings_of(
    case: Case, check: PlanCheck | None = None, acknowledged: Sequence[Acknowledgement] | None = None
) -> tuple[Finding, ...]:
    """Every finding the case holds, worst first: the plan's check (`check`, else the one stored, when it is for the
    plan as it stands), the stops of an accepted query's trial until the plan is counted on every row, the doubts,
    and the refusal that stopped the visit. `acknowledged` replaces the yeses the case holds (a plan that was just
    counted again has none of an older plan's)."""
    from engine.doctor.visit import active_section

    visit = case.visit
    items, _ = active_section(case)
    said = acknowledged if acknowledged is not None else (visit.acknowledged if visit is not None else ())
    counted = check if check is not None else (visit.plan_check if visit is not None else None)
    if counted is not None and counted.digest != plan_digest(case.files, items):
        counted = None
    out = [_from_plan(f, said) for f in counted.findings] if counted is not None else []
    if counted is None or counted.basis is None:  # a count of every row supersedes what a trial saw
        out.extend(_from_trials(items, said, {(f.code, f.item_id) for f in out}))
    for doubt in case.doubts[-_DOUBTS_KEPT:]:
        key = _key("DOUBT", doubt.doubt_id)
        out.append(
            Finding(
                key=key,
                code="DOUBT",
                subject=doubt.item_id or doubt.upload_id or doubt.doubt_id,
                level="flag",
                source="doubt",
                text=doubt.because,
                detail=f"{doubt.would_do} {doubt.if_not}".strip(),
                item_id=doubt.item_id,
                acknowledged=any(a.key == key for a in said),
            )
        )
    stopped = visit.stopped if visit is not None else None
    if stopped is not None:
        out.append(
            Finding(
                key=_key(stopped.code, "visit"),
                code=stopped.code,
                subject="visit",
                level="stop",
                source="visit",
                text=stopped.message,
                resolutions=(
                    Resolution(kind="retry", label="Try again"),
                    Resolution(kind="fix", label="Add a different file or change the plan"),
                ),
            )
        )
    return tuple(sorted(out, key=lambda f: _ORDER[f.level]))


def blocking(
    case: Case, check: PlanCheck | None = None, acknowledged: Sequence[Acknowledgement] | None = None
) -> list[Finding]:
    """What approval waits for: the hard rules and the stops nobody has said yes to."""
    return [f for f in findings_of(case, check, acknowledged) if f.blocks]


def find(case: Case, key: str, check: PlanCheck | None = None) -> Finding | None:
    return next((f for f in findings_of(case, check) if f.key == key and f.source != "doubt"), None)


def screen_ledger(case: Case) -> dict[str, Any] | None:
    """`view.ledger`: the plan's count as ever (`plan_check.ledger_view`) with the findings of every source, each
    with its `subject`, `measured` and `resolutions`; None when there is nothing to show."""
    from engine.doctor.visit import active_section

    visit = case.visit
    counted = visit.plan_check if visit is not None else None
    if counted is not None and counted.digest != plan_digest(case.files, active_section(case)[0]):
        counted = None
    base = (
        ledger_view(counted, visit.acknowledged if visit is not None else ()) if counted is not None else None
    )
    everything = [f for f in findings_of(case) if f.source != "doubt"]
    if base is None and not everything:
        return None
    shown = {f.key: f for f in everything}
    rows: list[dict[str, Any]] = []
    for row in (base or {}).get("findings", []):
        found = shown.pop(row["key"], None)
        rows.append(row if found is None else {**row, **_extras(found)})
    rows.extend(_row(f) for f in shown.values())
    return {**(base or {}), "findings": rows}


def _extras(found: Finding) -> dict[str, Any]:
    return {
        "subject": found.subject,
        "source": found.source,
        "measured": found.measured,
        "resolutions": [r.model_dump(mode="json") for r in found.resolutions],
    }


def _row(found: Finding) -> dict[str, Any]:
    """A finding that is not the plan check's, in the shape the plan screen already reads."""
    return {
        "level": "flag" if found.acknowledged else ("stop" if found.level == "hard" else found.level),
        "kind": "readiness" if found.source == "readiness" else "guard",
        "key": found.key,
        "text": " ".join(
            filter(None, [found.text, found.detail, "(confirmed by a person)" if found.acknowledged else ""])
        ),
        "acknowledged": found.acknowledged,
        "item_id": found.item_id,
        "code": found.code,
        "count": found.count,
        "scope": found.scope,
        "hard": found.level == "hard",
        "acknowledgeable": found.acknowledgeable,
        **_extras(found),
    }
