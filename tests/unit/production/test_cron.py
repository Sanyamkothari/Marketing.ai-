"""The five-field cron parser, its next-fire computation and its EventBridge translation (DEC-761)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engine.scheduling.cron import CronError, CronExpression


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "field", "expected"),
    [
        ("5 * * * *", "minutes", {5}),
        ("*/15 * * * *", "minutes", {0, 15, 30, 45}),
        ("1,2,40 * * * *", "minutes", {1, 2, 40}),
        ("10-12 * * * *", "minutes", {10, 11, 12}),
        ("0-30/10 * * * *", "minutes", {0, 10, 20, 30}),
        ("50/5 * * * *", "minutes", {50, 55}),
        ("0 9-17/4 * * *", "hours", {9, 13, 17}),
        ("0 0 1,15 * *", "days", {1, 15}),
        ("0 0 * 1-3,12 *", "months", {1, 2, 3, 12}),
        ("0 0 * * 1-5", "weekdays", {1, 2, 3, 4, 5}),
    ],
)
def test_the_supported_field_syntax(text: str, field: str, expected: set[int]) -> None:
    assert set(getattr(CronExpression.parse(text), field)) == expected


def test_seven_and_zero_both_mean_sunday() -> None:
    assert CronExpression.parse("0 0 * * 7").weekdays == frozenset({0})
    assert CronExpression.parse("0 0 * * 0,7").weekdays == frozenset({0})


def test_whitespace_is_normalised() -> None:
    assert CronExpression.parse("  0   2 *  * 1 ").text == "0 2 * * 1"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("0 2 * *", "CRON_INVALID"),
        ("0 2 * * * *", "CRON_INVALID"),
        ("60 * * * *", "CRON_INVALID"),
        ("0 24 * * *", "CRON_INVALID"),
        ("0 0 0 * *", "CRON_INVALID"),
        ("0 0 * 13 *", "CRON_INVALID"),
        ("0 0 * * 8", "CRON_INVALID"),
        ("0 0 * JAN *", "CRON_INVALID"),
        ("0 0 * * MON", "CRON_INVALID"),
        ("0 0 L * *", "CRON_INVALID"),
        ("0 0 ? * *", "CRON_INVALID"),
        ("5-1 * * * *", "CRON_INVALID"),
        ("*/0 * * * *", "CRON_INVALID"),
        ("0 0 31 2 *", "CRON_NEVER_FIRES"),
        ("0 2 1 * 1", "CRON_NOT_PORTABLE"),
        ("0 2 */2 * 1", "CRON_NOT_PORTABLE"),
    ],
)
def test_refusals_are_named(text: str, code: str) -> None:
    with pytest.raises(CronError) as caught:
        CronExpression.parse(text)
    assert caught.value.code == code
    assert caught.value.message


def test_an_unknown_timezone_is_refused() -> None:
    expression = CronExpression.parse("0 2 * * *")
    with pytest.raises(CronError) as caught:
        expression.next_after(utc(2026, 1, 1), timezone="Mars/Olympus")
    assert caught.value.code == "TIMEZONE_UNKNOWN"


# ---------------------------------------------------------------------------
# Next fire
# ---------------------------------------------------------------------------
def test_two_am_in_kolkata_is_half_past_eight_utc_the_evening_before() -> None:
    expression = CronExpression.parse("0 2 * * *")
    assert expression.next_after(utc(2026, 9, 1, 12, 0)) == utc(2026, 9, 1, 20, 30)
    assert expression.next_after(utc(2026, 9, 1, 20, 30)) == utc(2026, 9, 2, 20, 30), "strictly after"


def test_the_timezone_can_be_utc() -> None:
    assert CronExpression.parse("0 2 * * *").next_after(utc(2026, 9, 1, 12), timezone="UTC") == utc(
        2026, 9, 2, 2, 0
    )


def test_monthly_on_the_first() -> None:
    expression = CronExpression.parse("0 2 1 * *")
    # 02:00 IST on 1 October is 20:30 UTC on 30 September.
    assert expression.next_after(utc(2026, 9, 5), timezone="Asia/Kolkata") == utc(2026, 9, 30, 20, 30)


def test_weekly_on_monday() -> None:
    expression = CronExpression.parse("30 9 * * 1")
    moment = expression.next_after(utc(2026, 9, 23), timezone="UTC")  # a Wednesday
    assert moment == utc(2026, 9, 28, 9, 30)
    assert moment is not None and moment.isoweekday() == 1


def test_both_day_fields_restricted_match_either_as_vixie_cron_does() -> None:
    """Parsed for the rule's sake; `parse` refuses it as unportable, so it is built by hand here."""
    expression = CronExpression(
        text="0 0 13 * 5",
        minutes=frozenset({0}),
        hours=frozenset({0}),
        days=frozenset({13}),
        months=frozenset(range(1, 13)),
        weekdays=frozenset({5}),
        day_restricted=True,
        weekday_restricted=True,
    )
    assert expression.matches_date(datetime(2026, 11, 13).date())  # a Friday the 13th
    assert expression.matches_date(datetime(2026, 11, 20).date())  # any Friday
    assert expression.matches_date(datetime(2026, 10, 13).date())  # any 13th
    assert not expression.matches_date(datetime(2026, 10, 14).date())


def test_a_leap_day_schedule_fires_every_four_years() -> None:
    expression = CronExpression.parse("0 0 29 2 *")
    assert expression.next_after(utc(2026, 3, 1), timezone="UTC") == utc(2028, 2, 29)


def test_slots_between_is_inclusive_and_ordered() -> None:
    expression = CronExpression.parse("0 * * * *")
    slots = expression.slots_between(utc(2026, 9, 1, 10), utc(2026, 9, 1, 13), timezone="UTC")
    assert slots == (utc(2026, 9, 1, 10), utc(2026, 9, 1, 11), utc(2026, 9, 1, 12), utc(2026, 9, 1, 13))
    assert len(expression.slots_between(utc(2026, 9, 1), utc(2026, 9, 30), timezone="UTC", limit=5)) == 5


# ---------------------------------------------------------------------------
# EventBridge
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "eventbridge"),
    [
        ("0 2 * * *", "cron(0 2 * * ? *)"),
        ("0 2 1 * *", "cron(0 2 1 * ? *)"),
        ("*/15 8-18 * * *", "cron(*/15 8-18 * * ? *)"),
        ("0 2 * * 1", "cron(0 2 ? * 2 *)"),
        ("0 2 * * 1-5", "cron(0 2 ? * 2-6 *)"),
        ("0 2 * * 0", "cron(0 2 ? * 1 *)"),
        ("0 2 * * 7", "cron(0 2 ? * 1 *)"),
        ("0 2 * * 5-7", "cron(0 2 ? * 1,6-7 *)"),
        ("0 2 * * 0,3,6", "cron(0 2 ? * 1,4,7 *)"),
        ("0 2 * 1,7 */2", "cron(0 2 ? 1,7 1,3,5,7 *)"),
    ],
)
def test_the_eventbridge_translation(text: str, eventbridge: str) -> None:
    assert CronExpression.parse(text).to_eventbridge() == eventbridge


def test_the_eventbridge_weekdays_fire_on_the_same_days() -> None:
    """Cross-check the renumbering: EventBridge 1 is Sunday, so cron's day d is EventBridge d+1."""
    expression = CronExpression.parse("0 2 * * 1,3")
    fired = {
        slot.isoweekday() % 7
        for slot in expression.slots_between(utc(2026, 9, 1), utc(2026, 9, 30), timezone="UTC")
    }
    assert fired == {1, 3}
    days = expression.to_eventbridge().split()[4]
    assert {int(day) - 1 for day in days.split(",")} == fired
