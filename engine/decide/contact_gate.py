"""The contact-list gate: who is taken off the list before it is sent (G14).

The treat list is the file a campaign actually contacts, so the rules about who must not be contacted live in our
code on that path, not in a prompt. `engine.stages.actions` already takes off the customer whose configured consent
column is not a yes (a blank is not a yes). This gate adds what the scoring run cannot know from its configuration,
read from the customer file the run scored:

* **Unknown is not a yes.** A blank or unclear do-not-disturb or consent cell takes the customer off the list, unless
  the run's `contact_rules.json` says a blank means OK. "DND-Partial" counts as do not disturb unless it says otherwise.
* **Minors.** Age on the run's date is under 18 (from an age column, or a date of birth). An age that cannot be read
  is flagged, not guessed.
* **Statuses.** Deceased, fraud, legal, closed and the like (a list in `configs/decide/contact_gate.yaml`, extended by
  the rules; case and spacing do not matter).
* **Test accounts.** An ID or e-mail that looks like a test, QA or demo account (patterns in the same file).
* **One person per household.** When the file has a household column, the best-ranked member stays and the others
  are counted as "same household".
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
    "household_column",
    "load_gate_rules",
    "same_household",
    "write_contact_rules",
    "write_suppression_list",
]

_LOGGER = get_logger(__name__)

CONTACT_RULES_FILENAME: Final[str] = "contact_rules.json"
SUPPRESSION_LIST_FILENAME: Final[str] = "suppression_list.parquet"
GATE_FILENAME: Final[str] = "decide/contact_gate.yaml"
_DEFAULT_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "configs" / GATE_FILENAME

REASON_TEXT: Final[dict[str, str]] = {
    "blocked_status": "their status says deceased, fraud, closed or a legal matter",
    "test_account": "they look like test accounts",
    "minor": "they are under 18",
    "dnd": "they are on the do-not-disturb list",
    "dnd_unknown": "their do-not-disturb answer is blank or unclear",
    "no_consent": "they did not agree to be contacted",
    "consent_unknown": "their consent is blank or unclear",
    "suppression_list": "they are on your suppression list",
    "same_household": "someone else in their household is already on the list",
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
    household_column: str | None = Field(
        default=None,
        description="The customer column that names the household; null = a column named like the yaml's list.",
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
    test_account_columns: frozenset[str]
    test_account_pattern: re.Pattern[str] | None
    household_columns: frozenset[str]
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

    patterns = [str(p) for p in doc.get("test_account_patterns") or ()]

    return GateRules(
        minimum_age=int(doc.get("minimum_age", 18)),
        age_columns=names("age_columns"),
        birth_date_columns=names("birth_date_columns"),
        status_columns=names("status_columns"),
        dnd_columns=names("dnd_columns"),
        consent_columns=names("consent_columns"),
        blocked_statuses=_set(doc.get("blocked_statuses") or ()),
        test_account_columns=names("test_account_columns"),
        test_account_pattern=(
            re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE) if patterns else None
        ),
        household_columns=names("household_columns"),
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


def _find(frame: pd.DataFrame, listed: frozenset[str]) -> str | None:
    """The first column whose whole name is in `listed`."""
    return next((str(c) for c in frame.columns if _norm(c) in listed), None)


def _named(frame: pd.DataFrame, listed: frozenset[str]) -> list[tuple[str, bool]]:
    """Every column whose name is, or holds as whole words, a name in `listed` (`kyc_status`, `dnd_flag`), and
    whether the whole name matched."""
    found: list[tuple[str, bool]] = []
    for column in frame.columns:
        name = _norm(column)
        if name in listed:
            found.append((str(column), True))
        elif any(f"_{word}_" in f"_{name}_" for word in listed):
            found.append((str(column), False))
    return found


def _answer_columns(
    frame: pd.DataFrame, listed: frozenset[str], known: frozenset[str], what: str, skip: set[str]
) -> tuple[list[str], list[str]]:
    """The yes/no columns a rule reads, and notes on the columns it could not read.

    A column named exactly (`dnd`) is always read: an answer it does not know is "unclear", never a yes. A column
    whose name only holds the word (`dnd_flag`, `consent_date`) is read when most of its answers are ones the rule
    knows; otherwise the list says it was not used, unless it holds dates.
    """
    used: list[str] = []
    notes: list[str] = []
    for column, exact in _named(frame, listed):
        if _norm(column) in skip:
            continue
        if exact:
            used.append(column)
            continue
        cells = frame[column].head(1_000).map(_cell)
        cells = cells[cells != ""]
        if len(cells) and float(cells.isin(known).mean()) >= 0.5:
            used.append(column)
        elif (
            len(cells)
            and float(
                pd.to_datetime(frame[column].head(1_000), errors="coerce", format="mixed").notna().mean()
            )
            < 0.5
        ):
            notes.append(
                f"'{column}' looks like a {what} column but its answers were not recognised, so it was not used."
            )
    return used, notes


def _is_blank(value: Any) -> bool:
    return bool(value is None or (isinstance(value, float) and np.isnan(value)) or value is pd.NA)


def _looks_like_test(values: pd.Series[Any], rules: GateRules) -> Any:
    """True for each cell that matches a test-account pattern (`TEST001`, `qa+x@company.example`)."""
    pattern = rules.test_account_pattern
    if pattern is None:
        return np.zeros(len(values.index), dtype=bool)
    text = values.map(lambda v: "" if _is_blank(v) else str(v).strip())
    return text.map(lambda t: bool(pattern.search(t))).to_numpy(dtype=bool)


def household_column(source: pd.DataFrame, rules: GateRules, overrides: ContactRules) -> str | None:
    """The column naming each customer's household: the run's own, else the first one named like the yaml's list.

    A column that never repeats (the customer's own key) is no household, so `account_id` counts only when accounts
    are shared between customers."""
    if overrides.household_column in source.columns:
        return overrides.household_column
    for column in source.columns:
        values = source[column].dropna()
        if _norm(column) in rules.household_columns and values.nunique() < len(values.index):
            return str(column)
    return None


_NO_HOUSEHOLD: Final[frozenset[str]] = frozenset(
    {"", "na", "n a", "nan", "null", "none", "nil", "unknown", "0"}
)
"""Household cells that name no household (as `_cell` reads them): their customers are never grouped."""


def same_household(households: pd.Series[Any], staying: Any, score: pd.Series[Any] | None = None) -> Any:
    """Per customer: True when the customer is taken off because someone better ranked in the household stays.

    `staying` marks who is still on the list; the highest `score` of each household stays (file order breaks ties
    and ranks when there is no score). A customer with no household is never taken off."""
    n = len(households.index)
    ranked = pd.Series(np.zeros(n) if score is None else pd.to_numeric(score, errors="coerce").to_numpy())
    table = pd.DataFrame({"home": households.map(_cell).to_numpy(), "score": ranked.to_numpy()})
    table = table[np.asarray(staying, dtype=bool) & ~table["home"].isin(_NO_HOUSEHOLD)]
    best = table.sort_values("score", ascending=False, kind="stable", na_position="last")
    out = np.zeros(n, dtype=bool)
    out[best.index[best["home"].duplicated()]] = True
    return out


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
    statuses = [column for column, _ in _named(source, rules.status_columns)]
    for status in statuses:  # every status column: `kyc_status` must not hide `account_status`
        mark(source[status].map(_cell).isin(blocked), "blocked_status")
    if statuses:
        verdict.columns["blocked_status"] = ", ".join(statuses)

    # The columns that held a test account are named on the list; a file with none says nothing.
    tests = {
        str(name): _looks_like_test(source[name], rules)
        for name in source.columns
        if _norm(name) in rules.test_account_columns  # whole names only: `ab_test_id` holds no account
    }
    if key is not None:  # the key may be a column the file does not hold as such
        tests["the customer ID"] = _looks_like_test(key.reset_index(drop=True), rules)
    for hit in tests.values():
        mark(hit, "test_account")
    if any(hit.any() for hit in tests.values()):
        verdict.columns["test_account"] = ", ".join(name for name, hit in tests.items() if hit.any())

    ages, age_column = _ages(source, rules, as_of)
    if ages is not None and age_column is not None:
        verdict.columns["minor"] = age_column
        mark(ages < rules.minimum_age, "minor")
        unknown = int(ages.isna().sum())
        if unknown:
            verdict.flagged["age_unknown"] = unknown

    dnd_known = rules.dnd_yes | rules.dnd_no | rules.dnd_partial
    dnds, notes = _answer_columns(source, rules.dnd_columns, dnd_known, "do-not-disturb", set())
    verdict.notes.extend(notes)
    for dnd in dnds:  # on do-not-disturb in any column is on do-not-disturb
        cells = source[dnd].map(_cell)
        partial = cells.isin(rules.dnd_partial)
        mark(cells.isin(rules.dnd_yes) | (partial & (not overrides.partial_dnd_means_ok)), "dnd")
        unclear = ~cells.isin(dnd_known)
        if overrides.blank_dnd_means_ok:
            unclear = unclear & (cells != "")
        mark(unclear, "dnd_unknown")
    if dnds:
        verdict.columns["dnd"] = ", ".join(dnds)
        if overrides.dnd_scrubbed_on is not None:
            age_days = (as_of - overrides.dnd_scrubbed_on).days
            if age_days > rules.dnd_scrub_max_days:
                verdict.notes.append(
                    f"The do-not-disturb list was last checked {age_days} days ago "
                    f"(more than {rules.dnd_scrub_max_days}); get a fresh one before sending."
                )

    skipped = {_norm(c) for c in skip_consent}
    consents, notes = _answer_columns(
        source, rules.consent_columns, rules.consent_yes | rules.consent_no, "consent", skipped
    )
    verdict.notes.extend(notes)
    consent = next(iter(consents), None)  # consent may differ by channel: the first column found is read
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
    real_keys: Mapping[str, str] | None = None,
    scores: pd.Series[Any] | None = None,
) -> Verdict:
    """The gate's verdict for the scored customers (`row_keys`), read from the customer file `source`.

    `handled_columns` are the consent and opt-in columns the scoring run already applies (the use case's own), which
    are not read twice. `source_keys` are the file's own join keys (one per source row). A customer with several rows in the file is taken
    off when any row says so. The run's date (when it was created) is the age date unless `as_of` is given.
    `real_keys` maps a hashed working key to the real one (the upload's key vault), for the suppression list only.
    `scores` are the scored customers' ranking values (aligned to `row_keys`): the best of each household stays.
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
        # A hashed working key (a phone, a PAN) is matched to the suppression list by its real value.
        key=source_keys.reset_index(drop=True).map(lambda k: real_keys.get(k, k) if real_keys else k),
    )
    ranks = reasons.map(lambda r: _ORDER.index(r) if r is not None else len(_ORDER))
    best = ranks.groupby(source_keys.reset_index(drop=True).to_numpy()).min()
    aligned = best.reindex(pd.Index(row_keys)).fillna(len(_ORDER)).astype(int).to_numpy()
    verdict.reason = np.array([_ORDER[r] if r < len(_ORDER) else None for r in aligned], dtype=object)
    home = household_column(source, rules, overrides)
    if overrides.household_column and home != overrides.household_column:
        verdict.notes.append(
            f"The household column '{overrides.household_column}' is not in the customer file, so "
            + (f"'{home}' was used instead." if home else "households were not checked.")
        )
    if home is not None:  # after the other reasons, so the person kept is one who may be contacted
        by_customer = (
            source[home].reset_index(drop=True).groupby(source_keys.reset_index(drop=True).to_numpy()).first()
        )
        households = pd.Series(pd.Index(row_keys).map(by_customer))
        extra = same_household(households, pd.isna(verdict.reason), scores)
        if bool(extra.any()):
            verdict.reason[extra] = "same_household"
            verdict.columns["same_household"] = home
            if scores is None:
                verdict.notes.append(
                    f"The run has no ranking column, so the first customer listed in each '{home}' was kept."
                )
    return verdict
