"""Question 15: "This looks like religion, health or caste. Use it?", asked by the visit's rules, no AI.

`engine.privacy.sensitive` finds the columns (our code, from word lists). For each one the visit asks once:

* **Fairness report only (recommended)** - the column is not used to choose customers and is not shown to the AI;
  the fairness report still compares groups by it (`evaluation.fairness_column`).
* **Remove it** - a `drop_column` step takes it out of the data.
* **Use it** - a `pii_keep` item, which needs an approver's yes, and then `prepare.allow_sensitive_columns`.

Until the person answers, the column stays out of the features (`engine.stages.prepare`) and out of what the AI
sees (`engine.agent.tools.AgentContext`), so an unanswered question is the safe choice, never a leak.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from engine.agent.contracts import ProposalState, RecipeStep, RecipeStepKind
from engine.doctor.protocol import Ask, AskOption
from engine.doctor.visit_contracts import OptionEffect
from engine.privacy.sensitive import FAIRNESS_KIND, SENSITIVE_QUESTION, SENSITIVE_USES, find_sensitive

if TYPE_CHECKING:
    from engine.doctor.case import Case
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief
    from engine.doctor.visit import Detected, DetectorPort

__all__ = ["sensitive_ask", "sensitive_detector", "sensitive_overrides"]

_USE_KIND: Final[str] = "pii_keep"


def sensitive_detector() -> DetectorPort:
    """The visit's sensitive-column detector: one question for each sensitive column of each file."""

    def detect(case: Case, files: tuple[LoadedFile, ...], brief: GoalBrief | None) -> Detected:
        from engine.doctor.visit import Detected

        del brief, case
        asks = tuple(
            sensitive_ask(loaded.upload_id, found.column, found.reason(), len(loaded.frame))
            for loaded in files
            for found in find_sensitive(loaded.frame)
        )
        return Detected(asks=asks)

    return detect


def sensitive_ask(upload_id: str, column: str, reason: str, rows: int) -> Ask:
    """The question about one sensitive column, with the three answers and what each does."""
    options: list[AskOption] = []
    for index, (use, label) in enumerate(SENSITIVE_USES):
        option_id = f"c{index}"
        payload: dict[str, object] = {"upload_id": upload_id, "column": column, "use": use}
        if use == "remove":
            step = RecipeStep(
                order=1,
                kind=RecipeStepKind.DROP_COLUMN,
                column=column,
                reason=f"'{column}' looks sensitive, so it is removed.",
            )
            effect = OptionEffect(
                option_id=option_id,
                kind="recipe_step",
                title=f"Remove '{column}' from the data.",
                payload={
                    "upload_id": upload_id,
                    "path": None,
                    "value": None,
                    "step": step.model_dump(mode="json"),
                },
            )
        elif use == "use":
            effect = OptionEffect(
                option_id=option_id,
                kind=_USE_KIND,
                title=f"Use '{column}' to choose customers (a sensitive column).",
                payload=payload,  # type: ignore[arg-type]
            )
        else:
            effect = OptionEffect(
                option_id=option_id,
                kind=FAIRNESS_KIND,
                title=f"Keep '{column}' for the fairness report only; it does not choose customers.",
                payload=payload,  # type: ignore[arg-type]
            )
        options.append(AskOption(label=label, effect=effect, safe=use == "fairness_only"))
    return Ask(
        code=SENSITIVE_QUESTION,
        subject={"upload_id": upload_id, "column": column},
        text=f"{reason} Use it?",
        why="Religion, caste, health and the like are never used to choose customers unless you say so.",
        options=tuple(options),
        rows=rows,
        upload_id=upload_id,
        column=column,
        recommended="c0",
    )


def sensitive_overrides(case: Case) -> dict[str, object]:
    """The run settings the case's answers set: the allowed columns, and the column for the fairness report.

    Read from the ticked items only. `prepare.allow_sensitive_columns` lists the columns the person said to use;
    `evaluation.fairness_column` is the first column they kept for the fairness report. Empty when nothing was
    answered, in which case every sensitive column stays out.
    """
    allowed: list[str] = []
    fairness: list[str] = []
    for item in case.items:
        if item.state is not ProposalState.ACCEPTED:
            continue
        column = item.payload.get("column")
        if not isinstance(column, str):
            continue
        if item.kind == _USE_KIND and item.payload.get("use") == "use":
            allowed.append(column)
        elif item.kind == FAIRNESS_KIND:
            fairness.append(column)
    out: dict[str, object] = {}
    if allowed:
        out["prepare.allow_sensitive_columns"] = list(dict.fromkeys(allowed))
    if fairness:
        out["evaluation.fairness_column"] = fairness[0]
    return out
