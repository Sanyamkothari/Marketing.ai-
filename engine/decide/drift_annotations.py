"""Events the user noted against a scoring run's drift report (Plan J M109, DEC-1319 (c)-(h)).

A drift alarm that follows a price change, a competitor's launch or a promotion is not a surprise, and
today it reads like one. This module lets a person note such an event on a scoring run and shows it
beside the drift the run measured, so a change after a known event is explained and not only alarming.

**A new artefact, and the drift stage is untouched.** The notes live in `drift_annotations.json` in the
scoring run's own directory. `drift.json`, the predict stage that writes it and its verdict are never
read for writing and never changed: a noted event gives a *possible reason*, it does not lower the
measured change, silence an alert or lift the "retrain" action. The view says so in its last sentence.

**What counts as a reason (`build_view`).** An event explains nothing unless it can have come first:

* it is dated on or before the day the run's drift was measured (`drift.json`'s `computed_at`);
* it is dated on or after the day the model's training run was started, when that run can be read (data
  from before the model was trained is already in what the model learned). When the training run
  cannot be read there is no lower bound, and the view says so.

An event outside that window is kept and shown, labelled with the reason it is not counted.

**Naming the measures.** An event may name the measures (columns) it touches. A name must be one of the
measures the drift report compared; anything else is refused (`DRIFT_ANNOTATION_INVALID`) rather
than stored and shown as a connection nobody measured. An event that names none is a general one: it is
listed as a possible reason for the change as a whole, and it is never matched to a single measure.

**Nothing invented.** A run with no drift report refuses a note (`DRIFT_NOT_MEASURED`): there is nothing
to explain. A stable run lists its notes and says nothing needs explaining. Every sentence is plain
(`engine.pilot.plain.jargon_in` finds nothing in it; a test runs them all through it).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from engine.contracts import Artefact, DriftReport, DriftStatus, RunRecord
from engine.runs import RUN_FILENAME
from engine.storage import Storage, StorageError, run_key

__all__ = [
    "DRIFT_ANNOTATIONS_FILENAME",
    "DRIFT_ANNOTATION_CODES",
    "DRIFT_ANNOTATION_INVALID",
    "DRIFT_ANNOTATION_LIMIT",
    "DRIFT_ANNOTATION_NOT_FOUND",
    "DRIFT_NOT_MEASURED",
    "MAX_ANNOTATIONS",
    "MAX_NOTE_CHARACTERS",
    "DriftAnnotation",
    "DriftAnnotationError",
    "DriftAnnotations",
    "DriftEventRow",
    "DriftEventsView",
    "EventKind",
    "add_annotation",
    "build_view",
    "kind_options",
    "read_annotations",
    "remove_annotation",
]

DRIFT_ANNOTATIONS_FILENAME: Final[str] = "drift_annotations.json"
"""The artefact, in a scoring run's directory. Not row-level: it holds no customer."""

DRIFT_NOT_MEASURED: Final[str] = "DRIFT_NOT_MEASURED"
"""409: the run has no drift report, so there is nothing to explain."""
DRIFT_ANNOTATION_INVALID: Final[str] = "DRIFT_ANNOTATION_INVALID"
"""422: the note is empty, or the event names a measure the drift report did not compare."""
DRIFT_ANNOTATION_NOT_FOUND: Final[str] = "DRIFT_ANNOTATION_NOT_FOUND"
"""404: no noted event with that id on this run."""
DRIFT_ANNOTATION_LIMIT: Final[str] = "DRIFT_ANNOTATION_LIMIT"
"""409: the run already holds `MAX_ANNOTATIONS` noted events."""

DRIFT_ANNOTATION_CODES: Final[frozenset[str]] = frozenset(
    {DRIFT_NOT_MEASURED, DRIFT_ANNOTATION_INVALID, DRIFT_ANNOTATION_NOT_FOUND, DRIFT_ANNOTATION_LIMIT}
)
"""The codes this module raises, for the integrator to join into `PLAN_J_CODES` (DEC-1319)."""

MAX_NOTE_CHARACTERS: Final[int] = 200
MAX_ANNOTATIONS: Final[int] = 50
"""A run's notes are a short list a person reads; more than this is a sign the box is used as a log."""
_SHOWN_IN_HEADLINE: Final[int] = 3

_WORD: Final[re.Pattern[str]] = re.compile(r"\s+")


class EventKind(StrEnum):
    """What kind of thing happened. The labels are the screen's words (`kind_options`)."""

    PRICE_CHANGE = "price_change"
    PROMOTION = "promotion"
    COMPETITOR_LAUNCH = "competitor_launch"
    POLICY_CHANGE = "policy_change"
    SEASONAL = "seasonal"
    OTHER = "other"


_KIND_LABELS: Final[dict[EventKind, str]] = {
    EventKind.PRICE_CHANGE: "A price change",
    EventKind.PROMOTION: "A promotion or sale",
    EventKind.COMPETITOR_LAUNCH: "A competitor's launch",
    EventKind.POLICY_CHANGE: "A change of rules or policy",
    EventKind.SEASONAL: "A season or holiday",
    EventKind.OTHER: "Something else",
}


def kind_options() -> tuple[tuple[str, str], ...]:
    """`(value, label)` for every kind, in the order the screen lists them."""
    return tuple((kind.value, _KIND_LABELS[kind]) for kind in EventKind)


class DriftAnnotationError(Exception):
    """A refusal with a code and a plain sentence; the route maps `code` to a status."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DriftAnnotation(BaseModel):
    """One event a person noted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    annotation_id: str = Field(description="Short id, unique within the run.")
    event_date: date = Field(description="The day the event happened.")
    kind: EventKind = Field(description="What kind of event it was.")
    note: str = Field(
        min_length=1,
        max_length=MAX_NOTE_CHARACTERS,
        description="A short plain note. It must not hold personal data.",
    )
    measures: tuple[str, ...] = Field(
        default=(), description="The measures the event touches; empty for a general event."
    )
    recorded_at: AwareDatetime = Field(description="UTC time the note was added.")
    recorded_by: str | None = Field(default=None, description="Who added it, when sign-in is on.")


class DriftAnnotations(Artefact):
    """`drift_annotations.json`."""

    run_id: str = Field(description="The scoring run the events are noted against.")
    annotations: tuple[DriftAnnotation, ...] = Field(default=(), description="Oldest note first.")


class DriftEventRow(BaseModel):
    """A noted event as the screen shows it, with what the server decided about it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    annotation_id: str = Field(description="The note's id, for removing it.")
    event_date: date = Field(description="The day the event happened.")
    kind: EventKind = Field(description="What kind of event it was.")
    kind_label: str = Field(description="The kind in the screen's words.")
    note: str = Field(description="The note as written.")
    measures: tuple[str, ...] = Field(description="The measures it names; empty for a general event.")
    recorded_by: str | None = Field(description="Who added it, when sign-in is on.")
    counted: bool = Field(description="True when the event can be a reason for this run's change.")
    reason: str | None = Field(description="Why it is not counted; null when it is.")
    explains: tuple[str, ...] = Field(
        description="The moved measures it names; empty for a general event or an event not counted."
    )


class DriftEventsView(BaseModel):
    """`GET /runs/{run_id}/drift-events`: the notes beside the drift the run measured."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(description="The scoring run.")
    drift_measured: bool = Field(description="False when the run has no drift report.")
    drift_status: DriftStatus | None = Field(
        description="The run's own verdict, unchanged; null when not measured."
    )
    moved_measures: tuple[str, ...] = Field(
        description="Measures the report marks watch or drifted, most changed first."
    )
    window_start: date | None = Field(
        description="Events before this day are not counted; null when unknown."
    )
    window_end: date | None = Field(
        description="Events after this day are not counted; null when not measured."
    )
    events: tuple[DriftEventRow, ...] = Field(description="Every noted event, oldest first.")
    explained_measures: tuple[str, ...] = Field(description="Moved measures a counted event names.")
    unexplained_measures: tuple[str, ...] = Field(
        description="Moved measures no counted event names (a general event may still be a reason)."
    )
    headline: str | None = Field(
        description="One plain sentence; null when the run is stable or drift was not measured."
    )
    note: str = Field(description="What a noted event does and does not do.")
    kinds: tuple[tuple[str, str], ...] = Field(
        description="The kinds a person may choose, as (value, label)."
    )
    measure_choices: tuple[str, ...] = Field(
        description="The measures a note may name: those the drift report compared."
    )


_NOTE: Final[str] = (
    "A noted event is a possible reason, not a proof. It does not change the measured change, "
    "silence an alert or replace retraining."
)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def read_annotations(storage: Storage, run_id: str) -> DriftAnnotations:
    """The run's notes; none when nothing was noted."""
    key = run_key(run_id, DRIFT_ANNOTATIONS_FILENAME)
    if not storage.exists(key):
        return DriftAnnotations(run_id=run_id)
    return storage.read_model(key, DriftAnnotations)


def _read_drift(storage: Storage, run_id: str) -> DriftReport | None:
    key = run_key(run_id, "drift.json")
    if not storage.exists(key):
        return None
    return storage.read_model(key, DriftReport)


def _compared(drift: DriftReport) -> tuple[str, ...]:
    return tuple(item.feature for item in drift.features)


def _clean_note(note: str) -> str:
    return _WORD.sub(" ", note).strip()


def add_annotation(
    storage: Storage,
    run_id: str,
    *,
    event_date: date,
    kind: EventKind,
    note: str,
    measures: tuple[str, ...] = (),
    recorded_by: str | None = None,
    now: datetime | None = None,
) -> DriftAnnotation:
    """Note one event on `run_id`. Refuses a run with no drift report and a measure it did not compare."""
    drift = _read_drift(storage, run_id)
    if drift is None:
        raise DriftAnnotationError(
            DRIFT_NOT_MEASURED,
            "This run has no measure of how much the customers changed, so there is nothing to explain. "
            "Note events on a run that scored customers against a trained model.",
        )
    cleaned = _clean_note(note)
    if not cleaned:
        raise DriftAnnotationError(DRIFT_ANNOTATION_INVALID, "Write a short note about the event.")
    known = set(_compared(drift))
    unknown = [name for name in dict.fromkeys(measures) if name not in known]
    if unknown:
        raise DriftAnnotationError(
            DRIFT_ANNOTATION_INVALID,
            f"The change report did not compare {', '.join(repr(name) for name in unknown)}. "
            "Name only the measures it compared, or leave the list empty for a general event.",
        )
    current = read_annotations(storage, run_id)
    if len(current.annotations) >= MAX_ANNOTATIONS:
        raise DriftAnnotationError(
            DRIFT_ANNOTATION_LIMIT,
            f"A run holds at most {MAX_ANNOTATIONS} noted events. Remove one before adding another.",
        )
    moment = now or datetime.now(UTC)
    annotation = DriftAnnotation(
        annotation_id=uuid.uuid4().hex[:10],
        event_date=event_date,
        kind=kind,
        note=cleaned,
        measures=tuple(dict.fromkeys(measures)),
        recorded_at=moment,
        recorded_by=recorded_by,
    )
    updated = current.model_copy(update={"annotations": (*current.annotations, annotation)})
    storage.write_model(run_key(run_id, DRIFT_ANNOTATIONS_FILENAME), updated)
    return annotation


def remove_annotation(storage: Storage, run_id: str, annotation_id: str) -> DriftAnnotation:
    """Remove one note; the artefact is rewritten without it."""
    current = read_annotations(storage, run_id)
    found = [a for a in current.annotations if a.annotation_id == annotation_id]
    if not found:
        raise DriftAnnotationError(DRIFT_ANNOTATION_NOT_FOUND, "No noted event with that id on this run.")
    kept = tuple(a for a in current.annotations if a.annotation_id != annotation_id)
    storage.write_model(
        run_key(run_id, DRIFT_ANNOTATIONS_FILENAME), current.model_copy(update={"annotations": kept})
    )
    return found[0]


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------
def _training_start(storage: Storage, drift: DriftReport) -> date | None:
    """The day the model's training run was created, or None when that run cannot be read."""
    try:
        record = storage.read_model(run_key(drift.baseline_run_id, RUN_FILENAME), RunRecord)
    except StorageError:
        return None
    return record.created_at.astimezone(UTC).date()


def _day(value: date) -> str:
    return f"{value.day} {value:%b %Y}"


def _moved(drift: DriftReport) -> tuple[str, ...]:
    return tuple(item.feature for item in drift.features if item.status is not DriftStatus.STABLE)


def _row(
    annotation: DriftAnnotation, *, start: date | None, end: date | None, moved: tuple[str, ...]
) -> DriftEventRow:
    reason: str | None = None
    if end is not None and annotation.event_date > end:
        reason = f"It is dated after the day this run measured the change ({_day(end)})."
    elif start is not None and annotation.event_date < start:
        reason = (
            f"It is dated before the model was trained ({_day(start)}), so the model already learned from it."
        )
    counted = reason is None
    explains = tuple(name for name in annotation.measures if name in moved) if counted else ()
    return DriftEventRow(
        annotation_id=annotation.annotation_id,
        event_date=annotation.event_date,
        kind=annotation.kind,
        kind_label=_KIND_LABELS[annotation.kind],
        note=annotation.note,
        measures=annotation.measures,
        recorded_by=annotation.recorded_by,
        counted=counted,
        reason=reason,
        explains=explains,
    )


def _headline(
    drift: DriftReport,
    rows: tuple[DriftEventRow, ...],
    explained: tuple[str, ...],
    unexplained: tuple[str, ...],
) -> str | None:
    if drift.status is DriftStatus.STABLE:
        return None
    counted = [row for row in rows if row.counted]
    if not counted:
        return "No event has been noted for the period since the model was trained."
    shown = counted[:_SHOWN_IN_HEADLINE]
    names = "; ".join(f"{row.kind_label.lower()} on {_day(row.event_date)}" for row in shown)
    more = len(counted) - len(shown)
    head = f"Possibly explained by {names}" + (f" and {more} more" if more > 0 else "") + "."
    if explained and unexplained:
        head += (
            f" Named by a noted event: {', '.join(explained)}. "
            f"Not named by any: {', '.join(unexplained)}."
        )
    return head


def build_view(storage: Storage, run_id: str) -> DriftEventsView:
    """The run's notes beside its drift report, as the screen shows them."""
    notes = read_annotations(storage, run_id).annotations
    drift = _read_drift(storage, run_id)
    if drift is None:
        rows = tuple(
            DriftEventRow(
                annotation_id=a.annotation_id,
                event_date=a.event_date,
                kind=a.kind,
                kind_label=_KIND_LABELS[a.kind],
                note=a.note,
                measures=a.measures,
                recorded_by=a.recorded_by,
                counted=False,
                reason="This run has no measure of how much the customers changed.",
                explains=(),
            )
            for a in sorted(notes, key=lambda a: (a.event_date, a.recorded_at))
        )
        return DriftEventsView(
            run_id=run_id,
            drift_measured=False,
            drift_status=None,
            moved_measures=(),
            window_start=None,
            window_end=None,
            events=rows,
            explained_measures=(),
            unexplained_measures=(),
            headline=None,
            note=_NOTE,
            kinds=kind_options(),
            measure_choices=(),
        )
    start = _training_start(storage, drift)
    end = drift.computed_at.astimezone(UTC).date()
    moved = _moved(drift)
    rows = tuple(
        _row(a, start=start, end=end, moved=moved)
        for a in sorted(notes, key=lambda a: (a.event_date, a.recorded_at))
    )
    named = {name for row in rows for name in row.explains}
    explained = tuple(name for name in moved if name in named)
    unexplained = tuple(name for name in moved if name not in named)
    return DriftEventsView(
        run_id=run_id,
        drift_measured=True,
        drift_status=drift.status,
        moved_measures=moved,
        window_start=start,
        window_end=end,
        events=rows,
        explained_measures=explained,
        unexplained_measures=unexplained,
        headline=_headline(drift, rows, explained, unexplained),
        note=_NOTE,
        kinds=kind_options(),
        measure_choices=_compared(drift),
    )
