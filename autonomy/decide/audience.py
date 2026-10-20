"""Who a goal is for: evaluate a brief's `Audience` against one customer's state.

The Strategist writes audiences as a list of `AudienceFilter`s (field, op, value). A customer is in
the audience when every filter passes; an empty list means everyone.

Rules, chosen so that an audience never includes someone because of data we do not have:

* `field` names a `CustomerState` field or property (`purchases`, `last_purchase_at`,
  `avg_order_value`, ...). A dotted path reaches into dict fields and sub-models:
  `scores.churn_risk`, `preferences.no_discounts`, `channels.email.consent`.
* A field that cannot be resolved (a typo, or a dict key that is absent) is **missing**, and every
  op on a missing field is False, including `is_null` and `ne`.
* A field that exists but holds `None` passes only `is_null` (and `eq` with value `None`). Every
  comparison on it is False, so `days_since_gt` on a customer who never bought is False: never
  bought is not the same as lapsed.
* `gt`/`gte`/`lt`/`lte` compare numbers, or datetimes (the filter value may be an ISO string).
  A comparison between types that do not compare is False rather than an error.
* `in`/`not_in` take a tuple. On a tuple field (`top_categories`) `in` means "shares at least one
  element" and `not_in` means "shares none".
* `days_since_gt`/`days_since_lt` take a datetime field and a number of days, measured to `now`
  (passed in; never the clock).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TypeGuard

from pydantic import BaseModel

from autonomy.contracts import Audience, AudienceFilter, Channel, CustomerState

_MISSING = object()
_SECONDS_PER_DAY = 86_400.0


def matches(state: CustomerState, audience: Audience, now: datetime) -> bool:
    """True when the customer passes every filter of the audience (an empty audience is everyone)."""
    return all(passes(state, f, now) for f in audience.filters)


def passes(state: CustomerState, flt: AudienceFilter, now: datetime) -> bool:
    """True when this one filter holds for the customer. See the module docstring for the rules."""
    value = resolve(state, flt.field)
    if value is _MISSING:
        return False
    # A plain str, so mypy does not narrow the Literal away and call the final fallthrough unreachable.
    op: str = flt.op
    target = flt.value
    if op == "is_null":
        return value is None
    if op == "not_null":
        return value is not None
    if op == "eq":
        return bool(value == target) if value is not None else target is None
    if value is None:
        return False
    if op == "ne":
        return bool(value != target)
    if op in ("gt", "gte", "lt", "lte"):
        return _compare(value, op, target)
    if op in ("in", "not_in"):
        return _membership(value, op, target)
    if op in ("days_since_gt", "days_since_lt"):
        if isinstance(value, datetime) and value.tzinfo is not None and _is_number(target):
            days = (now - value).total_seconds() / _SECONDS_PER_DAY
            limit = float(target)
            return days > limit if op == "days_since_gt" else days < limit
        return False
    return False


def resolve(state: CustomerState, path: str) -> object:
    """The value at a dotted path on the state, or the `_MISSING` sentinel if there is none."""
    current: object = state
    for part in path.split("."):
        current = _step(current, part)
        if current is _MISSING:
            return _MISSING
    return current


def _step(obj: object, part: str) -> object:
    if isinstance(obj, Mapping):
        if part in obj:
            return obj[part]
        try:
            channel = Channel(part)
        except ValueError:
            return _MISSING
        return obj.get(channel, _MISSING)
    if isinstance(obj, BaseModel):
        if part.startswith("_"):
            return _MISSING
        cls = type(obj)
        if part in cls.model_fields or isinstance(getattr(cls, part, None), property):
            return getattr(obj, part)
    return _MISSING


def _is_number(x: object) -> TypeGuard[int | float]:
    return isinstance(x, int | float) and not isinstance(x, bool)


def _compare(value: object, op: str, target: object) -> bool:
    """Order two numbers, or two aware datetimes (compared as timestamps). Anything else is False."""
    if isinstance(value, datetime):
        if isinstance(target, str):
            try:
                target = datetime.fromisoformat(target)
            except ValueError:
                return False
        if not isinstance(target, datetime) or value.tzinfo is None or target.tzinfo is None:
            return False
        left, right = value.timestamp(), target.timestamp()
    elif _is_number(value) and _is_number(target):
        left, right = float(value), float(target)
    else:
        return False
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    return left <= right


def _membership(value: object, op: str, target: object) -> bool:
    if not isinstance(target, tuple | list):
        return False
    if isinstance(value, tuple | list | set | frozenset):
        hit = any(v in target for v in value)
    else:
        hit = value in target
    return hit if op == "in" else not hit
