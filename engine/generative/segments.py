"""Cutting scored rows into segments: the one grouping rule root-cause summaries and campaign copy share.

`engine.generative.root_cause` explains a run segment by segment and `engine.generative.win_back` can
write copy segment by segment (`campaign_copy.segment_by: top_reason`, DEC-1241). Both cut on the
same thing - a row's own strongest SHAP reason - and both order segments the same way, largest first
with ties broken by name, so the two screens never disagree about which customers "share a reason".
The rule lives here once rather than in both modules.

They differ only in what happens past the cap. A root-cause summary keeps the `max_segments` largest
segments and leaves the rest unexplained, because a summary of a dozen customers is noise. Copy cannot
leave anybody out - a row that is eligible for a message must get one - so `cap_with_other` folds every
smaller segment into one "other" bucket instead of dropping it.

Nothing here reads a file, imports pandas or sees a value from a row: the inputs are primary keys and
a per-key name, and the outputs are the same keys regrouped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from engine.contracts import RowExplanation

__all__ = ["cap_with_other", "group_keys", "top_reason_feature"]


def top_reason_feature(explanation: RowExplanation | None) -> str | None:
    """The feature behind a row's strongest reason, or `None` when the row has no reasons at all."""
    if explanation is None or not explanation.reasons:
        return None
    return explanation.reasons[0].feature


def group_keys(
    keys_ordered: Iterable[str], name_of: Callable[[str], str | None]
) -> list[tuple[str, list[str]]]:
    """Segment name -> the keys in it, largest segment first and ties by name; keys keep their order.

    A key whose `name_of` is `None` has nothing to segment on and is left out; the caller decides
    whether that is a row to skip (a root-cause summary) or a row to put somewhere (copy).
    """
    groups: dict[str, list[str]] = {}
    for key in keys_ordered:
        name = name_of(key)
        if name is None:
            continue
        groups.setdefault(name, []).append(key)
    return sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))


def cap_with_other(
    groups: Sequence[tuple[str, list[str]]], *, max_segments: int, other: str, extra: Sequence[str] = ()
) -> list[tuple[str, list[str]]]:
    """At most `max_segments` segments: the largest kept, every smaller one folded into `other`.

    `groups` must already be ordered as `group_keys` orders them. `extra` are keys that belong in
    `other` whatever the cap (rows with no reason to group on). The `other` bucket comes last and is
    present only when it holds a key, so a run with few distinct reasons is never given an empty one.
    """
    if max_segments < 2:
        raise ValueError("max_segments must leave room for at least one segment and the other bucket")
    if len(groups) <= max_segments and not extra:
        return [(name, list(keys)) for name, keys in groups]
    keep = max_segments - 1 if len(groups) > max_segments or extra else max_segments
    kept = [(name, list(keys)) for name, keys in groups[:keep]]
    folded = [key for _name, keys in groups[keep:] for key in keys]
    folded.extend(extra)
    if folded:
        kept.append((other, folded))
    return kept
