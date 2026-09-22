"""The composite primary key: the type is wide, the accessor is one function, the boundary refuses.

`PrimaryKey` is on the shared contracts from the start because those contracts live in append-only
files and a branch could not widen them later (DEC-077). Phase 1 sets a single column and nothing
threads a composite one through the stages yet, so what is pinned here is the pair of properties
that makes the half-built state safe: every contract accepts both spellings and round-trips them,
and every place that still needs one column refuses several by name rather than taking the first.
"""

from __future__ import annotations

import pytest

from engine.config import ConfigError, key_columns, sole_key


def test_one_column_reads_as_one_column() -> None:
    assert key_columns("customer_id") == ("customer_id",)


def test_several_columns_read_in_order() -> None:
    assert key_columns(["account_id", "line_id"]) == ("account_id", "line_id")


def test_the_accessor_removes_the_need_for_an_isinstance_check() -> None:
    """Both spellings answer the same question the same way; that is the whole point of it."""
    assert key_columns("only") == key_columns(["only"])


def test_narrowing_one_column_returns_it() -> None:
    assert sole_key("customer_id") == "customer_id"
    assert sole_key(["customer_id"]) == "customer_id"


def test_narrowing_several_columns_says_which_ones() -> None:
    with pytest.raises(ConfigError) as raised:
        sole_key(["account_id", "line_id"], what="A run")
    assert raised.value.code == "COMPOSITE_KEY_NOT_SUPPORTED"
    assert "account_id, line_id" in raised.value.message
    assert raised.value.message.startswith("A run needs a single primary-key column")


def test_narrowing_no_column_is_refused_too() -> None:
    """An empty list is not "the default column": it names nothing, and nothing can be joined on it."""
    with pytest.raises(ConfigError) as raised:
        sole_key([])
    assert raised.value.code == "COMPOSITE_KEY_NOT_SUPPORTED"
