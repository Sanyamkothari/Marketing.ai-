"""Algebraic properties of `deep_merge` under hypothesis."""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from engine.config import deep_merge

_SCALARS = st.one_of(
    st.none(), st.booleans(), st.integers(), st.text(max_size=4), st.lists(st.integers(), max_size=3)
)

# Free-form documents: any key may hold a scalar, a list or a nested mapping.
_KEYS = st.sampled_from(["a", "b", "c", "x", "y"])
_INNER = st.dictionaries(_KEYS, st.one_of(_SCALARS, st.dictionaries(_KEYS, _SCALARS, max_size=2)), max_size=3)
_FREE_DOCUMENTS = st.dictionaries(_KEYS, st.one_of(_SCALARS, _INNER), max_size=4)


def _shaped(depth: int) -> st.SearchStrategy[dict[str, Any]]:
    """Documents of one consistent shape: `a`/`b` are always mappings, `x`/`y`/`z` always leaves.

    Config layering has this property (a path is a block in every layer or a value in every layer);
    where two layers disagree about the shape of a path the later layer replaces the earlier one,
    which is deliberately not associative.
    """
    leaves = st.dictionaries(st.sampled_from(["x", "y", "z"]), _SCALARS, max_size=3)
    if depth == 0:
        return leaves
    return st.builds(
        lambda flat, nested: {**flat, **nested},
        leaves,
        st.dictionaries(st.sampled_from(["a", "b"]), _shaped(depth - 1), max_size=2),
    )


@settings(deadline=None)
@given(document=_FREE_DOCUMENTS)
def test_empty_overlay_is_the_identity(document: dict[str, Any]) -> None:
    assert deep_merge(document, {}) == document


@settings(deadline=None)
@given(document=_FREE_DOCUMENTS)
def test_empty_base_is_the_identity(document: dict[str, Any]) -> None:
    assert deep_merge({}, document) == document


@settings(deadline=None)
@given(base=_FREE_DOCUMENTS, overlay=_FREE_DOCUMENTS)
def test_inputs_are_never_mutated(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    before_base, before_overlay = repr(base), repr(overlay)
    deep_merge(base, overlay)
    assert repr(base) == before_base
    assert repr(overlay) == before_overlay


@settings(deadline=None)
@given(a=_shaped(2), b=_shaped(2), c=_shaped(2))
def test_associative_over_shape_consistent_documents(
    a: dict[str, Any], b: dict[str, Any], c: dict[str, Any]
) -> None:
    assert deep_merge(deep_merge(a, b), c) == deep_merge(a, deep_merge(b, c))
