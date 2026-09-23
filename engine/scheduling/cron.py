"""A small five-field cron parser, its next-fire computation, and its EventBridge translation (M49).

Schedules are written as the five fields everyone already reads - `minute hour day-of-month month
day-of-week` - in a named timezone (`Asia/Kolkata` unless the schedule says otherwise, because the
product's clients are in India and "02:00" should mean their 02:00). Two things consume one:

* the **local scheduler** (`engine.scheduling.scheduler.LocalScheduler`), which needs "when is this
  next due after instant T?" - `CronExpression.next_after`;
* **EventBridge Scheduler**, which takes its own six-field `cron(...)` dialect -
  `CronExpression.to_eventbridge`.

**Why a parser here rather than a dependency (DEC-761).** `croniter` and friends would answer the
first question and not the second, and the second is where the subtle part is: EventBridge numbers
the days of the week 1-7 from *Sunday* where cron numbers them 0-6 (7 is Sunday again), and it
refuses an expression that restricts both the day of the month and the day of the week (one of the
two must be `?`). A dependency for half the problem, plus a translation that has to re-derive the
parse anyway, is more code than the parse; and every new dependency is a pin in `pyproject.toml`
that the protocol asks to be justified. What is supported is exactly what the tests pin:

* numbers, `*`, comma lists, ranges `a-b`, steps `*/n`, `a-b/n` and `a/n` (from `a` to the field's
  maximum, as Quartz and EventBridge read it);
* day-of-week 0-7 with both 0 and 7 meaning Sunday;
* Vixie cron's day rule: when *both* day fields are restricted (neither starts with `*`) a day
  matches if *either* matches; otherwise both must.

Not supported, and refused by name rather than half-parsed: month and weekday names (`JAN`, `MON`),
`L`, `W`, `#`, `?` and the `@daily` macros - the presets in `engine.scheduling.schedules` cover the
common cadences in words instead.

**Portability is checked at parse time, not at deploy time.** A schedule written on a laptop with
`scheduler_backend=local` must still work the day the deployment switches to EventBridge, so
`CronExpression.parse` refuses (`CRON_NOT_PORTABLE`) the one shape EventBridge cannot express:
both day fields restricted. `0 2 1 * 1` ("the 1st, or any Monday") is a legal Vixie line and not a
legal EventBridge one; it is refused everywhere rather than working locally and failing in AWS.

Next-fire computation walks *local wall-clock* dates in the schedule's timezone and converts each
candidate to UTC, so "02:00 Asia/Kolkata" is 20:30 UTC the previous day, every day. A wall-clock
time that does not exist in a zone that observes DST (the skipped hour) resolves the way `zoneinfo`
resolves it (`fold=0`); an ambiguous one fires once, at its first occurrence. India observes no DST,
so the default zone has neither case.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "CRON_FIELD_COUNT",
    "DEFAULT_TIMEZONE",
    "CronError",
    "CronExpression",
    "zone",
]

DEFAULT_TIMEZONE: Final[str] = "Asia/Kolkata"
"""The zone a schedule is read in when it names none: the product's clients are in India."""

CRON_FIELD_COUNT: Final[int] = 5

_SEARCH_DAYS: Final[int] = 366 * 8 + 2
"""How far `next_after` looks before concluding an expression never fires. Eight years covers the
longest legal gap there is - 29 February, which recurs every four years except across a skipped
century leap year (2096 to 2104)."""

_BOUNDS: Final[tuple[tuple[str, int, int], ...]] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
)


class CronError(ValueError):
    """A cadence that cannot be used. `code` is CRON_INVALID | CRON_NOT_PORTABLE | CRON_NEVER_FIRES |
    TIMEZONE_UNKNOWN; `message` is a sentence a person can act on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def zone(name: str) -> ZoneInfo:
    """The `ZoneInfo` for an IANA name, or `CronError(TIMEZONE_UNKNOWN)`."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(
            "TIMEZONE_UNKNOWN", f"{name!r} is not a timezone name this system knows, e.g. 'Asia/Kolkata'."
        ) from exc


def _number(text: str, field: str, low: int, high: int) -> int:
    if not text.isdigit():
        raise CronError(
            "CRON_INVALID",
            f"The {field} field has {text!r}; only numbers, '*', ',', '-' and '/' are supported.",
        )
    value = int(text)
    if not low <= value <= high:
        raise CronError("CRON_INVALID", f"The {field} field allows {low} to {high}; it has {value}.")
    return value


def _field_values(text: str, field: str, low: int, high: int) -> frozenset[int]:
    """Every value one field matches. Raises `CronError(CRON_INVALID)` on anything unsupported."""
    if not text:
        raise CronError("CRON_INVALID", f"The {field} field is empty.")
    values: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            step = _number(step_text, field, 1, high)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, _, last = base.partition("-")
            start, end = _number(first, field, low, high), _number(last, field, low, high)
            if start > end:
                raise CronError(
                    "CRON_INVALID", f"The {field} range {base!r} runs backwards; write it low to high."
                )
        else:
            start = _number(base, field, low, high)
            end = high if step_text else start
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class CronExpression:
    """A parsed five-field cron line. Build with `CronExpression.parse`; never construct directly."""

    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    """0-6, Sunday = 0 (a 7 in the text is folded to 0)."""
    day_restricted: bool
    weekday_restricted: bool

    @classmethod
    def parse(cls, text: str) -> CronExpression:
        """Parse and check `text`: well-formed, fires at least once, and expressible in EventBridge."""
        fields = text.split()
        if len(fields) != CRON_FIELD_COUNT:
            raise CronError(
                "CRON_INVALID",
                f"A cadence has five fields - minute hour day-of-month month day-of-week - "
                f"and {text!r} has {len(fields)}.",
            )
        parsed = [
            _field_values(value, name, low, high)
            for value, (name, low, high) in zip(fields, _BOUNDS, strict=True)
        ]
        weekdays = frozenset(0 if day == 7 else day for day in parsed[4])
        expression = cls(
            text=" ".join(fields),
            minutes=parsed[0],
            hours=parsed[1],
            days=parsed[2],
            months=parsed[3],
            weekdays=weekdays,
            day_restricted=not fields[2].startswith("*"),
            weekday_restricted=not fields[4].startswith("*"),
        )
        expression.to_eventbridge()  # portability is a property of the text, checked once, here
        if expression.next_after(datetime(2000, 1, 1, tzinfo=UTC), timezone=DEFAULT_TIMEZONE) is None:
            raise CronError("CRON_NEVER_FIRES", f"{text!r} names a date that never occurs, e.g. 31 February.")
        return expression

    # ------------------------------------------------------------------
    def matches_date(self, day: date) -> bool:
        """Whether a calendar date is one this expression fires on (Vixie's day rule)."""
        if day.month not in self.months:
            return False
        in_days = day.day in self.days
        in_weekdays = (day.isoweekday() % 7) in self.weekdays  # isoweekday: Monday 1 .. Sunday 7
        if self.day_restricted and self.weekday_restricted:
            return in_days or in_weekdays
        return in_days and in_weekdays

    def next_after(self, after: datetime, *, timezone: str = DEFAULT_TIMEZONE) -> datetime | None:
        """The first firing strictly after `after`, as an aware UTC datetime; `None` if none in 8 years.

        Candidates are generated in the schedule's local wall clock and compared in UTC, so the
        answer is an instant and the caller never handles a local time.
        """
        tz = zone(timezone)
        instant = after.astimezone(UTC) if after.tzinfo else after.replace(tzinfo=UTC)
        local_start = instant.astimezone(tz)
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        for offset in range(_SEARCH_DAYS):
            day = local_start.date() + timedelta(days=offset)
            if not self.matches_date(day):
                continue
            for hour in hours:
                for minute in minutes:
                    candidate = datetime.combine(day, time(hour, minute), tzinfo=tz).astimezone(UTC)
                    if candidate > instant:
                        return candidate
        return None

    def slots_between(
        self, start: datetime, end: datetime, *, timezone: str = DEFAULT_TIMEZONE, limit: int = 1000
    ) -> tuple[datetime, ...]:
        """Every firing in `[start, end]` (both inclusive), oldest first, at most `limit` of them."""
        found: list[datetime] = []
        cursor = start - timedelta(microseconds=1)
        while len(found) < limit:
            upcoming = self.next_after(cursor, timezone=timezone)
            if upcoming is None or upcoming > end:
                break
            found.append(upcoming)
            cursor = upcoming
        return tuple(found)

    # ------------------------------------------------------------------
    def to_eventbridge(self) -> str:
        """The same cadence in EventBridge Scheduler's six-field `cron(...)` form.

        `cron(minutes hours day-of-month month day-of-week year)`. One of the two day fields must be
        `?`; EventBridge's weekdays run 1-7 from Sunday. Raises `CronError(CRON_NOT_PORTABLE)` for the
        one shape it cannot express.
        """
        minute, hour, day, month, weekday = self.text.split()
        if weekday == "*":
            return f"cron({minute} {hour} {day} {month} ? *)"
        if day == "*":
            return f"cron({minute} {hour} ? {month} {_eventbridge_weekdays(self.weekdays)} *)"
        raise CronError(
            "CRON_NOT_PORTABLE",
            "A cadence may restrict the day of the month or the day of the week, not both: "
            f"{self.text!r} does both, which EventBridge Scheduler cannot run. Use one of the two.",
        )


def _eventbridge_weekdays(weekdays: frozenset[int]) -> str:
    """Cron weekdays (Sunday 0) as EventBridge ones (Sunday 1), compacted into ranges: `1-5` -> `2-6`."""
    numbers = sorted(day + 1 for day in weekdays)
    runs: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        runs.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    runs.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(runs)
