"""The one canonical JSON every Plan K hash is taken over (design §3.2): stable, exact and strict."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from engine.agentic.canonical import canonical_bytes, canonical_json, digest, jsonable, sha256_hex


def test_key_order_and_whitespace_never_change_the_bytes() -> None:
    left = {"b": 1, "a": {"y": [1, 2], "x": None}}
    right = {"a": {"x": None, "y": [1, 2]}, "b": 1}
    assert canonical_json(left) == canonical_json(right) == '{"a":{"x":null,"y":[1,2]},"b":1}'
    assert digest(left) == digest(right)


def test_text_is_utf8_as_written_not_escaped() -> None:
    assert canonical_json({"name": "₹ Ünïcode"}) == '{"name":"₹ Ünïcode"}'
    assert canonical_bytes({"name": "é"}) == '{"name":"é"}'.encode()


def test_floats_are_the_shortest_round_trip() -> None:
    assert canonical_json([0.1, 1.0, 1e-7, 2.5e20, 1 / 3]) == "[0.1,1.0,1e-07,2.5e+20,0.3333333333333333]"
    value = 0.1 + 0.2
    assert float(canonical_json(value)) == value


def test_tuples_dates_and_models_have_one_form() -> None:
    class Point(BaseModel):
        x: int
        at: datetime

    stamp = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert canonical_json((1, 2)) == canonical_json([1, 2])
    assert canonical_json({"d": date(2026, 1, 2)}) == '{"d":"2026-01-02"}'
    assert jsonable(Point(x=1, at=stamp)) == {"x": 1, "at": "2026-10-10T12:00:00Z"}


@pytest.mark.parametrize("value", [math.nan, math.inf, {1, 2}, Decimal("1.5"), b"raw", {1: "a"}])
def test_values_with_no_defined_spelling_are_refused(value: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json({"v": value})


def test_the_digest_is_sha256_of_the_canonical_bytes() -> None:
    value = {"b": 1, "a": [1.5, "é"]}
    expected = hashlib.sha256('{"a":[1.5,"é"],"b":1}'.encode()).hexdigest()
    assert digest(value) == expected == sha256_hex(canonical_bytes(value))
    # pinned: a change to the canonical form changes every evidence id, digest and ledger hash ever written
    assert digest(value) == "b3aa7c2c75e6eda68896ca65edd78b023f766c052f625885b86d88bfa1f1a585"
