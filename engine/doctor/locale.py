"""The locale profile (Plan O M241, the design §8.1): how a client writes numbers, dates, yes/no, phones and IDs.

A **locale profile** is detected per client, confirmed once, and remembered. Each field records its source:
`default` (the India defaults below), `detected` (the files proved it) or `confirmed` (a person said so).

* `india_defaults` - the profile before anything is read: rupees, a decimal point with Indian grouping, day first
  (still asked when unproven), lakh and crore on, English + Hinglish + Devanagari yes/no, `+91`, PIN codes, PAN,
  Aadhaar, GSTIN, voter ID and UPI ids treated as personal data, Asia/Kolkata, the Indian consent rules, Hinglish
  headers on (O4).
* `detect_locale` (#60, READ, rules) - reads one or more frames and proves what it can: the decimal mark from values
  that read one way only (`engine.agent.formats.decimal_evidence`), Indian or western digit grouping, the day/month
  order (`formats.date_order`), the number words, the yes/no words, the currency tokens of money columns
  (`formats.CURRENCY_TOKENS`; recorded, never converted - that is M242), phone, PIN and ID shapes
  (`engine.doctor.india_ids`) and `+05:30` time stamps. **Counts only**: no cell value is returned.
* `locale_for` - the per-client flow: with no confirmed profile, the detection and **one question** to confirm it;
  with one, the confirmed profile **applied** with a visible notice ("Using the number and date settings you
  confirmed on 3 Sep 2026"), and a question only about a field a new file contradicts.
* `confirm_profile` / `saved_profile` / `forget_profile` - the per-client memory, under `locale_profiles/<client>.json`
  in the deployment's storage; it holds settings and counts, never a cell value.

No currency is converted and no rate is looked up anywhere here (M242). The workspace currency is recorded as the
settings give it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Generic, Literal, TypeVar

import pandas as pd
from pydantic import AwareDatetime, Field

from engine.agent.formats import CURRENCY_TOKENS, date_order, decimal_evidence, parse_numbers
from engine.config import StrictBase
from engine.doctor.india_ids import detect_india_ids
from engine.doctor.indian_values import boolean_word_family
from engine.storage import Storage, StorageError
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

__all__ = [
    "LOCALE_PREFIX",
    "LocaleConflict",
    "LocaleDetection",
    "LocaleOutcome",
    "LocaleProfile",
    "LocaleQuestion",
    "LocaleSetting",
    "confirm_profile",
    "dayfirst_for",
    "detect_locale",
    "forget_profile",
    "india_defaults",
    "locale_for",
    "locale_key",
    "saved_profile",
]

_LOG = get_logger(__name__)

LOCALE_PREFIX: Final[str] = "locale_profiles/"

T = TypeVar("T")
Source = Literal["default", "detected", "confirmed"]
DecimalMark = Literal[".", ","]
Grouping = Literal["indian", "western"]
DateOrder = Literal["day_first", "month_first"]
NumberWords = Literal["lakh_crore", "k_m", "none"]
BooleanWords = Literal["english", "hinglish", "devanagari"]
Postcode = Literal["pin", "zip", "other"]
HeaderWords = Literal["hinglish", "english"]

MIN_SHARE: Final[float] = 0.50
"""A text column speaks about numbers (or dates) only when at least this share of its values are numbers (dates)."""
MAX_DISTINCT: Final[int] = 20_000
"""Distinct values read per column; enough to prove a convention, bounded on a million-row file."""

_INDIAN_GROUPING: Final[re.Pattern[str]] = re.compile(r"(?<![\d,.])[1-9]\d?(?:,\d\d)+,\d{3}(?![\d,])")
"""`1,20,000`, `3,50,000`, `12,34,56,789`: at least one two-digit group before the last three digits."""
_WESTERN_GROUPING: Final[re.Pattern[str]] = re.compile(r"(?<![\d,.])[1-9]\d{0,2}(?:,\d{3}){2,}(?![\d,])")
"""`1,200,000`: two or more three-digit groups. `12,000` reads both ways and proves nothing."""
_NUMBER_WORD: Final[re.Pattern[str]] = re.compile(
    r"(?i)^\s*(?:₹|rs\.?|inr)?\s*[+-]?[\d][\d.,]*\s*(?P<word>l|lac|lacs|lakh|lakhs|cr|crore|crores|k|m)\.?\s*(?:/-)?\s*$"
)
_TRAILING_DASH: Final[re.Pattern[str]] = re.compile(r"\d\s*/-\s*$")
_NUMBERISH: Final[re.Pattern[str]] = re.compile(r"\d")
_IST_OFFSET: Final[re.Pattern[str]] = re.compile(r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*\+05:?30\b")
_OTHER_OFFSET: Final[re.Pattern[str]] = re.compile(
    r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:[+-](?!05:?30)\d{2}:?\d{2})\b"
)
_NUMERIC_DATE: Final[re.Pattern[str]] = re.compile(r"^\s*\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}")
_ZIP_HEADER: Final[re.Pattern[str]] = re.compile(r"(?i)(^|_)(zip|zipcode|zip_code)($|_)")
_ZIP: Final[re.Pattern[str]] = re.compile(r"\d{5}(?:-\d{4})?")
_CURRENCY_CODE: Final[dict[str, str]] = {
    "inr": "INR",
    "rs.": "INR",
    "rs": "INR",
    "₹": "INR",
    "usd": "USD",
    "us$": "USD",
    "$": "USD",
    "eur": "EUR",
    "€": "EUR",
    "gbp": "GBP",
    "£": "GBP",
}
_CURRENCY_RE: Final[re.Pattern[str]] = re.compile(
    "|".join(re.escape(token) for token in CURRENCY_TOKENS), flags=re.IGNORECASE
)


class LocaleSetting(StrictBase, Generic[T]):
    """One field of the profile: its value, where it came from, and the evidence (counts, never values)."""

    value: T
    source: Source = Field(
        description="`default`, `detected` (the files proved it) or `confirmed` (a person)."
    )
    evidence: tuple[str, ...] = Field(default=(), description="Plain-words counts that proved it.")


class LocaleProfile(StrictBase):
    """The design §8.1 profile, each field with its `source`."""

    client_id: str | None = None
    country: LocaleSetting[str]
    workspace_currency: LocaleSetting[str]
    money_units: LocaleSetting[dict[str, str]] = Field(
        description="Money column → the currency its values are written in (`INR`, `USD`, `mixed`); never converted."
    )
    decimal_mark: LocaleSetting[DecimalMark]
    digit_grouping: LocaleSetting[Grouping]
    date_order: LocaleSetting[DateOrder]
    number_words: LocaleSetting[NumberWords]
    boolean_words: LocaleSetting[tuple[BooleanWords, ...]]
    phone_format: LocaleSetting[str]
    postcode: LocaleSetting[Postcode]
    personal_ids: LocaleSetting[tuple[str, ...]]
    time_zone: LocaleSetting[str]
    consent_regime: LocaleSetting[str]
    header_words: LocaleSetting[HeaderWords]
    confirmed_by: str | None = None
    confirmed_at: AwareDatetime | None = None

    def values(self) -> dict[str, Any]:
        """Every setting's value, by field name (the profile's meaning, without sources or evidence)."""
        return {name: getattr(self, name).value for name in SETTINGS}

    @property
    def profile_hash(self) -> str:
        """A hash of the values: equal for two profiles that read files the same way, whoever confirmed them."""
        blob = json.dumps(
            self.values(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=list
        )
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


SETTINGS: Final[tuple[str, ...]] = (
    "country",
    "workspace_currency",
    "money_units",
    "decimal_mark",
    "digit_grouping",
    "date_order",
    "number_words",
    "boolean_words",
    "phone_format",
    "postcode",
    "personal_ids",
    "time_zone",
    "consent_regime",
    "header_words",
)
"""The profile's settings, in the order the design's table lists them."""


class LocaleConflict(StrictBase):
    """A field the files say one thing about and the profile (or another file) another."""

    field: str
    profile_value: Any = Field(description="What the profile holds (default, detected or confirmed).")
    found: Any = Field(description="What the files prove.")
    columns: tuple[str, ...] = Field(default=(), description="The columns that prove it.")


class LocaleDetection(StrictBase):
    """`detect_locale`'s answer: the profile the files prove, what they contradict, what stays unproven."""

    profile: LocaleProfile
    conflicts: tuple[LocaleConflict, ...] = ()
    unproven: tuple[str, ...] = Field(
        default=(), description="Fields the files need but do not prove (a day/month order every date fits)."
    )
    columns_read: int = 0


class LocaleQuestion(StrictBase):
    """The one question that confirms a profile: each field to confirm with its proposed value and source."""

    text: str
    fields: tuple[str, ...]
    proposed: dict[str, Any]
    sources: dict[str, Source]


class LocaleOutcome(StrictBase):
    """What a visit uses: the profile, and either one question or a visible notice that a saved one applies."""

    profile: LocaleProfile
    question: LocaleQuestion | None = None
    notice: str | None = None
    applied: bool = Field(default=False, description="True when a confirmed profile was applied.")
    conflicts: tuple[LocaleConflict, ...] = ()


def _default(value: T) -> LocaleSetting[T]:
    return LocaleSetting[T](value=value, source="default")


def india_defaults(client_id: str | None = None, *, workspace_currency: str = "INR") -> LocaleProfile:
    """The India defaults of the design §8.1, every field `default`."""
    return LocaleProfile(
        client_id=client_id,
        country=_default("IN"),
        workspace_currency=_default(workspace_currency),
        money_units=_default({}),
        decimal_mark=_default("."),
        digit_grouping=_default("indian"),
        date_order=_default("day_first"),
        number_words=_default("lakh_crore"),
        boolean_words=_default(("english", "hinglish", "devanagari")),
        phone_format=_default("+91"),
        postcode=_default("pin"),
        personal_ids=_default(("pan", "aadhaar", "gstin", "epic", "upi")),
        time_zone=_default("Asia/Kolkata"),
        consent_regime=_default("IN"),
        header_words=_default("hinglish"),
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
class _Votes:
    """What the columns of the files prove, field by field: value → the columns that prove it."""

    def __init__(self) -> None:
        self.by_field: dict[str, dict[Any, list[str]]] = {}
        self.unproven: set[str] = set()
        self.money: dict[str, str] = {}
        self.columns = 0

    def add(self, field: str, value: Any, column: str) -> None:
        self.by_field.setdefault(field, {}).setdefault(value, []).append(column)


def _distinct_text(series: pd.Series[Any]) -> pd.Series[Any]:
    present = series.dropna()
    present = present[[isinstance(v, str) for v in present.tolist()]]
    present = present[present.str.strip() != ""]
    return pd.Series(list(dict.fromkeys(present.str.strip()))[:MAX_DISTINCT], dtype=object)


def _read_column(label: str, series: pd.Series[Any], votes: _Votes) -> None:
    cells = _distinct_text(series)
    if cells.empty:
        return
    votes.columns += 1
    with_digits = cells[cells.str.contains(_NUMBERISH)]
    numeric_dates = with_digits[with_digits.str.match(_NUMERIC_DATE)]
    if len(numeric_dates) >= MIN_SHARE * len(cells):
        order = date_order(numeric_dates)
        if order is None:
            votes.unproven.add("date_order")
        else:
            votes.add("date_order", "day_first" if order else "month_first", label)
        return
    if len(with_digits) >= MIN_SHARE * len(cells):
        point, comma = decimal_evidence(with_digits)
        if point != comma:
            votes.add("decimal_mark", "." if point > comma else ",", label)
        indian = int(with_digits.str.contains(_INDIAN_GROUPING).sum())
        western = int(with_digits.str.contains(_WESTERN_GROUPING).sum())
        if indian != western:
            votes.add("digit_grouping", "indian" if indian > western else "western", label)
        words = {m.group("word").casefold() for m in (_NUMBER_WORD.match(t) for t in with_digits) if m}
        if (
            words & {"l", "lac", "lacs", "lakh", "lakhs", "cr", "crore", "crores"}
            or with_digits.str.contains(_TRAILING_DASH).any()
        ):
            votes.add("number_words", "lakh_crore", label)
        elif words:
            votes.add("number_words", "k_m", label)
        # A currency sign speaks only in a column of amounts: `rs` inside `3 hours` is not rupees.
        amounts = parse_numbers(with_digits, indian_scale_words=True).changed >= MIN_SHARE * len(with_digits)
        codes = {_CURRENCY_CODE[m.group(0).casefold()] for t in with_digits for m in _CURRENCY_RE.finditer(t)}
        if codes and amounts:
            votes.money[label] = codes.pop() if len(codes) == 1 else "mixed"
        if with_digits.str.contains(_IST_OFFSET).any():
            votes.add("time_zone", "Asia/Kolkata", label)
        elif with_digits.str.contains(_OTHER_OFFSET).any():
            votes.unproven.add("time_zone")
        return
    families = {boolean_word_family(t) for t in cells} if len(cells) <= 20 else set()
    if families and None not in families:
        for family in sorted(f for f in families if f is not None):
            votes.add("boolean_words", family, label)


def _frames(data: pd.DataFrame | Mapping[str, pd.DataFrame]) -> list[tuple[str, pd.DataFrame]]:
    if isinstance(data, pd.DataFrame):
        return [("", data)]
    return list(data.items())


def detect_locale(
    data: pd.DataFrame | Mapping[str, pd.DataFrame],
    *,
    base: LocaleProfile | None = None,
    client_id: str | None = None,
    workspace_currency: str = "INR",
) -> LocaleDetection:
    """What the files prove about the client's locale, on top of `base` (the saved profile, or the India defaults).

    A field the files prove becomes `detected`, unless `base` holds it `confirmed`: then a different proof is a
    conflict to ask about, never a silent change. Columns that disagree with each other (one file day-first,
    another month-first) are a conflict too, and the field keeps its value.
    """
    profile = base if base is not None else india_defaults(client_id, workspace_currency=workspace_currency)
    votes = _Votes()
    ids: dict[str, list[str]] = {}
    for name, frame in _frames(data):
        for column in frame.columns:
            label = f"{name}:{column}" if name else str(column)
            series = frame[column]
            if pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
                _read_column(label, series, votes)
        for finding in detect_india_ids(frame):
            label = f"{name}:{finding.column}" if name else finding.column
            ids.setdefault(finding.kind, []).append(label)
            if finding.kind == "phone_in":
                votes.add("phone_format", "+91", label)
            elif finding.kind == "pin":
                votes.add("postcode", "pin", label)
        for column in frame.columns:
            label = f"{name}:{column}" if name else str(column)
            if _ZIP_HEADER.search(str(column)) and _zip_column(frame[column]):
                votes.add("postcode", "zip", label)
    updates: dict[str, LocaleSetting[Any]] = {}
    conflicts: list[LocaleConflict] = []
    for field, found in votes.by_field.items():
        current: LocaleSetting[Any] = getattr(profile, field)
        if field == "boolean_words":
            seen = tuple(sorted(found))
            evidence = tuple(
                f"{len(cols)} columns write yes/no in {value}" for value, cols in sorted(found.items())
            )
            if current.source == "confirmed":
                extra = set(seen) - set(current.value)
                if extra:
                    conflicts.append(
                        LocaleConflict(
                            field=field,
                            profile_value=current.value,
                            found=tuple(sorted(extra)),
                            columns=tuple(c for v in sorted(extra) for c in found[v]),
                        )
                    )
                continue
            merged = tuple(dict.fromkeys((*seen, *(v for v in current.value if v not in seen))))
            updates[field] = LocaleSetting[Any](value=merged, source="detected", evidence=evidence)
            continue
        if len(found) > 1:
            conflicts.append(
                LocaleConflict(
                    field=field,
                    profile_value=current.value,
                    found=tuple(sorted(found)),
                    columns=tuple(c for cols in found.values() for c in cols),
                )
            )
            continue
        value, columns = next(iter(found.items()))
        evidence = (f"{len(columns)} column{'s' if len(columns) != 1 else ''} prove it",)
        if current.source == "confirmed":
            if value != current.value:
                conflicts.append(
                    LocaleConflict(
                        field=field, profile_value=current.value, found=value, columns=tuple(columns)
                    )
                )
            continue
        updates[field] = LocaleSetting[Any](value=value, source="detected", evidence=evidence)
    if votes.money:
        current_money: dict[str, str] = dict(profile.money_units.value)
        current_money.update(votes.money)
        source: Source = "confirmed" if profile.money_units.source == "confirmed" else "detected"
        updates["money_units"] = LocaleSetting[Any](
            value=dict(sorted(current_money.items())),
            source=source,
            evidence=(f"{len(votes.money)} columns carry a currency sign or code",),
        )
    found_ids = sorted(k for k in ids if k in {"gstin", "epic"})
    if found_ids and profile.personal_ids.source != "confirmed":
        merged_ids = tuple(dict.fromkeys((*profile.personal_ids.value, *found_ids)))
        updates["personal_ids"] = LocaleSetting[Any](
            value=merged_ids,
            source="detected" if merged_ids != profile.personal_ids.value else profile.personal_ids.source,
            evidence=tuple(f"{len(ids[k])} columns hold {k}" for k in found_ids),
        )
    unproven = sorted(f for f in votes.unproven if f not in votes.by_field)
    detected = profile.model_copy(update=updates)
    return LocaleDetection(
        profile=LocaleProfile.model_validate(detected.model_dump()),
        conflicts=tuple(conflicts),
        unproven=tuple(unproven),
        columns_read=votes.columns,
    )


def _zip_column(series: pd.Series[Any]) -> bool:
    cells = [str(v).strip() for v in series.dropna().head(1000)]
    return bool(cells) and sum(1 for c in cells if _ZIP.fullmatch(c)) >= 0.8 * len(cells)


def dayfirst_for(profile: LocaleProfile, proven: bool | None) -> bool | None:
    """The day/month order a `parse_date` step uses: what the column proves, else a **confirmed** profile's order.

    A default or a detection from other files is not enough on its own: an unproven order is still asked (the
    design §8.1, "day first, still asked when unproven"). None means ask.
    """
    if proven is not None:
        return proven
    if profile.date_order.source == "confirmed":
        return profile.date_order.value == "day_first"
    return None


# ---------------------------------------------------------------------------
# Asked once per client
# ---------------------------------------------------------------------------
_CLIENT_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,63}")


def locale_key(client_id: str) -> str:
    """`locale_profiles/<client>.json`."""
    if not _CLIENT_ID.fullmatch(client_id):
        raise ValueError(f"{client_id!r} is not a client id.")
    return f"{LOCALE_PREFIX}{client_id}.json"


def saved_profile(storage: Storage, client_id: str) -> LocaleProfile | None:
    """The client's confirmed profile, or None (never confirmed, forgotten, or unreadable - then asked again)."""
    key = locale_key(client_id)
    try:
        if not storage.exists(key):
            return None
        profile = storage.read_model(key, LocaleProfile)
    except (StorageError, ValueError):
        _LOG.info("doctor.locale.unreadable client=%s", client_id)
        return None
    return profile if profile.confirmed_at is not None else None


def confirm_profile(
    storage: Storage,
    profile: LocaleProfile,
    *,
    client_id: str,
    confirmed_by: str,
    answers: Mapping[str, Any] | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> LocaleProfile:
    """Save `profile` as the client's, every field `confirmed`; `answers` overrides values field by field.

    The person's id and the time are recorded. The latest confirmation wins; the earlier one is replaced.
    """
    if not confirmed_by:
        raise ValueError("a profile is confirmed by a person")
    unknown = set(answers or {}) - set(SETTINGS)
    if unknown:
        raise ValueError(f"{sorted(unknown)[0]!r} is not a locale setting")
    data = profile.model_dump()
    for name in SETTINGS:
        setting = data[name]
        if answers is not None and name in answers:
            setting["value"] = answers[name]
        setting["source"] = "confirmed"
    data.update(client_id=client_id, confirmed_by=confirmed_by, confirmed_at=clock())
    confirmed = LocaleProfile.model_validate(data)
    storage.write_model(locale_key(client_id), confirmed)
    _LOG.info("doctor.locale.confirmed client=%s", client_id)
    return confirmed


def forget_profile(storage: Storage, client_id: str) -> bool:
    """Delete the client's profile, so the next visit asks again; whether there was one."""
    key = locale_key(client_id)
    if not storage.exists(key):
        return False
    storage.delete(key)
    return True


_LABELS: Final[dict[str, str]] = {
    "country": "country",
    "workspace_currency": "currency the reports use",
    "money_units": "currency of each money column",
    "decimal_mark": "decimal mark",
    "digit_grouping": "digit grouping",
    "date_order": "day and month order",
    "number_words": "number words (lakh, crore, K, M)",
    "boolean_words": "yes and no words",
    "phone_format": "phone numbers",
    "postcode": "postcodes",
    "personal_ids": "ID numbers kept private",
    "time_zone": "time zone",
    "consent_regime": "consent rules",
    "header_words": "column-name words",
}


def _question(profile: LocaleProfile, fields: Sequence[str], text: str) -> LocaleQuestion:
    return LocaleQuestion(
        text=text,
        fields=tuple(fields),
        proposed={name: getattr(profile, name).value for name in fields},
        sources={name: getattr(profile, name).source for name in fields},
    )


def locale_for(
    storage: Storage,
    client_id: str,
    data: pd.DataFrame | Mapping[str, pd.DataFrame],
    *,
    workspace_currency: str = "INR",
) -> LocaleOutcome:
    """The profile a visit uses for this client's files: asked once, then applied visibly.

    * No confirmed profile: the detection, and one question listing every setting with what the files proved.
    * A confirmed one: applied, with a notice naming the date it was confirmed; a question only for the fields a
      new file contradicts (the confirmed value is kept until the person answers).
    """
    saved = saved_profile(storage, client_id)
    detection = detect_locale(data, base=saved, client_id=client_id, workspace_currency=workspace_currency)
    if saved is None:
        return LocaleOutcome(
            profile=detection.profile,
            question=_question(
                detection.profile,
                SETTINGS,
                "How does this client write numbers, dates and yes/no? We read the files and filled in what they "
                "show; please confirm once and we will use it for every file of this client.",
            ),
            conflicts=detection.conflicts,
        )
    at = saved.confirmed_at
    when = f"{at.day} {at:%b %Y}" if at is not None else "earlier"
    notice = f"Using the number, date and yes/no settings you confirmed on {when}. Change"
    question = None
    if detection.conflicts:
        fields = tuple(dict.fromkeys(c.field for c in detection.conflicts))
        names = ", ".join(_LABELS[f] for f in fields)
        question = _question(
            saved,
            fields,
            f"This file does not match the {names} you confirmed on {when}. Which is right for this file?",
        )
    return LocaleOutcome(
        profile=detection.profile,
        question=question,
        notice=notice,
        applied=True,
        conflicts=detection.conflicts,
    )
