"""Replay a prescription at training and at scoring, for files and built datasets (Plan O M243, DEC-1813).

The design §9.3. One function, :func:`replay_prescription`, runs every part of an approved prescription on new files -
next month's training file, a scoring file, a scheduled firing's - in a fixed order, and returns the prepared rows and
a receipt naming each part it replayed or checked:

1. **intake** - each file was read as approved (the same sheet, header row, intake on or off); new words read as empty
   are noted.
2. **locale** - the files still read as the approved locale profile says (number marks, date order, number and
   yes/no words, ...); M241's `detect_locale` on the frozen profile.
3. **currency** - every money column is still in the currencies the money basis (M242) froze; a `convert_currency`
   step replays with its frozen rate, date and source and is never proposed again.
4. **recipes** - each file slot's `DataRecipe`, under the levels and limit it was approved with; a `map_boolean`
   step's frozen spellings are checked first, so a new spelling is asked rather than failed quietly under the limit.
   Each prepared file is checked against its slot's column contract.
5. **build** - M237's `replay_build` through the same builder (training and scoring alike).
6. **outcome** (training only) - the approved definition (M239) by its hash: a column's frozen value map, or the
   builder's label for an outcome from events.
7. **post-build recipe** - for training **and** scoring (closing [INV] §8: a built dataset trains with it).
8. **contract** - the final prepared file against the prescription's contract.

**It stops and asks; it never re-learns.** A changed column, a new value, a new currency or a different locale raises
:class:`~engine.doctor.prescription.PrescriptionError` - `PRESCRIPTION_DRIFT`, `CURRENCY_FOREIGN` or `CURRENCY_MIXED`,
or the step's own `RECIPE_*`, `BUILD_*` or `OUTCOME_*` code - carrying what it found and the one question it asks. A
change that breaks nothing is a warning with a question, in the receipt. Nothing here writes a prescription, learns a
contract, proposes a step or calls an AI service: what was approved is what replays, or nothing does.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

import pandas as pd
from pydantic import Field

from engine.agent.contracts import DataRecipe, RecipeReceipt, RecipeStepKind
from engine.agent.recipe import RecipeError
from engine.config import StrictBase
from engine.doctor.build_driver import BuildDriveError, BuildDriveResult, replay_build
from engine.doctor.locale import SETTINGS, detect_locale
from engine.doctor.outcome_definition import OutcomeError, OutcomeReceipt, apply_outcome_values
from engine.doctor.prescription import (
    PRESCRIPTION_DRIFT,
    FilePart,
    IntakeDecision,
    Prescription,
    PrescriptionError,
    slot_name,
)
from engine.doctor.prescription_contract import (
    DataContract,
    DriftFinding,
    check_contract,
    drift_question,
    value_text,
)
from engine.intake.contracts import IntakeReport

if TYPE_CHECKING:
    from engine.agent.reshape import LeakCheck
    from engine.config import UseCaseConfig
    from engine.doctor.build_driver import BuilderPort

__all__ = [
    "Mode",
    "PartReceipt",
    "ReplayFile",
    "ReplayReceipt",
    "ReplayResult",
    "check_files",
    "drift_stop",
    "replay_on_prepared",
    "replay_prescription",
    "run_part_recipe",
    "step_drift",
]

Mode = Literal["train", "score"]
PartName = Literal["intake", "locale", "currency", "recipe", "build", "outcome", "post_build", "contract"]
_LOCALE_FIELDS: Final[tuple[str, ...]] = tuple(f for f in SETTINGS if f != "money_units")
"""The locale fields a replay compares; money columns are the currency check's."""


@dataclass(frozen=True)
class ReplayFile:
    """One new file, by the slot of the prescription it fills (several for a stacked source, oldest first)."""

    slot: str
    frame: pd.DataFrame
    intake: IntakeReport | None = None
    upload_id: str | None = None


class PartReceipt(StrictBase):
    """What the replay did with one part."""

    part: PartName
    slot: str | None = None
    status: Literal["replayed", "checked", "skipped"]
    detail: str = ""
    rows_in: int | None = None
    rows_out: int | None = None


class ReplayReceipt(StrictBase):
    """Every part replayed or checked, the warnings (each with its question), and the steps' own receipts."""

    prescription_hash: str
    mode: Mode
    goal_id: str | None = None
    parts: tuple[PartReceipt, ...] = ()
    warnings: tuple[DriftFinding, ...] = ()
    recipe_receipts: tuple[RecipeReceipt, ...] = ()
    build: BuildDriveResult | None = None
    outcome: OutcomeReceipt | None = None
    rows_out: int | None = Field(
        default=None, description="Rows of the final prepared file, when there is one."
    )

    def replayed(self) -> tuple[str, ...]:
        """The parts that ran or were checked, in order, each once."""
        return tuple(dict.fromkeys(p.part for p in self.parts if p.status != "skipped"))


@dataclass(frozen=True)
class ReplayResult:
    """The prepared rows (`frame`: the final file - the built dataset, or the one file - when there is one; `frames`:
    each slot's prepared file) and the receipt."""

    frame: pd.DataFrame | None
    frames: Mapping[str, pd.DataFrame]
    receipt: ReplayReceipt

    @property
    def warnings(self) -> tuple[DriftFinding, ...]:
        return self.receipt.warnings

    @property
    def questions(self) -> tuple[Any, ...]:
        return tuple(finding.question for finding in self.receipt.warnings)


@dataclass
class _Trail:
    parts: list[PartReceipt] = field(default_factory=list)
    warnings: list[DriftFinding] = field(default_factory=list)
    recipes: list[RecipeReceipt] = field(default_factory=list)

    def add(self, part: PartName, status: Literal["replayed", "checked", "skipped"], **extra: Any) -> None:
        self.parts.append(PartReceipt(part=part, status=status, **extra))

    def findings(self, found: Sequence[DriftFinding], *, part: str) -> None:
        """Warnings are kept; the first stop ends the replay with `PRESCRIPTION_DRIFT`."""
        stops = [f for f in found if f.stops]
        if stops:
            raise PrescriptionError(PRESCRIPTION_DRIFT, stops[0].summary, findings=found, part=part)
        self.warnings.extend(found)


def drift_stop(
    detector: Any,
    summary: str,
    question: str,
    *,
    part: str,
    slot: str | None = None,
    column: str | None = None,
    code: str = PRESCRIPTION_DRIFT,
    examples: Sequence[str] = (),
) -> PrescriptionError:
    finding = DriftFinding(
        detector=detector,
        stops=True,
        slot=slot,
        column=column,
        summary=summary,
        question=drift_question(detector, question, stops=True, slot=slot, column=column, examples=examples),
    )
    return PrescriptionError(code, summary, findings=(finding,), part=part)


# ---------------------------------------------------------------------------
# Raw files: intake, locale, currency
# ---------------------------------------------------------------------------
def _check_intake(part: FilePart, file: ReplayFile, trail: _Trail) -> None:
    if part.intake is None or file.intake is None:
        trail.add("intake", "skipped", slot=part.slot, detail="no intake decision to compare")
        return
    now = IntakeDecision.from_report(file.intake)
    frozen, found = part.intake.decisions(), now.decisions()
    changed = sorted(k for k in frozen if frozen[k] != found.get(k))
    if changed:
        words = ", ".join(changed)
        raise drift_stop(
            "INTAKE_CHANGED",
            f"The file for '{part.slot}' is read differently from the approved one ({words}).",
            f"The file for '{part.slot}' is laid out differently from the approved one ({words}). Is it the same "
            "kind of file?",
            part="intake",
            slot=part.slot,
        )
    new_words = {
        column: sorted(set(words) - set(part.intake.missing_words.get(column, ())))
        for column, words in now.missing_words.items()
    }
    new_words = {c: w for c, w in new_words.items() if w}
    if new_words:
        count = sum(len(w) for w in new_words.values())
        trail.warnings.append(
            DriftFinding(
                detector="INTAKE_CHANGED",
                stops=False,
                slot=part.slot,
                column=next(iter(sorted(new_words))),
                summary=f"{count} word{'s' if count != 1 else ''} meaning 'no value' are new in this file.",
                question=drift_question(
                    "INTAKE_CHANGED",
                    f"{count} new word{'s' if count != 1 else ''} in this file were read as empty. Is that right?",
                    stops=False,
                    slot=part.slot,
                    examples=[w for words in new_words.values() for w in words],
                ),
            )
        )
    trail.add("intake", "checked", slot=part.slot)


def _check_locale(prescription: Prescription, by_slot: Mapping[str, pd.DataFrame], trail: _Trail) -> None:
    locale = prescription.locale
    if locale is None or locale.profile is None:
        trail.add("locale", "skipped", detail="no locale profile in the prescription")
        return
    frozen = locale.profile
    detection = detect_locale(dict(by_slot), base=frozen, client_id=frozen.client_id)
    now = detection.profile.values()
    before = frozen.values()
    changed = sorted(
        {f for f in _LOCALE_FIELDS if now.get(f) != before.get(f)}
        | {c.field for c in detection.conflicts if c.field in _LOCALE_FIELDS}
    )
    if changed:
        words = ", ".join(f.replace("_", " ") for f in changed)
        raise drift_stop(
            "LOCALE_CHANGED",
            f"These files are written another way than the approved ones: {words}.",
            f"These files write {words} differently from the files that were approved. Which is right?",
            part="locale",
        )
    trail.add("locale", "checked", detail=f"profile {str(locale.profile_hash)[:19]}")


def _check_currency(prescription: Prescription, by_slot: Mapping[str, pd.DataFrame], trail: _Trail) -> None:
    from engine.doctor.currency import read_currencies

    locale = prescription.locale
    basis = None if locale is None else locale.money_basis
    if basis is None or not basis.columns:
        trail.add("currency", "skipped", detail="no money basis in the prescription")
        return
    for slot, frame in by_slot.items():
        for money in basis.columns:
            if money.column not in {str(c) for c in frame.columns}:
                continue
            (found,) = read_currencies(frame, columns=[money.column]) or (None,)
            if found is None:
                continue
            codes = set(found.codes)
            new = sorted(codes - set(money.written_in))
            if not new and not found.several:
                continue
            mixed = len(codes) > 1 or found.several > 0
            code = "CURRENCY_MIXED" if mixed else "CURRENCY_FOREIGN"
            raise drift_stop(
                code,
                f"'{money.column}' is written in {', '.join(new) or 'several currencies at once'}; the approved "
                f"preparation reads it in {', '.join(money.written_in)}.",
                f"'{money.column}' now holds amounts in {', '.join(new) or 'more than one currency'}. Which rate, "
                "or which column says the currency of each row? Amounts in different currencies are never added.",
                part="currency",
                slot=slot,
                column=money.column,
                code=code,
            )
    trail.add("currency", "checked", detail=f"basis {basis.basis_hash[:19]}")


def check_files(
    prescription: Prescription, files: Sequence[ReplayFile], *, trail: _Trail | None = None
) -> tuple[DriftFinding, ...]:
    """The raw-file checks (intake, locale, currency) on `files`; raises on a stop, returns the warnings."""
    own = trail if trail is not None else _Trail()
    by_slot = _frames_by_slot(files)
    for file in files:
        part = prescription.file(file.slot)
        if part is not None:
            _check_intake(part, file, own)
    _check_locale(prescription, by_slot, own)
    _check_currency(prescription, by_slot, own)
    return tuple(own.warnings)


def _frames_by_slot(files: Sequence[ReplayFile]) -> dict[str, pd.DataFrame]:
    grouped: dict[str, list[pd.DataFrame]] = {}
    for file in files:
        grouped.setdefault(file.slot, []).append(file.frame)
    return {
        slot: frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True)
        for slot, frames in grouped.items()
    }


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------
def _check_spellings(recipe: DataRecipe, frame: pd.DataFrame, *, slot: str | None) -> None:
    """A `map_boolean` step's frozen spellings: a value it has no meaning for stops (never a quiet failure)."""
    for step in recipe.steps:
        if step.kind is not RecipeStepKind.MAP_BOOLEAN or step.column not in frame.columns:
            continue
        known = {
            str(v).strip().casefold()
            for v in (*step.params.get("true_values", []), *step.params.get("false_values", []))  # type: ignore[misc]
        }
        texts = frame[step.column].dropna().map(value_text)
        texts = texts[texts != ""]
        unknown = sorted({t for t in texts if t.casefold() not in known})
        if unknown:
            rows = sum(1 for t in texts if t.casefold() not in known)
            raise drift_stop(
                "NEW_VALUE",
                f"'{step.column}' holds {len(unknown)} spelling{'s' if len(unknown) != 1 else ''} the approved yes/no "
                f"step has no meaning for, in {rows:,} row{'s' if rows != 1 else ''}.",
                f"'{step.column}' has {len(unknown)} new spelling{'s' if len(unknown) != 1 else ''}. Does each mean "
                "yes or no?",
                part="recipe",
                slot=slot,
                column=step.column,
                examples=unknown,
            )


def run_part_recipe(
    recipe: DataRecipe,
    frame: pd.DataFrame,
    *,
    config: UseCaseConfig | None,
    upload_id: str,
    leak_check: LeakCheck | None,
    contract: DataContract | None = None,
    slot: str | None = None,
    part: PartName = "recipe",
) -> Any:
    """One recipe, exactly as a saved recipe runs today (`run_saved_recipe`), with the levels and limit it was
    approved with. A step that fails on a column the contract knows is `PRESCRIPTION_DRIFT` (the column changed);
    otherwise the step's own code."""
    from engine.agent.config import AgentConfig
    from engine.agent.dataset_recipe import consistent_types, run_saved_recipe
    from engine.agent.recipe import run_recipe

    _check_spellings(recipe, frame, slot=slot)
    try:
        if config is not None:
            return run_saved_recipe(frame, recipe, config, upload_id=upload_id, leak_check=leak_check)
        agent = AgentConfig()
        return run_recipe(
            consistent_types(frame),
            recipe.steps,
            upload_id=upload_id,
            primary_key=recipe.primary_key,
            target=recipe.target,
            levels=recipe.levels if recipe.levels is not None else tuple(agent.levels),
            max_failure_pct=(
                recipe.max_failure_pct
                if recipe.max_failure_pct is not None
                else agent.max_conversion_failure_pct
            ),
            snapshot_column=recipe.snapshot_column,
            leak_check=leak_check,
        )
    except RecipeError as exc:
        raise step_drift(exc, contract, part=part, slot=slot) from exc


def step_drift(
    exc: RecipeError, contract: DataContract | None, *, part: str, slot: str | None = None
) -> PrescriptionError:
    """A step that cannot run: `PRESCRIPTION_DRIFT` when the column it reads is one the contract knows (the column
    is gone, or changed), else the step's own code."""
    column = exc.column
    if contract is None or column is None or contract.column(column) is None:
        return PrescriptionError(exc.code, exc.message, part=part)
    if exc.code == "RECIPE_COLUMN_MISSING":
        return drift_stop(
            "MISSING_COLUMN",
            f"'{column}' is not in this file; step {exc.order} of the approved preparation reads it.",
            f"This file has no column '{column}'. Is it called something else now?",
            part=part,
            slot=slot,
            column=column,
        )
    return drift_stop(
        "MEANING_CHANGED",
        f"Step {exc.order} of the approved preparation cannot run on '{column}' any more ({exc.code}): {exc.message}",
        f"'{column}' changed since the preparation was approved, and step {exc.order} cannot run on it. What does the "
        "column hold now?",
        part=part,
        slot=slot,
        column=column,
    )


def _leak(mode: Mode) -> LeakCheck:
    return "full" if mode == "train" else "narrow"


# ---------------------------------------------------------------------------
# After the build: outcome, post-build recipe, contract
# ---------------------------------------------------------------------------
def replay_on_prepared(
    prescription: Prescription,
    frame: pd.DataFrame,
    *,
    mode: Mode,
    goal_id: str | None = None,
    config: UseCaseConfig | None = None,
    upload_id: str = "prescription",
    trail: _Trail | None = None,
) -> tuple[pd.DataFrame, OutcomeReceipt | None, tuple[DriftFinding, ...]]:
    """Steps 6-8 on a prepared file (the built dataset, or the one prepared file): the outcome (training only), the
    post-build recipe, the contract. Raises on a stop; returns the rows, the outcome's receipt and the warnings.
    """
    own = trail if trail is not None else _Trail()
    outcome_receipt: OutcomeReceipt | None = None
    section = prescription.section(goal_id)
    outcome = None if section is None else section.outcome
    if mode == "score" or outcome is None:
        own.add("outcome", "skipped", detail="scoring" if mode == "score" else "no outcome definition")
    elif outcome.definition.kind == "column_values":
        if outcome.approval is None:
            raise PrescriptionError(
                "OUTCOME_NOT_APPROVED", "The outcome in this prescription has no approval.", part="outcome"
            )
        try:
            frame, outcome_receipt = apply_outcome_values(frame, outcome.definition, outcome.approval)
        except OutcomeError as exc:
            raise PrescriptionError(exc.code, exc.message, part="outcome") from exc
        own.add(
            "outcome",
            "replayed",
            detail=f"definition {outcome.definition.definition_hash[:16]}",
            rows_in=outcome_receipt.rows_in,
            rows_out=outcome_receipt.rows_out,
        )
    else:
        own.add(
            "outcome",
            "checked" if prescription.build is not None else "skipped",
            detail="built by the builder from the approved definition",
        )
    if prescription.post_build is not None:
        rows_in = len(frame)
        run = run_part_recipe(
            prescription.post_build,
            frame,
            config=config,
            upload_id=upload_id,
            leak_check=_leak(mode),
            contract=prescription.contract,
            part="post_build",
        )
        own.recipes.append(run.receipt)
        frame = run.frame
        own.add("post_build", "replayed", rows_in=rows_in, rows_out=len(frame))
    else:
        own.add("post_build", "skipped", detail="no post-build recipe")
    if prescription.contract is not None:
        own.findings(check_contract(frame, prescription.contract, mode=mode), part="contract")
        own.add("contract", "checked")
    return frame, outcome_receipt, tuple(own.warnings)


# ---------------------------------------------------------------------------
# The whole replay
# ---------------------------------------------------------------------------
def replay_prescription(
    prescription: Prescription,
    files: Sequence[ReplayFile],
    *,
    mode: Mode,
    goal_id: str | None = None,
    config: UseCaseConfig | None = None,
    builder: BuilderPort | None = None,
    load_built: Callable[[str], pd.DataFrame] | None = None,
    clock: Any = None,
) -> ReplayResult:
    """Every part of `prescription` on `files`, in the design's order (see the module docstring).

    `builder` and `load_built` replay a build part (the builder's port, and how to read the dataset it builds);
    `config` is the use case a recipe saved without its levels runs under. Raises `PrescriptionError` on any stop.
    """
    trail = _Trail()
    given = {file.slot for file in files}
    unknown = sorted(given - {part.slot for part in prescription.files})
    missing = [part.slot for part in prescription.files if part.slot not in given]
    if unknown or missing:
        slot = (unknown or missing)[0]
        raise drift_stop(
            "MISSING_FILE",
            (
                f"The approved preparation has no place for '{slot}'."
                if unknown
                else f"The approved preparation reads a file for '{slot}', and none was given."
            ),
            "Which approved file is this?" if unknown else f"Where is the file for '{slot}'?",
            part="intake",
            slot=slot,
        )
    check_files(prescription, files, trail=trail)
    prepared: dict[str, list[pd.DataFrame]] = {}
    for file in files:
        part = prescription.file(file.slot)
        assert part is not None  # every slot was matched above
        frame = file.frame
        if part.recipe is not None:
            run = run_part_recipe(
                part.recipe,
                frame,
                config=config,
                upload_id=file.upload_id or part.slot,
                leak_check=_leak(mode),
                contract=part.contract,
                slot=part.slot,
            )
            trail.recipes.append(run.receipt)
            trail.add("recipe", "replayed", slot=part.slot, rows_in=len(frame), rows_out=len(run.frame))
            frame = run.frame
        else:
            trail.add("recipe", "skipped", slot=part.slot, detail="no recipe for this file")
        prepared.setdefault(part.slot, []).append(frame)
    frames = {
        slot: parts[0] if len(parts) == 1 else pd.concat(parts, ignore_index=True)
        for slot, parts in prepared.items()
    }
    for part in prescription.files:
        if part.contract is not None:
            trail.findings(
                check_contract(frames[part.slot], part.contract, mode=mode, slot=part.slot), part="recipe"
            )
            trail.add("contract", "checked", slot=part.slot)
    built: BuildDriveResult | None = None
    final: pd.DataFrame | None = None
    if prescription.build is not None:
        if builder is None or load_built is None:
            raise PrescriptionError(
                "BUILD_REPLAY_FILES",
                "This preparation builds a dataset: the builder is needed to replay it.",
                part="build",
            )
        record = prescription.build.record
        sources = {
            source.source_name: prepared.get(slot_name(source.source_name), [])
            for source in (record.plan.entity, *record.plan.events)
        }
        try:
            built = replay_build(record, sources, builder, mode=mode, clock=clock)
        except BuildDriveError as exc:
            raise PrescriptionError(exc.code, exc.message, part="build") from exc
        if built.dataset_id is None:
            raise PrescriptionError("BUILD_STEP_FAILED", "The builder built no dataset.", part="build")
        final = load_built(built.dataset_id)
        trail.add("build", "replayed", detail=f"plan {record.plan.plan_hash[:16]}", rows_out=len(final))
    else:
        trail.add("build", "skipped", detail="no build in the prescription")
        if len(frames) == 1:
            final = next(iter(frames.values()))
    outcome_receipt: OutcomeReceipt | None = None
    if final is not None:
        final, outcome_receipt, _ = replay_on_prepared(
            prescription, final, mode=mode, goal_id=goal_id, config=config, trail=trail
        )
    receipt = ReplayReceipt(
        prescription_hash=prescription.prescription_hash,
        mode=mode,
        goal_id=goal_id if goal_id is not None else getattr(prescription.section(), "goal_id", None),
        parts=tuple(trail.parts),
        warnings=tuple(trail.warnings),
        recipe_receipts=tuple(trail.recipes),
        build=built,
        outcome=outcome_receipt,
        rows_out=None if final is None else len(final),
    )
    return ReplayResult(frame=final, frames=frames, receipt=receipt)
