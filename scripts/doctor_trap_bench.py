"""Trap bench v0: planted-trap data cases that Data Doctor must handle, graded on the RESULTING DATA.

Each case under `tests/fixtures/doctor_traps/<case>/` is a few small CSV files and a `case.json` written by a human:
one line saying what the trap is, the scenario ID it comes from (`SCENARIOS.md`), what a correct result looks like,
and the checks that grade it. The runner puts the files through the unified Data Doctor visit with **no AI** (the
rules path), the way a person who clicks through the product would: it answers every question with the answer the
case file gives, ticks every suggestion, gives each change that needs its own yes that yes, approves, and then
applies the approved changes to the files with the product's own replay (`engine.doctor.prescription_replay`).
What comes out is the table the next step would train or contact on. Only that table (and the questions the Doctor
asked) is graded; the Doctor's own wording never counts as evidence.

What is graded (a case lists the checks that apply; the first two apply to every case):

* **row loss** - every customer of the main file is still in the result (no silent loss);
* **unapproved change** - a column no approved change touched has the values it had in the file;
* **join** - keys not merged, one row per customer kept, no customer repeated, cells of named customers right;
* **double counting** - a money total in the result equals the true total, not a repeated one;
* **leak** - a column known only after the outcome is not left among the features;
* **outcome population** - customers who cannot have the outcome (never active, window not finished, already hold
  the product) are not counted, and the positive rate is the true one;
* **contact list** - who would be contacted, using the product's own consent rule (`engine.stages.actions`), leaves
  out opt-outs, minors, deceased, test accounts and a second person of the same household;
* **asked** - for ambiguous traps, the Doctor raised the right question (or a doubt) instead of guessing.

A check may be `any_of` several: "asked the right question, or left the values alone". A case passes a run only if
every check passes, and passes the bench only if **all k runs** pass (default 2). Many traps fail today; that is
the point. The bench measures; it does not have to pass.

    python -m scripts.doctor_trap_bench                       # writes reports/doctor_trap_bench.{md,json}
    python -m scripts.doctor_trap_bench --cases K1,V19 -k 3
    python -m scripts.doctor_trap_bench --list

`--model product` is reserved for the run with the real AI service and is not built yet.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    import pandas as pd

REPO: Final[Path] = Path(__file__).resolve().parent.parent
CASES_DIR: Final[Path] = REPO / "tests" / "fixtures" / "doctor_traps"
DEFAULT_OUT: Final[Path] = REPO / "reports" / "doctor_trap_bench"
DEFAULT_GOAL: Final[str] = "telco-churn"
DEFAULT_K: Final[int] = 2
CLIENT: Final[str] = "default"
ANALYST: Final[str] = "u-trap-bench"
MAX_TURNS: Final[int] = 40
ROW_CAP: Final[int] = 200_000
# The fixtures' as-of date (`generate.AS_OF`): ages are graded on it, so a minor stays a minor as the years pass.
AS_OF: Final[str] = "2026-09-30"

CATEGORIES: Final[dict[str, str]] = {
    "row_loss": "No silent row loss",
    "unapproved_change": "No unapproved change",
    "join": "Correct join (keys not merged, one row per customer)",
    "double_counting": "No double counting (totals)",
    "leak": "No leak column kept",
    "outcome_population": "Outcome population correct",
    "contact_list": "Contact-list exclusions applied",
    "asked": "Asked the right question",
}
"""The things graded, in the order the report lists them."""

_DONT_KNOW: Final[re.Pattern[str]] = re.compile(r"^(i don't know|none of these|leave )", re.IGNORECASE)


class BenchError(Exception):
    """A case file that cannot be read, or a setting the bench does not offer."""


# ---------------------------------------------------------------------------
# The case files
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TrapCase:
    """One planted trap, as the human wrote it."""

    case_id: str
    scenario: str
    theme: str
    description: str
    expected: str
    goal: str
    files: dict[str, str]
    """Table name -> CSV file name inside the case folder."""
    primary: tuple[str, ...]
    """The table(s) whose rows are the customers: the first is the one graded; the rest may be stacked into it."""
    key: str
    answers: dict[str, str]
    """What the person answers: question code (or `*`) -> the start of the option's label."""
    rows_may_drop: tuple[str, ...]
    checks: tuple[dict[str, Any], ...]
    ideal: dict[str, Any]
    path: Path

    def raw(self, table: str) -> list[dict[str, str]]:
        """The rows of a table exactly as written, every cell a string (what the person's file says)."""
        text = (self.path / self.files[table]).read_text(encoding="utf-8")
        return list(csv.DictReader(io.StringIO(text)))


def load_case(path: Path) -> TrapCase:
    """Read `path/case.json`; a case missing a required part is refused, never half-run."""
    try:
        doc = json.loads((path / "case.json").read_text(encoding="utf-8"))
        files = {str(k): str(v) for k, v in doc["files"].items()}
        primary = tuple(str(p) for p in doc["primary"])
        case = TrapCase(
            case_id=str(doc["id"]),
            scenario=str(doc["scenario"]),
            theme=str(doc["theme"]),
            description=str(doc["description"]),
            expected=str(doc["expected"]),
            goal=str(doc.get("goal") or DEFAULT_GOAL),
            files=files,
            primary=primary,
            key=str(doc["key"]),
            answers={str(k): str(v) for k, v in doc.get("answers", {}).items()},
            rows_may_drop=tuple(str(k) for k in doc.get("rows_may_drop", ())),
            checks=tuple(dict(c) for c in doc["checks"]),
            ideal=dict(doc.get("ideal", {})),
            path=path,
        )
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise BenchError(f"{path.name}: the case file cannot be read ({exc!r})") from exc
    missing = [t for t in (*primary, *files) if t not in files]
    if missing:
        raise BenchError(f"{path.name}: unknown table {missing[0]!r}")
    return case


def load_cases(root: Path = CASES_DIR, only: Sequence[str] = ()) -> list[TrapCase]:
    """Every case under `root` (sorted by id), or only those whose id starts with one of `only`."""
    cases = [load_case(p.parent) for p in sorted(root.glob("*/case.json"))]
    if only:
        wanted = [o.lower() for o in only]
        cases = [c for c in cases if any(c.case_id.lower().startswith(w) for w in wanted)]
    return cases


# ---------------------------------------------------------------------------
# What a visit shipped
# ---------------------------------------------------------------------------
@dataclass
class Shipped:
    """What a run handed on: the table(s), what was asked, what was approved. Nothing here is the Doctor's prose."""

    final: pd.DataFrame | None = None
    """The table of the main file after the approved changes (a join or stack made one table of several)."""
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    questions: list[dict[str, str]] = field(default_factory=list)
    """Every question the visit raised: `code`, `text`."""
    doubts: list[str] = field(default_factory=list)
    accepted: list[dict[str, Any]] = field(default_factory=list)
    """Items that were ticked: `kind`, `title`, `column`, `step_kind`."""
    key_column: str | None = None
    outcome_column: str | None = None
    consent_column: str | None = None
    touched: frozenset[str] = frozenset()
    """Columns an approved step changed or made."""
    notes: list[str] = field(default_factory=list)
    """Things that went wrong on the way (a refusal, a stop); never graded as evidence."""
    error: str | None = None

    def asked_text(self) -> list[str]:
        return [f"{q['code'].replace('_', ' ')} {q['text']}" for q in self.questions] + list(self.doubts)


# ---------------------------------------------------------------------------
# Running the visit
# ---------------------------------------------------------------------------
class _World:
    """The case's files as the Doctor reads them: written to a store, read back by the product's own reader."""

    def __init__(self, storage: Any, config: Any, case_key: str) -> None:
        self.storage = storage
        self.config = config
        self.case_key = case_key
        self.loaded: dict[str, Any] = {}
        self.full: dict[str, pd.DataFrame] = {}
        self.names: dict[str, str] = {}

    def add(self, upload_id: str, file_name: str, data: bytes) -> None:
        from engine.doctor.diagnose import LoadedFile
        from engine.stages import ingest
        from engine.storage import upload_key

        key = upload_key(upload_id, "source.csv")
        self.storage.write_bytes(key, data)
        read = ingest.read_upload(
            self.storage, key, file_format="csv", row_cap=ROW_CAP, keep_all_rows=True, keep_leading_zeros=True
        )
        profile = ingest.profile_dataset(
            read.frame,
            self.config,
            upload_id=upload_id,
            file_name=file_name,
            file_format=read.file_format,
            file_size_bytes=len(data),
            delimiter=read.delimiter,
            encoding=read.encoding,
            row_count=read.row_count,
        )
        self.loaded[upload_id] = LoadedFile(
            upload_id=upload_id, file_name=file_name, frame=read.frame, profile=profile
        )
        self.full[upload_id] = read.all_rows if read.all_rows is not None else read.frame
        self.names[upload_id] = file_name


def _pick(options: Sequence[Any], wanted: str | None) -> Any:
    """The option the person takes: the one whose label starts with `wanted`; else the first that is not a way out."""
    if wanted:
        for option in options:
            if option.label.lower().startswith(wanted.lower()):
                return option
    for option in options:
        if not _DONT_KNOW.match(option.label):
            return option
    return options[0]


def _answer_for(case: TrapCase, code: str) -> str | None:
    return case.answers.get(code, case.answers.get("*"))


def _drive(case: TrapCase, uploads: dict[str, str], deps: Any) -> Any:
    """Walk the visit like a careful person: answer, tick everything, confirm what needs its own yes, approve."""
    from engine.access.roles import LOCAL_OPERATOR
    from engine.agent.contracts import ProposalState
    from engine.doctor import visit
    from engine.doctor.case import CaseFile, CaseStore, VisitState
    from engine.doctor.protocol import item_sentence
    from engine.utils.time import utc_now

    store = CaseStore(deps.storage)
    files = [
        CaseFile(upload_id=uploads[t], file_name=case.files[t], added_at=utc_now(), added_by=ANALYST)
        for t in case.files
    ]
    current = store.create(client_id=CLIENT, created_by=ANALYST, files=files)
    current = visit.resume(CLIENT, current.case_id, deps, actor=ANALYST)
    current = visit.set_goal(
        CLIENT, current.case_id, case.goal, deps, expected_version=current.version, actor=ANALYST
    )
    approved = False
    for _turn in range(MAX_TURNS):
        now = store.get(CLIENT, current.case_id)
        blocking = visit.open_blocking(now)
        if blocking:
            question = blocking[0]
            option = _pick(question.options, _answer_for(case, question.code))
            current = visit.answer(
                CLIENT, now.case_id, question.question_id, option.option_id, deps,
                expected_version=now.version, actor=ANALYST,
            )  # fmt: skip
            continue
        items, _ = visit.active_section(now)
        pending = [i for i in items if i.state is ProposalState.PENDING]
        if pending:
            current = visit.decide(
                CLIENT, now.case_id, pending[0].item_id, True, deps,
                expected_version=now.version, actor=ANALYST,
            )  # fmt: skip
            continue
        missing = visit.unconfirmed(now, deps.protocol)
        if missing:
            item = missing[0]
            current = visit.confirm(
                CLIENT, now.case_id, item.item_id, deps, principal=LOCAL_OPERATOR,
                sentence=item_sentence(item.title, item.payload), expected_version=now.version,
            )  # fmt: skip
            continue
        if now.state is VisitState.APPROVE and not approved:
            current = visit.approve(CLIENT, now.case_id, deps, expected_version=now.version, actor=ANALYST)
            approved = True
            continue
        if approved and now.state not in {VisitState.APPROVE, VisitState.TREAT}:
            return now
        if not approved and now.state not in {VisitState.APPROVE, VisitState.TREAT}:
            current = visit.resume(CLIENT, now.case_id, deps, actor=ANALYST)
            if store.get(CLIENT, now.case_id).version == now.version:
                return now
            continue
        return now
    return store.get(CLIENT, current.case_id)


def _text(value: Any) -> str:
    """A cell as text for comparison: blanks are `""`, a whole number read as a float is the whole number."""
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
    return str(value).strip()


def ship(trap: TrapCase) -> Shipped:
    """One run: the case through the unified visit with no AI, then the approved changes applied to the files."""
    from engine.config import load_use_case
    from engine.doctor.case import Case, CaseFile, CaseItem, CaseStore
    from engine.doctor.cleaning import cleaning_detector
    from engine.doctor.combine_run import combine_item, upload_ids_of
    from engine.doctor.combining import combining_detector
    from engine.doctor.contracts import Diagnosis
    from engine.doctor.detect_time_keys import time_keys_detector
    from engine.doctor.diagnose import LoadedFile, Offer
    from engine.doctor.goal_brief import GoalBrief
    from engine.doctor.prescription import CasePrescriber
    from engine.doctor.sensitive_ask import sensitive_detector
    from engine.doctor.time_settings import time_settings_detector
    from engine.doctor.visit import VerifyResult, VisitDeps, stored_brief
    from engine.storage import LocalStorage
    from engine.utils.time import utc_now

    shipped = Shipped(key_column=trap.key)
    config = load_use_case(trap.goal)
    with tempfile.TemporaryDirectory(prefix="trap-bench-") as tmp:
        storage = LocalStorage(Path(tmp) / "store")
        stem = hashlib.sha256(trap.case_id.encode()).hexdigest()[:8]
        world = _World(storage, config, stem)
        uploads = {t: f"up-{stem}-{i}" for i, t in enumerate(trap.files)}
        for table, upload_id in uploads.items():
            world.add(upload_id, trap.files[table], (trap.path / trap.files[table]).read_bytes())

        def load_files(case: Case) -> tuple[LoadedFile, ...]:
            return tuple(world.loaded[u] for u in case.upload_ids())

        def combine(case: Case, item: CaseItem, actor: str) -> CaseFile:
            ids = upload_ids_of(item)
            made = combine_item(
                item, Diagnosis.model_validate(case.diagnosis), {u: world.full[u] for u in ids}, world.names
            )
            upload_id = f"up-{stem}-c{len(world.names)}"
            world.add(upload_id, made.file_name, made.frame.to_csv(index=False).encode("utf-8"))
            return CaseFile(
                upload_id=upload_id, file_name=made.file_name, original_upload_id=ids[0],
                added_at=utc_now(), added_by=actor,
            )  # fmt: skip

        def verify(case: Case, brief: GoalBrief | None, files: tuple[LoadedFile, ...]) -> VerifyResult:
            del case, brief, files
            return VerifyResult(verdict="ready", reason="the bench grades the data, not the readiness report")

        deps = VisitDeps(
            storage=storage,
            load_files=load_files,
            prescribe=CasePrescriber(storage, load_files=load_files),
            combine=combine,
            verify=verify,
            offers=(Offer(config=config, stage="Retain"),),
            detectors=(
                cleaning_detector((Offer(config=config, stage="Retain"),)),
                combining_detector(),
                time_keys_detector(),
                sensitive_detector(),
                time_settings_detector(storage),
            ),
        )
        try:
            done = _drive(trap, uploads, deps)
        except Exception as exc:  # a refusal or a crash is the Doctor's result, not the bench's
            shipped.error = f"the visit stopped: {type(exc).__name__}: {exc}"
            found = CaseStore(storage).list(CLIENT)
            if found:  # what it asked before it stopped still counts
                _collect(shipped, found[0])
            return shipped
        _collect(shipped, done)
        try:
            _apply(trap, shipped, done, world, uploads, deps, config, stored_brief)
        except Exception as exc:
            shipped.error = f"the approved changes could not be applied: {type(exc).__name__}: {exc}"
    return shipped


def _collect(shipped: Shipped, done: Any) -> None:
    from engine.agent.contracts import ProposalState

    shipped.questions = [{"code": q.code, "text": q.text} for q in done.questions]
    shipped.doubts = [f"{d.because} {d.would_do}" for d in done.doubts]
    for item in done.items:
        if item.state is not ProposalState.ACCEPTED:
            continue
        step = item.payload.get("step")
        step = step if isinstance(step, dict) else {}
        column = item.payload.get("column")
        shipped.accepted.append(
            {
                "kind": item.kind,
                "title": item.title,
                "column": column if isinstance(column, str) else step.get("column"),
                "step_kind": step.get("kind"),
            }
        )
        for name in (step.get("column"), step.get("new_column")):
            if isinstance(name, str):
                shipped.touched |= {name}
        if item.kind == "key" and isinstance(column, str):
            shipped.key_column = column
        elif item.kind == "outcome_column" and isinstance(column, str):
            shipped.outcome_column = column
        elif item.kind == "consent_column" and isinstance(column, str):
            shipped.consent_column = column


def _apply(
    case: TrapCase, shipped: Shipped, done: Any, world: _World, uploads: dict[str, str],
    deps: Any, config: Any, stored_brief: Callable[..., Any],
) -> None:  # fmt: skip
    """The approved changes on the whole files, by the product's own replay; the main file's result is `final`."""
    from engine.doctor.prescription import CasePrescriber
    from engine.doctor.prescription_replay import ReplayFile, replay_prescription

    prescriber = CasePrescriber(deps.storage, load_files=deps.load_files)
    prescription = prescriber.prescription(done, stored_brief(deps.storage, done))
    goal = next((g.goal_id for g in done.goals if g.active), None)
    files = [
        ReplayFile(slot=part.slot, frame=world.full[u], upload_id=u)
        for part in prescription.files
        for u in part.upload_ids
        if u in world.full
    ]
    if prescription.build is not None:
        shipped.notes.append("the approved build was not replayed (the bench has no builder)")
    result = replay_prescription(prescription, files, mode="train", goal_id=goal, config=config)
    slot_of = {u: part.slot for part in prescription.files for u in part.upload_ids}
    by_upload = {u: result.frames[slot] for u, slot in slot_of.items() if slot in result.frames}
    first = uploads[case.primary[0]]
    made = next((f for f in done.files if f.original_upload_id == first), None)
    holder = made.upload_id if made is not None else first
    shipped.final = by_upload.get(holder)
    if shipped.final is None and result.frame is not None:
        shipped.final = result.frame
    if result.frame is not None and made is None and len(result.frames) == 1:
        shipped.final = result.frame  # the outcome step may have changed the one file
    shipped.tables = {t: by_upload[u] for t, u in uploads.items() if u in by_upload}


# ---------------------------------------------------------------------------
# Grading the data
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Verdict:
    passed: bool
    detail: str


@dataclass
class Grader:
    case: TrapCase
    shipped: Shipped

    @property
    def primary(self) -> str:
        return self.case.primary[0]

    def truth(self, table: str | None = None) -> list[dict[str, str]]:
        return self.case.raw(table or self.primary)

    def final(self) -> pd.DataFrame | None:
        return self.shipped.final

    def key_in_final(self) -> str | None:
        frame = self.shipped.final
        if frame is None:
            return None
        for name in (self.case.key, self.shipped.key_column):
            if name is not None and name in frame.columns:
                return name
        return None


def _canon(value: Any) -> str:
    """A customer key for matching rows: the text, a float's `.0` gone, leading zeros gone (their loss is reported
    by the unapproved-change check, not as a lost customer)."""
    text = _text(value)
    try:
        number = float(text)
    except ValueError:
        return text.lstrip("0") or text
    return str(int(number)) if number.is_integer() else text


def _no_table(g: Grader) -> Verdict:
    why = g.shipped.error or "the visit handed on no table"
    return Verdict(False, f"no result table: {why}")


def _sample(items: Sequence[Any], n: int = 4) -> str:
    shown = ", ".join(str(i) for i in list(items)[:n])
    return shown + (f" and {len(items) - n} more" if len(items) > n else "")


def check_keep_all_rows(g: Grader, args: Mapping[str, Any]) -> Verdict:
    """Every customer of the main file is in the result (except those the case says may go)."""
    frame, key = g.final(), g.key_in_final()
    if frame is None:
        return _no_table(g)
    if key is None:
        return Verdict(False, f"the result has no '{g.case.key}' column to find the customers by")
    allowed = {_canon(k) for k in (*g.case.rows_may_drop, *args.get("except", ()))}
    wanted = {_canon(r[g.case.key]) for r in g.truth() if g.case.key in r} - allowed
    have = {_canon(v) for v in frame[key]}
    lost = sorted(wanted - have)
    if lost:
        return Verdict(
            False, f"{len(lost)} of {len(wanted)} customers are gone from the result: {_sample(lost)}"
        )
    return Verdict(True, f"all {len(wanted)} customers are in the result")


def check_row_count(g: Grader, args: Mapping[str, Any]) -> Verdict:
    """The result has as many rows as the file had (nothing dropped, nothing made up)."""
    frame = g.final()
    if frame is None:
        return _no_table(g)
    if len(frame) != int(args["rows"]):
        return Verdict(False, f"the result has {len(frame)} rows; the file has {int(args['rows'])}")
    return Verdict(True, f"{len(frame)} rows, as in the file")


def check_one_row_per_key(g: Grader, args: Mapping[str, Any]) -> Verdict:
    """The result has one row per customer, and the number of rows the case expects."""
    frame, key = g.final(), g.key_in_final()
    if frame is None:
        return _no_table(g)
    if key is None:
        return Verdict(False, f"the result has no '{g.case.key}' column")
    counts = Counter(_canon(v) for v in frame[key])
    repeated = sorted(k for k, n in counts.items() if n > 1)
    expected = args.get("expected_rows")
    if repeated:
        return Verdict(False, f"{len(repeated)} customers appear more than once: {_sample(repeated)}")
    if expected is not None and len(frame) != int(expected):
        return Verdict(False, f"the result has {len(frame)} rows; the customers are {int(expected)}")
    return Verdict(True, f"{len(frame)} rows, one per customer")


def check_values_unchanged(g: Grader, args: Mapping[str, Any]) -> Verdict:
    """Named columns still hold the values the file had, customer by customer."""
    frame, key = g.final(), g.key_in_final()
    if frame is None:
        return _no_table(g)
    if key is None:
        return Verdict(False, f"the result has no '{g.case.key}' column to line the rows up by")
    table = str(args.get("table", g.primary))
    truth = g.truth(table)
    keyname = str(args.get("by", g.case.key))
    columns = [str(c) for c in args["columns"]]
    absent = [c for c in columns if c not in frame.columns]
    if absent:
        return Verdict(False, f"column {absent[0]!r} is not in the result")
    by_key = {_canon(k): i for i, k in zip(frame.index, frame[key], strict=True)}
    repeats = {k for k, n in Counter(_canon(r[keyname]) for r in truth).items() if n > 1}
    changed: list[str] = []
    for row in truth:
        index = by_key.get(_canon(row[keyname]))
        if index is None or _canon(row[keyname]) in repeats:
            continue  # a lost row is the row-loss check's finding; rows that share a key cannot be lined up
        for column in columns:
            if _text(frame.at[index, column]) != row[column].strip():
                changed.append(
                    f"{column} of {row[keyname]}: '{row[column]}' became '{_text(frame.at[index, column])}'"
                )
    if changed:
        return Verdict(False, f"{len(changed)} values changed: {_sample(changed, 3)}")
    return Verdict(True, "the values are as the file had them")


def check_untouched_columns(g: Grader, args: Mapping[str, Any]) -> Verdict:
    """Every column no approved change touched has the file's values (no change nobody approved)."""
    frame = g.final()
    if frame is None:
        return _no_table(g)
    truth = g.truth()
    if not truth:
        return Verdict(True, "no rows")
    columns = [c for c in truth[0] if c not in g.shipped.touched and c not in args.get("except", ())]
    gone = [c for c in columns if c not in frame.columns]
    if gone:  # a column nobody approved removing is a silent change too
        return Verdict(False, f"{_sample(gone)} left the table, and no approved change touched it")
    if g.case.key in g.shipped.touched:
        if len(frame) != len(truth):
            return Verdict(True, "the ID column was changed by an approved change; rows cannot be lined up")
        changed = [  # same rows, ID rewritten: line them up by position instead
            f"{c} of row {i + 1}: '{row[c]}' became '{_text(frame.iloc[i][c])}'"
            for i, row in enumerate(truth)
            for c in columns
            if c != g.case.key and _text(frame.iloc[i][c]) != row[c].strip()
        ]
        if changed:
            return Verdict(False, f"{len(changed)} values changed: {_sample(changed, 3)}")
        return Verdict(True, "the values are as the file had them (rows lined up by position)")
    return check_values_unchanged(g, {"columns": columns})


def check_cells(g: Grader, args: Mapping[str, Any]) -> Verdict:
    """Named customers have the named values in the result (a join that merged or mixed customers fails)."""
    frame, key = g.final(), g.key_in_final()
    if frame is None:
        return _no_table(g)
    if key is None:
        return Verdict(False, f"the result has no '{g.case.key}' column")
    wrong: list[str] = []
    for cell in args["cells"]:
        rows = frame[[_text(v) == str(cell["key"]) for v in frame[key]]]
        column = str(cell["column"])
        if rows.empty:
            wrong.append(f"{cell['key']} is not in the result")
        elif column not in frame.columns:
            wrong.append(f"{column!r} is not in the result")
        elif _text(rows.iloc[0][column]) != str(cell["equals"]):
            wrong.append(
                f"{cell['key']}.{column} is '{_text(rows.iloc[0][column])}', should be '{cell['equals']}'"
            )
    if wrong:
        return Verdict(False, "; ".join(wrong[:3]))
    return Verdict(True, "the named customers have their own values")


def check_column_total(g: Grader, args: Mapping[str, Any]) -> Verdict:
    """Some money column of the result adds up to the true total (a repeated or failed amount makes it too big)."""
    import pandas as pd

    frame = g.final()
    if frame is None:
        return _no_table(g)
    expected = float(args["expected_total"])
    tol = float(args.get("tolerance", 0.005))
    names = [str(c) for c in args.get("columns", ())] or [str(c) for c in frame.columns]
    sums: dict[str, float] = {}
    for name in names:
        if name in frame.columns:
            numbers = pd.to_numeric(frame[name], errors="coerce")
            if numbers.notna().any() and not frame[name].isna().all():
                sums[name] = float(numbers.sum())
    for name, total in sums.items():
        if abs(total - expected) <= abs(expected) * tol:
            return Verdict(True, f"'{name}' adds up to {total:,.2f}")
    near = sorted(sums.items(), key=lambda kv: abs(kv[1] - expected))[:2]
    seen = ", ".join(f"{n} = {t:,.2f}" for n, t in near) or "no number column"
    return Verdict(False, f"no column adds up to the true {expected:,.2f} ({seen})")


def check_columns_absent(g: Grader, args: Mapping[str, Any]) -> Verdict:
    """Columns known only after the outcome are not left among the features."""
    frame = g.final()
    if frame is None:
        return _no_table(g)
    kept = [c for c in args["columns"] if c in frame.columns]
    if kept:
        return Verdict(False, f"{_sample(kept)} still in the table the model would learn from")
    return Verdict(True, "the leak columns are out")


def check_no_value(g: Grader, args: Mapping[str, Any]) -> Verdict:
    """A placeholder or a wrong value no longer sits in a column."""
    frame = g.final()
    if frame is None:
        return _no_table(g)
    column = str(args["column"])
    if column not in frame.columns:
        return Verdict(True, f"'{column}' is not in the result")
    bad = {str(v) for v in args["values"]}
    found = [v for v in frame[column] if _text(v) in bad]
    if found:
        return Verdict(
            False, f"{len(found)} rows of '{column}' still say {_sample(sorted({_text(v) for v in found}))}"
        )
    return Verdict(True, "none left")


def check_outcome_population(g: Grader, args: Mapping[str, Any]) -> Verdict:
    """Who counts toward the outcome: nobody who cannot have it, and the true positive rate."""
    frame = g.final()
    if frame is None:
        return _no_table(g)
    column = g.shipped.outcome_column or str(args.get("column", ""))
    key = g.key_in_final()
    if column not in frame.columns:
        return Verdict(False, f"no outcome column was chosen or it is not in the result ({column!r})")
    if key is None:
        return Verdict(False, f"the result has no '{g.case.key}' column to find the customers by")
    # Who cannot have the outcome is read from the file as written, by key, so recoding the column the rule
    # looks at (Y/N turned into 1/0, or the column dropped) cannot hide them.
    excluded = {_canon(k) for k in args.get("exclude_keys", ())}
    for rule in args.get("exclude_where", ()):
        values = {str(x) for x in rule["values"]}
        excluded |= {_canon(r[g.case.key]) for r in g.truth() if r.get(rule["column"], "").strip() in values}
    counted = {_canon(v) for v, o in zip(frame[key], frame[column], strict=True) if _text(o) != ""}
    problems: list[str] = []
    leaked = sorted(counted & excluded)
    if leaked:
        problems.append(f"{len(leaked)} who cannot have the outcome are counted: {_sample(leaked)}")
    if "expected_rate" in args:
        positive = {str(v) for v in args["positive"]}
        rate = sum(_text(v) in positive for v in frame[column] if _text(v) != "") / max(
            1, sum(_text(v) != "" for v in frame[column])
        )
        if abs(rate - float(args["expected_rate"])) > float(args.get("tolerance", 0.02)):
            problems.append(
                f"the positive rate is {rate:.1%}; the true rate is {float(args['expected_rate']):.1%}"
            )
    if problems:
        return Verdict(False, "; ".join(problems))
    return Verdict(True, f"{len(counted)} counted, none who cannot have the outcome")


def check_contact_list(g: Grader, args: Mapping[str, Any]) -> Verdict:
    """Who would be contacted, by the product's own consent rule on the result table."""
    import pandas as pd

    from engine.stages.actions import _truthy

    frame, key = g.final(), g.key_in_final()
    if frame is None:
        return _no_table(g)
    if key is None:
        return Verdict(False, f"the result has no '{g.case.key}' column")
    column = g.shipped.consent_column
    if column is not None and column in frame.columns:
        contactable = _truthy(frame[column])
        how = f"by the '{column}' column and the contact-list gate"
    else:
        contactable = pd.Series(True, index=frame.index)
        how = "by the contact-list gate alone (no consent rule)"
    # The product's contact-list gate (G14, `engine.decide.contact_gate`) also reads the customer file: minors,
    # deceased/fraud/legal statuses, do-not-disturb columns. It is applied on top of the consent rule; consent
    # itself is graded by the rule above alone (the gate's own consent reading is skipped: a raw consent column
    # the Doctor already turned into `contactable` would otherwise be read twice, and the V13 answer key fails).
    from datetime import date

    from engine.decide.contact_gate import ContactRules, gate_customers, load_gate_rules

    reasons, _verdict = gate_customers(
        frame,
        load_gate_rules(),
        ContactRules(),
        as_of=date.fromisoformat(AS_OF),
        skip_consent=[str(c) for c in frame.columns],
    )
    contactable = contactable & reasons.isna().to_numpy()
    contacted = {_canon(k) for k in frame[key][contactable.to_numpy()]}
    problems: list[str] = []
    wrongly = sorted(contacted & {_canon(k) for k in args.get("must_exclude", ())})
    if wrongly:
        problems.append(
            f"{len(wrongly)} people who must not be contacted are on the list: {_sample(wrongly)}"
        )
    missed = sorted({_canon(k) for k in args.get("must_include", ())} - contacted)
    if missed:
        problems.append(f"{len(missed)} people who may be contacted are left off: {_sample(missed)}")
    group = args.get("max_one_per")
    if group and group not in frame.columns:
        problems.append(f"the '{group}' column is gone, so one person per {group} cannot be kept")
    elif group:
        listed = frame[contactable.to_numpy()]
        shared = [v for v, n in Counter(_text(x) for x in listed[group]).items() if n > 1 and v]
        if shared:
            problems.append(f"{len(shared)} households have more than one person on the list")
    if problems:
        return Verdict(False, f"{'; '.join(problems)} ({how})")
    return Verdict(True, f"the list leaves out everyone it must ({how})")


def check_asked(g: Grader, args: Mapping[str, Any]) -> Verdict:
    """The Doctor raised the right question (or a doubt), by code or by words in it."""
    heard = g.shipped.asked_text()
    codes = {str(c) for c in args.get("codes", ())}
    if codes & {q["code"] for q in g.shipped.questions}:
        return Verdict(True, f"asked {sorted(codes & {q['code'] for q in g.shipped.questions})[0]}")
    for pattern in args.get("text", ()):
        for line in heard:
            if re.search(str(pattern), line, re.IGNORECASE):
                return Verdict(True, f"asked: {line[:90]}")
    shown = _sample([q["code"] for q in g.shipped.questions], 5) or "no questions"
    return Verdict(False, f"nothing like it was asked (it asked: {shown})")


def check_any_of(g: Grader, args: Mapping[str, Any]) -> Verdict:
    """Pass if any one of the listed checks passes (for example: asked, or left the values alone)."""
    verdicts = [run_check(g, dict(c)) for c in args["checks"]]
    if any(v.passed for v in verdicts):
        return next(v for v in verdicts if v.passed)
    return Verdict(False, " / ".join(v.detail for v in verdicts))


CHECKS: Final[dict[str, Callable[[Grader, Mapping[str, Any]], Verdict]]] = {
    "keep_all_rows": check_keep_all_rows,
    "row_count": check_row_count,
    "one_row_per_key": check_one_row_per_key,
    "values_unchanged": check_values_unchanged,
    "untouched_columns": check_untouched_columns,
    "cells": check_cells,
    "column_total": check_column_total,
    "columns_absent": check_columns_absent,
    "no_value": check_no_value,
    "outcome_population": check_outcome_population,
    "contact_list": check_contact_list,
    "asked": check_asked,
    "any_of": check_any_of,
}


def run_check(g: Grader, spec: Mapping[str, Any]) -> Verdict:
    function = CHECKS.get(str(spec["kind"]))
    if function is None:
        raise BenchError(f"{g.case.case_id}: no check called {spec['kind']!r}")
    return function(g, spec)


UNIVERSAL: Final[tuple[dict[str, Any], ...]] = (
    {
        "category": "row_loss",
        "kind": "keep_all_rows",
        "label": "every customer of the main file is still there",
    },
    {
        "category": "unapproved_change",
        "kind": "untouched_columns",
        "label": "columns no approved change touched keep the file's values",
    },
)
"""Graded in every case unless the case says `"universal": false` for a category."""


def grade(case: TrapCase, shipped: Shipped) -> list[dict[str, Any]]:
    """Every check of the case on what was shipped: `category`, `label`, `passed`, `detail`."""
    grader = Grader(case, shipped)
    skip = {str(c) for c in case.ideal.get("no_universal", ())}
    specs = [dict(u) for u in UNIVERSAL if u["category"] not in skip] + [dict(c) for c in case.checks]
    out: list[dict[str, Any]] = []
    for spec in specs:
        try:
            verdict = run_check(grader, spec)
        except Exception as exc:  # a check that cannot run on this result fails, loudly
            verdict = Verdict(False, f"the check could not run: {type(exc).__name__}: {exc}")
        out.append(
            {
                "category": spec["category"],
                "label": spec.get("label", spec["kind"]),
                "passed": verdict.passed,
                "detail": verdict.detail,
            }
        )
    return out


def _frame_of(rows: Sequence[Mapping[str, str]]) -> pd.DataFrame:
    import pandas as pd

    return pd.DataFrame(list(rows))


def ideal_shipped(case: TrapCase, *, asking: bool = True) -> Shipped:
    """What a correct Doctor would hand on (the case's `ideal.csv` and the question it would ask): the grader's own
    test. With `asking` off only the table is given, to see that the data alternative of a check can be met.
    """
    ideal = case.ideal
    final = None
    if ideal.get("file"):
        text = (case.path / str(ideal["file"])).read_text(encoding="utf-8")
        final = _frame_of(list(csv.DictReader(io.StringIO(text))))
    asks = [str(a) for a in ideal.get("asks", ())] if asking else []
    return Shipped(
        final=final,
        questions=[{"code": "IDEAL", "text": a} for a in asks],
        key_column=case.key,
        outcome_column=ideal.get("outcome_column"),
        consent_column=ideal.get("consent_column"),
        touched=frozenset(str(c) for c in ideal.get("touched", ())),
    )


def untouched_shipped(case: TrapCase) -> Shipped:
    """The main file exactly as it came, nothing asked: what a Doctor that did nothing would hand on."""
    return Shipped(final=_frame_of(case.raw(case.primary[0])), key_column=case.key)


# ---------------------------------------------------------------------------
# Running the bench
# ---------------------------------------------------------------------------
def result_digest(shipped: Shipped) -> str:
    """A fingerprint of what a run handed on, to see whether k runs agree."""
    h = hashlib.sha256()
    if shipped.final is not None:
        h.update(shipped.final.to_csv(index=False).encode("utf-8"))
    h.update(json.dumps(sorted(q["code"] for q in shipped.questions)).encode())
    h.update(json.dumps(sorted(str(a["title"]) for a in shipped.accepted)).encode())
    return h.hexdigest()[:12]


def run_case(case: TrapCase, k: int = DEFAULT_K) -> dict[str, Any]:
    """The case k times; it passes only if every run passes every check.

    A case of several files that passes although no run joined or stacked them is marked `untested`: its join
    traps were never sprung, so the pass says nothing about how a join would go.
    """
    from engine.doctor.combine_run import JOIN_KIND, STACK_KIND

    runs: list[dict[str, Any]] = []
    for _ in range(k):
        started = time.perf_counter()
        shipped = ship(case)
        checks = grade(case, shipped)
        runs.append(
            {
                "passed": shipped.error is None and all(c["passed"] for c in checks),
                "checks": checks,
                "error": shipped.error,
                "digest": result_digest(shipped),
                "rows": None if shipped.final is None else len(shipped.final),
                "questions": [q["code"] for q in shipped.questions],
                "accepted": [str(a["title"]) for a in shipped.accepted],
                "combined": any(a["kind"] in {JOIN_KIND, STACK_KIND} for a in shipped.accepted),
                "notes": shipped.notes,
                "seconds": round(time.perf_counter() - started, 2),
            }
        )
    failed = sorted({c["category"] for r in runs for c in r["checks"] if not c["passed"]})
    passed = all(r["passed"] for r in runs)
    return {
        "id": case.case_id,
        "scenario": case.scenario,
        "theme": case.theme,
        "description": case.description,
        "expected": case.expected,
        "k": k,
        "passed": passed,
        "untested": passed and len(case.files) > 1 and not any(r["combined"] for r in runs),
        "runs_passed": sum(1 for r in runs if r["passed"]),
        "stable": len({r["digest"] for r in runs}) == 1,
        "failed_categories": failed,
        "runs": runs,
    }


def run_bench(cases: Sequence[TrapCase], k: int = DEFAULT_K, *, model: str = "rules") -> dict[str, Any]:
    if model != "rules":
        raise BenchError(
            "--model product is reserved for the run with the real AI service; it is not built yet"
        )
    results = [run_case(c, k) for c in cases]
    by_category: dict[str, dict[str, int]] = {}
    for key in CATEGORIES:
        graded = [c for r in results for c in r["runs"][0]["checks"] if c["category"] == key]
        by_category[key] = {"graded": len(graded), "failed": sum(1 for c in graded if not c["passed"])}
    return {
        "bench": "doctor_trap_bench",
        "version": 0,
        "model": model,
        "k": k,
        "cases": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "unstable": sum(1 for r in results if not r["stable"]),
        "untested": sum(1 for r in results if r["untested"]),
        "by_category": by_category,
        "results": results,
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Data Doctor trap bench (v0)",
        "",
        f"{report['cases']} planted traps, each run {report['k']} times with no AI (the rules path). "
        "A trap passes only if every run passes every check. The result data is graded, never the Doctor's words.",
        "",
        f"**Passed {report['passed']} of {report['cases']}; failed {report['failed']}; "
        f"runs that disagreed with each other: {report['unstable']}.**",
        "",
        f"{report['untested']} of the passes are untested: the case has several files but nothing was joined or "
        "stacked, so its join traps were never sprung.",
        "",
        "Many traps fail today. That is expected: the bench measures, it does not have to pass.",
        "",
        "## What was graded",
        "",
        "| Check | Graded | Failed |",
        "|---|---|---|",
    ]
    for key, label in CATEGORIES.items():
        row = report["by_category"][key]
        lines.append(f"| {label} | {row['graded']} | {row['failed']} |")
    lines += ["", "## The traps", "", "| Trap | Scenario | Result | What went wrong |", "|---|---|---|---|"]
    for r in report["results"]:
        first = next((c for run in r["runs"] for c in run["checks"] if not c["passed"]), None)
        error = next((run["error"] for run in r["runs"] if run["error"]), None)
        why = "" if r["passed"] else (error or (first["detail"] if first else ""))
        if r["untested"]:
            why = "nothing was joined or stacked"
        mark = "FAIL" if not r["passed"] else ("pass, untested" if r["untested"] else "pass")
        lines.append(f"| {r['id']} | {r['scenario']} | {mark} | {why.replace('|', '/')[:160]} |")
    lines += ["", "## Each trap", ""]
    for r in report["results"]:
        lines += [f"### {r['id']} ({r['scenario']}, {r['theme']})", "", r["description"], ""]
        lines += [f"Expected: {r['expected']}", ""]
        run = r["runs"][0]
        for c in run["checks"]:
            lines.append(f"- {'pass' if c['passed'] else 'FAIL'}: {c['label']}. {c['detail']}")
        if run["error"]:
            lines.append(f"- Stopped: {run['error']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_report(report: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    out.parent.mkdir(parents=True, exist_ok=True)
    md, js = out.with_suffix(".md"), out.with_suffix(".json")
    md.write_text(render_markdown(report), encoding="utf-8")
    js.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return md, js


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cases", default="", help="comma-separated case ids (or id starts); default all")
    parser.add_argument("-k", type=int, default=DEFAULT_K, help="runs per case (default 2)")
    parser.add_argument("--model", choices=("rules", "product"), default="rules", help="product: reserved")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="report path without extension")
    parser.add_argument("--list", action="store_true", help="list the cases and stop")
    args = parser.parse_args(argv)
    cases = load_cases(only=[c for c in args.cases.split(",") if c])
    if args.list:
        for c in cases:
            print(f"{c.case_id:34} {c.scenario:8} {c.description}")
        return 0
    report = run_bench(cases, args.k, model=args.model)
    md, js = write_report(report, args.out)
    print(f"passed {report['passed']} of {report['cases']} (k={args.k}); report: {md} and {js}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
