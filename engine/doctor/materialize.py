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
* **the key and the outcome** the guards count by (:func:`roles_of`, over :mod:`engine.doctor.decisions`),
  **the case's as-of date** (``decisions(case).as_of``; a query keeps ``$as_of`` symbolic and is bound to it here),
  and **the future-data check** a training file gets;
* **the rows** - :class:`Sample` is a seeded random sample, the same for every caller with the same scope;
  :class:`Full` is every row the caller loaded.

It never swallows an error: a step that cannot run, or a refusal of the guards, is a stop :class:`PlanFinding`, and
a finding's key is its code and subject only (never its count), so a yes given on a sample still matches the whole
file. Findings hold counts, codes and names only - never a cell value.
"""

from __future__ import annotations

import contextlib
import functools
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from engine.agent.config import AgentConfig
from engine.agent.contracts import DataRecipe, RecipeStep, RecipeStepKind, recipe_hash
from engine.agent.dataset_recipe import consistent_types
from engine.agent.recipe import STEP_PHASE, RecipeError, RecipeRun, run_recipe
from engine.agent.sql_step import SqlStepError, sql_params
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.doctor.guards import Finding, GuardedRun, Ledger, Receipt, ReceiptMemo, run_guarded
from engine.doctor.visit_contracts import PlanFinding
from engine.utils.time import utc_now

if TYPE_CHECKING:
    import pandas as pd

    from engine.agent.config import AgentLevel
    from engine.agent.reshape import LeakCheck
    from engine.doctor.case import Case, CaseFile, CaseItem
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.prescription import PrescriptionError
    from engine.doctor.prescription_contract import DataContract

__all__ = [
    "CANDIDATE",
    "SAMPLE_SEED",
    "UNCHECKED_SETTINGS_NOTE",
    "ApprovedSettingsError",
    "Full",
    "NewSpellingError",
    "Prepared",
    "PreparedFile",
    "Sample",
    "Scope",
    "StepFailure",
    "Watch",
    "aliased",
    "check_approved_settings",
    "check_spellings",
    "materialize",
    "materialize_plan",
    "open_findings",
    "replay_recipe",
    "replay_steps",
    "roles_of",
    "scoped",
    "step_drift",
    "stop_finding",
    "table_names",
]

SAMPLE_SEED: Final[int] = 20_260_101
"""The seed every sample of the plan is drawn with: the same rows for every caller of the same scope."""
CANDIDATE: Final[str] = "candidate"
"""The item id of the step a caller asks about (:func:`materialize`'s `candidate`)."""
UNCHECKED_SETTINGS_NOTE: Final[str] = (
    "No approved number, date or currency settings are stored with this recipe, so this file's were not "
    "compared with the ones that were approved."
)
HARD_CODES: Final[frozenset[str]] = frozenset({"PLAN_STEP_CANNOT_RUN"})
"""The guards' stops no yes can lift: a step that cannot run is not a loss a person may accept."""
_HARD_CODES: Final[frozenset[str]] = HARD_CODES
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
class StepFailure:
    """A step that could not run, as the engine said it: the code and the plain-English sentence (never a cell)."""

    upload_id: str
    code: str
    message: str
    order: int | None
    item_id: str | None


Watch = Callable[[str, str, RecipeStep, "pd.DataFrame", "pd.DataFrame"], None]
"""`(upload id, item id, step, rows before, rows after)` after each step, for a caller that samples what it did."""


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
    findings: tuple[PlanFinding, ...] = ()
    """What the guards found about this file's steps (also in :attr:`Prepared.findings`)."""


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
    failures: tuple[StepFailure, ...] = ()
    """Why each of them could not run (the engine's own code and sentence)."""

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


ALIAS_KIND: Final[str] = "column_alias"
"""The item kind of a person's yes to "this column is the one the plan knew under another name" (next month's
recheck): payload `{"column": <name in this file>, "as": <name the plan reads>}`."""


def aliased(files: Sequence[LoadedFile], items: Sequence[CaseItem]) -> list[LoadedFile]:
    """`files` with every accepted alias applied: a column the person said is the plan's column under a new name is
    read by the plan's name, so the plan runs on next month's file exactly as it was approved (in memory only).
    """
    from dataclasses import replace

    from engine.agent.contracts import ProposalState

    out: list[LoadedFile] = []
    for loaded in files:
        names = {
            str(i.payload["column"]): str(i.payload["as"])
            for i in items
            if i.kind == ALIAS_KIND
            and i.state is ProposalState.ACCEPTED
            and i.upload_id in (loaded.upload_id, None)
            and isinstance(i.payload.get("column"), str)
            and isinstance(i.payload.get("as"), str)
            and i.payload["column"] in loaded.frame.columns
            and i.payload["as"] not in loaded.frame.columns
        }
        out.append(replace(loaded, frame=loaded.frame.rename(columns=names)) if names else loaded)
    return out


def roles_of(
    items: Sequence[CaseItem],
    upload_id: str,
    columns: Collection[str],
    *,
    candidate_made: Sequence[str] | None = None,
) -> tuple[str | None, str | None]:
    """`(key, outcome)` the guards count this file by: the accepted ID and outcome items that name a column of the
    file; the outcome may come from an accepted query's table (one row per customer), whose ID then follows.

    `candidate_made` are the columns of the table of a query being tried: it is read as if it were accepted, by the
    very same rule, so a trial and the run after the yes resolve the roles alike.
    """
    from engine.doctor.decisions import decisions_from
    from engine.doctor.prescription import prepared_key, query_makes

    decided = decisions_from(items)
    key = decided.column("key", upload_id, columns)
    target = decided.column("outcome", upload_id, columns)
    also = () if candidate_made is None else (candidate_made,)
    if key is None:
        # an ID a query makes (the row number the person said yes to) is the file's once the steps have run
        named = decided.declared("key", upload_id)
        if named is not None and query_makes(items, named, also=also):
            key = named
    if target is None:
        declared = decided.declared("outcome", upload_id)
        if declared is not None and query_makes(items, declared, also=also):
            target, key = declared, prepared_key(items, declared, key, also=also)
    if key is not None:
        # a query's table is the file once the steps have run: the ID follows it, outcome or not
        key = prepared_key(items, target, key, also=also, uploads={upload_id})
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
    watch: Watch | None = None,
    memo: ReceiptMemo | None = None,
) -> Prepared:
    """The case's plan run over `files` at `scope`, with the case's as-of date. `items` are the plan's (the active
    section). `candidate` is `(upload id, step)`: a step to try as if it were accepted (a query trial)."""
    from engine.doctor.decisions import decisions

    decided = decisions(case)
    return materialize_plan(
        case.files,
        items,
        files,
        scope,
        as_of=decided.as_of_date,
        training_as_of=decided.training_as_of_date,
        candidate=candidate,
        twice=twice,
        watch=watch,
        memo=memo,
    )


def materialize_plan(
    case_files: Sequence[CaseFile],
    items: Sequence[CaseItem],
    files: Sequence[LoadedFile],
    scope: Scope,
    *,
    as_of: date | None = None,
    training_as_of: date | None = None,
    candidate: tuple[str, RecipeStep] | None = None,
    twice: bool = True,
    watch: Watch | None = None,
    memo: ReceiptMemo | None = None,
) -> Prepared:
    """:func:`materialize` for a caller that has the case's files and as-of date rather than the case itself.

    `as_of` is a run's own date; with none, a query step keeps the date (or the rule) it carries. `watch` sees
    each step's input and output once (the preview samples what each step did from it). `memo` keeps the steps'
    receipts by what each step read (:class:`engine.doctor.guards.ReceiptMemo`): a step the plan did not change takes
    its kept receipt.
    """
    from engine.doctor.plan_check import plan_digest, recipe_steps_of

    digest = plan_digest(case_files, items)
    files = aliased(files, items)
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
    failures: list[StepFailure] = []

    plans: list[tuple[LoadedFile, list[tuple[RecipeStep, str]]]] = []
    for loaded in files:
        mine = recipe_steps_of(items, loaded.upload_id, only_file=len(files) == 1)
        if candidate is not None and candidate[0] == loaded.upload_id:
            phased = sorted([*mine, (candidate[1], CANDIDATE)], key=lambda pair: STEP_PHASE[pair[0].kind])
            mine = [(s.model_copy(update={"order": n}), i) for n, (s, i) in enumerate(phased, start=1)]
        if mine:
            plans.append((loaded, mine))
    # A file no query reads from is prepared first, so a query step reads the others as the plan makes them.
    plans.sort(key=lambda plan: any(s.kind in _SHAPING for s, _ in plan[1]))
    # One date for the whole training table: a query without its own date reads the outcome's, never a later one.
    decided_training = training_as_of
    training_as_of = snapshot_of([s for _, mine in plans for s, _ in mine], training_as_of)

    for loaded, mine in plans:
        ordered = [step for step, _ in mine]
        by_order = {step.order: item_id for step, item_id in mine}
        name = names[loaded.upload_id]
        frame = tables[name]
        key, target = roles_of(items, loaded.upload_id, {str(c) for c in frame.columns})
        file_name = loaded.file_name or "the file"
        ordered = [bound_as_of(_reads_own_table(step, name), as_of, training_as_of) for step in ordered]
        findings.extend(
            date_differences([(st, by_order[st.order]) for st in ordered], as_of, decided_training, scope)
        )

        def seen_by(
            step: RecipeStep,
            was: pd.DataFrame,
            became: pd.DataFrame,
            *,
            upload_id: str = loaded.upload_id,
            by_order: dict[int, str] = by_order,
        ) -> None:
            if watch is not None:
                watch(upload_id, by_order[step.order], step, was, became)

        run = functools.partial(
            _guarded,
            frame,
            ordered,
            loaded,
            tables,
            agent.max_conversion_failure_pct,
            twice,
            None,  # every query step carries its date (`bound_as_of`)
            seen_by if watch is not None else None,
            memo,
        )

        try:
            if candidate is not None and candidate[0] == loaded.upload_id:
                # A query being tried makes a new table, whose columns say where the outcome (and the ID that follows
                # it) is. They are read as if the query were accepted, by the one rule the run uses after the yes
                # (:func:`roles_of`), so a trial and the run give the same answer, the same stop included.
                table_columns = [str(c) for c in run(None, None).run.frame.columns]
                key, target = roles_of(
                    items, loaded.upload_id, {str(c) for c in frame.columns}, candidate_made=table_columns
                )
            guarded = run(key, target)
        except (RecipeError, SqlStepError, ValueError, KeyError, TypeError) as exc:
            order = getattr(exc, "order", None)
            item_id = by_order.get(order or 0)
            said = getattr(exc, "message", None) or type(exc).__name__
            found = Finding(
                level="stop",
                code="PLAN_STEP_CANNOT_RUN",
                order=order,
                message=f"A step could not run on {'a sample of ' if isinstance(scope, Sample) else ''}"
                f"{file_name}: {said}",
            )
            failures.append(
                StepFailure(loaded.upload_id, getattr(exc, "code", type(exc).__name__), said, order, item_id)
            )
            # a step that cannot run is a rule whichever sentence says why: the file is not prepared, so no yes may
            # let the plan go on as if it were
            findings.append(_finding(found, item_id, scope, seen, hard=True, file=loaded.upload_id))
            failed.append(loaded.upload_id)
            continue
        made = [(by_order[r.order], r) for r in guarded.receipts]
        receipts.extend(made)
        mine_found = tuple(
            _finding(
                f, by_order.get(f.order) if f.order is not None else None, scope, seen, file=loaded.upload_id
            )
            for f in guarded.findings
        )
        findings.extend(mine_found)
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
                findings=mine_found,
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
        failures=tuple(failures),
    )


class NewSpellingError(RecipeError):
    """A yes/no step meets spellings it has no meaning for (`RECIPE_NEW_VALUE`): asked, never failed quietly under
    the conversion limit."""

    def __init__(self, column: str, order: int, unknown: Sequence[str], rows: int) -> None:
        count = len(unknown)
        super().__init__(
            "RECIPE_NEW_VALUE",
            f"'{column}' holds {count} spelling{'s' if count != 1 else ''} the approved yes/no step has no meaning "
            f"for, in {rows:,} row{'s' if rows != 1 else ''}. Does each mean yes or no?",
            column=column,
            order=order,
        )
        self.unknown = tuple(unknown)
        self.rows = rows


class ApprovedSettingsError(RecipeError):
    """A file is written another way than the number, date and currency settings the person approved (the locale
    profile, the money basis). `detector` is the prescription's drift name (`LOCALE_CHANGED`, `CURRENCY_MIXED`,
    `CURRENCY_FOREIGN`) and is also the error's code; `question` is what the person is asked."""

    def __init__(
        self,
        detector: str,
        summary: str,
        question: str,
        *,
        column: str | None = None,
        slot: str | None = None,
    ) -> None:
        super().__init__(detector, summary, column=column)
        self.detector = detector
        self.question = question
        self.slot = slot


def check_approved_settings(
    frames: Mapping[str, Any], *, profile: Any = None, basis: Any = None
) -> tuple[str, ...]:
    """The approved locale profile and money basis against the raw files (`frames`, by slot). Raises
    :class:`ApprovedSettingsError` on the first difference; returns what was checked (`"locale"`, `"currency"`).

    The one check of both: a stored recipe's replay (:func:`replay_recipe`, with what the recipe carries) and the
    prescription's raw-file checks (`prescription_replay.check_files`, with what the prescription froze) call it, so
    a scoring file, a firing and a training replay meet the same stops. A field the files do not decide stays as
    approved; a money column the files do not have is not read."""
    from engine.doctor.currency import read_currencies
    from engine.doctor.locale import SETTINGS, detect_locale

    checked: list[str] = []
    if profile is not None:
        fields = tuple(f for f in SETTINGS if f != "money_units")
        detection = detect_locale(dict(frames), base=profile, client_id=profile.client_id)
        now, before = detection.profile.values(), profile.values()

        def _same(field: str) -> bool:
            # yes/no families are a set: the order a detection lists them in depends on which families a file shows
            if field == "boolean_words":
                return set(now[field]) == set(before[field])
            return bool(now.get(field) == before.get(field))

        changed = sorted(
            {f for f in fields if not _same(f)} | {c.field for c in detection.conflicts if c.field in fields}
        )
        if changed:
            words = ", ".join(f.replace("_", " ") for f in changed)
            raise ApprovedSettingsError(
                "LOCALE_CHANGED",
                f"These files are written another way than the approved ones: {words}.",
                f"These files write {words} differently from the files that were approved. Which is right?",
            )
        checked.append("locale")
    if basis is not None and basis.columns:
        for slot, frame in frames.items():
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
                raise ApprovedSettingsError(
                    "CURRENCY_MIXED" if mixed else "CURRENCY_FOREIGN",
                    f"'{money.column}' is written in {', '.join(new) or 'several currencies at once'}; the approved "
                    f"preparation reads it in {', '.join(money.written_in)}.",
                    f"'{money.column}' now holds amounts in {', '.join(new) or 'more than one currency'}. Which "
                    "rate, or which column says the currency of each row? Amounts in different currencies are "
                    "never added.",
                    column=money.column,
                    slot=slot,
                )
        checked.append("currency")
    return tuple(checked)


def _recipe_settings(recipe: DataRecipe) -> tuple[Any, Any]:
    """The locale profile and money basis stored with a recipe, as models (None for each a recipe does not carry)."""
    from pydantic import ValidationError

    from engine.doctor.currency import MoneyBasis
    from engine.doctor.locale import LocaleProfile

    try:
        profile = (
            None if recipe.locale_profile is None else LocaleProfile.model_validate(recipe.locale_profile)
        )
        basis = None if recipe.money_basis is None else MoneyBasis.model_validate(recipe.money_basis)
    except ValidationError:  # a record this build cannot read is not an approval to check against
        return None, None
    return profile, basis


def check_spellings(recipe: DataRecipe, frame: Any) -> None:
    """A `map_boolean` step's frozen spellings: a value it has no meaning for raises :class:`NewSpellingError`."""
    from engine.doctor.prescription_contract import value_text

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
            raise NewSpellingError(str(step.column), step.order, unknown, rows)


def step_drift(
    exc: RecipeError, contract: DataContract | None, *, part: str, slot: str | None = None
) -> PrescriptionError:
    """A stored recipe that cannot run, as the prescription replay says it: a new yes/no spelling is `NEW_VALUE`;
    a step that fails on a column the contract knows is `PRESCRIPTION_DRIFT` (the column is gone, or changed);
    otherwise the step's own code. A scoring route answers the same `RecipeError` with its one-error report
    (`recipe_failure_report`), so a replay without a prescription raises it as it is."""
    from engine.doctor.prescription import PrescriptionError
    from engine.doctor.prescription_replay import drift_stop

    column = exc.column
    if isinstance(exc, ApprovedSettingsError):
        currency = exc.detector != "LOCALE_CHANGED"
        return drift_stop(
            exc.detector,
            exc.message,
            exc.question,
            part="currency" if currency else "locale",
            slot=exc.slot or slot,
            column=column,
            **({"code": exc.detector} if currency else {}),
        )
    if isinstance(exc, NewSpellingError):
        count = len(exc.unknown)
        return drift_stop(
            "NEW_VALUE",
            f"'{column}' holds {count} spelling{'s' if count != 1 else ''} the approved yes/no "
            f"step has no meaning for, in {exc.rows:,} row{'s' if exc.rows != 1 else ''}.",
            f"'{column}' has {count} new spelling{'s' if count != 1 else ''}. Does each mean yes or no?",
            part=part,
            slot=slot,
            column=column,
            examples=exc.unknown,
        )
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


def replay_recipe(
    frame: Any,
    recipe: DataRecipe,
    *,
    levels: Sequence[AgentLevel],
    max_failure_pct: float,
    upload_id: str,
    derived_upload_id: str | None = None,
    leak_check: LeakCheck | None = None,
    tables: Mapping[str, Any] | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> RecipeRun:
    """An approved recipe's steps on new rows (next month's file, a scoring file, a firing's dataset). Raises
    `RecipeError`. This is the one place a stored recipe is applied to a file; every stored-recipe replay calls it.

    The checks that need only the recipe run on every such replay: a yes/no step's frozen spellings
    (:func:`check_spellings`, `NewSpellingError`), the number, date and currency settings the person approved
    (:func:`check_approved_settings`, `ApprovedSettingsError`; a recipe carries its locale profile and money basis
    from the day it is approved, and a recipe saved before that replays without them, with a note on its receipt)
    and the guards' stops. The prescription's intake check and its joint check of several files stay in
    `engine.doctor.prescription_replay.check_files`, which calls the same :func:`check_approved_settings`. It differs from :func:`materialize_plan` (a plan still being decided, checked at every visit) on purpose: its
    steps already carry the dates the person approved, so nothing is bound to a run's as-of date or compared for
    date differences, and the leak check is the caller's (training `full`, scoring `narrow`) where a plan is
    always checked `full`.

    A recipe approved under the guards (Data Doctor: `recipe.acknowledged_stops` is not None) replays under them:
    every step is counted by :func:`engine.doctor.guards.run_guarded`, and a *stop* raises `RECIPE_GUARD_STOP` instead
    of preparing the rows, unless a person said yes to that kind of stop at approval. Next month's file and the
    scoring file never go on silently when the same steps now lose or double customers. A recipe approved without
    the guards (Guided setup, or one saved before them) replays as it was approved: its intended losses were never
    counted, so they cannot be told from new ones.
    """
    check_spellings(recipe, frame)
    profile, basis = _recipe_settings(recipe)
    check_approved_settings({"file": frame}, profile=profile, basis=basis)
    args: dict[str, Any] = {
        "upload_id": upload_id,
        "primary_key": recipe.primary_key,
        "target": recipe.target,
        "levels": levels,
        "max_failure_pct": max_failure_pct,
        "snapshot_column": recipe.snapshot_column,
        "derived_upload_id": derived_upload_id,
        "leak_check": leak_check,
        "tables": tables,
        "clock": clock,
    }
    if recipe.acknowledged_stops is None:
        return run_recipe(consistent_types(frame), recipe.steps, **args)
    unchecked = profile is None and basis is None
    run = run_guarded(
        consistent_types(frame), recipe.steps, twice=False, **args
    )  # repeatability: checked at approval
    stops = [f for f in run.findings if f.level == "stop" and f.code not in recipe.acknowledged_stops]
    if stops:
        raise RecipeError(
            "RECIPE_GUARD_STOP",
            " ".join(f.message for f in stops[:3])
            + " The rows were not prepared. Open the Data Doctor check to look at it, or change the plan.",
            order=stops[0].order,
        )
    if unchecked:
        receipt = run.run.receipt.model_copy(
            update={"notes": (*run.run.receipt.notes, UNCHECKED_SETTINGS_NOTE)}
        )
        return RecipeRun(frame=run.run.frame, receipt=receipt)
    return run.run


def replay_steps(
    frame: Any,
    steps: Sequence[RecipeStep],
    *,
    upload_id: str,
    levels: Sequence[AgentLevel],
    max_failure_pct: float,
    primary_key: str | None = None,
    target: str | None = None,
    snapshot_column: str | None = None,
    leak_check: LeakCheck | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> RecipeRun:
    """Approved steps that are not (yet) a saved recipe on rows (a time setting's steps, a split's recipe): the steps
    as an unguarded, unstamped :class:`DataRecipe` through :func:`replay_recipe`, so they meet its checks and run
    under the levels and limit the caller names."""
    ordered = tuple(steps)
    hash_ = recipe_hash(ordered)
    recipe = DataRecipe(
        recipe_id=f"rc-{hash_.removeprefix('sha256:')[:16]}",
        use_case_id="steps",
        source_fingerprint="steps",
        steps=ordered,
        recipe_hash=hash_,
        primary_key=primary_key,
        target=target,
        snapshot_column=snapshot_column,
        created_at=clock(),
    )
    return replay_recipe(
        frame,
        recipe,
        levels=levels,
        max_failure_pct=max_failure_pct,
        upload_id=upload_id,
        leak_check=leak_check,
        clock=clock,
    )


def _guarded(
    frame: pd.DataFrame,
    steps: Sequence[RecipeStep],
    loaded: LoadedFile,
    tables: dict[str, pd.DataFrame],
    max_failure_pct: float,
    twice: bool,
    as_of: date | None,
    watch: Callable[[RecipeStep, pd.DataFrame, pd.DataFrame], None] | None,
    memo: ReceiptMemo | None,
    primary_key: str | None,
    outcome: str | None,
) -> GuardedRun:
    """One file's steps under the guards, with the tables the plan has made so far."""
    from engine.doctor.plan_check import guard_levels

    return run_guarded(
        frame,
        steps,
        primary_key=primary_key,
        target=outcome,
        tables=tables,
        upload_id=loaded.upload_id,
        source_name=loaded.file_name or None,
        levels=guard_levels(steps),
        max_failure_pct=max_failure_pct,
        twice=twice,
        as_of=as_of,
        leak_check="full",
        outcome_open=outcome is None,  # one column of a query that reads the future is the outcome-to-be
        watch=watch,
        memo=memo,
    )


def snapshot_of(steps: Sequence[RecipeStep], training_as_of: date | None = None) -> date | None:
    """The one date a plan's training table is cut at: the decided training snapshot, else the own date of a query
    that reads the future (the outcome's), so a step without a date of its own never reads rows inside the outcome's
    window. None when the plan has neither."""
    if training_as_of is not None:
        return training_as_of
    for step in steps:
        p = step.params
        if (
            step.kind is RecipeStepKind.SQL_QUERY
            and p.get("reads_future")
            and p.get("as_of")
            and not p.get("as_of_rule")
        ):
            with contextlib.suppress(ValueError):
                return date.fromisoformat(str(p["as_of"]))
    return None


def bound_as_of(step: RecipeStep, as_of: date | None, training_as_of: date | None = None) -> RecipeStep:
    """A query step with the plan's date in it: `$as_of` stays symbolic in the plan and is bound here (and in the
    prescription) so a trial, a check and a run count the same rows. A step's own explicit date is never changed
    (:func:`date_differences` tells the person); a step without one reads the training snapshot when the plan has one
    (:func:`snapshot_of`), else the case date. The date wins over the step's `as_of_rule`, so the rule is dropped.
    """
    params = step.params
    if step.kind is not RecipeStepKind.SQL_QUERY or (params.get("as_of") and not params.get("as_of_rule")):
        return step
    chosen = training_as_of or as_of
    if chosen is None or (params.get("as_of") == chosen.isoformat() and "as_of_rule" not in params):
        return step
    kept = {k: v for k, v in params.items() if k != "as_of_rule"}
    return step.model_copy(update={"params": {**kept, "as_of": chosen.isoformat()}})


def tried_step(step: RecipeStep, as_of: date | None, training_as_of: date | None) -> RecipeStep:
    """A query step as a trial runs it: bound the way a run binds it (the decided date wins over a rule), and the rule
    dropped (its date is already in `as_of`), so a trial and the run count the same rows."""
    bound = bound_as_of(step, as_of, training_as_of)
    kept = {k: v for k, v in bound.params.items() if k != "as_of_rule"}
    return bound.model_copy(update={"params": kept})


def date_differences(
    bound: Sequence[tuple[RecipeStep, str | None]],
    as_of: date | None,
    training_as_of: date | None,
    scope: Scope,
) -> list[PlanFinding]:
    """A stop for each query step that reads another date than the one decided (the training snapshot, else the case's
    as-of date): the person says yes to it. It also covers a date that was the case's when the step was made.
    """
    decided = training_as_of or as_of
    if decided is None:
        return []
    out: list[PlanFinding] = []
    for step, item_id in bound:
        have = step.params.get("as_of")
        if step.kind is RecipeStepKind.SQL_QUERY and have and have != decided.isoformat() and item_id:
            out.append(
                _stop(
                    "STEP_DATE_DIFFERS",
                    f"A query reads the data as of {have}, not {decided.isoformat()} (the date decided for the "
                    "case). Say yes to keep it; a scoring file made by this recipe reads as of that date too.",
                    step.order,
                    item_id,
                    scope,
                )
            )
    return out


def _reads_own_table(step: RecipeStep, table: str) -> RecipeStep:
    """A query that names the file it is prepared on reads that file as the steps before it left it (never the raw
    upload): the step is run with the file's table name as its `this_table`. In memory only; the stored step is
    not changed, and a stored `this_table` (an older step's) says the same."""
    if step.kind is not RecipeStepKind.SQL_QUERY:
        return step
    try:
        params = sql_params(step.params)
    except SqlStepError:
        return step  # the run says why it is not valid
    if params.this_table is not None or table not in params.inputs:
        return step
    return step.model_copy(update={"params": {**step.params, "this_table": table}})


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
