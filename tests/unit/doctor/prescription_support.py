"""Shared fixtures for the M243 prescription tests: a customer file, its approved recipe, outcome, locale and money
basis, and the prescription they make (Plan O M243, DEC-1813)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

import numpy as np
import pandas as pd

from engine.agent.advisor import ordered_steps
from engine.agent.config import AgentLevel
from engine.agent.contracts import DataRecipe, RecipeReceipt, RecipeStep, RecipeStepKind, recipe_hash
from engine.doctor.currency import ApprovedRate, money_basis
from engine.doctor.locale import detect_locale
from engine.doctor.outcome_definition import OutcomeDefinition, OutcomeReceipt, ValueMeaning, approval_for
from engine.doctor.prescription import (
    FilePart,
    GoalSection,
    IntakeDecision,
    LocalePart,
    OutcomePart,
    Prescription,
    Provenance,
    SettingChoice,
    case_tables,
    make_prescription,
)
from engine.doctor.prescription_contract import DriftFinding, learn_contract
from engine.doctor.prescription_replay import ReplayFile, check_files, replay_after_build, run_part_recipe
from engine.doctor.prescription_runs import check_prepared
from engine.intake.contracts import IntakeReport

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
ANALYST = "u-analyst"
GOAL = "telco-churn"
SLOT = "customers"
RATE: dict[str, Any] = {
    "from": "USD",
    "to": "INR",
    "rate": 83.1,
    "rate_date": "2026-10-10",
    "source": "RBI reference rate, 10 Oct 2026",
}
CITIES = tuple(f"City {n:02d}" for n in range(30))


def customers(rows: int = 600, seed: int = 0) -> pd.DataFrame:
    """One month's customer file: a closed plan column, an open city column, Hinglish yes/no, dollar amounts, text
    numbers, a day-first join date, and a status the outcome is defined on."""
    rng = np.random.default_rng(seed)
    dollars = np.round(rng.gamma(2.0, 40.0, rows), 2)
    return pd.DataFrame(
        {
            "customer_id": [f"C{seed:02d}{i:05d}" for i in range(rows)],
            "plan": rng.choice(["basic", "standard", "premium"], rows),
            "city": [CITIES[i % len(CITIES)] for i in range(rows)],
            "active": rng.choice(["haan", "nahi", "yes", "no"], rows),
            "amount": [f"$ {value:,.2f}" for value in dollars],
            "spend": [f"{int(v):,}" for v in rng.integers(500, 50_000, rows)],
            "joined": [f"{13 + i % 16:02d}/{1 + i % 12:02d}/2026" for i in range(rows)],
            "status": rng.choice(["Active", "Churned", "Suspended"], rows, p=[0.6, 0.3, 0.1]),
        }
    )


def step(kind: RecipeStepKind, column: str, **params: Any) -> RecipeStep:
    return RecipeStep(order=1, kind=kind, column=column, params=params, reason="approved fix")


STEPS: tuple[RecipeStep, ...] = (
    step(RecipeStepKind.MAP_BOOLEAN, "active", true_values=["haan", "yes"], false_values=["nahi", "no"]),
    step(RecipeStepKind.CONVERT_CURRENCY, "amount", **RATE),
    step(RecipeStepKind.PARSE_NUMBER, "amount", unit="INR"),
    step(RecipeStepKind.PARSE_NUMBER, "spend"),
)


def recipe(steps: tuple[RecipeStep, ...] = STEPS, **extra: Any) -> DataRecipe:
    ordered = ordered_steps(steps)
    values: dict[str, Any] = {
        "recipe_id": "rc-approved",
        "use_case_id": GOAL,
        "source_fingerprint": "sha256:f",
        "steps": ordered,
        "recipe_hash": recipe_hash(ordered),
        "primary_key": "customer_id",
        "target": "status",
        "approved_by": ANALYST,
        "levels": (AgentLevel.CLEAN, AgentLevel.DERIVE),
        "max_failure_pct": 5.0,
        "created_at": NOW,
    }
    values.update(extra)
    return DataRecipe(**values)


def definition() -> OutcomeDefinition:
    return OutcomeDefinition(
        kind="column_values",
        name="status",
        column="status",
        map=(
            ValueMeaning(value="Active", meaning="no"),
            ValueMeaning(value="Churned", meaning="yes"),
            ValueMeaning(value="Suspended", meaning="exclude"),
        ),
        exclude=("Suspended",),
    )


def rate() -> ApprovedRate:
    return ApprovedRate(
        column="amount",
        from_currency="USD",
        to_currency="INR",
        rate=83.1,
        rate_date=date(2026, 10, 10),
        source="RBI reference rate, 10 Oct 2026",
        sentence="Convert amount from US dollars at 83.1.",
        approved_by=ANALYST,
        approved_at=NOW,
    )


def intake_report(**extra: Any) -> IntakeReport:
    values: dict[str, Any] = {"source_format": "csv", "applied": False}
    values.update(extra)
    return IntakeReport(**values)


def prescription(frame: pd.DataFrame | None = None, **parts: Any) -> Prescription:
    """The prescription approved on `frame` (September's file by default): one file slot, the locale and money
    basis, and the churn goal's section with its outcome."""
    frame = customers() if frame is None else frame
    the_recipe = parts.pop("recipe", recipe())
    prepared = run_part_recipe(the_recipe, frame, config=None, upload_id="u_sep", leak_check="full").frame
    profile = detect_locale(frame, client_id="default").profile
    basis = money_basis(the_recipe.steps, workspace_currency="INR", rates=[rate()])
    the_definition = definition()
    values: dict[str, Any] = {
        "files": (
            FilePart(
                slot=SLOT,
                upload_ids=("u_sep",),
                file_names=("customers_sep.csv",),
                intake=IntakeDecision.from_report(intake_report()),
                recipe=the_recipe,
                contract=learn_contract(prepared, primary_key="customer_id", target="status"),
            ),
        ),
        "locale": LocalePart(profile=profile, money_basis=basis),
        "goals": (
            GoalSection(
                goal_id=GOAL,
                active=True,
                brief_hash="b" * 64,
                outcome=OutcomePart(
                    definition=the_definition,
                    approval=approval_for(
                        the_definition, "The model learns 'status'.", user_id=ANALYST, at=NOW
                    ),
                ),
                roles={"key": "customer_id", "outcome": "status"},
                settings=(SettingChoice(path="split.test_fraction", value=0.2),),
            ),
        ),
        "provenance": Provenance(case_id="case_0123456789abcdef", client_id="default", created_at=NOW),
    }
    values.update(parts)
    return make_prescription(**values)


def october(**changes: Any) -> pd.DataFrame:
    """Next month's file: the same kind of file, other customers."""
    frame = customers(seed=1)
    for column, value in changes.items():
        frame[column] = value
    return frame


def replay_file(frame: pd.DataFrame, **extra: Any) -> ReplayFile:
    return ReplayFile(
        slot=SLOT, frame=frame, intake=extra.pop("intake", intake_report()), upload_id="u_oct", **extra
    )


@dataclass(frozen=True)
class Replayed:
    """A prescription run on new files by the run paths' own parts: the prepared rows and what each part did."""

    frame: pd.DataFrame | None
    frames: dict[str, pd.DataFrame]
    outcome: OutcomeReceipt | None
    recipe_receipts: tuple[RecipeReceipt, ...]
    warnings: tuple[DriftFinding, ...]
    replayed: tuple[str, ...]

    @property
    def questions(self) -> tuple[Any, ...]:
        return tuple(finding.question for finding in self.warnings)


def replay(
    approved: Prescription,
    files: Sequence[ReplayFile],
    *,
    mode: Literal["train", "score"],
    goal_id: str | None = None,
    config: Any = None,
) -> Replayed:
    """The file checks (`check_files`), each slot's recipe and contract, then the outcome, post-build recipe and
    contract on the final file (`replay_after_build`): what `prescription_runs` and the run routes do, in order.
    """
    warnings = list(check_files(approved, files))
    leak: Literal["full", "narrow"] = "full" if mode == "train" else "narrow"
    prepared: dict[str, list[pd.DataFrame]] = {}
    receipts: list[RecipeReceipt] = []
    ran: list[str] = []
    for file in files:
        part = approved.file(file.slot)
        assert part is not None, f"no slot {file.slot}"
        frame = file.frame
        if part.recipe is not None:
            run = run_part_recipe(
                part.recipe,
                frame,
                config=config,
                upload_id=file.upload_id or part.slot,
                leak_check=leak,
                contract=part.contract,
                slot=part.slot,
            )
            receipts.append(run.receipt)
            frame = run.frame
            ran.append("recipe")
        prepared.setdefault(part.slot, []).append(frame)
    frames = {
        slot: parts[0] if len(parts) == 1 else pd.concat(parts, ignore_index=True)
        for slot, parts in prepared.items()
    }
    for part in approved.files:
        if part.contract is not None:
            warnings.extend(check_prepared(part, frames[part.slot], mode=mode))
            ran.append("contract")
    final: pd.DataFrame | None = None
    if len(frames) == 1 or (
        approved.post_build is not None
        and any(step.kind is RecipeStepKind.SQL_QUERY for step in approved.post_build.steps)
    ):
        final = next(iter(frames.values()))  # a query reads the other files by name
    outcome: OutcomeReceipt | None = None
    if final is not None:
        after = replay_after_build(
            approved,
            final,
            mode=mode,
            goal_id=goal_id,
            config=config,
            tables=case_tables(frames, approved.files),
        )
        final, outcome = after.frame, after.outcome
        warnings.extend(after.warnings)
        ran.extend(after.replayed)
        if after.post_build is not None:
            receipts.append(after.post_build)
    return Replayed(
        frame=final,
        frames=frames,
        outcome=outcome,
        recipe_receipts=tuple(receipts),
        warnings=tuple(warnings),
        replayed=tuple(dict.fromkeys(ran)),
    )
