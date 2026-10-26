"""Moments: the times a message is worth more to one customer (plan-v3 decision 2).

A moment is something that happened lately which makes a message land: looking at the same product
twice, leaving a cart, being due to refill, a price drop on something they looked at, coming back
after a long absence, payday, a festival. Each has a signal (a function of the customer's state, the
clock, the catalogue's price changes and the country's calendar), a value multiplier and a fade.
`configs/v3/moments.yaml` holds the multipliers and fades; `configs/v3/calendar_<country>.yaml` holds
the paydays and festival dates as data, one file per country.

    book = MomentBook.load()
    book.factor(state, now)      # 1.0 when no moment is live, up to the config's max_multiplier
    book.reasons(state, now)     # plain sentences for the explanation, strongest first

How a moment counts. At the start of a moment a message is worth `multiplier` times as much. The raise
(`multiplier - 1`) then halves every `half_life_days` and is gone after `window_days`; a moment that
is a window (a refill that is due, a festival) holds its full raise while it lasts. When several are
live the strongest raise counts in full and each other at `rest_weight` (half), capped at
`max_multiplier`, so two weak moments never look like one strong one and nothing runs away.

`GoalBidder(moments=book)` is the opt-in hook. It wraps the goal's `Urgency` in `MomentUrgency`, so
the factor multiplies the whole expected net exactly where cart recovery's fade does, and a goal
built without a book prices every option as before. A moment is skipped for a goal that already
prices it: refill goals have `refill_boost`, and cart recovery has its own fade from the cart.

The country of a customer picks the calendar, by its code or one of the calendar's `aliases`
("IN", "India", "IND"; case and spaces do not matter). A customer with no country gets the shop's
`default_country` (`MomentBook(default_country=...)`, a per-shop setting; None, the default, gives
them no payday or festival moment, so a shop never pushes India's Diwali to someone it cannot place).
A customer whose country has no calendar gets no payday or festival moment either. Dates are read in
the customer's time zone (the calendar's when the customer's is still the UTC default).

Pure and deterministic: no clock reads, no randomness; the files are read only by the `load_*`
functions.
"""

from __future__ import annotations

import calendar as _calendar
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import Field, field_validator, model_validator

from autonomy.contracts import Contract, CustomerState, GoalKind
from autonomy.decide.bidders import (
    REFILL_DUE_DAYS,
    REFILL_P14,
    REFILL_P14_HALF,
    REFILL_WINDOW_OPEN,
    Urgency,
    refill_window_open,
)

__all__ = [
    "CONFIG_DIR",
    "SIGNALS",
    "ActiveMoment",
    "Calendar",
    "Festival",
    "FestivalWindow",
    "MomentBook",
    "MomentSpec",
    "MomentUrgency",
    "MomentsConfig",
    "PriceChange",
    "country_key",
    "load_calendar",
    "load_config",
    "price_drops",
    "register_moment",
    "skipped_for",
    "wrap_urgency",
]

CONFIG_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "configs" / "v3"
"""Where `moments.yaml` and the `calendar_<country>.yaml` files live."""

_SECONDS_PER_DAY = 86_400.0
_FIXED_DAY = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


# --- configuration -------------------------------------------------------------------------------


class MomentSpec(Contract):
    """One moment's settings. A moment reads only the knobs that apply to it (see moments.yaml)."""

    label: str
    enabled: bool = True
    multiplier: float = Field(ge=1.0, description="How much a message is worth at the start of the moment")
    half_life_days: float = Field(default=1.0, gt=0, description="The raise halves this often")
    window_days: float = Field(default=3.0, gt=0, description="The moment is over after this long")
    min_views: int = Field(default=2, ge=2, description="Views of one product that make a moment")
    min_purchases: int = Field(default=3, ge=2, description="Purchases before buying rhythm is trusted")
    lead_days: float = Field(default=3.0, ge=0, description="A refill window opens this long before due")
    late_days: float = Field(default=7.0, ge=0, description="... and closes this long after")
    min_drop_share: float = Field(default=0.05, gt=0, lt=1, description="Smallest price drop that counts")
    full_drop_share: float = Field(
        default=0.20, gt=0, le=1, description="A drop this big gives the full raise"
    )
    min_gap_days: float = Field(default=14.0, ge=1, description="Shortest absence that counts as long")

    @model_validator(mode="after")
    def _drop_shares(self) -> MomentSpec:
        if self.full_drop_share < self.min_drop_share:
            raise ValueError("full_drop_share must be at least min_drop_share")
        return self


class Combine(Contract):
    rest_weight: float = Field(default=0.5, ge=0, le=1)
    max_multiplier: float = Field(default=2.5, ge=1)


class MomentsConfig(Contract):
    version: int = 1
    combine: Combine = Field(default_factory=Combine)
    moments: dict[str, MomentSpec]


class Festival(Contract):
    """A festival or sale day. Give `dates` (one per year) or `fixed` ("MM-DD", every year), or both."""

    id: str
    name: str
    dates: tuple[date, ...] = ()
    fixed: str | None = None
    lead_days: int = Field(default=0, ge=0, description="Active this many days before the day")
    tail_days: int = Field(default=0, ge=0, description="... and this many days after")
    multiplier: float | None = Field(default=None, ge=1.0, description="None: the festival moment's own")

    @field_validator("fixed")
    @classmethod
    def _fixed(cls, value: str | None) -> str | None:
        if value is not None and not _FIXED_DAY.match(value):
            raise ValueError("fixed is a day of the year written MM-DD")
        return value

    @model_validator(mode="after")
    def _has_a_day(self) -> Festival:
        if not self.dates and self.fixed is None:
            raise ValueError(f"festival {self.id!r} has neither dates nor fixed")
        return self

    def days_in(self, year: int) -> tuple[date, ...]:
        """The festival's days in a calendar year (a fixed 02-29 only in leap years)."""
        found = {d for d in self.dates if d.year == year}
        if self.fixed is not None:
            month, day = int(self.fixed[:2]), int(self.fixed[3:])
            if day <= _calendar.monthrange(year, month)[1]:
                found.add(date(year, month, day))
        return tuple(sorted(found))


@dataclass(frozen=True)
class FestivalWindow:
    """A festival whose window covers a day: `days_to` is negative after the day itself."""

    festival: Festival
    day: date
    days_to: int


class Payday(Contract):
    days: tuple[int | Literal["last"], ...] = (1, "last")

    @field_validator("days")
    @classmethod
    def _days(cls, value: tuple[int | Literal["last"], ...]) -> tuple[int | Literal["last"], ...]:
        for d in value:
            if d != "last" and not 1 <= d <= 28:
                raise ValueError("a payday is a day 1 to 28 of the month, or 'last'")
        if not value:
            raise ValueError("give at least one payday")
        return value


class Calendar(Contract):
    """Paydays and festival days for one country."""

    country: str
    name: str = ""
    aliases: tuple[str, ...] = Field(
        default=(), description="Other ways a customer file writes the country: 'India', 'IND' ..."
    )
    timezone: str = "UTC"
    payday: Payday = Field(default_factory=Payday)
    festivals: tuple[Festival, ...] = ()

    @field_validator("country")
    @classmethod
    def _country(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(country_key(a) for a in value if a.strip())

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    def payday_age(self, today: date) -> int | None:
        """Days since the most recent payday on or before `today` (0 on the day), None if none is set."""
        best: int | None = None
        for year, month in ((today.year, today.month), _previous_month(today.year, today.month)):
            last = _calendar.monthrange(year, month)[1]
            for d in self.payday.days:
                day = last if d == "last" else min(d, last)
                paid = date(year, month, day)
                if paid <= today:
                    age = (today - paid).days
                    best = age if best is None else min(best, age)
        return best

    def festivals_on(self, today: date) -> tuple[FestivalWindow, ...]:
        """Festivals whose window (lead before to tail after) covers `today`."""
        out: list[FestivalWindow] = []
        for fest in self.festivals:
            for year in (today.year - 1, today.year, today.year + 1):
                for day in fest.days_in(year):
                    ahead = (day - today).days
                    if -fest.tail_days <= ahead <= fest.lead_days:
                        out.append(FestivalWindow(fest, day, ahead))
        return tuple(out)


def country_key(country: str) -> str:
    """How a country is matched to a calendar: upper case, inner spaces collapsed ("  india " -> "INDIA")."""
    return " ".join(country.split()).upper()


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_config(path: Path | None = None) -> MomentsConfig:
    """The moments configuration (default `configs/v3/moments.yaml`). A moment with no signal is an error."""
    config = MomentsConfig.model_validate(_read_yaml(path or CONFIG_DIR / "moments.yaml"))
    unknown = sorted(set(config.moments) - set(SIGNALS))
    if unknown:
        raise ValueError(f"moments with no signal: {', '.join(unknown)}; known: {', '.join(SIGNALS)}")
    return config


def load_calendar(country: str, directory: Path | None = None) -> Calendar | None:
    """`calendar_<country>.yaml` from the config folder, or None when the country has no file."""
    path = (directory or CONFIG_DIR) / f"calendar_{country.strip().lower()}.yaml"
    if not path.is_file():
        return None
    return Calendar.model_validate(_read_yaml(path))


# --- catalogue price changes ---------------------------------------------------------------------


@dataclass(frozen=True)
class PriceChange:
    """A catalogue price that changed on `at`."""

    item_id: str
    at: datetime
    old_price: float
    new_price: float

    @property
    def drop_share(self) -> float:
        """The share of the old price taken off (0 for a rise or an unknown old price)."""
        if self.old_price <= 0 or self.new_price >= self.old_price:
            return 0.0
        return (self.old_price - self.new_price) / self.old_price


def price_drops(
    before: Mapping[str, float], after: Mapping[str, float], at: datetime
) -> dict[str, PriceChange]:
    """The items whose catalogue price fell between two snapshots (item id -> price, same currency)."""
    out: dict[str, PriceChange] = {}
    for item, new in after.items():
        old = before.get(item)
        if old is not None and 0 < new < old:
            out[item] = PriceChange(item_id=item, at=at, old_price=old, new_price=new)
    return out


# --- the moments ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ActiveMoment:
    """A moment that is live for one customer now."""

    name: str
    label: str
    lift: float
    """How much extra a message is worth, as a share: 0.3 means 30% more (already faded)."""
    reason: str

    @property
    def factor(self) -> float:
        return 1.0 + self.lift


@dataclass(frozen=True)
class _Context:
    """What a signal may read besides the state."""

    now: datetime
    today: date
    calendar: Calendar | None
    prices: Mapping[str, PriceChange]


Signal = Callable[[CustomerState, MomentSpec, _Context], "ActiveMoment | None"]
"""A moment's signal: the customer's state and the moment's settings in, the live moment or None out."""


def _days_since(then: datetime, now: datetime) -> float:
    return (now - then).total_seconds() / _SECONDS_PER_DAY


def _fade(age_days: float, spec: MomentSpec) -> float:
    """1 at the start of the moment, halving every half-life, 0 after the window (and before the start)."""
    if age_days < 0 or age_days > spec.window_days:
        return 0.0
    return float(0.5 ** (age_days / spec.half_life_days))


def _ago(days: float) -> str:
    if days < 1.0 / 24.0:
        return "just now"
    if days < 1.0:
        return f"{round(days * 24)} hours ago"
    whole = round(days)
    return "a day ago" if whole <= 1 else f"{whole} days ago"


def _live(name: str, spec: MomentSpec, share: float, reason: str) -> ActiveMoment | None:
    lift = (spec.multiplier - 1.0) * share
    return ActiveMoment(name, spec.label, lift, reason) if lift > 0 else None


def _bought_since(state: CustomerState, then: datetime) -> bool:
    return state.last_purchase_at is not None and state.last_purchase_at >= then


def _viewed_twice(state: CustomerState, spec: MomentSpec, ctx: _Context) -> ActiveMoment | None:
    best: tuple[float, str, int, float] | None = None
    for item, views in state.viewed_items.items():
        # Buying something since looking is taken to end the interest: the state drops a bought
        # product by its id, and a later purchase without one is read the same way.
        if views.count < spec.min_views or _bought_since(state, views.last_at):
            continue
        age = _days_since(views.last_at, ctx.now)
        fade = _fade(age, spec)
        if fade > 0 and (best is None or fade > best[0]):
            best = (fade, item, views.count, age)
    if best is None:
        return None
    fade, item, count, age = best
    return _live("viewed_twice", spec, fade, f"Looked at {item} {count} times, last {_ago(age)}")


def _cart_left(state: CustomerState, spec: MomentSpec, ctx: _Context) -> ActiveMoment | None:
    cart = state.last_cart_at
    if cart is None or _bought_since(state, cart):
        return None
    age = _days_since(cart, ctx.now)
    return _live("cart_left", spec, _fade(age, spec), f"Left a cart {_ago(age)}")


def _refill_due(state: CustomerState, spec: MomentSpec, ctx: _Context) -> ActiveMoment | None:
    if REFILL_WINDOW_OPEN in state.scores or REFILL_DUE_DAYS in state.scores:
        # The catalogue's refill model (autonomy.catalog.cycle) has spoken: use it, and nothing else.
        if not refill_window_open(state):
            return None
        due = _finite(state.scores.get(REFILL_DUE_DAYS))
        p14 = _finite(state.scores.get(REFILL_P14))
        share = 1.0 if p14 is None else min(max(p14, 0.0), 1.0) / (min(max(p14, 0.0), 1.0) + REFILL_P14_HALF)
        return _live("refill_due", spec, share, _refill_sentence(due))
    # No refill model: the products they buy again on a rhythm of their own (purchase days, not order
    # lines), the one most nearly due first.
    best: tuple[float, str] | None = None
    for item, bought in state.bought_items.items():
        if bought.days < spec.min_purchases or bought.gap_days is None:
            continue
        due_in = bought.gap_days - _days_since(bought.last_at, ctx.now)
        if -spec.late_days <= due_in <= spec.lead_days and (best is None or abs(due_in) < abs(best[0])):
            best = (due_in, item)
    if best is not None:
        return _live("refill_due", spec, 1.0, _rhythm_sentence(best[0], best[1]))
    if state.bought_items:
        # Their products are known and none is due: the rhythm of all their buying says no more.
        return None
    # No product ids at all: their rhythm across every purchase, once it rests on enough gaps.
    last, gap = state.last_purchase_at, state.purchase_gap_days
    if last is None or gap is None or state.purchase_gaps < spec.min_purchases - 1:
        return None
    due_in = gap - _days_since(last, ctx.now)
    if not -spec.late_days <= due_in <= spec.lead_days:
        return None
    return _live("refill_due", spec, 1.0, _rhythm_sentence(due_in, None))


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _refill_sentence(due_in: float | None) -> str:
    if due_in is None:
        return "Probably running low"
    if due_in < -0.5:
        return f"Refill about {-due_in:.0f} days overdue"
    if due_in <= 0.5:
        return "Refill due about now"
    return f"Refill due in about {due_in:.0f} days"


def _rhythm_sentence(due_in: float, item: str | None) -> str:
    """The reason for a refill read from their own buying rhythm: no claim that the stock runs out."""
    what = f"buys {item}" if item is not None else "buys"
    if due_in < -0.5:
        return f"Usually {what} again about now ({_days_word(-due_in)} later than usual)"
    if due_in <= 0.5:
        return f"Usually {what} again about now"
    return f"Usually {what} again in {_days_word(due_in)}"


def _days_word(days: float) -> str:
    whole = round(days)
    return "about a day" if whole <= 1 else f"about {whole} days"


def _price_drop(state: CustomerState, spec: MomentSpec, ctx: _Context) -> ActiveMoment | None:
    best: tuple[float, str, PriceChange] | None = None
    for item, views in state.viewed_items.items():
        change = ctx.prices.get(item)
        if change is None or change.drop_share < spec.min_drop_share:
            continue
        # They must have seen the old price, and not have bought since it fell.
        if views.last_at > change.at or _bought_since(state, change.at):
            continue
        fade = _fade(_days_since(change.at, ctx.now), spec)
        share = fade * min(change.drop_share / spec.full_drop_share, 1.0)
        if share > 0 and (best is None or share > best[0]):
            best = (share, item, change)
    if best is None:
        return None
    share, item, change = best
    age = _days_since(change.at, ctx.now)
    reason = (
        f"The price of {item} dropped {change.drop_share * 100:.0f}% {_ago(age)}, after they looked at it"
    )
    return _live("price_drop", spec, share, reason)


def _back_after_absence(state: CustomerState, spec: MomentSpec, ctx: _Context) -> ActiveMoment | None:
    back, gap = state.last_return_at, state.last_return_gap_days
    if back is None or gap is None or gap < spec.min_gap_days:
        return None
    age = _days_since(back, ctx.now)
    return _live("back_after_absence", spec, _fade(age, spec), f"Back {_ago(age)} after {gap:.0f} days away")


def _payday(state: CustomerState, spec: MomentSpec, ctx: _Context) -> ActiveMoment | None:  # noqa: ARG001
    if ctx.calendar is None:
        return None
    age = ctx.calendar.payday_age(ctx.today)
    if age is None:
        return None
    sentence = "It is payday" if age == 0 else f"Payday was {age} {'day' if age == 1 else 'days'} ago"
    return _live("payday", spec, _fade(float(age), spec), sentence)


def _festival(state: CustomerState, spec: MomentSpec, ctx: _Context) -> ActiveMoment | None:  # noqa: ARG001
    if ctx.calendar is None:
        return None
    best: tuple[float, FestivalWindow] | None = None
    for window in ctx.calendar.festivals_on(ctx.today):
        multiplier = window.festival.multiplier or spec.multiplier
        if best is None or multiplier > best[0]:
            best = (multiplier, window)
    if best is None:
        return None
    multiplier, window = best
    days, name = window.days_to, window.festival.name
    if days > 1:
        when = f"{name} is in {days} days"
    elif days == 1:
        when = f"{name} is tomorrow"
    elif days == 0:
        when = f"It is {name}"
    else:
        when = f"{name} was {'yesterday' if days == -1 else f'{-days} days ago'}"
    lift = multiplier - 1.0
    return ActiveMoment("festival", spec.label, lift, when) if lift > 0 else None


SIGNALS: dict[str, Signal] = {
    "viewed_twice": _viewed_twice,
    "cart_left": _cart_left,
    "refill_due": _refill_due,
    "price_drop": _price_drop,
    "back_after_absence": _back_after_absence,
    "payday": _payday,
    "festival": _festival,
}
"""The registry of moments, by the name moments.yaml gives them. Add one with `register_moment`."""


def register_moment(name: str, signal: Signal, *, replace: bool = False) -> None:
    """Make a new moment known. Its settings go in moments.yaml under the same name."""
    if name in SIGNALS and not replace:
        raise ValueError(f"a moment called {name!r} is already registered")
    SIGNALS[name] = signal


# --- the book ------------------------------------------------------------------------------------


class MomentBook:
    """The live moments of a customer, from the configuration, the calendars and the price changes."""

    def __init__(
        self,
        config: MomentsConfig,
        calendars: Iterable[Calendar] = (),
        price_changes: Mapping[str, PriceChange] | None = None,
        *,
        default_country: str | None = None,
    ) -> None:
        unknown = sorted(set(config.moments) - set(SIGNALS))
        if unknown:
            raise ValueError(f"moments with no signal: {', '.join(unknown)}")
        self.config = config
        self.calendars: dict[str, Calendar] = {c.country: c for c in calendars}
        self.price_changes: dict[str, PriceChange] = dict(price_changes or {})
        self.default_country = default_country
        """The shop's own country, for customers with none (None: they get no calendar moment)."""
        self._by_name: dict[str, Calendar] = {}
        for cal in self.calendars.values():
            for name in (cal.country, *cal.aliases):
                self._by_name.setdefault(country_key(name), cal)

    @classmethod
    def load(
        cls,
        directory: Path | None = None,
        price_changes: Mapping[str, PriceChange] | None = None,
        *,
        default_country: str | None = None,
    ) -> MomentBook:
        """The book from `moments.yaml` and every `calendar_*.yaml` in the folder (default configs/v3).

        `default_country` is the shop's country, read for customers whose file names none."""
        folder = directory or CONFIG_DIR
        config = load_config(folder / "moments.yaml")
        calendars = [
            cal
            for path in sorted(folder.glob("calendar_*.yaml"))
            if (cal := load_calendar(path.stem.removeprefix("calendar_"), folder)) is not None
        ]
        return cls(config, calendars, price_changes, default_country=default_country)

    def with_price_changes(self, changes: Mapping[str, PriceChange]) -> MomentBook:
        """A book that also knows these catalogue price changes (a newer change to an item replaces the older)."""
        return MomentBook(
            self.config,
            self.calendars.values(),
            {**self.price_changes, **changes},
            default_country=self.default_country,
        )

    def calendar_for(self, state: CustomerState) -> Calendar | None:
        """The customer's country's calendar (by code or alias), else the shop's; None when neither has one."""
        country = state.country if state.country and state.country.strip() else self.default_country
        if country is None:
            return None
        return self._by_name.get(country_key(country))

    def active(
        self, state: CustomerState, now: datetime, *, skip: Iterable[str] = ()
    ) -> tuple[ActiveMoment, ...]:
        """The moments live for this customer at `now`, strongest first. `skip` names moments to leave out."""
        calendar = self.calendar_for(state)
        ctx = _Context(now, _local_day(state, calendar, now), calendar, self.price_changes)
        left = frozenset(skip)
        found: list[ActiveMoment] = []
        for name, spec in self.config.moments.items():
            if not spec.enabled or name in left:
                continue
            moment = SIGNALS[name](state, spec, ctx)
            if moment is not None:
                found.append(moment)
        found.sort(key=lambda m: (-m.lift, m.name))
        return tuple(found)

    def factor(self, state: CustomerState, now: datetime, *, skip: Iterable[str] = ()) -> float:
        """What a message is worth now as a multiple of its usual worth: 1.0 when no moment is live."""
        return self.combine(self.active(state, now, skip=skip))

    def combine(self, moments: Iterable[ActiveMoment]) -> float:
        lifts = sorted((m.lift for m in moments), reverse=True)
        if not lifts:
            return 1.0
        total = lifts[0] + self.config.combine.rest_weight * sum(lifts[1:])
        return min(1.0 + total, self.config.combine.max_multiplier)

    def reasons(self, state: CustomerState, now: datetime, *, skip: Iterable[str] = ()) -> tuple[str, ...]:
        """One plain sentence per live moment, strongest first, for the explanation of a decision."""
        return tuple(m.reason for m in self.active(state, now, skip=skip))


def _local_day(state: CustomerState, calendar: Calendar | None, now: datetime) -> date:
    zone = state.timezone
    if zone == "UTC" and calendar is not None:
        # An unknown zone reads as the UTC default; for a customer in the calendar's country the
        # calendar's own zone is the better guess.
        zone = calendar.timezone
    try:
        return now.astimezone(ZoneInfo(zone)).date()
    except (KeyError, ValueError, OSError):
        return now.date()


# --- the hook ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MomentUrgency(Urgency):
    """A goal's `Urgency` times the live moments' factor: the opt-in hook of `GoalBidder(moments=...)`.

    `at()` is the base urgency (1 when the goal has none) multiplied by `book.factor`, so a moment
    raises the expected net of every option of the goal alike and fades with the moment. With no
    moment live it equals the base urgency exactly.
    """

    book: MomentBook = field(default_factory=lambda: MomentBook(MomentsConfig(moments={})), compare=False)
    base: Urgency | None = None
    skip: frozenset[str] = frozenset()

    def at(self, state: CustomerState, now: datetime) -> float:
        base = self.base.at(state, now) if self.base is not None else 1.0
        return base * self.book.factor(state, now, skip=self.skip)


def skipped_for(kind: GoalKind, refill: bool) -> frozenset[str]:
    """The moments a goal already prices itself: refill goals have `refill_boost`, cart recovery its fade."""
    skip: set[str] = set()
    if refill:
        skip.add("refill_due")
    if kind is GoalKind.CART_RECOVERY:
        skip.add("cart_left")
    return frozenset(skip)


def wrap_urgency(base: Urgency | None, book: MomentBook, *, kind: GoalKind, refill: bool) -> MomentUrgency:
    """`base` with the book's moments on top, for a goal of this kind."""
    half_life = base.half_life_days if base is not None else 1.0
    return MomentUrgency(half_life_days=half_life, book=book, base=base, skip=skipped_for(kind, refill))
