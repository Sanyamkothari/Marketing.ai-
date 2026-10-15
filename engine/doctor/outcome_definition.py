"""The outcome is defined, not edited (Plan O M239, DEC-1809; the design §5.5 D #45-#47, §6.6, §9.2).

The outcome decides what the model learns, so Data Doctor never picks it on its own and never changes it with a recipe
step (Plan L promise 6). It is written down instead, as an **outcome definition** (`OutcomeDefinition`, the plan's §5
contract), which a person approves word for word and an outcome builder applies:

* **`column_values`** - a column the file already has, read through a frozen map from each of its values to *yes*,
  *no* or *leave out* (`Active` -> no, `Churned` -> yes, `Suspended` -> left out). :func:`propose_value_map` is the
  rules' half of the `map_outcome_values` tool; :func:`apply_outcome_values` is the outcome builder for it - an
  executor, never a tool, bound to an approval of exactly this definition (`OutcomeApproval`).
* **`event_presence`, `event_absence`, `value_threshold`** - the outcome read from an event file, with an optional
  grace period, event files whose events in the window leave a customer out (`exclude`), and censoring (month-ends
  whose window is not over are left out, counted). :func:`propose_event_outcome` is the `define_outcome` tool's
  rules. Each definition is the onboarding builder's `LabelSpec` (:func:`label_spec_of`), measured by the builder's
  own `build_labels` on month-end snapshots, through the customer-ID rules M157's offer uses since M237 (a prefix,
  zeros, case, spaces, +91 phones; never a near match).

**Never picked silently.** What changes the meaning - which value is *yes*, which file and date the outcome is
read from, the window - is either stated by the caller or asked: a value the rules cannot place, a second event file,
a second date column or an unchosen window each become a blocking question with no recommended answer (the design
§6.5: "none for meaning-bearing questions"), and no definition is proposed until it is settled. What the rules do
place (a value spelled like yes or no) is a pending suggestion, never ticked, stated in the sentence the person
approves.

**Exact-text agreement.** Every definition comes with the sentence the person approves, counts included. The
approval sends that sentence back; if the server would now show another (the file or the definition changed),
nothing is recorded and `APPROVAL_SENTENCE_CHANGED` is raised (:func:`check_outcome_approval`, through M233's
`data.approve_class` rule, which also checks the role and separation of duties). Applying the definition later
(next month's file) needs only the approved definition's hash: its counts may change, its meaning may not, and a
value the approved map does not place stops the builder (`OUTCOME_VALUE_NOT_MAPPED`) rather than being guessed.

Nothing here reaches the AI service, and nothing here runs unless a caller of the Data Doctor registry (behind
`data_doctor.unified`) or a test calls it.
"""

from __future__ import annotations

import contextlib
import keyword
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import pandas as pd
from pydantic import AwareDatetime, Field, model_validator

from engine.agent.contracts import Question, QuestionOption
from engine.agent.key_steps import KeyRule
from engine.agentic.canonical import canonical_bytes, sha256_hex
from engine.config import StrictBase
from engine.onboarding.specs import LabelSpec

if TYPE_CHECKING:
    from engine.access.roles import Principal
    from engine.config import RoleCatalogue, UseCaseConfig
    from engine.doctor.case import Case, CaseStore
    from engine.doctor.outcomes import KeyRulePair, OutcomeCatalogue, OutcomeFile

__all__ = [
    "EVENT_KINDS",
    "MAX_OUTCOME_VALUES",
    "NO_WORDS",
    "OUTCOME_CODES",
    "OUTCOME_POLICY_KINDS",
    "EventOutcomeProposal",
    "OutcomeApproval",
    "OutcomeCounts",
    "OutcomeDefinition",
    "OutcomeError",
    "OutcomeKind",
    "OutcomeReceipt",
    "ValueCount",
    "ValueMapProposal",
    "ValueMeaning",
    "WindowChoice",
    "apply_outcome_values",
    "approval_for",
    "check_outcome_approval",
    "label_spec_of",
    "propose_event_outcome",
    "propose_value_map",
    "record_outcome_approval",
    "rules_meaning",
    "value_text",
]

OUTCOME_CODES: Final[frozenset[str]] = frozenset(
    {
        "OUTCOME_UNDECIDED",
        "OUTCOME_TOO_MANY_VALUES",
        "OUTCOME_CANNOT_MEASURE",
        "OUTCOME_NOT_APPROVED",
        "OUTCOME_VALUE_NOT_MAPPED",
    }
)
"""The codes M239 adds (for `PLAN_O_CODES` and `configs/pilot/help.yaml` at integration). The exact-text refusal
reuses the plan's `APPROVAL_SENTENCE_CHANGED`, a text regression target the readiness report's
`X_REGRESSION_TARGET_NUMERIC`, and the approval's role and separation refusals M233's `TOOL_ROLE_REQUIRED` and
`POLICY_ROLE_SEPARATION`."""

OUTCOME_POLICY_KINDS: Final[frozenset[str]] = frozenset({"data.define_outcome", "data.map_outcome_values"})
"""The policy kinds of the two tools that propose an outcome definition: APPROVE-class, always (design §6.6)."""

MAX_OUTCOME_VALUES: Final[int] = 20
"""A column with more different values than this is not an outcome a person can map value by value: a number is
an amount (regression) or a threshold (`define_outcome`), text with many values is not an outcome."""

NO_WORDS: Final[tuple[str, ...]] = (
    "0",
    "false",
    "f",
    "no",
    "n",
    "active",
    "retained",
    "stayed",
    "on time",
    "not churned",
    "not converted",
)
"""Values the rules suggest mean *no*. *Yes* is the use case's `target.positive_label` and the run's own positive
words (`engine.stages.validate.POSITIVE_TOKENS`: `1`, `true`, `yes`, `churned`, `converted`, ...). Nothing is ever
suggested as *leave out*: only a person decides that a value's rows are not learnt from."""

OutcomeKind = Literal["column_values", "event_presence", "event_absence", "value_threshold"]
EVENT_KINDS: Final[tuple[str, ...]] = ("event_presence", "event_absence", "value_threshold")
Meaning = Literal["yes", "no", "exclude"]
Operator = Literal[">", ">=", "<", "<="]
_OPERATOR_WORDS: Final[dict[str, str]] = {
    ">": "more than",
    ">=": "at least",
    "<": "less than",
    "<=": "at most",
}
_DEFAULT_WINDOWS: Final[tuple[int, ...]] = (30, 60, 90)
"""The windows offered when the use case has no sentence in `configs/outcomes.yaml`."""
_READABLE_SHARE: Final[float] = 0.95
"""A threshold column is read as numbers when at least this share of its non-empty values are numbers."""
_RESERVED: Final[frozenset[str]] = frozenset({"entity_key", "event_time", "snapshot_date"})


class OutcomeError(Exception):
    """An outcome that cannot be proposed, approved or applied, with a code the screen and the help explain."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
class ValueMeaning(StrictBase):
    """One value of an outcome column and what it means."""

    value: str = Field(description="The value as the outcome builder reads it (`value_text`).")
    meaning: Meaning = Field(description="`yes`, `no`, or `exclude` (its rows are left out, counted).")


class OutcomeDefinition(StrictBase):
    """What the model learns, written down (the plan's §5 `OutcomeDefinition`; the design §9.1-§9.2).

    The meaning only: no count, no wording. Its hash (:attr:`definition_hash`) is what an approval names and what a
    replay checks; the sentence the person approves (with this file's counts) is the proposal's.
    """

    kind: OutcomeKind
    name: str = Field(description="The outcome column of the dataset the model learns.")
    column: str | None = Field(
        default=None,
        description="`column_values`: the file's column read through `map`; `value_threshold`: the event file's "
        "column the threshold tests; null otherwise.",
    )
    map: tuple[ValueMeaning, ...] = Field(
        default=(), description="`column_values`: every value seen, with its meaning, sorted by value."
    )
    entity_upload_id: str | None = Field(default=None, description="Events: the customer list.")
    entity_id_column: str | None = None
    event_upload_id: str | None = Field(
        default=None, description="Events: the file the outcome is read from."
    )
    event_id_column: str | None = None
    date_column: str | None = Field(default=None, description="Events: the event file's date column.")
    event_role: str | None = Field(
        default=None, description="Events: what the event file is (an onboarding role)."
    )
    days: int | None = Field(
        default=None, ge=1, le=3650, description="Events: the window after the snapshot."
    )
    grace_days: int | None = Field(
        default=None, ge=1, le=365, description="Events: days' grace added to the window (Plan J M93)."
    )
    operator: Operator | None = Field(
        default=None, description="`value_threshold`: how `column` is compared."
    )
    threshold: float | None = Field(
        default=None, description="`value_threshold`: the number it is compared with."
    )
    exclude: tuple[str, ...] = Field(
        default=(),
        description="`column_values`: the values whose rows are left out; events: the roles of the event files "
        "whose events in the window leave a customer out (`exclude_roles`).",
    )
    exclude_upload_ids: tuple[str, ...] = Field(
        default=(), description="Events: the files `exclude` names, in the same order."
    )
    exclude_date_columns: tuple[str, ...] = Field(
        default=(),
        description="Events: the date column each of those files' window is read on, in the same order (asked "
        "when a file has several, never picked).",
    )
    censoring: Literal["leave_out"] | None = Field(
        default=None,
        description="Events: month-ends whose window is not over by the end of the data are left out, counted "
        "(`build_labels` with `drop_censored`); null for `column_values`, whose values are known.",
    )
    entity_key_rule: KeyRule | None = Field(
        default=None, description="Events: how the list's IDs are written when they meet only by a rule."
    )
    event_key_rule: KeyRule | None = Field(
        default=None, description="Events: how the event file's IDs are written then (`normalise_key`)."
    )

    @model_validator(mode="after")
    def _shape(self) -> OutcomeDefinition:
        if self.kind == "column_values":
            values = [item.value for item in self.map]
            if not self.column or self.column != self.name:
                raise ValueError("a column_values outcome reads its own column, in place")
            if not values or values != sorted(set(values)):
                raise ValueError("the map lists every value once, sorted")
            meanings = {item.meaning for item in self.map}
            if not {"yes", "no"} <= meanings:
                raise ValueError("the map gives both a yes and a no")
            if self.exclude != tuple(item.value for item in self.map if item.meaning == "exclude"):
                raise ValueError("exclude lists the values the map leaves out")
            if self.event_upload_id or self.days or self.operator or self.censoring:
                raise ValueError("a column_values outcome has no event file, window, threshold or censoring")
            if self.exclude_upload_ids or self.exclude_date_columns:
                raise ValueError("a column_values outcome has no file that leaves customers out")
            return self
        if self.map:
            raise ValueError("an outcome from events has no value map")
        needed = (self.entity_upload_id, self.event_upload_id, self.date_column, self.event_role, self.days)
        if any(value is None for value in needed) or self.censoring != "leave_out":
            raise ValueError("an outcome from events names its files, date, role, window and censoring")
        threshold = self.kind == "value_threshold"
        if threshold != (
            self.column is not None and self.operator is not None and self.threshold is not None
        ):
            raise ValueError("only a value_threshold outcome has a column, an operator and a threshold")
        if len(self.exclude) != len(self.exclude_upload_ids):
            raise ValueError("exclude and exclude_upload_ids name the same files")
        if len(self.exclude_date_columns) != len(self.exclude_upload_ids):
            raise ValueError("exclude_date_columns names the date of each file exclude_upload_ids names")
        return self

    @property
    def definition_hash(self) -> str:
        """sha256 of the canonical definition: what an approval names and a replay checks."""
        return sha256_hex(canonical_bytes(self.model_dump(mode="json")))

    def meaning_of(self) -> dict[str, Meaning]:
        return {item.value: item.meaning for item in self.map}


class OutcomeCounts(StrictBase):
    """What the definition makes of the file, counted (never estimated)."""

    measured_on: date | None = Field(
        default=None, description="Events: the latest month-end whose window is over; null for a column."
    )
    rows: int = Field(description="Rows (a column) or customers at `measured_on` (events) counted.")
    yes: int
    no: int
    excluded: int = Field(description="Rows or customers left out by the definition.")
    empty: int = Field(default=0, description="A column's rows with no value: left out.")
    not_over: int = Field(default=0, description="Events: later month-ends left out, their window not over.")
    yes_rate: float | None = Field(description="`yes` / (`yes` + `no`), 4 places.")


class OutcomeApproval(StrictBase):
    """A person's yes to one definition, with the sentence they read, word for word."""

    definition_hash: str
    sentence: str = Field(min_length=1)
    approved_by: str = Field(min_length=1, description="The user id; never an agent.")
    approved_at: AwareDatetime


def approval_for(definition: OutcomeDefinition, sentence: str, *, user_id: str, at: Any) -> OutcomeApproval:
    """The approval a person gives by reading `sentence` and saying yes (for screens and tests)."""
    return OutcomeApproval(
        definition_hash=definition.definition_hash, sentence=sentence, approved_by=user_id, approved_at=at
    )


def _rate(yes: int, no: int) -> float | None:
    return round(yes / (yes + no), 4) if yes + no else None


# ---------------------------------------------------------------------------
# column_values: the map (map_outcome_values)
# ---------------------------------------------------------------------------
def value_text(value: Any) -> str | None:
    """A cell as the outcome map reads it: whole numbers without `.0`, yes/no as `true`/`false`, text stripped;
    None for an empty cell. The proposal and the builder read values the same way."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if hasattr(value, "item") and not isinstance(value, str | bytes):
        with contextlib.suppress(AttributeError, ValueError):
            value = value.item()  # a numpy scalar as its Python value
        if isinstance(value, bool):
            return "true" if value else "false"
    if isinstance(value, float):
        if math.isnan(value):
            return None
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, int):
        return str(value)
    if value is pd.NaT:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


def _texts(series: pd.Series[Any]) -> pd.Series[Any]:
    return series.astype(object).map(value_text)


def rules_meaning(text: str, positive_label: Any = None) -> Meaning | None:
    """What the rules suggest a value means: the use case's positive label or a positive word is *yes*, a listed
    no-word is *no*; anything else is asked. Never *leave out*."""
    from engine.stages.validate import POSITIVE_TOKENS

    key = text.casefold()
    wanted = value_text(positive_label) if positive_label is not None else None
    if wanted is not None and key == wanted.casefold():
        return "yes"
    if key in POSITIVE_TOKENS:
        return "yes"
    if key in NO_WORDS:
        return "no"
    return None


class ValueCount(StrictBase):
    """One value of the outcome column: its rows, and its meaning when it has one."""

    value: str
    rows: int
    meaning: Meaning | None = Field(description="Null while nobody has said what it means.")
    placed_by: Literal["rules", "person"] | None = Field(description="Who gave the meaning.")


class ValueMapProposal(StrictBase):
    """The rules' (and the person's) map of one column's values, before the tool adds who proposed it."""

    column: str
    rows: int
    empty: int
    values: tuple[ValueCount, ...]
    definition: OutcomeDefinition | None
    counts: OutcomeCounts | None
    sentence: str | None
    questions: tuple[Question, ...]
    blocking: tuple[str, ...]
    summary: str


def _quoted(values: Sequence[str]) -> str:
    shown = [f"'{value}'" for value in values]
    if len(shown) <= 1:
        return "".join(shown)
    return ", ".join(shown[:-1]) + " and " + shown[-1]


def _value_question(column: str, index: int, item: ValueCount) -> Question:
    plural = "row" if item.rows == 1 else "rows"
    options = (
        QuestionOption(option_id="yes", label="Yes", effect="Its rows count as yes."),
        QuestionOption(option_id="no", label="No", effect="Its rows count as no."),
        QuestionOption(
            option_id="exclude",
            label="Leave them out",
            effect="Its rows are left out of training and testing.",
        ),
        QuestionOption(
            option_id="dont-know", label="I don't know", effect="Nothing is decided; the outcome stays open."
        ),
    )
    return Question(
        question_id=f"outcome-value-{index}",
        text=f"In '{column}', what does '{item.value}' mean ({item.rows:,} {plural})?",
        options=options,
        blocking=True,
        examples=(item.value,),
    )


def column_sentence(definition: OutcomeDefinition, counts: OutcomeCounts) -> str:
    """The sentence a `column_values` definition is approved with, its counts included."""
    by: dict[str, list[str]] = {"yes": [], "no": [], "exclude": []}
    for item in definition.map:
        by[item.meaning].append(item.value)
    yes, no = by["yes"], by["no"]
    text = (
        f"The model learns '{definition.name}' from its values: {_quoted(yes)} "
        f"{'means' if len(yes) == 1 else 'mean'} yes ({counts.yes:,} rows), {_quoted(no)} "
        f"{'means' if len(no) == 1 else 'mean'} no ({counts.no:,} rows)"
    )
    if by["exclude"]:
        text += f"; the {counts.excluded:,} rows with {_quoted(by['exclude'])} are left out of training and testing"
    if counts.empty:
        text += f"; {counts.empty:,} rows with no value are left out"
    return text + "."


def propose_value_map(
    frame: pd.DataFrame,
    column: str,
    *,
    positive_label: Any = None,
    given: Mapping[str, Meaning] | None = None,
) -> ValueMapProposal:
    """The map of `column`'s values to yes, no or leave out: the person's answers (`given`) first, then the rules'
    suggestions; every other value is a blocking question. A definition is proposed only when every value has a
    meaning and there is a yes and a no; its sentence carries the counts."""
    if column not in frame.columns:
        raise OutcomeError("OUTCOME_CANNOT_MEASURE", f"There is no column '{column}' in this file.")
    texts = _texts(frame[column])
    empty = int(texts.isna().sum())
    counted = texts.dropna().value_counts()
    if len(counted) > MAX_OUTCOME_VALUES:
        raise OutcomeError(
            "OUTCOME_TOO_MANY_VALUES",
            f"'{column}' has {len(counted):,} different values; an outcome is mapped value by value only up to "
            f"{MAX_OUTCOME_VALUES}. A number is an amount to predict, or a threshold (define_outcome).",
        )
    answers = dict(given or {})
    unknown = sorted(set(answers) - set(map(str, counted.index)))
    if unknown:
        raise OutcomeError(
            "OUTCOME_UNDECIDED",
            f"{len(unknown)} of the answers name a value '{column}' does not have; send the values as shown.",
        )
    items: list[ValueCount] = []
    for value in sorted(map(str, counted.index)):
        rows = int(counted[value])
        if value in answers:
            items.append(ValueCount(value=value, rows=rows, meaning=answers[value], placed_by="person"))
            continue
        suggested = rules_meaning(value, positive_label)
        items.append(
            ValueCount(value=value, rows=rows, meaning=suggested, placed_by="rules" if suggested else None)
        )
    open_items = [item for item in items if item.meaning is None]
    questions = tuple(_value_question(column, index, item) for index, item in enumerate(open_items, start=1))
    meanings = {item.meaning for item in items}
    blocking: list[str] = []
    if open_items or not {"yes", "no"} <= meanings:
        blocking.append("OUTCOME_UNDECIDED")
    if blocking:
        if open_items:
            summary = (
                f"{len(open_items)} of the {len(items)} values of '{column}' {'has' if len(open_items) == 1 else 'have'} no meaning yet; say what each "
                "means before the outcome can be approved."
            )
        else:
            summary = f"Every value of '{column}' means the same; an outcome needs a yes and a no."
        return ValueMapProposal(
            column=column,
            rows=len(frame),
            empty=empty,
            values=tuple(items),
            definition=None,
            counts=None,
            sentence=None,
            questions=questions,
            blocking=tuple(blocking),
            summary=summary,
        )
    mapping = tuple(ValueMeaning(value=item.value, meaning=item.meaning) for item in items if item.meaning)
    definition = OutcomeDefinition(
        kind="column_values",
        name=column,
        column=column,
        map=mapping,
        exclude=tuple(item.value for item in mapping if item.meaning == "exclude"),
    )
    yes = sum(item.rows for item in items if item.meaning == "yes")
    no = sum(item.rows for item in items if item.meaning == "no")
    excluded = sum(item.rows for item in items if item.meaning == "exclude")
    counts = OutcomeCounts(
        rows=len(frame), yes=yes, no=no, excluded=excluded, empty=empty, yes_rate=_rate(yes, no)
    )
    return ValueMapProposal(
        column=column,
        rows=len(frame),
        empty=empty,
        values=tuple(items),
        definition=definition,
        counts=counts,
        sentence=column_sentence(definition, counts),
        questions=(),
        blocking=(),
        summary=f"Every value of '{column}' has a meaning; approve the sentence to use it as the outcome.",
    )


class OutcomeReceipt(StrictBase):
    """What the outcome builder did to a file: every row in and out counted."""

    column: str
    definition_hash: str
    rows_in: int
    rows_out: int
    yes: int
    no: int
    excluded: int
    empty: int
    by_value: tuple[ValueCount, ...]


def apply_outcome_values(
    frame: pd.DataFrame, definition: OutcomeDefinition, approval: OutcomeApproval
) -> tuple[pd.DataFrame, OutcomeReceipt]:
    """The outcome builder for `column_values` (an executor, never a tool; the design §5.1, §9.2).

    Runs only the approved definition (its hash; the sentence was checked when the approval was recorded, and next
    month's counts may differ). The column becomes 1 for *yes* and 0 for *no*, in place; rows whose value is left
    out, and rows with no value, are removed and counted. A value the map does not place stops it
    (`OUTCOME_VALUE_NOT_MAPPED`): a new value is asked, never guessed. Nothing else in the file changes.
    """
    if definition.kind != "column_values" or definition.column is None:
        raise OutcomeError(
            "OUTCOME_CANNOT_MEASURE",
            "An outcome from events is built by the dataset builder (build_dataset), not from one file.",
        )
    if approval.definition_hash != definition.definition_hash:
        raise OutcomeError(
            "OUTCOME_NOT_APPROVED",
            "This outcome is not the one that was approved; review it and approve again.",
        )
    column = definition.column
    if column not in frame.columns:
        raise OutcomeError("OUTCOME_CANNOT_MEASURE", f"There is no column '{column}' in this file.")
    texts = _texts(frame[column])
    meaning = definition.meaning_of()
    unplaced = texts.notna() & ~texts.isin(set(meaning))
    if bool(unplaced.any()):
        distinct = int(texts[unplaced].nunique())
        raise OutcomeError(
            "OUTCOME_VALUE_NOT_MAPPED",
            f"{int(unplaced.sum()):,} rows of '{column}' hold {distinct} value{'s' if distinct != 1 else ''} the "
            "approved outcome does not place; say what each means and approve the outcome again.",
        )
    meant = texts.map(meaning)
    keep = meant.isin(("yes", "no"))
    out = frame.loc[keep].copy()
    out[column] = (meant[keep] == "yes").astype("int64")
    counted = texts.dropna().value_counts()
    by_value = tuple(
        ValueCount(value=value, rows=int(counted[value]), meaning=meaning[value], placed_by=None)
        for value in sorted(map(str, counted.index))
    )
    receipt = OutcomeReceipt(
        column=column,
        definition_hash=definition.definition_hash,
        rows_in=len(frame),
        rows_out=len(out),
        yes=int((meant == "yes").sum()),
        no=int((meant == "no").sum()),
        excluded=int((meant == "exclude").sum()),
        empty=int(texts.isna().sum()),
        by_value=by_value,
    )
    return out.reset_index(drop=True), receipt


# ---------------------------------------------------------------------------
# Events: define_outcome
# ---------------------------------------------------------------------------
def label_spec_of(definition: OutcomeDefinition, *, description: str = "") -> LabelSpec:
    """The onboarding builder's `LabelSpec` an event definition is (what `build_dataset` builds with)."""
    if definition.kind not in EVENT_KINDS or definition.days is None:
        raise OutcomeError(
            "OUTCOME_CANNOT_MEASURE", "Only an outcome from events is built by the dataset builder."
        )
    from engine.config import LabelType

    expression: str | None = None
    if definition.kind == "value_threshold":
        expression = f"{definition.column} {definition.operator} {_number(definition.threshold)}"
    return LabelSpec(
        name=definition.name,
        type=LabelType(definition.kind),
        role=definition.event_role,
        horizon_days=definition.days,
        expression=expression,
        description=description,
        grace_days=definition.grace_days,
        exclude_roles=definition.exclude,
    )


def _number(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


class WindowChoice(StrictBase):
    """One window the outcome could be read over, measured."""

    days: int
    counts: OutcomeCounts | None = Field(description="Null when no month-end has this window over.")


class EventOutcomeProposal(StrictBase):
    """`define_outcome`'s rules, before the tool adds who proposed it."""

    definition: OutcomeDefinition | None
    counts: OutcomeCounts | None
    sentence: str | None
    choices: tuple[WindowChoice, ...]
    questions: tuple[Question, ...]
    blocking: tuple[str, ...]
    entity_file_name: str | None
    event_file_name: str | None
    data_end: date | None
    notes: tuple[str, ...]


@dataclass(frozen=True)
class _Paired:
    """One event file met to the customer list: as written, or through a rule pair (M237)."""

    file: OutcomeFile
    role: str
    id_column: str
    coverage: float
    rules: KeyRulePair | None


def _event_role(file: OutcomeFile, roles: RoleCatalogue) -> str | None:
    kind = file.diagnosis.kind
    if kind is None or kind not in roles.roles:
        kind = "other_event" if "other_event" in roles.roles else None
    if kind is None or not roles.roles[kind].is_event:
        return None
    return kind


def _pair(
    file: OutcomeFile, roles: RoleCatalogue, entity: OutcomeFile, entity_keys: pd.Series[Any], entity_ids: Any
) -> _Paired | None:
    """The event file's customer column against the list: as written (`join_coverage`), else through the rules
    M157's offer uses since M237 (`propose_key_rules`; never a near match)."""
    from engine.doctor.outcomes import _join_column, _ruled_join_column

    role = _event_role(file, roles)
    if role is None or not file.diagnosis.date_columns:
        return None
    entity_id = entity.diagnosis.id_column or ""
    joined = _join_column(file, entity_keys, entity_id)
    if joined is not None:
        return _Paired(file, role, joined[0], joined[1], None)
    ruled = _ruled_join_column(file, entity_ids, entity_id)
    if ruled is None:
        return None
    return _Paired(file, role, ruled[0], ruled[1], ruled[2])


def _table(
    paired: _Paired, date_column: str, value_column: str | None = None
) -> tuple[pd.DataFrame, int, int]:
    """`(entity_key, event_time[, value_column])` read as the builder reads them (`outcomes._event_table`), the
    rows whose date could not be read, and the rows whose value is not a number."""
    from engine.agent.key_steps import KeyColumn
    from engine.config import StandardType
    from engine.doctor.outcomes import _key_text
    from engine.onboarding.transforms import cast_series

    frame = paired.file.frame
    times = pd.to_datetime(cast_series(frame[date_column], StandardType.DATE).values)
    keys = frame[paired.id_column]
    if paired.rules is not None:
        keys = pd.Series(KeyColumn(keys).rows_of(paired.rules.event), index=frame.index, dtype=object)
    table = pd.DataFrame({"entity_key": keys.to_numpy(), "event_time": times})
    unreadable = int((frame[date_column].notna().to_numpy() & table["event_time"].isna().to_numpy()).sum())
    not_numbers = 0
    if value_column is not None:
        raw = frame[value_column]
        numbers = pd.to_numeric(raw, errors="coerce")
        not_numbers = int((raw.notna() & numbers.isna()).sum())
        table[value_column] = numbers.to_numpy()
    table = table.dropna(subset=["entity_key", "event_time"])
    if paired.rules is None:
        table["entity_key"] = _key_text(table["entity_key"])
    return table.reset_index(drop=True), unreadable, not_numbers


def _counted(
    entity_keys: pd.Series[Any],
    table: pd.DataFrame,
    spec: LabelSpec,
    *,
    excluded_tables: Mapping[str, pd.DataFrame],
    inclusive: bool,
) -> OutcomeCounts | None:
    """`build_labels` on month-end snapshots near the data's end (M157's `_measure`, with the tables an exclusion
    reads registered too); the latest month-end whose window is over. None when there is none."""
    import duckdb

    from engine.doctor.outcomes import LOOK_BACK_SLACK_DAYS
    from engine.onboarding.labels import build_labels
    from engine.onboarding.snapshots import plan_snapshots
    from engine.onboarding.specs import SnapshotFrequency, SnapshotMode, SnapshotSpec

    window = spec.window_days or 0
    first = pd.Timestamp(table["event_time"].min()).date()
    end = pd.Timestamp(table["event_time"].max()).date()
    # A window is over only where every table the label reads has data (`build_labels`), so the month-ends
    # measured reach back from the earliest end of them.
    known = [
        pd.Timestamp(frame["event_time"].max()).date() for frame in excluded_tables.values() if len(frame)
    ]
    start = max(first, min([end, *known]) - timedelta(days=window + LOOK_BACK_SLACK_DAYS))
    plan = plan_snapshots(
        SnapshotSpec(
            mode=SnapshotMode.PERIODIC,
            frequency=SnapshotFrequency.MONTHLY,
            start=start,
            end=end,
            max_snapshots=120,
            min_history_days=0,
        ),
        first_event=first,
        last_event=end,
        horizon_days=0,
    )
    if not plan.dates:
        return None
    entities = pd.DataFrame({"entity_key": entity_keys.drop_duplicates().reset_index(drop=True)})
    spine = pd.DataFrame({"snapshot_date": pd.to_datetime(list(plan.dates))})
    snapshots = entities.merge(spine, how="cross").reset_index(drop=True)
    role = str(spec.role)
    con = duckdb.connect()
    try:
        con.register(role, table)
        for other, frame in excluded_tables.items():
            con.register(other, frame)
        result = build_labels(
            con,
            spec,
            snapshots,
            drop_censored=True,
            mapped_roles={role, *excluded_tables},
            mode=SnapshotMode.PERIODIC,
            inclusive=inclusive,
        )
    finally:
        con.close()
    complete = [stat for stat in result.per_snapshot if not stat.censored and stat.positives is not None]
    not_over = sum(1 for stat in result.per_snapshot if stat.censored)
    if not complete:
        return None
    latest = complete[-1]
    yes = int(latest.positives or 0)
    no = int(latest.entities) - yes
    return OutcomeCounts(
        measured_on=latest.date,
        rows=int(latest.entities),
        yes=yes,
        no=no,
        excluded=int(latest.excluded or 0),
        not_over=not_over,
        yes_rate=_rate(yes, no),
    )


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


@dataclass(frozen=True)
class ExcludedFile:
    """A file whose events in the window leave a customer out, as its sentence names it."""

    word: str
    file_name: str
    date_column: str
    unreadable: int = 0


def _unread_words(rows: int, file_name: str, column: str, effect: str, effect_one: str) -> str:
    """'N rows of <file> have a '<date>' that cannot be read as a date and <effect>.' (one row: singular)."""
    if rows == 1:
        return f" 1 row of {file_name} has a '{column}' that cannot be read as a date and {effect_one}."
    return f" {rows:,} rows of {file_name} have a '{column}' that cannot be read as a date and {effect}."


def event_sentence(
    definition: OutcomeDefinition,
    *,
    entity: str,
    event: str,
    event_file: str,
    excluded_files: Sequence[ExcludedFile],
    counts: OutcomeCounts | None,
    data_end: date,
    rules_note: str | None,
    unreadable: int = 0,
    not_numbers: int = 0,
) -> str:
    """The sentence an event definition is approved with: what *yes* means, the window and its grace, which date
    each file is read on, who is left out, every row that counts for nothing (a date that cannot be read, a
    threshold value that is not a number), the censoring, how the IDs meet and the counts. A change in any of
    them is a new sentence, so an approval of the old one no longer holds (`APPROVAL_SENTENCE_CHANGED`)."""
    days = definition.days or 0
    an_entity = f"{_article(entity).capitalize()} {entity}"
    if definition.kind == "event_presence":
        text = f"{an_entity} is 'yes' when there is {_article(event)} {event} in {event_file} within {days} days after the snapshot"
    elif definition.kind == "event_absence":
        text = f"{an_entity} is 'yes' when there is no {event} in {event_file} for {days} days after the snapshot"
    else:
        words = _OPERATOR_WORDS[str(definition.operator)]
        text = (
            f"{an_entity} is 'yes' when {_article(event)} {event} in {event_file} within {days} days after the "
            f"snapshot has '{definition.column}' {words} {_number(definition.threshold)}"
        )
    if definition.grace_days:
        text += f", with {definition.grace_days} days' grace ({days + definition.grace_days} days in all)"
    text += f", read on '{definition.date_column}'; the outcome is saved as '{definition.name}'."
    if unreadable:
        text += _unread_words(
            unreadable, event_file, str(definition.date_column), "count as no event", "counts as no event"
        )
    if not_numbers:
        text += (
            f" 1 '{definition.column}' value is not a number and never meets the threshold."
            if not_numbers == 1
            else f" {not_numbers:,} '{definition.column}' values are not numbers and never meet the threshold."
        )
    for item in excluded_files:
        text += (
            f" {an_entity} with {_article(item.word)} {item.word} in {item.file_name} in that window, read on "
            f"'{item.date_column}', is left out."
        )
        if item.unreadable:
            text += _unread_words(
                item.unreadable, item.file_name, item.date_column, "leave nobody out", "leaves nobody out"
            )
    if rules_note:
        text += f" {rules_note}"
    text += f" Month-ends whose window is not over by {data_end:%d %b %Y} are left out"
    if counts is not None and counts.not_over:
        text += f" ({counts.not_over} month-end{'s' if counts.not_over != 1 else ''})"
    text += "."
    if counts is None or counts.measured_on is None:
        text += f" No month-end has its {days} days over yet, so nothing is counted."
        return text
    text += (
        f" At {counts.measured_on:%d %b %Y}, {counts.yes:,} of {counts.rows:,} {entity}s "
        f"({(counts.yes_rate or 0.0):.1%}) are 'yes'"
    )
    if counts.excluded:
        text += f"; {counts.excluded:,} are left out"
    return text + "."


def _choose_question(question_id: str, text: str, options: Sequence[tuple[str, str, str]]) -> Question:
    return Question(
        question_id=question_id,
        text=text,
        options=tuple(
            QuestionOption(option_id=oid, label=label, effect=effect) for oid, label, effect in options
        ),
        blocking=True,
    )


def _undecided(
    entity_file_name: str | None,
    event_file_name: str | None,
    question: Question,
    *,
    choices: tuple[WindowChoice, ...] = (),
    data_end: date | None = None,
    notes: tuple[str, ...] = (),
) -> EventOutcomeProposal:
    """A proposal still waiting for one meaning-bearing answer: no definition, one blocking question."""
    return EventOutcomeProposal(
        definition=None,
        counts=None,
        sentence=None,
        choices=choices,
        questions=(question,),
        blocking=("OUTCOME_UNDECIDED",),
        entity_file_name=entity_file_name,
        event_file_name=event_file_name,
        data_end=data_end,
        notes=notes,
    )


def _counts_words(counts: OutcomeCounts | None) -> str:
    if counts is None or counts.measured_on is None:
        return "Not counted: no month-end has this window over yet."
    return f"{counts.yes:,} of {counts.rows:,} are 'yes' at {counts.measured_on:%d %b %Y}."


def propose_event_outcome(
    files: Sequence[OutcomeFile],
    config: UseCaseConfig,
    roles: RoleCatalogue,
    catalogue: OutcomeCatalogue,
    *,
    kind: Literal["event_presence", "event_absence", "value_threshold"],
    event_upload_id: str | None = None,
    date_column: str | None = None,
    days: int | None = None,
    grace_days: int | None = None,
    exclude_upload_ids: Sequence[str] = (),
    exclude_date_columns: Mapping[str, str] | None = None,
    column: str | None = None,
    operator: Operator | None = None,
    threshold: float | None = None,
) -> EventOutcomeProposal:
    """`define_outcome`'s rules: the outcome from an event file, measured by the builder's `build_labels`.

    The customer list is the diagnosis's (M157's rule). The event file, its date column and the window are the
    caller's, and so is the date each file that leaves customers out is read on (`exclude_date_columns`, by upload
    id); when one is not given and more than one is possible, it is a blocking question and no definition is
    proposed. Rows whose date cannot be read and threshold values that are not numbers are counted in the
    sentence the person approves. Every count is the label builder's, through the ID rules M157's offer uses.
    """
    from engine.agent.key_steps import KeyColumn
    from engine.doctor.outcomes import _entity_file, _key_text, _label_name, event_word

    name = _label_name(config)
    if name is None:
        raise OutcomeError("OUTCOME_CANNOT_MEASURE", "This use case names no outcome column to build.")
    entity = _entity_file(files, roles)
    if entity is None or entity.diagnosis.id_column is None:
        raise OutcomeError(
            "OUTCOME_CANNOT_MEASURE",
            "No file is a customer list with an ID column, so nobody can be counted.",
        )
    entity_keys = _key_text(entity.frame[entity.diagnosis.id_column])
    entity_ids = KeyColumn(entity.frame[entity.diagnosis.id_column])
    others = [file for file in files if file is not entity]
    paired = [p for p in (_pair(file, roles, entity, entity_keys, entity_ids) for file in others) if p]
    if event_upload_id is not None:
        if not any(file.diagnosis.upload_id == event_upload_id for file in others):
            raise OutcomeError("OUTCOME_CANNOT_MEASURE", "That event file is not one of the case's files.")
        paired = [p for p in paired if p.file.diagnosis.upload_id == event_upload_id]
        if not paired:
            raise OutcomeError(
                "OUTCOME_CANNOT_MEASURE",
                "That file is not an event file with a date whose customer IDs meet the customer list, as "
                "written or once written one way; nothing is matched on a near match.",
            )
    if not paired:
        raise OutcomeError(
            "OUTCOME_CANNOT_MEASURE",
            "No event file with a date has customer IDs that meet the customer list; nothing is matched on a "
            "near match.",
        )
    if len(paired) > 1 and event_upload_id is None:
        question = _choose_question(
            "outcome-event-file",
            "Which file is the outcome read from?",
            [
                (
                    f"file-{index}",
                    p.file.diagnosis.file_name,
                    f"Read the outcome from {p.file.diagnosis.upload_id}.",
                )
                for index, p in enumerate(paired, start=1)
            ],
        )
        return _undecided(entity.diagnosis.file_name, None, question)
    chosen = paired[0]
    diagnosis = chosen.file.diagnosis
    if date_column is not None and date_column not in diagnosis.date_columns:
        raise OutcomeError(
            "OUTCOME_CANNOT_MEASURE", f"'{date_column}' is not a date column of {diagnosis.file_name}."
        )
    if date_column is None and len(diagnosis.date_columns) > 1:
        question = _choose_question(
            "outcome-date-column",
            f"Which date in {diagnosis.file_name} says when each event happened?",
            [
                (f"date-{index}", name_, f"Read the window on '{name_}'.")
                for index, name_ in enumerate(diagnosis.date_columns[:8], start=1)
            ],
        )
        return _undecided(entity.diagnosis.file_name, diagnosis.file_name, question)
    when = date_column or diagnosis.date_columns[0]
    value_column: str | None = None
    if kind == "value_threshold":
        if column is None or operator is None or threshold is None:
            raise OutcomeError(
                "OUTCOME_CANNOT_MEASURE",
                "A threshold outcome names its column, how it compares and the number.",
            )
        if column not in chosen.file.frame.columns:
            raise OutcomeError(
                "OUTCOME_CANNOT_MEASURE", f"There is no column '{column}' in {diagnosis.file_name}."
            )
        if not column.isidentifier() or keyword.iskeyword(column) or column in _RESERVED:
            raise OutcomeError(
                "OUTCOME_CANNOT_MEASURE",
                f"'{column}' cannot be tested by name; rename it to letters, digits and '_' first.",
            )
        if not math.isfinite(threshold):
            raise OutcomeError("OUTCOME_CANNOT_MEASURE", "The threshold is a number.")
        value_column = column
    table, unreadable, not_numbers = _table(chosen, when, value_column)
    if table.empty:
        raise OutcomeError("OUTCOME_CANNOT_MEASURE", f"No row of {diagnosis.file_name} has a readable date.")
    if value_column is not None:
        filled = int(chosen.file.frame[value_column].notna().sum())
        if filled == 0 or (filled - not_numbers) / filled < _READABLE_SHARE:
            raise OutcomeError(
                "OUTCOME_CANNOT_MEASURE",
                f"{not_numbers:,} of {filled:,} values in '{value_column}' are not numbers; a threshold reads "
                "numbers only.",
            )
    notes: list[str] = []
    if unreadable:
        notes.append(
            f"{unreadable:,} rows of {diagnosis.file_name} have a '{when}' that could not be read as a date; they count for nothing."
        )
    if not_numbers:
        notes.append(
            f"{not_numbers:,} values in '{value_column}' are not numbers; those events never meet the threshold."
        )
    excluded_dates = dict(exclude_date_columns or {})
    unknown = set(excluded_dates) - set(exclude_upload_ids)
    if unknown:
        raise OutcomeError(
            "OUTCOME_CANNOT_MEASURE",
            "A date is given for a file that does not leave customers out: "
            + ", ".join(sorted(unknown))
            + ".",
        )
    for file in (entity, chosen.file):
        if len(file.frame) < file.diagnosis.rows:
            notes.append(
                f"Counted on a random sample of {len(file.frame):,} of {file.diagnosis.rows:,} rows of {file.diagnosis.file_name}."
            )
    entity_rule = chosen.rules.entity if chosen.rules is not None else None
    if chosen.rules is not None:
        entity_keys = pd.Series(entity_ids.rows_of(chosen.rules.entity), dtype=object).dropna()
    excluded_tables: dict[str, pd.DataFrame] = {}
    excluded_files: list[ExcludedFile] = []
    exclude_roles: list[str] = []
    for upload_id in dict.fromkeys(exclude_upload_ids):
        match = next((file for file in others if file.diagnosis.upload_id == upload_id), None)
        if match is None or match is chosen.file:
            raise OutcomeError(
                "OUTCOME_CANNOT_MEASURE",
                "A file that leaves customers out is another event file of the case.",
            )
        pair = _pair(match, roles, entity, _key_text(entity.frame[entity.diagnosis.id_column]), entity_ids)
        same_rule = pair is not None and (
            (pair.rules is None and chosen.rules is None)
            or (
                pair.rules is not None
                and chosen.rules is not None
                and pair.rules.entity == chosen.rules.entity
            )
        )
        if pair is None or not same_rule:
            raise OutcomeError(
                "OUTCOME_CANNOT_MEASURE",
                f"The customer IDs of {match.diagnosis.file_name} do not meet the customer list the way "
                f"{diagnosis.file_name}'s do.",
            )
        if pair.role == chosen.role or pair.role in exclude_roles:
            raise OutcomeError(
                "OUTCOME_CANNOT_MEASURE",
                f"{match.diagnosis.file_name} is read as the same kind of file as another the outcome reads.",
            )
        moved = match.diagnosis
        excluded_date = excluded_dates.get(upload_id)
        if excluded_date is not None and excluded_date not in moved.date_columns:
            raise OutcomeError(
                "OUTCOME_CANNOT_MEASURE", f"'{excluded_date}' is not a date column of {moved.file_name}."
            )
        if excluded_date is None and len(moved.date_columns) > 1:
            question = _choose_question(
                "outcome-exclude-date-column",
                f"Which date in {moved.file_name} says when each event that leaves a {config.entity or 'customer'} "
                "out happened?",
                [
                    (f"date-{index}", name_, f"Read {moved.upload_id}'s window on '{name_}'.")
                    for index, name_ in enumerate(moved.date_columns[:8], start=1)
                ],
            )
            return _undecided(entity.diagnosis.file_name, diagnosis.file_name, question, notes=tuple(notes))
        excluded_date = excluded_date or moved.date_columns[0]
        excluded_table, excluded_bad, _ = _table(pair, excluded_date)
        if excluded_bad:
            notes.append(
                f"{excluded_bad:,} rows of {moved.file_name} have a '{excluded_date}' that could not be read as a "
                "date; they leave nobody out."
            )
        if len(match.frame) < moved.rows:
            notes.append(
                f"Counted on a random sample of {len(match.frame):,} of {moved.rows:,} rows of {moved.file_name}."
            )
        excluded_tables[pair.role] = excluded_table
        exclude_roles.append(pair.role)
        excluded_dates[upload_id] = excluded_date
        excluded_files.append(
            ExcludedFile(
                word=event_word(moved.file_name, pair.role, catalogue),
                file_name=moved.file_name,
                date_column=excluded_date,
                unreadable=excluded_bad,
            )
        )
    template = catalogue.templates.get(config.id)
    windows = (days,) if days is not None else (template.days if template is not None else _DEFAULT_WINDOWS)
    inclusive = config.onboarding.snapshots.inclusive_snapshot_time
    data_end = min(
        pd.Timestamp(frame["event_time"].max()).date()
        for frame in (table, *excluded_tables.values())
        if len(frame)
    )
    word = event_word(diagnosis.file_name, chosen.role, catalogue)
    noun = config.entity or "customer"
    rules_note: str | None = None
    if chosen.rules is not None:
        rules_note = (
            f"The IDs of {diagnosis.file_name} meet {entity.diagnosis.file_name} once written one way "
            f"({entity.diagnosis.file_name}: {chosen.rules.entity.words()}; {diagnosis.file_name}: "
            f"{chosen.rules.event.words()}); nothing is matched on a near match."
        )

    def definition_for(window: int) -> OutcomeDefinition:
        return OutcomeDefinition(
            kind=kind,
            name=name,
            column=value_column,
            entity_upload_id=entity.diagnosis.upload_id,
            entity_id_column=entity.diagnosis.id_column,
            event_upload_id=diagnosis.upload_id,
            event_id_column=chosen.id_column,
            date_column=when,
            event_role=chosen.role,
            days=window,
            grace_days=grace_days,
            operator=operator if value_column else None,
            threshold=float(threshold) if value_column and threshold is not None else None,
            exclude=tuple(exclude_roles),
            exclude_upload_ids=tuple(dict.fromkeys(exclude_upload_ids)),
            exclude_date_columns=tuple(
                excluded_dates[upload_id] for upload_id in dict.fromkeys(exclude_upload_ids)
            ),
            censoring="leave_out",
            entity_key_rule=entity_rule,
            event_key_rule=chosen.rules.event if chosen.rules is not None else None,
        )

    def measured(definition: OutcomeDefinition) -> OutcomeCounts | None:
        try:
            spec = label_spec_of(definition)
        except (ValueError, OutcomeError) as exc:  # a definition the label model refuses is not proposed
            raise OutcomeError("OUTCOME_CANNOT_MEASURE", f"The outcome cannot be built: {exc}") from exc
        return _counted(entity_keys, table, spec, excluded_tables=excluded_tables, inclusive=inclusive)

    choices = tuple(WindowChoice(days=window, counts=measured(definition_for(window))) for window in windows)
    if days is None:
        question = _choose_question(
            "outcome-window",
            "Over how many days after the snapshot is the outcome read?",
            [
                (f"days-{choice.days}", f"{choice.days} days", _counts_words(choice.counts))
                for choice in choices
            ],
        )
        return _undecided(
            entity.diagnosis.file_name,
            diagnosis.file_name,
            question,
            choices=choices,
            data_end=data_end,
            notes=tuple(notes),
        )
    definition = definition_for(days)
    counts = choices[0].counts
    sentence = event_sentence(
        definition,
        entity=noun,
        event=word,
        event_file=diagnosis.file_name,
        excluded_files=excluded_files,
        counts=counts,
        data_end=data_end,
        rules_note=rules_note,
        unreadable=unreadable,
        not_numbers=not_numbers,
    )
    return EventOutcomeProposal(
        entity_file_name=entity.diagnosis.file_name,
        event_file_name=diagnosis.file_name,
        data_end=data_end,
        notes=tuple(notes),
        definition=definition,
        counts=counts,
        sentence=sentence,
        choices=choices,
        questions=(),
        blocking=(),
    )


# ---------------------------------------------------------------------------
# Approval: the exact sentence (M233's `data.approve_class`)
# ---------------------------------------------------------------------------
def check_outcome_approval(
    *,
    kind: str,
    definition: OutcomeDefinition,
    current_sentence: str | None,
    approval: OutcomeApproval,
    approver: Principal,
    proposed_by: str | None = None,
    approval_required: bool = False,
    config_root: Path | None = None,
) -> None:
    """Refuse an approval that does not hold, recording nothing.

    The approval names this definition (`OUTCOME_NOT_APPROVED`) and is the approver's own; then M233's rule set for
    `kind` decides at approval: the approver holds its role (`TOOL_ROLE_REQUIRED`), the sentence they read is, word
    for word, the one the server shows now (`APPROVAL_SENTENCE_CHANGED`), and with `governance.approval_required`
    they did not propose it themselves (`POLICY_ROLE_SEPARATION`). A kind with no rule set is refused
    (`POLICY_NO_RULES`, default deny).
    """
    from engine.doctor import policy as pol

    if kind not in OUTCOME_POLICY_KINDS:
        raise OutcomeError("OUTCOME_NOT_APPROVED", f"{kind} is not an outcome definition.")
    if approval.definition_hash != definition.definition_hash or approval.approved_by != approver.user_id:
        raise OutcomeError(
            "OUTCOME_NOT_APPROVED",
            "This approval is not for this outcome, or not by this person; approve it again.",
        )
    subject = pol.DataProposal(kind=kind, source="rules", proposed_by=proposed_by, sentence=current_sentence)
    facts = pol.ApprovalFacts(
        approver=approver,
        approved_sentence=approval.sentence,
        current_sentence=current_sentence,
        approval_required=approval_required,
    )
    ctx = pol.DataPolicyContext(policy=pol.policy_or_deny(config_root), approval=facts)
    found = pol.refusal(subject, ctx, pol.APPROVE_CLASS)
    if found is not None:
        raise OutcomeError(found.code or "POLICY_NO_RULES", str(found.detail.get("reason", "Not allowed.")))


def record_outcome_approval(
    store: CaseStore,
    *,
    client_id: str,
    case_id: str,
    expected_version: int,
    item_id: str,
    kind: str,
    definition: OutcomeDefinition,
    current_sentence: str | None,
    approval: OutcomeApproval,
    approver: Principal,
    proposed_by: str | None = None,
    approval_required: bool = False,
    config_root: Path | None = None,
) -> Case:
    """Record an approved outcome in the case: its own APPROVE-class approval, with who, when and the exact text,
    and one `approval` timeline event (ids and hashes only). Checked first (:func:`check_outcome_approval`): a
    refused approval writes nothing - not the case, not the timeline."""
    from engine.doctor.case import CaseApproval, EventDraft, TimelineKind

    check_outcome_approval(
        kind=kind,
        definition=definition,
        current_sentence=current_sentence,
        approval=approval,
        approver=approver,
        proposed_by=proposed_by,
        approval_required=approval_required,
        config_root=config_root,
    )
    approval_id = f"outcome-{definition.definition_hash[:16]}"
    record = CaseApproval(
        approval_id=approval_id,
        item_ids=(item_id,),
        approved_by=approval.approved_by,
        approved_at=approval.approved_at,
        text_shown=approval.sentence,
        approve_class=True,
    )

    def change(case: Case) -> Any:
        kept = tuple(a for a in case.approvals if a.approval_id != approval_id)
        return (
            case.model_copy(update={"approvals": (*kept, record)}),
            (
                EventDraft(
                    kind=TimelineKind.APPROVAL,
                    item_id=approval_id,
                    details={
                        "kind": kind,
                        "outcome_kind": definition.kind,
                        "definition_hash": definition.definition_hash,
                    },
                ),
            ),
        )

    return store.update(
        client_id, case_id, expected_version=expected_version, actor=approval.approved_by, change=change
    )
