"""The one decisions model: what the person decided about the data, read from one place.

A decision used to live in five kinds of items, in question answers, in option labels and in step parameters, and
eight readers each applied their own rule (first match here, last match there, a regular expression over an option's
label in a third). :func:`decisions` is the only reader. It answers, for a case:

* **the key** of each table (`key` items),
* **the outcome** of each goal (`outcome_column` items),
* **the as-of date** of the data, and the **as-of column** (the date each row describes) of each table,
* **the outcome window** in days, and
* **the consent column** of each goal,

each as a :class:`Decided`: the value, who decided it, when, and from what (`item:<id>` or `question:<id>:<option>`).

Only accepted things count: a proposal nobody ticked, and one the person took back, decide nothing.

**An answer is a decision.** Each option of a question declares what it sets (`CaseOption.sets`): `as_of`, `outcome`,
`key`, or `note` (it sets nothing, and says so). The as-of question (Q8) sets the date: the one typed with the answer,
else the one the chosen option names ("The latest date in the file: 2011-12-09"). An option that adds a step (an
accepted `key` or `outcome_column` item made by the answer) is read from that item, so it is never counted twice.
Older cases stored no `sets`: Q8's options are read as `as_of` (the lazy migration; asking again is the safe side).

**Precedence** (the first accepted match wins, as the visit's key reader always did):

* key, outcome, as-of column, consent: the first accepted item in the case's order; a later one for the same place is
  ignored (and counted in :attr:`Decisions.ignored`). The prescription used to let the *last* one win; it now reads
  this, and says so in the log when the two differ.
* as-of date: the newest accepted item that carries one, else the answer to Q8. A query keeps `$as_of` symbolic; the
  materialiser binds this date when it runs the step, so a later answer reaches a step already in the plan. With
  none decided, a step keeps its own frozen date or rule (a date one step carries is never the whole case's).
* outcome window: the newest accepted `setting` that sets it.

Nothing here reads a cell: values are column names, dates and whole numbers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from engine.agent.contracts import ProposalState
from engine.agentic.canonical import canonical_bytes, sha256_hex

if TYPE_CHECKING:
    from engine.doctor.case import Case, CaseItem, CaseOption, CaseQuestion

__all__ = [
    "AS_OF_QUESTION",
    "DECISION_TARGETS",
    "Decided",
    "Decisions",
    "Sets",
    "decisions",
    "decisions_from",
    "role_columns",
    "sets_of_effect",
]

log = logging.getLogger(__name__)

DecisionKind = Literal["key", "outcome", "as_of", "as_of_column", "consent", "window"]
Sets = Literal["as_of", "outcome", "key", "as_of_column", "consent", "note"]
"""What an option of a question may declare it sets (`CaseOption.sets`); `note` = it sets nothing, on purpose."""

AS_OF_QUESTION: Final[str] = "Q08_AS_OF"
"""The question that asks for the as-of date: the one question whose answer this module reads for its date."""
DECISION_TARGETS: Final[frozenset[str]] = frozenset(
    {"as_of", "outcome", "key", "as_of_column", "consent", "note"}
)
"""What an option may declare it sets (`CaseOption.sets`); `note` sets nothing, on purpose."""
_ITEM_KINDS: Final[dict[str, DecisionKind]] = {
    "key": "key",
    "outcome_column": "outcome",
    "as_of_column": "as_of_column",
    "consent_column": "consent",
}
_KIND_SETS: Final[dict[str, Sets]] = {
    "key": "key",
    "outcome_column": "outcome",
    "as_of_column": "as_of_column",
    "consent_column": "consent",
}
_ROLE_PROTECTED: Final[frozenset[str]] = frozenset({"outcome_column", "consent_column", "as_of_column"})
_ISO_DATE: Final = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_WINDOW_PATH: Final[str] = "outcome_window_days"


@dataclass(frozen=True)
class Decided:
    """One decision: what it is, who decided it, when, and from what."""

    kind: DecisionKind
    value: str | int
    """A column name, an ISO date (`as_of`) or a number of days (`window`)."""
    by: str | None = None
    at: datetime | None = None
    source: str = ""
    """`item:<item id>` or `question:<question id>:<option id>`."""
    upload_id: str | None = None
    """The file the deciding item is about (the item's own `upload_id`)."""
    in_upload: str | None = None
    """The file the column is in: a goal item names it in its payload, else the item's file."""
    goal_id: str | None = None

    @property
    def column(self) -> str:
        return str(self.value)


@dataclass(frozen=True)
class Decisions:
    """Everything decided about a case, in the order the precedence reads it (see the module's docstring)."""

    records: tuple[Decided, ...] = ()
    ignored: int = 0
    """Accepted decisions left out because an earlier one already holds that place (first accepted wins)."""
    shadowed: tuple[Decided, ...] = ()
    """Those left-out decisions themselves: :meth:`column` reads them after the first, so a second accepted key that
    fits the file is found when the first names a column the file lacks."""

    # -- generic -----------------------------------------------------------------------------------------------
    def of(self, kind: DecisionKind) -> tuple[Decided, ...]:
        return tuple(r for r in self.records if r.kind == kind)

    def column(
        self, kind: DecisionKind, upload_id: str, columns: Collection[str] | None = None
    ) -> str | None:
        """The column decided for `kind` that the file `upload_id` holds (an item of no file counts for any file);
        with `columns`, only one of them."""
        for record in (*self.records, *self.shadowed):
            if (
                record.kind == kind
                and record.upload_id in (upload_id, None)
                and isinstance(record.value, str)
                and (columns is None or record.value in columns)
            ):
                return record.value
        return None

    def column_of_upload(
        self,
        kind: DecisionKind,
        upload_id: str,
        *,
        goal_id: str | None = None,
        columns: Collection[str] | None = None,
    ) -> str | None:
        """The column decided for `kind` on exactly this file; with `goal_id`, not one decided for another goal;
        with `columns`, one of them."""
        return next(
            (
                r.value
                for r in self.records
                if r.kind == kind
                and r.upload_id == upload_id
                and isinstance(r.value, str)
                and (goal_id is None or r.goal_id in (None, goal_id))
                and (columns is None or r.value in columns)
            ),
            None,
        )

    def declared(self, kind: DecisionKind, upload_id: str) -> str | None:
        """The column decided for `kind` for this file, whatever the file holds (it may be built by a query)."""
        return self.column(kind, upload_id)

    def _per_goal(self, kind: DecisionKind, goal_id: str) -> Decided | None:
        return next(
            (
                r
                for r in self.records
                if r.kind == kind
                and r.goal_id == goal_id
                and r.in_upload is not None
                and isinstance(r.value, str)
            ),
            None,
        )

    def pair(self, kind: DecisionKind, goal_id: str) -> tuple[str, str] | None:
        """`(upload id, column)` decided for a goal: the file the column is in, and its name."""
        found = self._per_goal(kind, goal_id)
        return (found.in_upload, found.column) if found is not None and found.in_upload is not None else None

    # -- the named decisions -----------------------------------------------------------------------------------
    def key(self, upload_id: str) -> str | None:
        """The ID column decided for this file."""
        return self.column_of_upload("key", upload_id)

    @property
    def keys(self) -> dict[str, Decided]:
        """The key of each table (file), by upload id."""
        found: dict[str, Decided] = {}
        for r in self.of("key"):
            if r.upload_id and isinstance(r.value, str):
                found.setdefault(r.upload_id, r)
        return found

    def outcome(self, goal_id: str) -> Decided | None:
        """The outcome of a goal: the column and the file it is in (`in_upload`)."""
        return self._per_goal("outcome", goal_id)

    @property
    def outcomes(self) -> dict[str, Decided]:
        found: dict[str, Decided] = {}
        for r in self.of("outcome"):
            if r.goal_id and r.in_upload is not None and isinstance(r.value, str):
                found.setdefault(r.goal_id, r)
        return found

    def outcome_of_upload(self, upload_id: str) -> str | None:
        return self.column_of_upload("outcome", upload_id)

    def as_of_column(self, goal_id: str) -> Decided | None:
        """The column that says when each row happened, for a goal."""
        return self._per_goal("as_of_column", goal_id)

    @property
    def as_of_columns(self) -> dict[str, Decided]:
        """The as-of column of each table, by upload id."""
        found: dict[str, Decided] = {}
        for r in self.of("as_of_column"):
            if r.upload_id and isinstance(r.value, str):
                found.setdefault(r.upload_id, r)
        return found

    def consent(self, goal_id: str) -> Decided | None:
        return self._per_goal("consent", goal_id)

    @property
    def consents(self) -> dict[str, Decided]:
        found: dict[str, Decided] = {}
        for r in self.of("consent"):
            if r.goal_id and r.in_upload is not None and isinstance(r.value, str):
                found.setdefault(r.goal_id, r)
        return found

    @property
    def as_of(self) -> Decided | None:
        """The as-of date of the data, if anyone decided one."""
        return next(iter(self.of("as_of")), None)

    @property
    def as_of_iso(self) -> str | None:
        found = self.as_of
        return str(found.value) if found is not None else None

    @property
    def as_of_date(self) -> date | None:
        iso = self.as_of_iso
        return date.fromisoformat(iso) if iso is not None else None

    def window_days(self, goal_id: str | None = None) -> int | None:
        """The outcome window in days: the goal's, else one decided for no goal in particular."""
        found = self.of("window")
        for r in found:
            if goal_id is not None and r.goal_id == goal_id and isinstance(r.value, int):
                return r.value
        return next((r.value for r in found if r.goal_id is None and isinstance(r.value, int)), None)

    @property
    def digest(self) -> str:
        """What was decided, for a memo's key: the same decisions give the same digest, whoever decided them."""
        rows = sorted(
            [r.kind, str(r.value), r.upload_id or "", r.in_upload or "", r.goal_id or ""]
            for r in self.records
        )
        return sha256_hex(canonical_bytes(rows))[:16]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def decisions(case: Case) -> Decisions:
    """What the case's accepted items and answered questions decide, in the shared base and the active goal's section
    (other goals' sections are set aside, as everywhere else). The one reader."""
    from engine.doctor.visit import active_section

    items, questions = active_section(case)
    return decisions_from(items, questions)


def decisions_from(items: Iterable[CaseItem], questions: Sequence[CaseQuestion] = ()) -> Decisions:
    """:func:`decisions` for a caller that holds the items (the active section's, say) and not the case."""
    listed = list(items)
    accepted = [i for i in listed if i.state is ProposalState.ACCEPTED]
    records: list[Decided] = []
    ignored = 0
    shadowed: list[Decided] = []
    seen: set[tuple[Any, ...]] = set()

    def add(record: Decided, place: tuple[Any, ...]) -> None:
        nonlocal ignored
        if place in seen:
            ignored += 1
            shadowed.append(record)
            return
        seen.add(place)
        records.append(record)

    for item in accepted:
        kind = _ITEM_KINDS.get(item.kind)
        column = item.payload.get("column")
        if kind is None or not isinstance(column, str):
            continue
        named = item.payload.get("upload_id")
        in_upload = named if isinstance(named, str) else item.upload_id
        add(
            Decided(
                kind=kind,
                value=column,
                by=item.decided_by,
                at=item.decided_at,
                source=f"item:{item.item_id}",
                upload_id=item.upload_id,
                in_upload=in_upload,
                goal_id=item.goal_id,
            ),
            (kind, item.upload_id, item.goal_id, in_upload),
        )
    _as_of(accepted, questions, add)
    _window(accepted, add)
    return Decisions(records=tuple(records), ignored=ignored, shadowed=tuple(shadowed))


def _option_of(question: CaseQuestion) -> CaseOption | None:
    return next((o for o in question.options if o.option_id == question.answer), None)


def _target_of(question: CaseQuestion, option: CaseOption) -> str | None:
    """What an answered option sets: what it declares, else - for the as-of question of an older case, which
    stored none - the as-of date."""
    if option.sets is not None:
        return option.sets
    return "as_of" if question.code == AS_OF_QUESTION else None


def _valid_date(text: object) -> str | None:
    if isinstance(text, str) and _ISO_DATE.fullmatch(text):
        try:
            date.fromisoformat(text)
        except ValueError:
            return None
        return text
    return None


def _as_of(accepted: Sequence[CaseItem], questions: Sequence[CaseQuestion], add: Any) -> None:
    candidates: list[Decided] = []
    for item in reversed(accepted):
        given = _valid_date(item.payload.get("as_of"))
        if given is not None:
            candidates.append(
                Decided(
                    kind="as_of",
                    value=given,
                    by=item.decided_by,
                    at=item.decided_at,
                    source=f"item:{item.item_id}",
                    upload_id=item.upload_id,
                    goal_id=item.goal_id,
                )
            )
    for question in questions:
        if question.answer is None:
            continue
        option = _option_of(question)
        if option is None or _target_of(question, option) != "as_of":
            continue
        given = _valid_date(
            getattr(question, "answer_value", None)
        )  # the date typed beats the one an option names
        if given is None:
            found = _ISO_DATE.search(option.label)
            given = _valid_date(found.group(1)) if found is not None else None
        if given is not None:
            candidates.append(
                Decided(
                    kind="as_of",
                    value=given,
                    by=question.answered_by,
                    at=question.answered_at,
                    source=f"question:{question.question_id}:{option.option_id}",
                    goal_id=question.goal_id,
                )
            )
    for record in candidates:
        add(record, ("as_of",))  # the first holds the place; the others are counted as ignored


def _window(accepted: Sequence[CaseItem], add: Any) -> None:
    settings = [
        i
        for i in accepted
        if i.kind == "setting"
        and isinstance(i.payload.get("path"), str)
        and str(i.payload["path"]).endswith(_WINDOW_PATH)
        and isinstance(i.payload.get("value"), int)
        and not isinstance(i.payload.get("value"), bool)
    ]
    settings.sort(key=lambda i: i.decided_at or i.proposed_at, reverse=True)
    for item in settings:
        days = item.payload["value"]
        if not isinstance(days, int):
            continue
        add(
            Decided(
                kind="window",
                value=days,
                by=item.decided_by,
                at=item.decided_at,
                source=f"item:{item.item_id}",
                upload_id=item.upload_id,
                goal_id=item.goal_id,
            ),
            ("window", item.goal_id),
        )


def sets_of_effect(kind: str | None) -> Sets | None:
    """What an option declares it sets, from the item kind its effect adds: a role item sets that role, an option
    that adds nothing is an explicit `note`, and any other step is read from its own item (None)."""
    if kind is None:
        return "note"
    return _KIND_SETS.get(kind)


def role_columns(case: Case) -> tuple[set[str], set[str]]:
    """`(ids, protected)`: the columns that hold a meaning the AI must not touch, whatever state the item is in
    (a pending or rejected proposal still names the column): the IDs, and the outcome, consent and date columns.
    Not a decision (nothing here is accepted): the guard reads what has been *named*."""
    ids: set[str] = set()
    protected: set[str] = set()
    for item in case.items:
        column = item.payload.get("column")
        if not isinstance(column, str):
            continue
        if item.kind == "key":
            ids.add(column)
        elif item.kind in _ROLE_PROTECTED:
            protected.add(column)
    return ids, protected
