"""The outcome from events, shown back as a sentence (Plan L M157, DEC-1570 ... DEC-1574).

Most clients have no "churned" column, but they have a log of what customers did: recharges, orders,
logins. The outcome is then a sentence - *a customer has churned when there is no recharge for 60 days
after the snapshot* - and this module offers that sentence, with its choices, for each use case on Home
whose outcome no file names (M151's `missing`).

Three rules hold the offer honest.

* **The sentences are configuration, not code.** `configs/outcomes.yaml` says, per use case, the plain
  sentence with its blanks, which kind of onboarding `LabelSpec` it builds and the choices (30, 60 or 90
  days). Only `event_absence` and `event_presence` are offered: they need nothing but an ID and a date,
  so every number below can be measured. Each choice is a real `LabelSpec`, validated by its own model.
* **Every count is measured, by onboarding's own label builder.** For each choice the event file is read
  the way the builder reads it (`cast_series` for the dates; keys compared as text, as `join_coverage`
  does), month-end snapshots near the end of the data are planned by `plan_snapshots` for every
  customer in the list, and `engine.onboarding.labels.build_labels` labels them. The figure shown is its
  `SnapshotStat` for the latest month-end whose window is over: how many customers in the list, and how
  many of them are "yes". Nothing here estimates.
* **Censoring is the builder's, and it is said in words.** A month-end whose window runs past the last
  date in the event file has no outcome yet; `build_labels` marks it censored, and those customers are
  counted neither as "yes" nor as "no" - the offer says how many such month-ends were left out and why.

Nothing is built from an offer. `POST /doctor/outcome` records the person's explicit choice of one
sentence and returns its `LabelSpec`; the recipe that builds the dataset is the onboarding builder's
(`docs/DATA_DOCTOR.md`, "The outcome from your events", lists the calls).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from engine.config import ConfigError, LabelType, StrictBase
from engine.onboarding.specs import LabelSpec
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    import pandas as pd

    from engine.agent.key_steps import KeyColumn, KeyRule
    from engine.config import RoleCatalogue, UseCaseConfig
    from engine.doctor.contracts import FileDiagnosis, UseCaseReadiness

__all__ = [
    "OUTCOMES_FILENAME",
    "AudienceOption",
    "AudienceSetting",
    "OutcomeAgreement",
    "OutcomeCatalogue",
    "OutcomeChoice",
    "OutcomeFile",
    "OutcomeOffer",
    "OutcomeRequest",
    "OutcomeTemplate",
    "TemplateBuild",
    "TemplateSettings",
    "WindowSetting",
    "attach_outcome_offers",
    "event_word",
    "load_outcome_catalogue",
    "next_steps",
    "offer_outcome",
    "sentence_for",
]

_LOG = get_logger(__name__)

OUTCOMES_FILENAME: Final[str] = "outcomes.yaml"

LOOK_BACK_SLACK_DAYS: Final[int] = 62
"""Month-ends measured: those within a window plus this many days of the data's end. Any 62 days hold
at least one month-end, so the latest month-end whose window is over is always among them when the data
is long enough, and every later (censored) month-end is too."""

MAX_JOIN_COLUMNS: Final[int] = 40
"""Columns of an event file tried as its customer ID, at most."""


# ---------------------------------------------------------------------------
# configs/outcomes.yaml
# ---------------------------------------------------------------------------
class WindowSetting(BaseModel):
    """The days after the snapshot that the outcome looks at: the windows allowed, the one proposed first, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    allowed: tuple[Annotated[int, Field(ge=1, le=3650)], ...] = Field(
        min_length=1, max_length=6, description="The windows offered, in days."
    )
    default: int = Field(description="The window proposed first; one of `allowed`.")
    ask: bool = Field(
        default=False,
        description="True: the person is asked before anything is built. False: the default is proposed with its "
        "reason and the person may change it in the chat.",
    )
    why: str = Field(default="", description="Why the default fits the goal, in a sentence.")

    @field_validator("allowed")
    @classmethod
    def _sorted_once(cls, days: tuple[int, ...]) -> tuple[int, ...]:
        if len(set(days)) != len(days):
            raise ValueError("a window is listed twice")
        return tuple(sorted(days))

    def model_post_init(self, _context: Any) -> None:
        if self.default not in self.allowed:
            raise ValueError(f"the default window {self.default} is not one of {list(self.allowed)}")


class AudienceOption(BaseModel):
    """One reading of who the built table is about: every customer active on or before the snapshot (`active`), or
    only those who have gone quiet (`quiet`: no row in the `quiet_days` days before it)."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: Literal["active", "quiet"]
    label: str = Field(
        min_length=1,
        description='Who they are, in plain words; blanks {entities} ("shoppers") and {quiet_days}.',
    )
    description: str = Field(default="", description="What choosing it means for the table, in a sentence.")
    quiet_days: int | None = Field(
        default=None, ge=1, le=3650, description="For `quiet`: days before the snapshot with no row."
    )

    def model_post_init(self, _context: Any) -> None:
        if (self.id == "quiet") != (self.quiet_days is not None):
            raise ValueError("`quiet_days` belongs to the `quiet` audience, and to it alone")
        try:
            self.label.format(entities="customers", quiet_days=self.quiet_days)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"the audience label has an unknown blank: {exc}") from exc

    def phrase(self, entities: str) -> str:
        """The label with `entities` filled in ("shoppers", or "1,204 shoppers" once counted)."""
        return self.label.format(entities=entities, quiet_days=self.quiet_days)


class AudienceSetting(BaseModel):
    """Who the built table is about, when the goal names a narrower audience than every customer known by the
    snapshot. A build counts both readings; the goal says which one it is for and the person decides."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    ask: bool = Field(default=True, description="The person is asked before anything is built.")
    options: tuple[AudienceOption, ...] = Field(description="Both readings: `active` and `quiet`.")
    recommended: Literal["active", "quiet"] | None = Field(
        default=None,
        description="The reading the goal's own meaning points at, stated here and never read from text. It is "
        "offered only when the facts show it reaches the minimums.",
    )
    because: str = Field(default="", description="Why `recommended` fits the goal, in a sentence.")
    dont_know: Literal["active", "quiet"] | None = Field(
        default=None,
        description='The reading "I don\'t know" takes; when it is too small, the widest one that is large enough.',
    )

    def model_post_init(self, _context: Any) -> None:
        ids = [option.id for option in self.options]
        if sorted(ids) != ["active", "quiet"]:
            raise ValueError("the audience offers exactly two readings, `active` and `quiet`")
        for name in ("recommended", "dont_know"):
            if getattr(self, name) is not None and getattr(self, name) not in ids:
                raise ValueError(f"{name} is not one of the audience options")
        if self.recommended is not None and not self.because:
            raise ValueError("a recommended audience needs its `because`")

    def option(self, option_id: str) -> AudienceOption | None:
        return next((o for o in self.options if o.id == option_id), None)


class TemplateSettings(BaseModel):
    """What the person may set for a goal's outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    window_days: WindowSetting
    audience: AudienceSetting | None = Field(
        default=None, description="Set when the goal names a narrower audience; a build then asks which one."
    )


class TemplateBuild(BaseModel):
    """How the outcome is built from events."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    recipe: Literal["rfm", "events"] = Field(
        default="rfm",
        description="`rfm`: recency, frequency and amount measures when the file has an amount; `events`: counts of "
        "events only.",
    )
    outcome_column: str | None = Field(
        default=None,
        min_length=1,
        description="The built outcome column, named for every window (`reactivated`, never `reactivated_90d`); "
        "null: the use case's own target column.",
    )


_LEGACY_QUIET_LABEL: Final[str] = (
    "{entities} who have gone quiet (nothing in the {quiet_days} days before the cut-off date)"
)
_LEGACY_ACTIVE_LABEL: Final[str] = "{entities} active on or before the cut-off date"


class OutcomeTemplate(BaseModel):
    """One use case's outcome sentence, the `LabelSpec` kind it builds, and the person's choices.

    `days`, `default_days` and `audience` are the older spelling of `settings`: a template that gives them is read
    into `settings` (`window_days` and `audience`), so it needs no edit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    question: str = Field(min_length=1, description="The question the sentence answers, in plain words.")
    sentence: str = Field(min_length=1, description="The sentence with its blanks ({days}, {event}, ...).")
    label_type: Literal["event_absence", "event_presence"] = Field(
        description="The onboarding `LabelSpec` kind the sentence builds; only kinds measured on an ID and a date."
    )
    event_kinds: tuple[str, ...] = Field(
        min_length=1, description="What the event file may be (onboarding roles), best first."
    )
    settings: TemplateSettings = Field(description="What the person may set: the window and the audience.")
    snapshot_rule: Literal["latest_month_end_with_window_over"] = Field(
        default="latest_month_end_with_window_over", description="Where the snapshot is put."
    )
    build: TemplateBuild = Field(default_factory=TemplateBuild, description="How the outcome is built.")

    @model_validator(mode="before")
    @classmethod
    def _read_older_spelling(cls, data: Any) -> Any:
        if not isinstance(data, Mapping) or not {"days", "default_days", "audience"} & set(data):
            return data
        if "settings" in data:
            raise ValueError(
                "`settings` replaces `days`, `default_days` and `audience`: give one or the other"
            )
        rest = {k: v for k, v in data.items() if k not in {"days", "default_days", "audience"}}
        settings: dict[str, Any] = {
            "window_days": {"allowed": data.get("days", ()), "default": data.get("default_days")}
        }
        older = data.get("audience")
        if older is not None:
            quiet = (
                older.get("quiet_days") if isinstance(older, Mapping) else getattr(older, "quiet_days", None)
            )
            label = older.get("label") if isinstance(older, Mapping) else getattr(older, "label", None)
            settings["audience"] = {
                "options": [
                    {"id": "active", "label": _LEGACY_ACTIVE_LABEL},
                    {"id": "quiet", "label": label or _LEGACY_QUIET_LABEL, "quiet_days": quiet},
                ]
            }
        return {**rest, "settings": settings}

    @property
    def days(self) -> tuple[int, ...]:
        return self.settings.window_days.allowed

    @property
    def default_days(self) -> int:
        return self.settings.window_days.default

    @property
    def audience(self) -> AudienceSetting | None:
        return self.settings.audience

    def model_post_init(self, _context: Any) -> None:
        try:
            sentence_for(self, entity="customer", event="event", days=self.days[0])
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"the sentence has an unknown blank: {exc}") from exc


class OutcomeCatalogue(BaseModel):
    """`configs/outcomes.yaml`."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: int = Field(default=1, description="Version of this file's layout.")
    event_words: tuple[str, ...] = Field(
        default=(), description="Words in an event file's name that name what happened."
    )
    kind_words: dict[str, str] = Field(
        default_factory=dict, description="What happened, in plain words, by what the file was read as."
    )
    templates: dict[str, OutcomeTemplate] = Field(
        default_factory=dict, description="Per use case id, its outcome sentence."
    )

    @field_validator("event_words")
    @classmethod
    def _lowered(cls, words: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(word.strip().lower() for word in words if word.strip()))


_CACHE: dict[Path, tuple[float, OutcomeCatalogue]] = {}
_LOCK: Final[threading.Lock] = threading.Lock()


def load_outcome_catalogue(root: Path) -> OutcomeCatalogue:
    """`<root>/outcomes.yaml`, or an empty catalogue (no sentence offered) when the directory has none.

    Cached per file and modification time, like `configs/intake.yaml`. A malformed file is a
    `ValueError` naming the file, never a silent fallback.
    """
    path = Path(root) / OUTCOMES_FILENAME
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return OutcomeCatalogue()
    with _LOCK:
        cached = _CACHE.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        catalogue = OutcomeCatalogue.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"{OUTCOMES_FILENAME} is not valid: {exc}") from exc
    with _LOCK:
        _CACHE[path] = (mtime, catalogue)
    return catalogue


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------
class OutcomeChoice(StrictBase):
    """One way to finish the sentence, and what it would mean on the files, measured."""

    days: int = Field(description="The window, in days after the snapshot.")
    sentence: str = Field(description="The whole sentence for this choice, as the person reads it.")
    default: bool = Field(default=False, description="True for the choice shown first.")
    label_spec: LabelSpec = Field(description="The onboarding outcome this sentence builds.")
    measured_on: date | None = Field(
        description="The latest month-end whose window is over: the date the counts are for; null when none is."
    )
    customers: int | None = Field(description="Customers in the list at `measured_on`.")
    yes: int | None = Field(
        description="Of them, how many the sentence makes 'yes', counted by the label builder."
    )
    no: int | None = Field(description="Of them, how many it makes 'no'.")
    yes_rate: float | None = Field(description="`yes` / `customers`, rounded to 4 places.")
    not_over: int = Field(
        description="Later month-ends left out because their window runs past the last date in the file."
    )
    summary: str = Field(description="The counts in one plain sentence.")


class OutcomeOffer(StrictBase):
    """The outcome Data Doctor can build from an event file, as a sentence to agree (or not)."""

    use_case_id: str
    question: str = Field(description="What the sentence answers: 'When has a customer churned?'")
    label_type: str = Field(description="`event_absence` or `event_presence`.")
    entity_upload_id: str = Field(description="The customer list.")
    entity_file_name: str
    entity_id_column: str = Field(description="The column naming each customer in the list.")
    customers: int = Field(description="Distinct customers in the list, counted.")
    event_upload_id: str = Field(description="The event file the outcome is read from.")
    event_file_name: str
    event_id_column: str = Field(description="The event file's column that names the customer.")
    date_column: str = Field(description="The event file's date column the window is read on.")
    event_role: str = Field(
        description="What the event file is (an onboarding role); the `LabelSpec`'s role."
    )
    event_word: str = Field(description="What happened, in the sentence's words ('recharge').")
    data_start: date = Field(description="The first readable date in `date_column`.")
    data_end: date = Field(
        description="The last readable date in `date_column`: where every window must end."
    )
    matched_customers: int = Field(
        description="Customers in the list with at least one row in the event file."
    )
    event_rows_matched: float = Field(
        description="Share of the event file's rows whose customer is in the list (`join_coverage`)."
    )
    unreadable_dates: int = Field(
        description="Event rows whose date could not be read; they count for nothing."
    )
    censoring: str = Field(description="Why some customers are not counted yet, in plain words.")
    choices: tuple[OutcomeChoice, ...] = Field(
        description="The ways to finish the sentence, shortest window first."
    )
    notes: tuple[str, ...] = Field(
        default=(), description="Anything else the person should know about the counts."
    )


class OutcomeRequest(StrictBase):
    """Body of `POST /doctor/outcome`: the one sentence the person picked, and their yes."""

    use_case: str = Field(min_length=1, max_length=64)
    entity_upload_id: str = Field(min_length=1, max_length=64, description="The customer list.")
    event_upload_id: str = Field(min_length=1, max_length=64, description="The event file.")
    date_column: str = Field(min_length=1, max_length=256, description="The event file's date column.")
    days: int = Field(ge=1, le=3650, description="The window the person picked.")
    sentence: str = Field(
        min_length=1, max_length=500, description="The sentence exactly as it was shown for that choice."
    )
    agreed: bool = Field(description="The person's explicit yes; false is refused.")


class OutcomeAgreement(StrictBase):
    """Body of the `201` from `POST /doctor/outcome`, and the record kept beside the customer list."""

    agreement_id: str
    use_case_id: str
    sentence: str = Field(description="The sentence the person agreed to.")
    label_spec: LabelSpec = Field(description="The outcome it builds: the recipe's `label_spec`.")
    choice: OutcomeChoice = Field(description="The choice as measured when the person agreed.")
    entity_upload_id: str
    entity_id_column: str
    event_upload_id: str
    event_id_column: str
    date_column: str
    event_role: str
    data_end: date
    agreed_at: AwareDatetime
    next_steps: tuple[str, ...] = Field(description="What the screen does next to build the dataset.")


# ---------------------------------------------------------------------------
# The sentence
# ---------------------------------------------------------------------------
def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def sentence_for(template: OutcomeTemplate, *, entity: str, event: str, days: int) -> str:
    """The template's sentence with its blanks filled; only the words given are ever placed in it."""
    return template.sentence.format(
        An_entity=f"{_article(entity).capitalize()} {entity}",
        entity=entity,
        an_event=f"{_article(event)} {event}",
        event=event,
        days=days,
    )


_NAME_WORDS: Final[re.Pattern[str]] = re.compile(r"[a-z]+(?:-[a-z]+)?")


def event_word(file_name: str, kind: str, catalogue: OutcomeCatalogue) -> str:
    """What happened, for the sentence: a listed word in the file's name, else the word for its kind."""
    stem = Path(file_name).stem.lower()
    listed = set(catalogue.event_words)
    for token in map(str, _NAME_WORDS.findall(stem)):
        singular = (
            token[:-1] if len(token) > 3 and token.endswith("s") and not token.endswith("ss") else token
        )
        for word in (token, singular):
            if word in listed:
                return word
    return catalogue.kind_words.get(kind, "event")


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OutcomeFile:
    """One diagnosed file and the rows the diagnosis read."""

    diagnosis: FileDiagnosis
    frame: pd.DataFrame


@dataclass(frozen=True)
class _Events:
    file: OutcomeFile
    kind: str
    id_column: str
    date_column: str
    table: pd.DataFrame  # entity_key (text), event_time (timestamp), readable rows only
    unreadable: int
    coverage: float
    # Plan O M237 (DEC-1807): set only when the IDs meet the list once written one way (`normalise_key`), not
    # as written; the table's keys and the list's are then compared as the two rules write them.
    rules: KeyRulePair | None = None


@dataclass(frozen=True)
class KeyRulePair:
    """The list's rule and the event file's, under which their IDs meet (`propose_key_rules`)."""

    entity: KeyRule
    event: KeyRule


def _key_text(series: pd.Series[Any]) -> pd.Series[Any]:
    """Keys as text, the way `join_coverage` compares them; whole-number floats lose their `.0`."""
    import pandas as pd

    values = series.dropna()
    if pd.api.types.is_float_dtype(values) and bool((values % 1 == 0).all()):
        values = values.astype("int64")
    return values.astype(str).str.strip()


def _event_table(
    frame: pd.DataFrame, id_column: str, date_column: str, rules: KeyRulePair | None = None
) -> tuple[pd.DataFrame, int]:
    """`(entity_key, event_time)` read as the builder reads them; and how many dates could not be read. With
    `rules`, the keys are the event file's IDs written by its rule (Plan O M237)."""
    import pandas as pd

    from engine.config import StandardType
    from engine.onboarding.transforms import cast_series

    times = cast_series(frame[date_column], StandardType.DATE).values
    keys = frame[id_column]
    if rules is not None:
        from engine.agent.key_steps import KeyColumn

        keys = pd.Series(KeyColumn(keys).rows_of(rules.event), index=frame.index, dtype=object)
    table = pd.DataFrame({"entity_key": keys, "event_time": pd.to_datetime(times)})
    unreadable = int((frame[date_column].notna() & table["event_time"].isna()).sum())
    table = table.dropna(subset=["entity_key", "event_time"])
    if rules is None:
        table["entity_key"] = _key_text(table["entity_key"])
    return table.reset_index(drop=True), unreadable


def _key_like(series: pd.Series[Any]) -> bool:
    """Text or whole numbers: what an ID can be. Yes/no, dates and fractional numbers are not."""
    kind = series.dtype.kind
    if kind in "bcmM":
        return False
    if kind == "f":
        values = series.dropna()
        return bool((values % 1 == 0).all())
    return True


def _join_column(
    events: OutcomeFile, entity_keys: pd.Series[Any], entity_id: str
) -> tuple[str, float] | None:
    """The event file's column that names the list's customers: the best `join_coverage`, above zero."""
    from engine.onboarding.sources import join_coverage

    diagnosis = events.diagnosis
    dates = set(diagnosis.date_columns)
    best: tuple[tuple[float, int, int], str, float] | None = None
    for name in list(events.frame.columns)[:MAX_JOIN_COLUMNS]:
        column = str(name)
        series = events.frame[name]
        if column in dates or not _key_like(series):
            continue
        coverage = join_coverage(_key_text(series), entity_keys)
        if coverage <= 0:
            continue
        rank = (
            round(coverage, 2),
            int(column.casefold() == entity_id.casefold()),
            int(column == diagnosis.id_column),
        )
        if best is None or rank > best[0]:
            best = (rank, column, coverage)
    return None if best is None else (best[1], best[2])


def _ruled_join_column(
    events: OutcomeFile, entity_ids: KeyColumn, entity_id: str
) -> tuple[str, float, KeyRulePair] | None:
    """Plan O M237 (DEC-1807): when no column meets the list as written, the column whose IDs meet it once
    each side is written one way (`propose_key_rules`: a prefix, zeros, case, spaces, +91 phones; never a near
    match), at onboarding's own bar (`KEY_JOIN_OK`). None when none does."""
    from engine.agent.key_steps import KeyColumn, propose_key_rules

    diagnosis = events.diagnosis
    dates = set(diagnosis.date_columns)
    best: tuple[tuple[float, int, int], str, float, KeyRulePair] | None = None
    for name in list(events.frame.columns)[:MAX_JOIN_COLUMNS]:
        column = str(name)
        series = events.frame[name]
        if column in dates or not _key_like(series):
            continue
        found = propose_key_rules(KeyColumn(series), entity_ids)
        if not found.meets:
            continue
        rank = (
            round(found.fixed_coverage, 2),
            int(column.casefold() == entity_id.casefold()),
            int(column == diagnosis.id_column),
        )
        if best is None or rank > best[0]:
            best = (rank, column, found.fixed_coverage, KeyRulePair(entity=found.base_rule, event=found.rule))
    return None if best is None else (best[1], best[2], best[3])


@dataclass(frozen=True)
class _Measured:
    measured_on: date | None
    customers: int | None
    yes: int | None
    not_over: int


def _measure(
    entity_keys: pd.Series[Any], events: pd.DataFrame, spec: LabelSpec, *, role: str, inclusive: bool
) -> _Measured:
    """`build_labels` on month-end snapshots near the data's end; the latest one whose window is over."""
    import duckdb
    import pandas as pd

    from engine.onboarding.labels import build_labels
    from engine.onboarding.snapshots import plan_snapshots
    from engine.onboarding.specs import SnapshotFrequency, SnapshotMode, SnapshotSpec

    window = spec.window_days or 0
    first = pd.Timestamp(events["event_time"].min()).date()
    end = pd.Timestamp(events["event_time"].max()).date()
    start = max(first, end - timedelta(days=window + LOOK_BACK_SLACK_DAYS))
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
        return _Measured(None, None, None, 0)
    # `build_snapshot_frame(entities, plan.dates, signup_column=None)` without its warning: the offer
    # counts every customer in the list at each date, which is what a list with no signup date means.
    entities = pd.DataFrame({"entity_key": entity_keys.drop_duplicates().reset_index(drop=True)})
    spine = pd.DataFrame({"snapshot_date": pd.to_datetime(list(plan.dates))})
    snapshots = entities.merge(spine, how="cross").reset_index(drop=True)
    con = duckdb.connect()
    try:
        con.register(role, events)
        result = build_labels(
            con,
            spec,
            snapshots,
            drop_censored=True,
            mapped_roles={role},
            mode=SnapshotMode.PERIODIC,
            inclusive=inclusive,
        )
    finally:
        con.close()
    complete = [stat for stat in result.per_snapshot if not stat.censored and stat.positives is not None]
    not_over = sum(1 for stat in result.per_snapshot if stat.censored)
    if not complete:
        return _Measured(None, None, None, not_over)
    latest = complete[-1]
    return _Measured(latest.date, latest.entities, latest.positives, not_over)


# ---------------------------------------------------------------------------
# The offer
# ---------------------------------------------------------------------------
def _label_name(config: UseCaseConfig) -> str | None:
    """The column the use case learns from (`target.column`), which the dataset's outcome is named after."""
    pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    for name in (config.target.column, config.label.name if config.label is not None else None):
        if name and pattern.match(name):
            return name
    return None


def _entity_file(files: Sequence[OutcomeFile], roles: RoleCatalogue) -> OutcomeFile | None:
    lists = [
        file
        for file in files
        if file.diagnosis.kind == roles.entity_role and file.diagnosis.id_column is not None
    ]
    if not lists:
        return None
    return max(lists, key=lambda file: (-abs((file.diagnosis.rows_per_id or 1.0) - 1.0), file.diagnosis.rows))


def _candidates(
    files: Sequence[OutcomeFile],
    entity: OutcomeFile,
    entity_keys: pd.Series[Any],
    template: OutcomeTemplate,
    roles: RoleCatalogue,
    *,
    event_upload_id: str | None,
    date_column: str | None,
    entity_ids: KeyColumn | None = None,
) -> list[_Events]:
    events: list[_Events] = []
    fallback = "other_event" if "other_event" in roles.roles else None
    for file in files:
        diagnosis = file.diagnosis
        if file is entity or not diagnosis.date_columns:
            continue
        if event_upload_id is not None and diagnosis.upload_id != event_upload_id:
            continue
        kind = diagnosis.kind or fallback
        spec = roles.roles.get(kind) if kind is not None else None
        if kind is None or spec is None or not spec.is_event or kind not in template.event_kinds:
            continue
        when = diagnosis.date_columns[0] if date_column is None else date_column
        if when not in diagnosis.date_columns:
            continue
        joined = _join_column(file, entity_keys, entity.diagnosis.id_column or "")
        rules: KeyRulePair | None = None
        if joined is None and entity_ids is not None:
            ruled = _ruled_join_column(file, entity_ids, entity.diagnosis.id_column or "")
            if ruled is not None:
                joined, rules = (ruled[0], ruled[1]), ruled[2]
        if joined is None:
            continue
        table, unreadable = _event_table(file.frame, joined[0], when, rules)
        if table.empty:
            continue
        events.append(_Events(file, kind, joined[0], when, table, unreadable, joined[1], rules))
    events.sort(key=lambda item: (template.event_kinds.index(item.kind), -item.coverage, -len(item.table)))
    return events


def _summary(measured: _Measured, *, entity: str, days: int, end: date) -> str:
    plural = f"{entity}s"
    if measured.measured_on is None or measured.customers is None or measured.yes is None:
        return (
            f"Not counted: no month-end in the file has {days} days after it that are over "
            f"(the file runs to {end:%d %b %Y})."
        )
    share = 100 * measured.yes / measured.customers if measured.customers else 0.0
    text = (
        f"At {measured.measured_on:%d %b %Y}, the latest month-end whose {days} days are over, "
        f"{measured.yes:,} of {measured.customers:,} {plural} ({share:.1f}%) are 'yes'."
    )
    if measured.not_over:
        months = "month-end" if measured.not_over == 1 else "month-ends"
        text += f" {measured.not_over} later {months} not counted: their {days} days are not over yet."
    return text


def offer_outcome(
    config: UseCaseConfig,
    template: OutcomeTemplate,
    files: Sequence[OutcomeFile],
    *,
    catalogue: OutcomeCatalogue,
    roles: RoleCatalogue,
    event_upload_id: str | None = None,
    date_column: str | None = None,
) -> OutcomeOffer | None:
    """The sentence and its choices for `config`, measured on `files`; None when the files cannot say.

    Needs a customer list (a file read as the entity role, with an ID column) and an event file of one
    of the template's kinds with a readable date column and a column whose values are the list's IDs.
    `event_upload_id` and `date_column` narrow the offer to the ones the person picked.
    """
    name = _label_name(config)
    entity = _entity_file(files, roles)
    if name is None or entity is None or entity.diagnosis.id_column is None:
        return None
    entity_keys = _key_text(entity.frame[entity.diagnosis.id_column])
    from engine.agent.key_steps import KeyColumn

    entity_ids = KeyColumn(entity.frame[entity.diagnosis.id_column])
    candidates = _candidates(
        files,
        entity,
        entity_keys,
        template,
        roles,
        event_upload_id=event_upload_id,
        date_column=date_column,
        entity_ids=entity_ids,
    )
    if not candidates:
        return None
    chosen = candidates[0]
    ruled_note: str | None = None
    if chosen.rules is not None:
        # The list's IDs as its rule writes them, so the counts below compare like with like; the list itself
        # is never rewritten (the builder writes the event file's IDs as the list writes them).
        import pandas as pd

        entity_keys = pd.Series(entity_ids.rows_of(chosen.rules.entity), dtype=object).dropna()
        ruled_note = (
            f"The IDs of {chosen.file.diagnosis.file_name} meet {entity.diagnosis.file_name} once written one way "
            f"({entity.diagnosis.file_name}: {chosen.rules.entity.words()}; {chosen.file.diagnosis.file_name}: "
            f"{chosen.rules.event.words()}); nothing is matched on a near match."
        )
    word = event_word(chosen.file.diagnosis.file_name, chosen.kind, catalogue)
    noun = config.entity or "customer"
    inclusive = config.onboarding.snapshots.inclusive_snapshot_time
    end = chosen.table["event_time"].max().date()
    start = chosen.table["event_time"].min().date()
    choices: list[OutcomeChoice] = []
    for days in template.days:
        sentence = sentence_for(template, entity=noun, event=word, days=days)
        try:
            spec = LabelSpec(
                name=name,
                type=LabelType(template.label_type),
                role=chosen.kind,
                horizon_days=days,
                description=sentence,
            )
        except (ConfigError, ValidationError) as exc:  # a template the label model refuses is not offered
            _LOG.info("doctor.outcome_label_refused use_case=%s error=%s", config.id, exc)
            return None
        measured = _measure(entity_keys, chosen.table, spec, role=chosen.kind, inclusive=inclusive)
        no = None if measured.customers is None or measured.yes is None else measured.customers - measured.yes
        choices.append(
            OutcomeChoice(
                days=days,
                sentence=sentence,
                default=days == template.default_days,
                label_spec=spec,
                measured_on=measured.measured_on,
                customers=measured.customers,
                yes=measured.yes,
                no=no,
                yes_rate=(
                    round(measured.yes / measured.customers, 4)
                    if measured.yes is not None and measured.customers
                    else None
                ),
                not_over=measured.not_over,
                summary=_summary(measured, entity=noun, days=days, end=end),
            )
        )
    customers = int(entity_keys.nunique())
    matched = int(entity_keys.drop_duplicates().isin(set(chosen.table["entity_key"])).sum())
    notes: list[str] = [] if ruled_note is None else [ruled_note]
    for file in (entity, chosen.file):
        if len(file.frame) < file.diagnosis.rows:
            notes.append(
                f"Counted on a random sample of {len(file.frame):,} of {file.diagnosis.rows:,} rows of "
                f"{file.diagnosis.file_name}."
            )
    if chosen.unreadable:
        notes.append(
            f"{chosen.unreadable:,} rows of {chosen.file.diagnosis.file_name} have a {chosen.date_column} "
            "that could not be read as a date; they count for nothing."
        )
    return OutcomeOffer(
        use_case_id=config.id,
        question=template.question,
        label_type=template.label_type,
        entity_upload_id=entity.diagnosis.upload_id,
        entity_file_name=entity.diagnosis.file_name,
        entity_id_column=entity.diagnosis.id_column,
        customers=customers,
        event_upload_id=chosen.file.diagnosis.upload_id,
        event_file_name=chosen.file.diagnosis.file_name,
        event_id_column=chosen.id_column,
        date_column=chosen.date_column,
        event_role=chosen.kind,
        event_word=word,
        data_start=start,
        data_end=end,
        matched_customers=matched,
        event_rows_matched=chosen.coverage,
        unreadable_dates=chosen.unreadable,
        censoring=(
            f"{chosen.file.diagnosis.file_name} runs to {end:%d %b %Y}. A {noun} is counted only at a "
            f"month-end whose whole window is over by then; at a later month-end we cannot know yet, so "
            f"those {noun}s are neither 'yes' nor 'no', and the dataset leaves those dates out."
        ),
        choices=tuple(choices),
        notes=tuple(notes),
    )


def attach_outcome_offers(
    verdicts: Sequence[UseCaseReadiness],
    files: Sequence[OutcomeFile],
    configs: Mapping[str, UseCaseConfig],
    root: Path,
) -> tuple[UseCaseReadiness, ...]:
    """The verdicts, each `missing` one whose outcome no file names given its sentence when one fits."""
    from engine.config import get_roles

    catalogue = load_outcome_catalogue(root)
    roles = get_roles(root)
    updated: list[UseCaseReadiness] = []
    for verdict in verdicts:
        template = catalogue.templates.get(verdict.use_case_id)
        config = configs.get(verdict.use_case_id)
        offer = None
        if verdict.status == "missing" and verdict.upload_id is None and template and config:
            try:
                offer = offer_outcome(config, template, files, catalogue=catalogue, roles=roles)
            except Exception as exc:  # an offer is a courtesy; a file it cannot read is not a 500 for all
                _LOG.info(
                    "doctor.outcome_offer_failed use_case=%s error=%s",
                    verdict.use_case_id,
                    type(exc).__name__,
                )
        updated.append(verdict if offer is None else verdict.model_copy(update={"outcome_offer": offer}))
    return tuple(updated)


def next_steps(agreement: OutcomeAgreement) -> tuple[str, ...]:
    """What the screen does to build the dataset with the agreed outcome, through the onboarding builder."""
    return (
        f"Add the customer list (upload {agreement.entity_upload_id}) to the client as its entity source: "
        "POST /clients/{client_id}/sources with role 'entity'.",
        f"Add the event file (upload {agreement.event_upload_id}) as an event source: "
        f"POST /clients/{{client_id}}/sources with role '{agreement.event_role}'.",
        f"Map both: in the customer list {agreement.entity_id_column} is entity_key; in the event file "
        f"{agreement.event_id_column} is entity_key and {agreement.date_column} is event_time "
        "(POST /clients/{client_id}/mappings/suggest, then PUT /clients/{client_id}/mappings/{mapping_id}).",
        "Save the recipe with this label_spec unchanged: POST /clients/{client_id}/onboarding-specs.",
        "Build it: POST /datasets. Month-ends whose window is not over are left out "
        "(LABEL_HORIZON_CENSORED), as the sentence said.",
    )
