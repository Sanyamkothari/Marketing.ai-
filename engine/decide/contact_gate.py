"""The contact-list gate: who is taken off the list before it is sent (G14).

The treat list is the file a campaign actually contacts, so the rules about who must not be contacted live in our
code on that path, not in a prompt. `engine.stages.actions` already takes off the customer whose configured consent
column is not a yes (a blank is not a yes). This gate adds what the scoring run cannot know from its configuration,
read from the customer file the run scored:

* **Unknown is not a yes.** A blank or unclear do-not-disturb or consent cell takes the customer off the list, unless
  the run's `contact_rules.json` says a blank means OK. "DND-Partial" counts as do not disturb unless it says otherwise.
* **Minors.** Age on the run's date is under 18 (from an age column, or a date of birth). An age that cannot be read
  is flagged, not guessed.
* **Statuses.** Deceased, fraud and legal (a list in `configs/decide/contact_gate.yaml`, extended by the rules).
* **Suppression list.** A file of keys (`suppression_list.parquet`, a `key` column) is matched against the customers
  by cleaned key, and the share of the list found among the customers is reported.

Each customer is counted under the first reason that applies, and the counts are written into the list's summary
(`contact_exclusions`, `contact_exclusion_lines`), which the contact-list page prints as "excluded: N because ...".
Everything here is deterministic and reads no cell into a message: the sentences hold counts and reasons only.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pandas as pd
import yaml
from pydantic import Field

from engine.contracts import Artefact
from engine.privacy.working_key import clean_contact_key
from engine.storage import Storage, StorageError, run_key
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from engine.contracts import RunRecord

__all__ = [
    "CONTACT_RULES_FILENAME",
    "REASON_TEXT",
    "SUPPRESSION_LIST_FILENAME",
    "ContactRules",
    "GateRules",
    "Verdict",
    "clean_contact_key",
    "contact_gate_for_run",
    "exclusion_lines",
    "gate_customers",
    "load_gate_rules",
    "write_contact_rules",
    "write_suppression_list",
]

_LOGGER = get_logger(__name__)

CONTACT_RULES_FILENAME: Final[str] = "contact_rules.json"
SUPPRESSION_LIST_FILENAME: Final[str] = "suppression_list.parquet"
GATE_FILENAME: Final[str] = "decide/contact_gate.yaml"
_DEFAULT_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "configs" / GATE_FILENAME

REASON_TEXT: Final[dict[str, str]] = {
    "blocked_status": "their status says deceased, fraud or a legal matter",
    "minor": "they are under 18",
    "dnd": "they are on the do-not-disturb list",
    "dnd_unknown": "their do-not-disturb answer is blank or unclear",
    "no_consent": "they did not agree to be contacted",
    "consent_unknown": "their consent is blank or unclear",
    "suppression_list": "they are on your suppression list",
}
"""The reasons in the order they are applied (a customer is counted under the first), with the words for each."""
_ORDER: Final[tuple[str, ...]] = tuple(REASON_TEXT)

_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")
_CAMEL: Final[re.Pattern[str]] = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class ContactRules(Artefact):
    """`contact_rules.json`: what the person decided about blanks, a stale scrub and extra statuses for this run."""

    blank_dnd_means_ok: bool = Field(
        default=False, description="A blank do-not-disturb cell is not a reason to exclude."
    )
    blank_consent_means_ok: bool = Field(
        default=False, description="A blank consent cell is not a reason to exclude."
    )
    partial_dnd_means_ok: bool = Field(default=False, description="'DND-Partial' is not a reason to exclude.")
    extra_blocked_statuses: tuple[str, ...] = Field(
        default=(), description="Statuses to exclude, besides the list."
    )
    suppression_column: str | None = Field(
        default=None,
        description="The customer column the suppression list's keys are matched to; null = the key.",
    )
    dnd_scrubbed_on: date | None = Field(
        default=None, description="When the do-not-disturb list was last checked."
    )


@dataclass(frozen=True)
class GateRules:
    """`configs/decide/contact_gate.yaml`, read."""

    minimum_age: int
    age_columns: frozenset[str]
    birth_date_columns: frozenset[str]
    status_columns: frozenset[str]
    dnd_columns: frozenset[str]
    consent_columns: frozenset[str]
    blocked_statuses: frozenset[str]
    dnd_yes: frozenset[str]
    dnd_no: frozenset[str]
    dnd_partial: frozenset[str]
    consent_yes: frozenset[str]
    consent_no: frozenset[str]
    dnd_scrub_max_days: int


@dataclass
class Verdict:
    """What the gate decided for the scored customers, aligned to their rows."""

    reason: np.ndarray[Any, Any]
    """Per scored row: the reason code the customer is taken off for, or None."""
    flagged: dict[str, int] = field(default_factory=dict)
    """Customers kept but worth a look (an age that cannot be read), counted by what is wrong."""
    notes: list[str] = field(default_factory=list)
    suppression_listed: int | None = None
    suppression_matched: int | None = None
    columns: dict[str, str] = field(default_factory=dict)
    """Which customer column each rule read (reason code to column name)."""


def _norm(text: str) -> str:
    return "_".join(_WORD.findall(_CAMEL.sub(" ", str(text)).lower()))


def _set(values: Iterable[Any]) -> frozenset[str]:
    return frozenset(" ".join(_WORD.findall(str(v).lower())) for v in values)


@lru_cache(maxsize=8)
def _load(path: str) -> GateRules:
    doc: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    def names(key: str) -> frozenset[str]:
        return frozenset(_norm(v) for v in doc.get(key) or ())

    return GateRules(
        minimum_age=int(doc.get("minimum_age", 18)),
        age_columns=names("age_columns"),
        birth_date_columns=names("birth_date_columns"),
        status_columns=names("status_columns"),
        dnd_columns=names("dnd_columns"),
        consent_columns=names("consent_columns"),
        blocked_statuses=_set(doc.get("blocked_statuses") or ()),
        dnd_yes=_set(doc.get("dnd_yes") or ()),
        dnd_no=_set(doc.get("dnd_no") or ()),
        dnd_partial=_set(doc.get("dnd_partial") or ()),
        consent_yes=_set(doc.get("consent_yes") or ()),
        consent_no=_set(doc.get("consent_no") or ()),
        dnd_scrub_max_days=int(doc.get("dnd_scrub_max_days", 30)),
    )


def load_gate_rules(config_root: Path | None = None) -> GateRules:
    """The gate's lists: the config root's `decide/contact_gate.yaml` when it has one, else the shipped file."""
    path = (config_root / GATE_FILENAME) if config_root is not None else _DEFAULT_PATH
    return _load(str(path if path.is_file() else _DEFAULT_PATH))


def _cell(value: Any) -> str:
    """A cell as a lower-case phrase: blank is the empty string, 1.0 is "1"."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(_WORD.findall(str(value).lower()))


def _find(frame: pd.DataFrame, listed: frozenset[str], *, tokens: bool = False) -> str | None:
    for column in frame.columns:
        name = _norm(column)
        if name in listed or (tokens and set(name.split("_")) & listed):
            return str(column)
    return None


def _ages(frame: pd.DataFrame, rules: GateRules, as_of: date) -> tuple[pd.Series[Any] | None, str | None]:
    """Age in years on `as_of` per row (NaN when it cannot be read), and the column it came from."""
    column = _find(frame, rules.age_columns)
    if column is not None:
        return pd.to_numeric(frame[column], errors="coerce"), column
    column = _find(frame, rules.birth_date_columns)
    if column is None:
        return None, None
    born = pd.to_datetime(frame[column], errors="coerce")
    later_in_year = (born.dt.month > as_of.month) | (
        (born.dt.month == as_of.month) & (born.dt.day > as_of.day)
    )
    years = as_of.year - born.dt.year - later_in_year.astype(int)
    return years.where(born.notna() & (born <= pd.Timestamp(as_of))), column


def gate_customers(
    source: pd.DataFrame,
    rules: GateRules,
    overrides: ContactRules,
    *,
    as_of: date,
    skip_consent: Iterable[str] = (),
    suppression_keys: Sequence[Any] | None = None,
    key: pd.Series[Any] | None = None,
) -> tuple[pd.Series[Any], Verdict]:
    """The reason each row of `source` is taken off the list (None when it stays), and what the gate noted.

    `skip_consent` names consent columns another rule already applies (the use case's own), so they are not read twice.
    `suppression_keys` are matched against `overrides.suppression_column`, else `key`.
    """
    n = len(source.index)
    rank = np.full(n, len(_ORDER), dtype=int)  # the index of the reason; len(_ORDER) = none
    verdict = Verdict(reason=np.full(n, None, dtype=object))

    def mark(mask: Any, reason: str) -> None:
        hit = np.asarray(mask, dtype=bool)
        rank[hit] = np.minimum(rank[hit], _ORDER.index(reason))

    blocked = rules.blocked_statuses | _set(overrides.extra_blocked_statuses)
    status = _find(source, rules.status_columns, tokens=True)
    if status is not None:
        verdict.columns["blocked_status"] = status
        mark(source[status].map(_cell).isin(blocked), "blocked_status")

    ages, age_column = _ages(source, rules, as_of)
    if ages is not None and age_column is not None:
        verdict.columns["minor"] = age_column
        mark(ages < rules.minimum_age, "minor")
        unknown = int(ages.isna().sum())
        if unknown:
            verdict.flagged["age_unknown"] = unknown

    dnd = _find(source, rules.dnd_columns)
    if dnd is not None:
        verdict.columns["dnd"] = dnd
        cells = source[dnd].map(_cell)
        partial = cells.isin(rules.dnd_partial)
        clear = cells.isin(rules.dnd_no)
        mark(cells.isin(rules.dnd_yes) | (partial & (not overrides.partial_dnd_means_ok)), "dnd")
        unclear = ~(cells.isin(rules.dnd_yes) | clear | partial)
        if overrides.blank_dnd_means_ok:
            unclear = unclear & (cells != "")
        mark(unclear, "dnd_unknown")
        if overrides.dnd_scrubbed_on is not None:
            age_days = (as_of - overrides.dnd_scrubbed_on).days
            if age_days > rules.dnd_scrub_max_days:
                verdict.notes.append(
                    f"The do-not-disturb list was last checked {age_days} days ago "
                    f"(more than {rules.dnd_scrub_max_days}); get a fresh one before sending."
                )

    skipped = {_norm(c) for c in skip_consent}
    consent = next(
        (str(c) for c in source.columns if _norm(c) in rules.consent_columns and _norm(c) not in skipped),
        None,
    )
    if consent is not None:
        verdict.columns["consent"] = consent
        cells = source[consent].map(_cell)
        yes = cells.isin(rules.consent_yes)
        mark(cells.isin(rules.consent_no), "no_consent")
        unclear = ~(yes | cells.isin(rules.consent_no))
        if overrides.blank_consent_means_ok:
            unclear = unclear & (cells != "")
        mark(unclear, "consent_unknown")

    if suppression_keys is not None:
        listed = {k for k in (clean_contact_key(v) for v in suppression_keys) if k is not None}
        column = overrides.suppression_column
        if column is not None and column in source.columns:
            ours = source[column]
        elif key is not None:
            ours = key
        else:
            ours = None
        if ours is not None and listed:
            cleaned = ours.map(clean_contact_key)
            mark(cleaned.isin(listed), "suppression_list")
            verdict.suppression_listed = len(listed)
            verdict.suppression_matched = len(listed & set(cleaned.dropna()))
            verdict.columns["suppression_list"] = column or "the customer ID"
    reasons = pd.Series(
        [_ORDER[r] if r < len(_ORDER) else None for r in rank], index=source.index, dtype=object
    )
    return reasons, verdict


def exclusion_lines(counts: Mapping[str, int], verdict: Verdict | None = None) -> list[str]:
    """Plain sentences for the list page: `12 excluded because they are under 18`, one for each reason."""
    lines = [
        f"{counts[reason]:,} excluded because {REASON_TEXT[reason]}"
        for reason in _ORDER
        if counts.get(reason, 0) > 0
    ]
    if verdict is not None:
        if verdict.suppression_listed:
            rate = (verdict.suppression_matched or 0) / verdict.suppression_listed
            lines.append(
                f"{verdict.suppression_matched or 0:,} of {verdict.suppression_listed:,} on your suppression list "
                f"were found among these customers ({rate:.0%})"
            )
        unknown_age = verdict.flagged.get("age_unknown", 0)
        if unknown_age:
            lines.append(
                f"{unknown_age:,} have an age that could not be read, so they were not checked for being under 18"
            )
        lines.extend(verdict.notes)
    return lines


def write_contact_rules(storage: Storage, run_id: str, rules: ContactRules) -> None:
    """Save what the person decided about blanks and statuses for this run."""
    storage.write_model(run_key(run_id, CONTACT_RULES_FILENAME), rules)


def write_suppression_list(storage: Storage, run_id: str, keys: Iterable[Any]) -> None:
    """Save the run's suppression list: a `key` column of phones, e-mails or IDs, as the client sent them."""
    frame = pd.DataFrame({"key": [str(k) for k in keys]})
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    storage.write_bytes(run_key(run_id, SUPPRESSION_LIST_FILENAME), buffer.getvalue())


def _read_rules(storage: Storage, run_id: str) -> ContactRules:
    try:
        return storage.read_model(run_key(run_id, CONTACT_RULES_FILENAME), ContactRules)
    except (StorageError, ValueError):
        return ContactRules()


def _read_suppression(storage: Storage, run_id: str) -> list[str] | None:
    try:
        data = storage.read_bytes(run_key(run_id, SUPPRESSION_LIST_FILENAME))
        frame = pd.read_parquet(io.BytesIO(data))
    except (StorageError, ValueError, OSError):
        return None
    return [str(v) for v in frame["key"].tolist()] if "key" in frame.columns else None


def contact_gate_for_run(
    storage: Storage,
    record: RunRecord,
    source: pd.DataFrame,
    row_keys: pd.Series[Any],
    source_keys: pd.Series[Any],
    *,
    handled_columns: Iterable[str | None] = (),
    as_of: date | datetime | None = None,
    config_root: Path | None = None,
) -> Verdict:
    """The gate's verdict for the scored customers (`row_keys`), read from the customer file `source`.

    `handled_columns` are the consent and opt-in columns the scoring run already applies (the use case's own), which
    are not read twice. `source_keys` are the file's own join keys (one per source row). A customer with several rows in the file is taken
    off when any row says so. The run's date (when it was created) is the age date unless `as_of` is given.
    """
    rules = load_gate_rules(config_root)
    overrides = _read_rules(storage, record.run_id)
    day = as_of or record.created_at
    day = day.date() if isinstance(day, datetime) else day
    suppression = _read_suppression(storage, record.run_id)
    reasons, verdict = gate_customers(
        source.reset_index(drop=True),
        rules,
        overrides,
        as_of=day,
        skip_consent=[c for c in handled_columns if c],
        suppression_keys=suppression,
        key=source_keys.reset_index(drop=True),
    )
    ranks = reasons.map(lambda r: _ORDER.index(r) if r is not None else len(_ORDER))
    best = ranks.groupby(source_keys.reset_index(drop=True).to_numpy()).min()
    aligned = best.reindex(pd.Index(row_keys)).fillna(len(_ORDER)).astype(int).to_numpy()
    verdict.reason = np.array([_ORDER[r] if r < len(_ORDER) else None for r in aligned], dtype=object)
    return verdict
