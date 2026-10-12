"""Notice what I don't know (Plan O M245, DEC-1815; the design §7, [REQ] 8 and 11).

Data Doctor must notice what it cannot read, ask **one** clear question, remember the answer for the client and apply
it next time, visibly and revocably. This module is the noticing: the detectors of the design §7.1, as rules over
the case's files, plugged into the visit through M246's detector port (`engine.doctor.visit.DetectorPort`). M246's
protocol does the rest: it settles each ambiguity (AUTO, a stated assumption, or one question), applies the client's
standing answers pre-ticked and saying so, and never stores an APPROVE-class answer as standing.

**In one file** (every file of the case):

| Detector | Fires when | The question's choices |
|---|---|---|
| `UNREADABLE_VALUES` | a column of numbers (at least 80% read) holds a few spellings no number reader reads (`nil`, `-`) | treat them as empty; leave them as written |
| `UNIT_MISMATCH` | M238's `detect_units`: unit words that disagree (`2 GB` beside `500 MB`), or a magnitude jump of about 10^3, 10^5 or 10^7 | the unit the plain numbers are in (M238's steps); leave as written; hide the column |
| `UNKNOWN_BOOLEAN` | a yes/no column (known yes and no words, at most 20 spellings) with spellings no lexicon has a meaning for (`bilkul`, `nako`) | treat them as empty; leave them as written |

**Against what was approved** (a file added to a case that has a prescription: next month's file). The file is
prepared by its slot's recipe exactly as a replay prepares it and checked against the slot's column contract (M243's
`check_contract`, the tool `drift_check`):

| Detector | Fires when | Blocks? |
|---|---|---|
| `MEANING_CHANGED` | a column holds another kind of value, or its common values are mostly new (Jaccard of the values at least 10 rows hold, under 0.5) | yes |
| `UNIT_MISMATCH` | the column's middle value moved about 10^3, 10^5 (a lakh switch) or 10^7 times | yes |
| `VALUE_DRIFT` | the middle value left the approved 1st-99th percentile range, the empty share moved over ten points, or the share of yes moved by half of itself (a doubled yes-rate) | no |
| `NEW_CATEGORY` | a spelling not seen at approval holds over 1% of the rows (or any new value of a closed column) | a closed column's, yes |
| `MISSING_COLUMN`, `NEW_COLUMN` | a column is gone, or new | gone: yes |

**One question per column of a file**: when several detectors fire on one column, the first in the order above asks.
Every question names its column and its values (`value_signature`), so the client's standing answer is found again
next month (the same detector, column and values), applied pre-ticked with "Using your answer from ...", and asked
again once revoked (Settings, `POST /clients/{client_id}/standing-answers/{answer_id}/revoke`).

**New patterns, with permission** ([REQ] 11). For `UNKNOWN_BOOLEAN` and `UNREADABLE_VALUES` the AI - when one is
connected - may propose a fix (`engine.doctor.pattern_fix`): the rules look at the column first (Plan G's
`value_counts`, cited by its evidence id), the AI answers with one of the fixed step kinds, `propose_pattern_fix`
tries it on the first 1,000 rows and the data policy decides it (`data.propose_pattern_fix`: grounded, tried, O10's
change share, never ticked). It becomes one more choice of the same question, labelled as the AI's, never
recommended and never pre-ticked; the person's answer applies it (recorded with their user id), approval counts its
pattern towards a candidate rule for the product team (`engine.doctor.patterns`), and the answer stands for the
client. When no step kind can do it the question says "I can't do this safely yet" and a tool request is recorded.

Rules only, deterministic, no AI needed: with no AI service connected every detector still asks its question.
Nothing here logs a value or a column name.
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import pandas as pd
from pydantic import Field, JsonValue

from engine.agent.contracts import DataRecipe, RecipeStepKind
from engine.config import StrictBase
from engine.doctor.prescription_contract import (
    MIN_GROUP,
    ColumnContract,
    DataContract,
    DriftFinding,
    check_contract,
    learn_contract,
    value_text,
)
from engine.doctor.protocol import Ask, AskOption
from engine.doctor.visit_contracts import OptionEffect
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from datetime import datetime

    from engine.config import UseCaseConfig
    from engine.doctor.case import Case
    from engine.doctor.diagnose import LoadedFile, Offer
    from engine.doctor.goal_brief import GoalBrief
    from engine.doctor.pattern_fix import PatternAI
    from engine.doctor.prescription import FilePart, Prescription
    from engine.doctor.registry import DataToolRegistry, DataToolResult
    from engine.doctor.visit import Detected, DetectorPort
    from engine.storage import Storage

__all__ = [
    "AI_FIX_LABEL",
    "DRIFT_ORDER",
    "FILE_ORDER",
    "NOTICE_CODES",
    "DriftReport",
    "Notice",
    "NoticeFinding",
    "drift_check",
    "file_notices",
    "notice_detector",
]

_LOG = get_logger(__name__)

NOTICE_CODES: Final[tuple[str, ...]] = (
    "MEANING_CHANGED",
    "UNIT_MISMATCH",
    "MISSING_COLUMN",
    "VALUE_DRIFT",
    "NEW_CATEGORY",
    "NEW_COLUMN",
    "UNKNOWN_BOOLEAN",
    "UNREADABLE_VALUES",
)
"""Every question this module asks (all of them standing codes of M246's `standing.STANDING_CODES`)."""
DRIFT_ORDER: Final[tuple[str, ...]] = (
    "MISSING_COLUMN",
    "MEANING_CHANGED",
    "UNIT_MISMATCH",
    "NEW_CATEGORY",
    "VALUE_DRIFT",
    "NEW_COLUMN",
)
FILE_ORDER: Final[tuple[str, ...]] = ("UNKNOWN_BOOLEAN", "UNREADABLE_VALUES", "UNIT_MISMATCH")
AI_FIX_LABEL: Final[str] = "Use the AI's suggested fix"
"""The AI's choice is always labelled the same way, so a standing answer finds it again next month."""

MIN_READ_SHARE: Final[float] = 0.8
"""A text column is a column of numbers when at least this share of its non-empty values reads as one."""
MAX_PATTERN_SPELLINGS: Final[int] = 20
"""More distinct unreadable or unknown spellings than this is free text, not a pattern: nothing is asked."""
MEANING_JACCARD: Final[float] = 0.5
_JUMPS: Final[Mapping[int, str]] = {3: "thousands", 5: "lakhs", 7: "crores"}


# ---------------------------------------------------------------------------
# What a detector found
# ---------------------------------------------------------------------------
class NoticeFinding(StrictBase):
    """One thing a detector noticed, before it is a question: counts, codes and the values it is about."""

    code: str
    column: str | None = None
    stops: bool = Field(default=False, description="A question that blocks until it is answered.")
    summary: str = Field(description="What was noticed, in plain words; counts only.")
    signature: tuple[str, ...] = Field(default=(), description="The values (or measure) it is about.")
    rows: int = Field(default=0, ge=0, description="Rows it concerns.")
    measure: str | None = Field(default=None, description="VALUE_DRIFT: median, empty_share or outcome_rate.")
    power: int | None = Field(default=None, description="UNIT_MISMATCH: the power of ten the values moved by.")


class DriftReport(StrictBase):
    """`drift_check`'s result: a file against what was approved (a prescription's slot, or another file)."""

    upload_id: str
    against: Literal["prescription", "upload"]
    prescription_hash: str | None = None
    slot: str | None = None
    against_upload_id: str | None = None
    mode: Literal["train", "score"]
    columns_checked: int = Field(ge=0)
    findings: tuple[NoticeFinding, ...] = ()
    stops: bool = Field(description="Whether anything found blocks until it is answered.")


@dataclass(frozen=True)
class Notice:
    """A finding with its question's choices, ready to become an M246 `Ask`."""

    finding: NoticeFinding
    upload_id: str
    text: str
    why: str
    options: tuple[AskOption, ...]
    rows: int
    meaning_bearing: bool = False
    evidence_ids: tuple[str, ...] = ()
    pattern: bool = False
    """A new pattern the AI may propose a fix for (`UNKNOWN_BOOLEAN`, `UNREADABLE_VALUES`)."""
    extras: Mapping[str, Any] = field(default_factory=dict)

    def ask(self, goal_id: str | None) -> Ask:
        finding = self.finding
        subject: dict[str, JsonValue] = {
            "upload": self.upload_id,
            "column": finding.column,
            "values": list(finding.signature),
        }
        return Ask(
            code=finding.code,
            subject=subject,
            text=self.text,
            why=self.why,
            options=self.options,
            rows=self.rows,
            upload_id=self.upload_id,
            column=finding.column,
            goal_id=goal_id,
            blocking=finding.stops,
            meaning_bearing=self.meaning_bearing,
            value_signature=finding.signature,
            evidence_ids=self.evidence_ids,
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _texts(series: pd.Series[Any]) -> pd.Series[Any]:
    present = series.dropna().map(value_text)
    return present[present != ""]


def _fold(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _quoted(values: Sequence[str], limit: int = 3) -> str:
    from engine.agent.formats import masked_cut

    shown = [f"“{masked_cut(v, 30)}”" for v in values[:limit]]
    more = len(values) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _step(upload_id: str, kind: RecipeStepKind, column: str, params: Mapping[str, Any], reason: str) -> dict[str, JsonValue]:
    return {
        "upload_id": upload_id,
        "step": {"order": 1, "kind": kind.value, "column": column, "params": dict(params), "reason": reason},
    }


def _effect(option_id: str, title: str, payload: Mapping[str, JsonValue] | None = None, *, source: str = "rules") -> OptionEffect:
    if payload is None:
        return OptionEffect(option_id=option_id, title=title)
    return OptionEffect(option_id=option_id, kind="recipe_step", title=title, payload=dict(payload), source=source)


def _hide(upload_id: str, column: str, rows: int, *, safe: bool = False, option_id: str = "hide") -> AskOption:
    return AskOption(
        label="Hide the column from the model",
        effect=_effect(
            option_id,
            f"Hide “{column}”: the model does not read it.",
            _step(upload_id, RecipeStepKind.DROP_COLUMN, column, {}, "Its values no longer mean what was approved."),
        ),
        rows_changed=rows,
        safe=safe,
    )


def _leave(rows_changed: int = 0, *, option_id: str = "leave", label: str = "Leave them as written") -> AskOption:
    return AskOption(label=label, effect=_effect(option_id, "Leave the values as they are."), rows_changed=rows_changed, safe=True)


def _as_empty(upload_id: str, column: str, values: Sequence[str], rows: int) -> AskOption:
    return AskOption(
        label="Treat them as empty",
        effect=_effect(
            "empty",
            f"Treat {_quoted(values)} in “{column}” as empty.",
            _step(upload_id, RecipeStepKind.SET_MISSING_TEXT, column, {"values": list(values)}, "No rule can read them."),
        ),
        rows_changed=rows,
    )


# ---------------------------------------------------------------------------
# Detectors in one file
# ---------------------------------------------------------------------------
def _unreadable(frame: pd.DataFrame, column: str, upload_id: str) -> Notice | None:
    from engine.agent.formats import _decimal_style, parse_numbers

    series = frame[column]
    if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
        return None
    texts = _texts(series)
    if len(texts) < MIN_GROUP:
        return None
    decimal = _decimal_style(texts) or "."
    parsed = parse_numbers(texts, decimal=decimal, indian_scale_words=True)
    read = int(parsed.values.notna().sum())
    if read / len(texts) < MIN_READ_SHARE or read == len(texts):
        return None
    failing = texts[parsed.values.isna().to_numpy()]
    counts = failing.map(lambda t: t.strip()).value_counts()
    spellings = [str(v) for v in counts.index]
    if not spellings or len(spellings) > MAX_PATTERN_SPELLINGS or any(re.search(r"\d", s) for s in spellings):
        return None  # a number written in a way no reader knows yet is M238's, not a placeholder pattern
    rows = int(counts.sum())
    finding = NoticeFinding(
        code="UNREADABLE_VALUES",
        column=column,
        summary=f"{_plural(rows, 'value')} of “{column}” cannot be read as numbers.",
        signature=tuple(sorted({_fold(s) for s in spellings})),
        rows=rows,
    )
    return Notice(
        finding=finding,
        upload_id=upload_id,
        text=f"{_plural(rows, 'value')} in “{column}” cannot be read as numbers ({_quoted(spellings)}). What are they?",
        why=f"{read:,} of {len(texts):,} values of “{column}” read as numbers; these do not.",
        options=(_as_empty(upload_id, column, spellings, rows), _leave()),
        rows=len(frame),
        pattern=True,
        extras={"unknown": tuple(spellings)},
    )


def _lexicon() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    from engine.agent.formats import BOOLEAN_FALSE, BOOLEAN_TRUE
    from engine.doctor.indian_values import (
        AMBIGUOUS_NO,
        DEVANAGARI_FALSE,
        DEVANAGARI_TRUE,
        HINGLISH_FALSE,
        HINGLISH_TRUE,
    )

    yes = frozenset(_fold(w) for w in (*BOOLEAN_TRUE, *HINGLISH_TRUE, *DEVANAGARI_TRUE))
    no = frozenset(_fold(w) for w in (*BOOLEAN_FALSE, *HINGLISH_FALSE, *DEVANAGARI_FALSE))
    return yes, no, frozenset(_fold(w) for w in AMBIGUOUS_NO)


def _unknown_boolean(
    frame: pd.DataFrame, column: str, upload_id: str, *, known: Sequence[str] = ()
) -> Notice | None:
    series = frame[column]
    if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
        return None
    texts = _texts(series)
    if texts.empty:
        return None
    counts = texts.map(_fold).value_counts()
    if len(counts) > MAX_PATTERN_SPELLINGS:
        return None
    yes, no, ambiguous = _lexicon()
    approved = {_fold(w) for w in known}
    spellings = set(counts.index)
    if not approved and not (spellings & yes and spellings & no):
        return None
    unknown = sorted(s for s in spellings if s not in yes | no | ambiguous | approved)
    if not unknown:
        return None
    rows = int(sum(int(counts[s]) for s in unknown))
    originals = [next(t for t in texts if _fold(t) == s) for s in unknown]
    finding = NoticeFinding(
        code="UNKNOWN_BOOLEAN",
        column=column,
        summary=f"“{column}” holds yes and no, and {_plural(len(unknown), 'spelling')} no rule has a meaning for.",
        signature=tuple(unknown),
        rows=rows,
    )
    return Notice(
        finding=finding,
        upload_id=upload_id,
        text=f"In “{column}”, what do {_quoted(originals)} mean?",
        why=f"“{column}” holds yes and no; {_plural(rows, 'value')} are written in a way no rule knows.",
        options=(_as_empty(upload_id, column, originals, rows), _leave()),
        rows=len(frame),
        pattern=True,
        extras={"unknown": tuple(originals)},
    )


def _units(frame: pd.DataFrame, column: str, upload_id: str) -> Notice | None:
    from engine.doctor.units import detect_units

    series = frame[column]
    if series.dropna().empty:
        return None
    report = detect_units(series, column=column)
    if report.finding != "UNIT_MISMATCH":
        return None
    non_empty = report.non_empty
    options: list[AskOption] = []
    signature: tuple[str, ...]
    if report.question is not None:
        for option in report.question.options:
            if option.step is None:
                continue
            params = dict(option.step.params)
            units = params.get("units")
            if isinstance(units, list):
                params["units"] = {str(pair[0]): pair[1] for pair in units if isinstance(pair, list) and len(pair) == 2}
            options.append(
                AskOption(
                    label=option.label,
                    effect=_effect(
                        option.option_id,
                        option.step.title,
                        _step(upload_id, RecipeStepKind.CONVERT_UNITS, column, params, option.step.reason),
                    ),
                    rows_changed=option.step.changed,
                )
            )
        options.append(_leave(label="Leave them as written"))
        signature = tuple(sorted(u.unit for u in report.units))
        text = report.question.text
    else:
        options = [_leave(label="Leave the column as written"), _hide(upload_id, column, non_empty)]
        signature = tuple(sorted(u.unit for u in report.units)) or ("magnitude",)
        text = f"“{column}” seems to mix units. What does it hold?"
    finding = NoticeFinding(
        code="UNIT_MISMATCH",
        column=column,
        summary=report.sentence or f"“{column}” mixes units.",
        signature=signature,
        rows=non_empty,
    )
    return Notice(
        finding=finding,
        upload_id=upload_id,
        text=text,
        why=report.sentence or "The units in this column disagree.",
        options=tuple(options),
        rows=len(frame),
    )


def file_notices(
    loaded: LoadedFile, *, skip: frozenset[str] = frozenset(), known_booleans: Mapping[str, Sequence[str]] | None = None
) -> list[Notice]:
    """What the rules notice in one file, one notice per column at most (:data:`FILE_ORDER`); personal-data columns
    and the columns in `skip` are not read."""
    frame = loaded.frame
    personal = {c.name for c in loaded.profile.columns if c.pii_kinds}
    out: list[Notice] = []
    for name in frame.columns:
        column = str(name)
        if column in personal or column in skip:
            continue
        known = (known_booleans or {}).get(column, ())
        for found in (
            _unknown_boolean(frame, column, loaded.upload_id, known=known),
            _unreadable(frame, column, loaded.upload_id),
            _units(frame, column, loaded.upload_id),
        ):
            if found is not None:
                out.append(found)
                break
    return out


# ---------------------------------------------------------------------------
# drift_check: a file against what was approved
# ---------------------------------------------------------------------------
def _measure(finding: DriftFinding) -> str:
    if finding.summary.startswith("The share of yes"):
        return "outcome_rate"
    if finding.summary.startswith("The middle value"):
        return "median"
    return "empty_share"


def _median(series: pd.Series[Any], frozen: ColumnContract) -> float | None:
    from engine.doctor.prescription_contract import _numbers

    numbers = _numbers(series, frozen.type_group)
    return None if numbers.empty else float(numbers.quantile(0.5))


def _power(new: float | None, old: float | None) -> int | None:
    """The power of ten (3, 5 or 7, signed) the middle value moved by, when it moved by one of them (within 2x)."""
    if new is None or old is None or new <= 0 or old <= 0:
        return None
    ratio = math.log10(new / old)
    power = min(_JUMPS, key=lambda p: abs(p - abs(ratio)))
    if abs(power - abs(ratio)) > math.log10(2.0):
        return None
    return power if ratio > 0 else -power


def _common(series: pd.Series[Any]) -> set[str]:
    counts = _texts(series).value_counts()
    return {str(v) for v, n in counts.items() if int(n) >= MIN_GROUP}


def _refine(finding: DriftFinding, frame: pd.DataFrame, contract: DataContract) -> NoticeFinding:
    column = finding.column
    frozen = contract.column(column) if column else None
    present = column is not None and column in frame.columns
    examples = tuple(finding.question.examples)
    code: str = finding.detector
    stops = finding.stops
    summary = finding.summary
    signature: tuple[str, ...] = ()
    measure: str | None = None
    power: int | None = None
    rows = 0
    if code == "VALUE_DRIFT" and frozen is not None and present:
        measure = _measure(finding)
        signature = (measure,)
        if measure == "median":
            power = _power(_median(frame[str(column)], frozen), frozen.p50)
            if power is not None:
                word = _JUMPS[abs(power)]
                code, stops, signature = "UNIT_MISMATCH", True, (f"x10^{power}",)
                bigger = "larger" if power > 0 else "smaller"
                summary = (
                    f"The values of “{column}” are about 10^{abs(power)} times {bigger} than when the preparation "
                    f"was approved, as if they were {'no longer ' if power > 0 else ''}written in {word}."
                )
    elif code in ("NEW_CATEGORY", "NEW_VALUE") and frozen is not None and present:
        listed = {item.value for item in frozen.top}
        common = _common(frame[str(column)])
        texts = _texts(frame[str(column)])
        new = sorted(
            {t for t in texts.tolist() if t not in listed},
            key=lambda v: (-int((texts == v).sum()), v),
        )
        rows = int(texts.isin(new).sum())
        union = listed | common
        jaccard = len(listed & common) / len(union) if union else 1.0
        if listed and common and jaccard < MEANING_JACCARD:
            code, stops, signature = "MEANING_CHANGED", True, ("values",)
            summary = (
                f"Most common values of “{column}” are new since the preparation was approved: it may hold "
                "something else now."
            )
        else:
            code, signature = "NEW_CATEGORY", tuple(sorted({_fold(v) for v in new[:MAX_PATTERN_SPELLINGS]}))
            examples = tuple(new[:3])
    elif code == "MEANING_CHANGED" and frozen is not None and present:
        from engine.doctor.prescription_contract import type_group

        signature = (frozen.type_group, type_group(frame[str(column)]))
    return NoticeFinding(
        code=code,
        column=column,
        stops=stops,
        summary=summary,
        signature=signature or tuple(sorted({_fold(v) for v in examples})),
        rows=rows,
        measure=measure,
        power=power,
    )


def _skip_booleans(recipe: DataRecipe, frame: pd.DataFrame) -> tuple[DataRecipe, frozenset[str]]:
    """The recipe without the yes/no steps whose column holds a spelling they have no meaning for (and the later
    steps on those columns): the new spellings are the file detector's question, the other columns are still
    checked."""
    held: set[str] = set()
    for step in recipe.steps:
        if step.kind is not RecipeStepKind.MAP_BOOLEAN or step.column not in frame.columns:
            continue
        known = {
            _fold(str(v))
            for v in (*step.params.get("true_values", []), *step.params.get("false_values", []))  # type: ignore[misc]
        }
        if any(_fold(t) not in known for t in _texts(frame[step.column])):
            held.add(step.column)
    if not held:
        return recipe, frozenset()
    kept = [s for s in recipe.steps if s.column not in held]
    steps = [s.model_copy(update={"order": i}) for i, s in enumerate(kept, start=1)]
    return recipe.model_copy(update={"steps": steps}), frozenset(held)


def drift_check(
    frame: pd.DataFrame,
    contract: DataContract,
    *,
    upload_id: str,
    mode: Literal["train", "score"],
    recipe: DataRecipe | None = None,
    slot: str | None = None,
    against: Literal["prescription", "upload"] = "prescription",
    prescription_hash: str | None = None,
    against_upload_id: str | None = None,
) -> DriftReport:
    """`frame` (as sent) prepared by `recipe` as a replay prepares it, then checked against `contract`.

    M243's `check_contract` says what moved; this adds what it does not tell apart: a middle value about 10^3, 10^5
    or 10^7 times away (`UNIT_MISMATCH`, a lakh switch) and common values that are mostly new (`MEANING_CHANGED`). A
    step that cannot run on a column the contract knows is that column's finding (`MISSING_COLUMN`,
    `MEANING_CHANGED`). Nothing is learnt, written or proposed."""
    from engine.doctor.prescription import PrescriptionError
    from engine.doctor.prescription_replay import run_part_recipe

    prepared = frame
    held: frozenset[str] = frozenset()
    findings: list[NoticeFinding] = []
    if recipe is not None and recipe.steps:
        runnable, held = _skip_booleans(recipe, frame)
        try:
            prepared = run_part_recipe(
                runnable, frame, config=None, upload_id=upload_id, leak_check=None, contract=contract, slot=slot
            ).frame
        except PrescriptionError as exc:
            findings = [_refine(f, frame, contract) for f in exc.findings]
            if not findings:
                findings = [
                    NoticeFinding(code="MEANING_CHANGED", summary=exc.message[:300], stops=True, signature=(exc.code,))
                ]
            return _report(findings, upload_id, contract, mode, slot, against, prescription_hash, against_upload_id)
    kept = DataContract(columns=tuple(c for c in contract.columns if c.column not in held))
    checked = prepared.drop(columns=[c for c in prepared.columns if str(c) in held])
    for finding in check_contract(checked, kept, mode=mode, slot=slot):
        findings.append(_refine(finding, checked, kept))
    return _report(findings, upload_id, kept, mode, slot, against, prescription_hash, against_upload_id)


def _report(
    findings: Sequence[NoticeFinding],
    upload_id: str,
    contract: DataContract,
    mode: Literal["train", "score"],
    slot: str | None,
    against: Literal["prescription", "upload"],
    prescription_hash: str | None,
    against_upload_id: str | None,
) -> DriftReport:
    rank = {code: i for i, code in enumerate(DRIFT_ORDER)}
    ordered = sorted(findings, key=lambda f: (rank.get(f.code, len(rank)), f.column or ""))
    return DriftReport(
        upload_id=upload_id,
        against=against,
        prescription_hash=prescription_hash,
        slot=slot,
        against_upload_id=against_upload_id,
        mode=mode,
        columns_checked=len(contract.columns),
        findings=tuple(ordered),
        stops=any(f.stops for f in ordered),
    )


def contract_of(frame: pd.DataFrame, *, primary_key: str | None = None, target: str | None = None) -> DataContract:
    """Another file's contract, learnt as PRESCRIBE learns one (`drift_check` against an upload)."""
    return learn_contract(frame, primary_key=primary_key, target=target)


# ---------------------------------------------------------------------------
# From a drift finding to its question
# ---------------------------------------------------------------------------
def drift_notice(finding: NoticeFinding, *, upload_id: str, rows: int, file_name: str) -> Notice | None:
    """One drift finding as a question with its choices; None for a finding this module does not ask about."""
    column = finding.column or ""
    name = file_name or "this file"
    code = finding.code
    expected = AskOption(label="The change is expected; go on", effect=_effect("expected", "Use the file as it is."))
    review = AskOption(
        label="Review it first", effect=_effect("review", "Look at the file again before it is used."), safe=True
    )
    if code == "MISSING_COLUMN":
        text = f"{name} has no column “{column}”, which the approved preparation reads. Is it called something else now?"
        options: tuple[AskOption, ...] = (
            AskOption(label="It is gone; go on without it", effect=_effect("gone", "Go on without the column.")),
            review,
        )
    elif code == "MEANING_CHANGED":
        text = f"“{column}” in {name} looks different from what was approved. Is it still the same column?"
        options = (
            AskOption(label="It is the same column", effect=_effect("same", "Keep reading the column as approved.")),
            _hide(upload_id, column, rows, safe=True),
        )
    elif code == "UNIT_MISMATCH":
        word = _JUMPS.get(abs(finding.power or 5), "another unit")
        text = (
            f"The values of “{column}” are about 10^{abs(finding.power or 5)} times "
            f"{'larger' if (finding.power or 0) > 0 else 'smaller'} than last time. Did the file change how it writes "
            f"them (for example, {word})?"
        )
        options = (
            AskOption(
                label="The new values are right as written",
                effect=_effect("right", "Read the values as they are written now."),
            ),
            _hide(upload_id, column, rows, safe=True),
        )
    elif code == "VALUE_DRIFT":
        what = {
            "median": "moved a long way from what was approved",
            "empty_share": "is empty much more or less often than when it was approved",
            "outcome_rate": "has a very different share of yes than when it was approved",
        }.get(finding.measure or "", "moved")
        text = f"“{column}” {what}. Is that expected?"
        options = (expected, review)
    elif code == "NEW_CATEGORY":
        new = list(finding.signature)
        text = f"“{column}” has {_plural(len(new), 'value')} not seen when the preparation was approved ({_quoted(new)}). What are they?"
        options = (
            AskOption(label="Keep them as their own group", effect=_effect("keep", "Keep the new values as they are."), safe=True),
            _as_empty(upload_id, column, new, finding.rows),
            AskOption(
                label="Leave those rows out",
                effect=_effect(
                    "drop_rows",
                    f"Leave out the rows whose “{column}” is {_quoted(new)}.",
                    {
                        **_step(
                            upload_id,
                            RecipeStepKind.REMOVE_ROWS,
                            column,
                            {"operator": "in", "values": new},
                            "These values were not there when the preparation was approved.",
                        ),
                    },
                ),
                rows_changed=finding.rows,
            ),
        )
    elif code == "NEW_COLUMN":
        text = f"{name} has a new column “{column}”. Should the model read it?"
        options = (
            AskOption(label="Use it", effect=_effect("use", "Let the model read the new column.")),
            _hide(upload_id, column, rows, safe=True),
        )
    else:
        return None
    return Notice(finding=finding, upload_id=upload_id, text=text, why=finding.summary, options=options, rows=rows)


# ---------------------------------------------------------------------------
# The detector the visit calls
# ---------------------------------------------------------------------------
def _baseline(storage: Storage, case: Case) -> Prescription | None:
    """The case's latest stored prescription, or None."""
    from engine.doctor.prescription import PrescriptionError, load_prescription

    if not case.prescriptions:
        return None
    try:
        return load_prescription(storage, case.client_id, case.case_id, case.prescriptions[-1].prescription_hash)
    except (PrescriptionError, ValueError, OSError):
        return None


def _slot_for(prescription: Prescription, loaded: LoadedFile) -> FilePart | None:
    """The slot a file added after approval fills: the one whose contract shares the most columns with it (at least
    half of the contract's); none for a file the prescription was approved on."""
    if any(loaded.upload_id in part.upload_ids for part in prescription.files):
        return None
    columns = {str(c) for c in loaded.frame.columns}
    best: tuple[float, FilePart] | None = None
    for part in prescription.files:
        if part.contract is None or not part.contract.columns:
            continue
        wanted = {c.column for c in part.contract.columns}
        if part.recipe is not None:
            wanted |= {s.column for s in part.recipe.steps}
        share = len(columns & wanted) / len(wanted)
        if share >= 0.5 and (best is None or share > best[0]):
            best = (share, part)
    return None if best is None else best[1]


def _booleans_of(recipe: DataRecipe | None) -> dict[str, tuple[str, ...]]:
    if recipe is None:
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for step in recipe.steps:
        if step.kind is RecipeStepKind.MAP_BOOLEAN:
            words = [*step.params.get("true_values", []), *step.params.get("false_values", [])]  # type: ignore[misc]
            out[step.column] = (*out.get(step.column, ()), *(str(w) for w in words))
    return out


def _config_for(brief: GoalBrief | None, offers: Sequence[Offer], config_root: Path | None) -> UseCaseConfig | None:
    from engine.config import ConfigError, load_use_case

    if brief is not None:
        try:
            return load_use_case(brief.goal_id, config_root)
        except ConfigError:
            return None
    return offers[0].config if offers else None


@dataclass
class _AIStep:
    """What the AI part added to one notice: its option, evidence and words for the question."""

    option: AskOption | None = None
    evidence: list[DataToolResult] = field(default_factory=list)
    note: str = ""


def _ai_option(
    notice: Notice,
    loaded: LoadedFile,
    case: Case,
    *,
    ai: PatternAI | None,
    storage: Storage,
    registry: DataToolRegistry,
    config: UseCaseConfig,
    config_root: Path | None,
    clock: Callable[[], datetime],
) -> _AIStep:
    """Look at the column, ask the AI (or its cached answer), try the fix through `propose_pattern_fix`."""
    from engine.agent.contracts import ProposalSource
    from engine.agent.tools import AgentContext, AgentToolError
    from engine.config import RunMode
    from engine.doctor.pattern_fix import (
        CANNOT_SAFELY,
        cached_suggestion,
        look_at,
        record_tool_request,
        unusable_reason,
    )
    from engine.doctor.registry import DataToolContext

    column = str(notice.finding.column)
    agent = AgentContext(
        use_case_id=config.id,
        config=config,
        config_root=config_root,
        upload_id=loaded.upload_id,
        mode=RunMode.TRAIN,
        profile=loaded.profile,
        frame=loaded.frame,
    )
    ctx = DataToolContext(
        agent=agent, caller="rules", storage=storage, files=(loaded,), config_root=config_root, clock=clock
    )
    out = _AIStep()
    try:
        looked = registry.call("value_counts", {"column": column, "top": 50}, ctx)
    except AgentToolError:
        return out
    out.evidence.append(looked)
    look = look_at(
        loaded.frame,
        column,
        upload_id=loaded.upload_id,
        detector=notice.finding.code,
        unknown=tuple(notice.extras.get("unknown", ())),
        evidence_id=looked.evidence_id,
    )
    suggestion = cached_suggestion(storage, ai, look, client_id=case.client_id, signature=notice.finding.signature)
    if suggestion is None:
        return out
    reason = unusable_reason(suggestion, column)
    if reason is not None:
        if suggestion.cannot or (suggestion.kind is not None and "not one of the fixes" in reason):
            record_tool_request(
                storage,
                detector=notice.finding.code,
                need=reason,
                place=case.case_id,
                client_id=case.client_id,
                at=clock(),
            )
            out.note = f"{CANNOT_SAFELY}: the AI says this needs a change no fix can make, so it is noted for the team."
        return out
    given = {
        "column": column,
        "kind": suggestion.kind,
        "params": suggestion.params,
        "reason": suggestion.reason,
        "evidence_ids": [looked.evidence_id],
    }
    try:
        tried = registry.call("propose_pattern_fix", given, dataclasses.replace(ctx, evidence=(looked,)))
    except AgentToolError as exc:
        out.note = f"The AI suggested a fix, but it is not offered: {exc.message}"
        return out
    out.evidence.append(tried)
    output = tried.output
    trial = output.get("trial") or {}
    pairs = trial.get("before_after") or []
    shown = "; ".join(
        f"“{p['before']}” → {('“' + str(p['after']) + '”') if p.get('after') is not None else 'empty'} ({p['rows']:,})"
        for p in pairs[:4]
    )
    unmapped = trial.get("unmapped") or []
    out.note = (
        f"The AI suggests a {suggestion.kind} fix (“{suggestion.reason[:120]}”), tried on the first "
        f"{int(trial.get('rows_tried', 0)):,} rows: {shown}. It changes {int(trial.get('changed', 0)):,} of "
        f"{int(trial.get('out_of', 0)):,} values"
        + (f"; it could not map {_quoted([str(v) for v in unmapped])}" if unmapped else "")
        + "."
        + (f" {output['warning']}" if output.get("warning") else "")
    )
    payload: dict[str, JsonValue] = {
        "upload_id": loaded.upload_id,
        "evidence_id": tried.evidence_id,
        "step": {
            "order": 1,
            "kind": str(suggestion.kind),
            "column": column,
            "params": dict(suggestion.params),
            "reason": suggestion.reason[:300] or "Suggested by the AI.",
        },
    }
    out.option = AskOption(
        label=AI_FIX_LABEL,
        effect=_effect(
            "ai_fix",
            f"Fix “{column}” as the AI suggests ({suggestion.kind}), approved by you.",
            payload,
            source=ProposalSource.AI.value,
        ),
        rows_changed=int(trial.get("changed", 0)),
    )
    return out


def notice_detector(
    storage: Storage,
    *,
    config_root: Path | None = None,
    offers: Sequence[Offer] = (),
    ai: Callable[[], PatternAI | None] | None = None,
    registry: DataToolRegistry | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> DetectorPort:
    """The visit's notice detector (`engine.doctor.visit.DetectorPort`; the module docstring).

    `ai` builds the AI's side when a new pattern needs it (connected lazily: no pattern, no connection); None or a
    builder returning None means no AI service: the rules' questions alone. Cached suggestions are reused either way.
    """
    built: list[PatternAI | None] = []

    def the_ai() -> PatternAI | None:
        if ai is None:
            return None
        if not built:
            built.append(ai())
        return built[0]

    def detect(case: Case, files: tuple[LoadedFile, ...], brief: GoalBrief | None) -> Detected:
        from engine.doctor.visit import Detected, visit_registry

        goal_id = brief.goal_id if brief is not None else None
        mode: Literal["train", "score"] = "train" if brief is not None else "score"
        baseline = _baseline(storage, case)
        config = _config_for(brief, offers, config_root)
        tools = registry if registry is not None else visit_registry()
        asks: list[Ask] = []
        evidence: list[DataToolResult] = []
        for loaded in files:
            notices: list[Notice] = []
            asked: set[str] = set()
            booleans: dict[str, tuple[str, ...]] = {}
            part = _slot_for(baseline, loaded) if baseline is not None else None
            if part is not None and part.contract is not None and baseline is not None:
                booleans = _booleans_of(part.recipe)
                report = drift_check(
                    loaded.frame,
                    part.contract,
                    upload_id=loaded.upload_id,
                    mode=mode,
                    recipe=part.recipe,
                    slot=part.slot,
                    prescription_hash=baseline.prescription_hash,
                )
                for finding in report.findings:
                    if finding.column in asked:
                        continue
                    made = drift_notice(
                        finding, upload_id=loaded.upload_id, rows=len(loaded.frame), file_name=loaded.file_name
                    )
                    if made is not None:
                        notices.append(made)
                        asked.add(str(finding.column))
            for found in file_notices(loaded, skip=frozenset(asked), known_booleans=booleans):
                if found.pattern and config is not None:
                    extra = _ai_option(
                        found,
                        loaded,
                        case,
                        ai=the_ai(),
                        storage=storage,
                        registry=tools,
                        config=config,
                        config_root=config_root,
                        clock=clock,
                    )
                    evidence.extend(extra.evidence)
                    if extra.option is not None or extra.note:
                        options = found.options if extra.option is None else (extra.option, *found.options)
                        found = Notice(
                            finding=found.finding,
                            upload_id=found.upload_id,
                            text=found.text,
                            why=f"{found.why} {extra.note}".strip(),
                            options=options,
                            rows=found.rows,
                            meaning_bearing=extra.option is not None,
                            evidence_ids=tuple(e.evidence_id for e in extra.evidence),
                            pattern=True,
                            extras=found.extras,
                        )
                notices.append(found)
            asks.extend(n.ask(goal_id) for n in notices)
        return Detected(asks=tuple(asks), evidence=tuple(evidence))

    return detect
