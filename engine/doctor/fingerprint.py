"""Data fingerprints: what a client's data looked like when a case was approved, and a check of today's file against it.

After a case is approved, `record_fingerprint` saves a small description of the main file for that client and goal:
the number of rows and customers, each column's type, missing share and distinct count, the shares of a column with
few categories, the outcome rate, the newest date and the shape of the IDs. No customer values are kept: only counts,
shares, a hash of the most common values and, for a column with a few categories, those categories.

Each time the visit treats the case (a new file, a goal chosen, an answer changed: always before approval)
`fingerprint_detector` compares the case's newest file with the last fingerprint of an earlier file and with the
client's facts (`engine.doctor.facts`) and reports what moved, in plain sentences with both numbers:

* **stop** - the file is far from what the person said or from last time (customers off by more than twice the
  tolerance, the outcome rate off by more than the factor squared, the key or outcome column gone, a file older than
  last time). A blocking question; nothing goes on until a person answers it.
* **flag** - a clear change worth a look (customers off by more than the tolerance, a column renamed, missing or
  changed type, a category share moved more than 20 points, the ID format changed, no newer dates than last time).
  A doubt in three parts, every number in it grounded in the evidence this detector stores.
* **note** - a smaller change the person may want to know (customers off by a tenth, new columns). Also a doubt,
  with a code that says it is only a note.

No fingerprint and no facts: no findings (the first visit has nothing to compare with).
"""

from __future__ import annotations

import difflib
import hashlib
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

import pandas as pd
from pydantic import AwareDatetime, Field, JsonValue

from engine.agent.contracts import ProposalState
from engine.config import StrictBase
from engine.doctor.case import CASE_PREFIX, CLIENT_ID_PATTERN, CaseDoubt
from engine.doctor.facts import ClientFact, FactKind, FactStore
from engine.doctor.protocol import Ask, AskOption, ProtocolError, express_doubt
from engine.doctor.registry import DataToolKind, DataToolResult, Exposure, data_evidence_id
from engine.doctor.visit_contracts import OptionEffect
from engine.storage import Storage, StorageError
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.doctor.case import Case
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief
    from engine.doctor.visit import Detected, DetectorPort

__all__ = [
    "FINGERPRINTS_FILENAME",
    "ColumnPrint",
    "Finding",
    "Fingerprint",
    "FingerprintStore",
    "compare",
    "fingerprint_detector",
    "fingerprint_file",
    "record_fingerprint",
]

_LOG = get_logger(__name__)

FINGERPRINTS_FILENAME: Final[str] = "fingerprints.json"
COMPARE_TOOL: Final[str] = "compare_with_memory"
KEEP_PER_GOAL: Final[int] = 6
SHARE_MOVE: Final[float] = 0.2
"""A category share that moves by more than this (20 points) is flagged."""
_MAX_CATEGORIES: Final[int] = 12
_YES: Final[frozenset[str]] = frozenset({"1", "1.0", "true", "yes", "y", "t", "haan"})
_NO: Final[frozenset[str]] = frozenset({"0", "0.0", "false", "no", "n", "f", "nahi"})
_FAMILY: Final[dict[str, str]] = {
    "integer": "number",
    "float": "number",
    "string": "text",
    "text": "text",
    "date": "date",
    "datetime": "date",
    "boolean": "yes/no",
}
Level = Literal["stop", "flag", "note"]
_RANK: Final[dict[str, int]] = {"note": 0, "flag": 1, "stop": 2}
_LOCK: Final = threading.Lock()


# ---------------------------------------------------------------------------
# The fingerprint
# ---------------------------------------------------------------------------
class ColumnPrint(StrictBase):
    name: str
    type: str = Field(description="The engine's type for the column (integer, float, string, date, ...).")
    missing_share: float = Field(ge=0, le=1)
    distinct: int = Field(ge=0)
    top_hash: str = Field(default="", description="Hash of the most common values; the values are not kept.")
    shares: dict[str, float] = Field(
        default_factory=dict,
        description="Share per category, only for a column with a few non-personal ones.",
    )


class Fingerprint(StrictBase):
    goal_id: str | None = None
    upload_id: str
    file_name: str = ""
    taken_at: AwareDatetime
    taken_by: str = ""
    rows: int = Field(ge=0)
    customers: int | None = Field(default=None, description="Distinct IDs; null when there is no ID column.")
    key_column: str | None = None
    key_pattern: str | None = Field(
        default=None, description="The most common ID shape, 9 = digit, A/a = letter."
    )
    key_pattern_share: float = 0.0
    key_lengths: dict[str, float] = Field(default_factory=dict, description="Share of IDs per length.")
    outcome_column: str | None = None
    outcome_rate: float | None = None
    date_column: str | None = None
    date_last: str | None = Field(default=None, description="The newest date in the file (ISO).")
    columns: tuple[ColumnPrint, ...] = ()


class Fingerprints(StrictBase):
    """`cases/<client_id>/fingerprints.json`: the last few per goal, oldest first."""

    client_id: str
    prints: tuple[Fingerprint, ...] = ()


def fingerprints_key(client_id: str) -> str:
    return f"{CASE_PREFIX}{client_id}/{FINGERPRINTS_FILENAME}"


class FingerprintStore:
    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def _read(self, client_id: str, *, strict: bool = False) -> Fingerprints:
        """Unreadable: logged, and an empty memory for a read; `strict` (a write) raises, so history is never lost."""
        key = fingerprints_key(client_id)
        try:
            if not self._storage.exists(key):
                return Fingerprints(client_id=client_id)
            return self._storage.read_model(key, Fingerprints)
        except (StorageError, ValueError) as exc:
            _LOG.warning("doctor.fingerprints_unreadable client=%s error=%s", client_id, type(exc).__name__)
            if strict:
                raise
            return Fingerprints(client_id=client_id)

    def uploads(self, client_id: str) -> frozenset[str]:
        """The uploads that already have a fingerprint (any goal)."""
        if re.fullmatch(CLIENT_ID_PATTERN, client_id) is None:
            return frozenset()
        return frozenset(p.upload_id for p in self._read(client_id).prints)

    def last(self, client_id: str, goal_id: str | None, *, besides: str | None = None) -> Fingerprint | None:
        """The newest fingerprint for this goal, never the one of upload `besides` (a file is not compared with
        itself)."""
        if re.fullmatch(CLIENT_ID_PATTERN, client_id) is None:
            return None
        mine = [p for p in self._read(client_id).prints if p.goal_id == goal_id and p.upload_id != besides]
        return mine[-1] if mine else None

    def save(self, client_id: str, print_: Fingerprint) -> None:
        with _LOCK:
            document = self._read(client_id, strict=True)
            same = [
                p for p in document.prints if p.goal_id == print_.goal_id and p.upload_id != print_.upload_id
            ]
            others = [p for p in document.prints if p.goal_id != print_.goal_id]
            kept = (*others, *same[-(KEEP_PER_GOAL - 1) :], print_)
            self._storage.write_model(
                fingerprints_key(client_id), document.model_copy(update={"prints": kept})
            )


def _hash(values: Sequence[str]) -> str:
    return hashlib.sha256("\x1f".join(sorted(values)).encode()).hexdigest()[:16]


def _shape(value: str) -> str:
    """`C1042` -> `A9999`, as runs: `A9{4}` is stored as 'A9{4}'."""
    out: list[str] = []
    for char in value:
        out.append("9" if char.isdigit() else "A" if char.isupper() else "a" if char.islower() else char)
    text = "".join(out)
    return re.sub(r"(.)\1+", lambda m: f"{m.group(1)}{{{len(m.group(0))}}}", text)


def _describe_shape(shape: str, say: _Say) -> str:
    parts: list[str] = []
    for char, count in re.findall(r"(.)(?:\{(\d+)\})?", shape):
        n = int(count) if count else 1
        word = {"9": "digit", "A": "capital letter", "a": "lowercase letter"}.get(char, f"“{char}”")
        parts.append(f"{say.count(n)} {word}{'' if n == 1 else 's'}")
    return " + ".join(parts) if parts else "empty"


def _id_text(series: pd.Series) -> pd.Series:
    """IDs as text; whole numbers read as floats (a column with gaps) lose their `.0`."""
    values = series.dropna()
    if pd.api.types.is_float_dtype(values) and bool((values % 1 == 0).all()):
        values = values.astype("int64")
    text = values.astype(str).str.strip()
    return text[text != ""]


def _positive_rate(series: pd.Series, positive: object) -> float | None:
    values = series.dropna()
    if values.empty:
        return None
    text = values.astype(str).str.strip().str.lower()
    seen = set(text.unique())
    if positive is not None and str(positive).strip().lower() in seen:
        return float((text == str(positive).strip().lower()).mean())
    if seen <= _YES | _NO:
        return float(text.isin(_YES).mean())
    return None


def _time_column(
    frame: pd.DataFrame, brief: GoalBrief | None, profile_candidates: Sequence[str]
) -> str | None:
    wanted: tuple[str | None, ...] = ()
    if brief is not None:
        wanted = (brief.time_rules.snapshot_column, brief.time_rules.time_column)
    for name in (*wanted, *profile_candidates):
        if name and name in frame.columns:
            return str(name)
    return None


def fingerprint_file(
    loaded: LoadedFile,
    *,
    goal_id: str | None,
    key: str | None,
    outcome: str | None,
    brief: GoalBrief | None,
    at: datetime,
    actor: str = "",
) -> Fingerprint:
    """The fingerprint of one file. Counts come from the whole-file profile; the rest from the rows loaded."""
    frame, profile = loaded.frame, loaded.profile
    rows = int(profile.row_count)
    capped = len(frame) < rows
    key = (
        key
        if key in frame.columns
        else next((k for k in profile.primary_key_candidates if k in frame.columns), None)
    )
    customers: int | None = None
    pattern: str | None = None
    share = 0.0
    lengths: dict[str, float] = {}
    if key is not None:
        ids = _id_text(frame[key])
        if not capped:
            customers = int(ids.nunique())
        elif len(ids) and ids.nunique() == len(ids):
            customers = rows  # unique in the part read, so one row per customer
        if len(ids):
            sample = ids.head(2_000)
            shapes = sample.map(_shape).value_counts(normalize=True)
            pattern, share = str(shapes.index[0]), float(shapes.iloc[0])
            by_length = sample.str.len().value_counts(normalize=True).head(3)
            lengths = {str(int(float(str(k)))): round(float(v), 4) for k, v in by_length.items()}
    rate = (
        _positive_rate(frame[outcome], brief.outcome.positive_label if brief else None)
        if outcome in frame.columns
        else None
    )
    date_column = _time_column(frame, brief, profile.time_column_candidates)
    date_last: str | None = None
    if date_column is not None:
        parsed = pd.to_datetime(frame[date_column], errors="coerce", format="mixed").dropna()
        if len(parsed):
            date_last = parsed.max().date().isoformat()
    columns: list[ColumnPrint] = []
    for column in profile.columns:
        if column.name not in frame.columns:
            continue
        small = (
            0 < column.distinct_count <= _MAX_CATEGORIES
            and not column.looks_like_id
            and not column.pii_kinds
            and column.inferred_type.value in {"string", "boolean"}
        )
        columns.append(
            ColumnPrint(
                name=column.name,
                type=column.inferred_type.value,
                missing_share=round(float(column.null_rate), 4),
                distinct=int(column.distinct_count),
                top_hash=_hash([c.value for c in column.top_categories]) if column.top_categories else "",
                shares={c.value: round(float(c.share), 4) for c in column.top_categories} if small else {},
            )
        )
    return Fingerprint(
        goal_id=goal_id,
        upload_id=loaded.upload_id,
        file_name=loaded.file_name,
        taken_at=at,
        taken_by=actor,
        rows=rows,
        customers=customers,
        key_column=key,
        key_pattern=pattern,
        key_pattern_share=round(share, 4),
        key_lengths=lengths,
        outcome_column=outcome if outcome in frame.columns else None,
        outcome_rate=rate,
        date_column=date_column,
        date_last=date_last,
        columns=tuple(columns),
    )


# ---------------------------------------------------------------------------
# Words with numbers: every number said is recorded, to be stored as the evidence
# ---------------------------------------------------------------------------
@dataclass
class _Say:
    numbers: list[float | int | str] = field(default_factory=list)

    def count(self, value: float | int) -> str:
        self.numbers.append(value)
        return f"{value:,}"

    def pct(self, share: float) -> str:
        shown = round(share * 100, 1) if share < 0.1 else round(share * 100)
        self.numbers.extend([share, shown])
        return f"{shown:g}%"

    def text(self, value: str) -> str:
        self.numbers.append(value)
        return value


@dataclass
class Finding:
    level: Level
    code: str
    subject: str
    text: str
    would_do: str
    if_not: str
    column: str | None = None
    numbers: tuple[float | int | str, ...] = ()


def _mk(level: Level, code: str, subject: str, text: str, say: _Say, column: str | None = None) -> Finding:
    would_do, if_not = _CONSEQUENCE[level]
    return Finding(level, code, subject, text, would_do, if_not, column, tuple(say.numbers))


_CONSEQUENCE: Final[dict[str, tuple[str, str]]] = {
    "stop": (
        "wait for you to say whether this is the right file",
        "If this is the wrong or an old file, everything made from it would be wrong.",
    ),
    "flag": (
        "carry on with this file, and keep this on the plan so you can check it",
        "If the file is wrong or out of date, the results made from it will be wrong too.",
    ),
    "note": ("carry on as planned", "Nothing changes; this is only so you know."),
}


def _level(change: float, band: float, *, ratio: bool) -> Level | None:
    """The level of a move: `change` is a relative change (a count) or a ratio >= 1 (a rate)."""
    if ratio:
        return (
            "stop"
            if change > band**2
            else "flag" if change > band else "note" if change > band**0.75 else None
        )
    return "stop" if change > 2 * band else "flag" if change > band else "note" if change > band / 3 else None


def _worst(*levels: Level | None) -> Level | None:
    worst: Level | None = None
    for level in levels:
        if level is not None and (worst is None or _RANK[level] > _RANK[worst]):
            worst = level
    return worst


def _fact(facts: Sequence[ClientFact], kind: FactKind, goal_id: str | None) -> ClientFact | None:
    live = [f for f in facts if f.kind is kind and f.revoked_at is None]
    return next((f for f in live if f.goal_id == goal_id), None) or next(
        (f for f in live if f.goal_id is None), None
    )


def _count_finding(now: Fingerprint, prev: Fingerprint | None, fact: ClientFact | None) -> Finding | None:
    if now.customers is None:
        return None
    say = _Say()
    levels: list[Level | None] = []
    parts: list[str] = []
    if fact is not None and isinstance(fact.value, int | float):
        ref = float(fact.value)
        levels.append(_level(abs(now.customers - ref) / ref, fact.band(), ratio=False))
        parts.append(
            f"You told me {'about' if fact.approximate else 'exactly'} {say.count(int(ref))} customers."
        )
    if prev is not None and prev.customers:
        levels.append(_level(abs(now.customers - prev.customers) / prev.customers, 0.3, ratio=False))
        parts.append(f"Last time: {say.count(prev.customers)} customers.")
    level = _worst(*levels)
    if level is None:
        return None
    parts.append(f"Now: {say.count(now.customers)} customers.")
    return _mk(level, "FACTS_CUSTOMER_COUNT", "customer_count", " ".join(parts), say)


def _rate_ratio(now: float, ref: float) -> float:
    if ref <= 0 or now <= 0:
        return float("inf") if (ref > 0) != (now > 0) else 1.0
    return max(now / ref, ref / now)


def _rate_finding(now: Fingerprint, prev: Fingerprint | None, fact: ClientFact | None) -> Finding | None:
    if now.outcome_rate is None:
        return None
    say = _Say()
    name = f"‘{now.outcome_column}’"
    levels: list[Level | None] = []
    parts: list[str] = []
    if fact is not None and isinstance(fact.value, int | float):
        levels.append(_level(_rate_ratio(now.outcome_rate, float(fact.value)), fact.band(), ratio=True))
        parts.append(
            f"You told me {name} is yes {'about' if fact.approximate else 'exactly'} {say.pct(float(fact.value))} of the time."
        )
    if prev is not None and prev.outcome_rate is not None:
        levels.append(_level(_rate_ratio(now.outcome_rate, prev.outcome_rate), 2.0, ratio=True))
        parts.append(f"Last time: {say.pct(prev.outcome_rate)} yes.")
    level = _worst(*levels)
    if level is None:
        return None
    parts.append(f"Now: {say.pct(now.outcome_rate)} yes.")
    return _mk(level, "FACTS_OUTCOME_RATE", "outcome_rate", " ".join(parts), say, now.outcome_column)


def _id_length_finding(now: Fingerprint, fact: ClientFact | None) -> Finding | None:
    if fact is None or not now.key_lengths or not isinstance(fact.value, int | float):
        return None
    share = now.key_lengths.get(str(int(fact.value)), 0.0)
    level: Level | None = "stop" if share < 0.2 else "flag" if share < 0.9 else None
    if level is None:
        return None
    say = _Say()
    text = f"You told me IDs are {say.count(int(fact.value))} characters long. In this file {say.pct(share)} of the IDs are."
    return _mk(level, "FACTS_ID_LENGTH", "id_length", text, say, now.key_column)


def _same_kind(a: ColumnPrint, b: ColumnPrint) -> bool:
    return _FAMILY.get(a.type, a.type) == _FAMILY.get(b.type, b.type)


def _looks_renamed(gone: ColumnPrint, new: ColumnPrint) -> bool:
    if not _same_kind(gone, new):
        return False
    if gone.top_hash and gone.top_hash == new.top_hash:
        return True
    if difflib.SequenceMatcher(None, gone.name.lower(), new.name.lower()).ratio() >= 0.6:
        return True
    close = abs(gone.distinct - new.distinct) <= max(2, 0.1 * max(gone.distinct, new.distinct))
    return close and abs(gone.missing_share - new.missing_share) <= 0.05 and gone.distinct > 12


def _column_findings(now: Fingerprint, prev: Fingerprint) -> list[Finding]:
    found: list[Finding] = []
    mine = {c.name: c for c in now.columns}
    before = {c.name: c for c in prev.columns}
    gone = [c for name, c in before.items() if name not in mine]
    new = [c for name, c in mine.items() if name not in before]
    important = {prev.key_column, prev.outcome_column}
    for lost in gone:
        say = _Say()
        twin = next((c for c in new if _looks_renamed(lost, c)), None)
        level: Level = "stop" if lost.name in important else "flag"
        if twin is not None:
            new.remove(twin)
            text = f"Last time there was a column ‘{lost.name}’. This file has no such column, but it has ‘{twin.name}’ with similar values; it may have been renamed."
            found.append(_mk(level, "FACTS_COLUMN_RENAMED", f"renamed.{lost.name}", text, say, lost.name))
        else:
            text = f"Last time there was a column ‘{lost.name}’. This file does not have it."
            found.append(_mk(level, "FACTS_COLUMN_MISSING", f"missing.{lost.name}", text, say, lost.name))
    for name, column in mine.items():
        old = before.get(name)
        if old is not None and not _same_kind(old, column):
            say = _Say()
            text = f"Column ‘{name}’ was {old.type} last time and is {column.type} now."
            level = "stop" if name in important else "flag"
            found.append(_mk(level, "FACTS_TYPE_CHANGED", f"type.{name}", text, say, name))
        if old is not None and old.shares and column.shares:
            moved = max(
                (
                    (abs(column.shares.get(v, 0.0) - old.shares.get(v, 0.0)), v)
                    for v in {*old.shares, *column.shares}
                ),
                default=(0.0, ""),
            )
            if moved[0] > SHARE_MOVE:
                say = _Say()
                value = moved[1]
                text = (
                    f"In ‘{name}’, “{value}” was {say.pct(old.shares.get(value, 0.0))} of the rows last time "
                    f"and is {say.pct(column.shares.get(value, 0.0))} now."
                )
                found.append(_mk("flag", "FACTS_CATEGORY_SHARE", f"share.{name}", text, say, name))
    if new:
        say = _Say()
        names = ", ".join(f"‘{c.name}’" for c in new[:5]) + (" and others" if len(new) > 5 else "")
        found.append(_mk("note", "FACTS_NEW_COLUMNS", "new_columns", f"New since last time: {names}.", say))
    return found


def _key_and_date_findings(now: Fingerprint, prev: Fingerprint) -> list[Finding]:
    found: list[Finding] = []
    if (
        now.key_pattern
        and prev.key_pattern
        and now.key_pattern != prev.key_pattern
        and min(now.key_pattern_share, prev.key_pattern_share) >= 0.6
    ):
        say = _Say()
        text = (
            f"IDs looked like {_describe_shape(prev.key_pattern, say)} last time. "
            f"Now they look like {_describe_shape(now.key_pattern, say)}."
        )
        found.append(_mk("flag", "FACTS_KEY_FORMAT", "key_format", text, say, now.key_column))
    if now.date_last and prev.date_last and now.date_last <= prev.date_last:
        say = _Say()
        same = now.date_last == prev.date_last
        text = (
            f"The newest date in ‘{now.date_column}’ is {say.text(now.date_last)}, "
            f"{'the same as' if same else 'older than'} last time ({say.text(prev.date_last)}). This may be an old file."
        )
        found.append(
            _mk(
                "flag" if same else "stop",
                "FACTS_DATES_NOT_LATER",
                "dates_not_later",
                text,
                say,
                now.date_column,
            )
        )
    return found


def compare(now: Fingerprint, prev: Fingerprint | None, facts: Sequence[ClientFact] = ()) -> list[Finding]:
    """What moved since `prev` and against the client's `facts`, worst first. Nothing to compare with: nothing."""
    goal = now.goal_id
    found = [
        f
        for f in (
            _count_finding(now, prev, _fact(facts, FactKind.CUSTOMER_COUNT, goal)),
            _rate_finding(now, prev, _fact(facts, FactKind.OUTCOME_RATE, goal)),
            _id_length_finding(now, _fact(facts, FactKind.ID_LENGTH, goal)),
        )
        if f is not None
    ]
    if prev is not None:
        found += _column_findings(now, prev) + _key_and_date_findings(now, prev)
    return sorted(found, key=lambda f: -_RANK[f.level])


# ---------------------------------------------------------------------------
# The visit's detector, and the record after approval
# ---------------------------------------------------------------------------
def _decided(case: Case, kind: str, upload_id: str, goal_id: str | None = None) -> str | None:
    for item in case.items:
        if item.kind == kind and item.upload_id == upload_id and item.state is ProposalState.ACCEPTED:
            if goal_id is not None and item.goal_id not in (None, goal_id):
                continue
            column = item.payload.get("column")
            if isinstance(column, str):
                return column
    return None


def _main_file(
    case: Case, files: Sequence[LoadedFile], prev: Fingerprint | None, seen: frozenset[str] = frozenset()
) -> LoadedFile | None:
    """The file that carries the case: the one that looks most like last time's, else the one with the outcome.
    A file not fingerprinted yet comes first (next month's file sits in the case beside last month's)."""
    files = [f for f in files if f.upload_id not in seen] or list(files)
    if not files:
        return None
    if prev is not None:
        names = {c.name for c in prev.columns}
        best = max(files, key=lambda f: len(names & {str(c) for c in f.frame.columns}))
        if names & {str(c) for c in best.frame.columns}:
            return best
    for loaded in files:
        if _decided(case, "outcome_column", loaded.upload_id) is not None:
            return loaded
    return max(files, key=lambda f: f.profile.row_count)


def _current(
    case: Case, loaded: LoadedFile, brief: GoalBrief | None, at: datetime, actor: str = ""
) -> Fingerprint:
    goal = brief.goal_id if brief is not None else None
    outcome = _decided(case, "outcome_column", loaded.upload_id, goal)
    if outcome is None and brief is not None and brief.outcome.column in loaded.frame.columns:
        outcome = brief.outcome.column
    return fingerprint_file(
        loaded,
        goal_id=goal,
        key=_decided(case, "key", loaded.upload_id),
        outcome=outcome,
        brief=brief,
        at=at,
        actor=actor,
    )


def _same_file_kind(now: Fingerprint, prev: Fingerprint) -> bool:
    """Whether `prev` is a print of the same kind of file: half of the smaller file's columns are in both. A file
    that only shares its ID column with it is another table of the same client, not last month's file changed.
    """
    a, b = {c.name for c in now.columns}, {c.name for c in prev.columns}
    return 2 * len(a & b) >= min(len(a), len(b))


def fingerprint_detector(storage: Storage, *, clock: Callable[[], datetime] = utc_now) -> DetectorPort:
    """Compare the case's main file with the client's facts and the last fingerprint of the same goal."""

    def detect(case: Case, files: tuple[LoadedFile, ...], brief: GoalBrief | None) -> Detected:
        from engine.doctor.visit import Detected

        goal = brief.goal_id if brief is not None else None
        store = FingerprintStore(storage)
        facts = FactStore(storage).list(case.client_id)
        loaded = _main_file(case, files, store.last(case.client_id, goal), store.uploads(case.client_id))
        if loaded is None:
            return Detected()
        prev = store.last(case.client_id, goal, besides=loaded.upload_id)
        if prev is None and not facts:
            return Detected()
        now = _current(case, loaded, brief, clock())
        if prev is not None and not _same_file_kind(now, prev):
            prev = None  # last time's print was of another file (an orders log, beside this customer table)
        findings = compare(now, prev, facts)
        if not findings:
            return Detected()
        return _report(findings, loaded, goal, clock())

    return detect


def _report(findings: list[Finding], loaded: LoadedFile, goal: str | None, at: datetime) -> Detected:
    from engine.doctor.visit import Detected

    given = {"upload_id": loaded.upload_id, "goal_id": goal}
    output = {
        "findings": [
            {"code": f.code, "level": f.level, "text": f.text, "numbers": list(f.numbers)} for f in findings
        ]
    }
    evidence = DataToolResult(
        evidence_id=data_evidence_id(COMPARE_TOOL, given, output),
        tool=COMPARE_TOOL,
        kind=DataToolKind.READ,
        exposure=Exposure.AGGREGATE,
        input=given,
        output=output,
        computed_at=at,
    )
    asks: list[Ask] = []
    doubts: list[CaseDoubt] = []
    names = [loaded.upload_id]
    for finding in findings:
        if finding.level == "stop":
            asks.append(_stop_ask(finding, loaded, goal, evidence.evidence_id))
            continue
        code = finding.code if finding.level == "flag" else finding.code.replace("FACTS_", "FACTS_NOTE_", 1)
        try:
            doubts.append(
                express_doubt(
                    code,
                    because=finding.text,
                    would_do=finding.would_do,
                    if_not=finding.if_not,
                    evidence={evidence.evidence_id: evidence.output},
                    names=names,
                    upload_id=loaded.upload_id,
                    goal_id=goal,
                )
            )
        except ProtocolError as exc:  # a sentence whose numbers are not all stored never reaches the person
            _LOG.warning("doctor.fingerprint_ungrounded code=%s error=%s", finding.code, exc.code)
            asks.append(_stop_ask(finding, loaded, goal, evidence.evidence_id))
    return Detected(asks=tuple(asks), doubts=tuple(doubts), evidence=(evidence,))


def _stop_ask(finding: Finding, loaded: LoadedFile, goal: str | None, evidence_id: str) -> Ask:
    subject: dict[str, JsonValue] = {
        "upload_id": loaded.upload_id,
        "check": finding.subject,
        "numbers": [str(n) for n in finding.numbers],
    }
    return Ask(
        code=finding.code,
        subject=subject,
        text=f"{finding.text} Is this the file you meant?",
        why="It is far from what you told me or from last time, so I will not go on until you say it is right.",
        options=(
            AskOption(label="Yes, this is the right file", effect=OptionEffect(option_id="ok")),
            AskOption(label="No, I will check the file", effect=OptionEffect(option_id="stop"), safe=True),
        ),
        rows=int(loaded.profile.row_count),
        upload_id=loaded.upload_id,
        column=finding.column,
        goal_id=goal,
        blocking=True,
        meaning_bearing=True,
        evidence_ids=(evidence_id,),
    )


def record_fingerprint(
    storage: Storage, case: Case, files: Sequence[LoadedFile], brief: GoalBrief | None, *, at: datetime
) -> Fingerprint | None:
    """After approval: keep what this client's data looked like. Best effort, logged when it cannot be saved."""
    store = FingerprintStore(storage)
    goal = brief.goal_id if brief is not None else None
    approver = case.approvals[-1].approved_by if case.approvals else ""
    try:
        loaded = _main_file(case, files, store.last(case.client_id, goal), store.uploads(case.client_id))
        if loaded is None:
            return None
        print_ = _current(case, loaded, brief, at, approver)
        store.save(case.client_id, print_)
    except Exception as exc:  # the hand-off goes on; its timeline event says nothing was remembered
        _LOG.warning("doctor.fingerprint_not_saved case=%s error=%s", case.case_id, type(exc).__name__)
        return None
    return print_
