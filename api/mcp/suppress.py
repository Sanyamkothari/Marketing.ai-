"""Small-group suppression on every grouped output (Plan K M133, DEC-1404 (e); design §7.3).

Plan I's `rate_by` rule (`engine.agent.tools.MIN_GROUP_ROWS`, DEC-1250…): **a group of 1 to 9 customers shows
no figure**, and while what is withheld from a list that adds up (a partition: bands, arms, reason codes) comes
to fewer than 10 customers, the next smallest group is withheld with it, so the totals beside the list cannot
give the missing figures back (complementary suppression). A group of nobody (0) describes nobody and is shown.

The rules are declared per tool (`RULES`) over the tool's output, as JSON:

* `Groups(path, counts, additive)`: a list of groups. An element is withheld - **removed from the list**, so the
  result still validates against the tool's output model - when any of its `counts` is 1 to 9. `additive`
  names the counts that add up across the list; they drive the complementary rule (empty: the elements are not
  a partition, e.g. channels, which each cover every customer).
* `CountMap(path)`: a `{code: rows}` map that adds up (a channel's reasons); a small entry is removed, with the
  same complementary rule.
* `Whole(path, counts)`: an object that is one group as a whole (a campaign's report, an off-policy estimate);
  it is replaced by `null` when any of its counts is 1 to 9 (each such field is nullable in its output model).

`path` steps through object keys; `*` steps into every element of a list. What was withheld is returned by
path (`{"bands": 2}`), and the server puts it in the result's `_meta` (`marketing-ai/withheld`) and its text.

**Narrowing.** `value_segment` can be asked for one band. Answering a narrowed question from the unsuppressed
data would let two overlapping questions isolate a small band (all bands, then the band that complemented it).
The server therefore always asks for every band, suppresses, then narrows (`NARROWING`): a band withheld in the
full answer is withheld in every narrower one.

`tests/unit/mcp/test_suppress.py` walks every exposed tool's output model and fails on a list of groups or a
map of counts that no rule covers, so a tool added later cannot bypass the rule.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "MIN_GROUP_ROWS",
    "NARROWING",
    "RULES",
    "CountMap",
    "Groups",
    "Narrowing",
    "Whole",
    "narrow",
    "suppress",
    "widen",
]

MIN_GROUP_ROWS: Final[int] = 10
"""Plan I's minimum (`engine.agent.tools.MIN_GROUP_ROWS`): the same number, stated where it is enforced."""


@dataclass(frozen=True)
class Groups:
    path: tuple[str, ...]
    counts: tuple[str, ...]
    additive: tuple[str, ...] = ()


@dataclass(frozen=True)
class CountMap:
    path: tuple[str, ...]


@dataclass(frozen=True)
class Whole:
    path: tuple[str, ...]
    counts: tuple[str, ...]


Rule = Groups | CountMap | Whole

_REPORT: Final[tuple[Rule, ...]] = (
    Whole(("report",), ("treated_rows", "control_rows")),
    Whole(("report", "offers_combined"), ("treated_rows", "control_rows")),
    Groups(("report", "arms"), ("rows", "treated_rows", "control_rows"), additive=("treated_rows",)),
    Groups(("report", "arms", "*", "segments"), ("rows",), additive=("rows",)),
)

RULES: Final[dict[str, tuple[Rule, ...]]] = {
    "check_contactable": (
        Groups(("channels",), ("contactable_rows", "not_contactable_eligible_rows")),
        CountMap(("channels", "*", "reasons")),
    ),
    "value_segment": (Groups(("bands",), ("rows", "rows_valued"), additive=("rows", "rows_valued")),),
    "measure": _REPORT,
    "audit_campaign": (Whole(("audit",), ("assignment_rows",)), *_REPORT),
    "evaluate_policy": (Whole(("ope",), ("rows",)), Whole(("risk_comparison",), ("rows",))),
    # M141: the gate's counts are one group as a whole (a small number withheld or moved is a small group), and
    # the reasons add up as a channel's do.
    "check_before_contact": (
        Whole(("counts",), ("customers", "sent_rows", "withheld_rows", "rerouted_rows")),
        CountMap(("withheld",)),
    ),
}
"""The grouped outputs of the exposed tools. A tool absent here returns no group (`plan_test` computes from the
counts its caller sends; `get_proof_pack`'s figures are guarded by the campaign's arm sizes in
`api.mcp.demo`; `draft_test_plan` returns ids and codes)."""


@dataclass(frozen=True)
class Narrowing:
    """An argument that narrows a list, answered from the suppressed full list instead."""

    argument: str
    list_key: str
    item_key: str
    cleared: tuple[str, ...]
    """Top-level fields the tool leaves null when narrowed (so the narrowed answer has the same shape)."""


NARROWING: Final[dict[str, Narrowing]] = {
    "value_segment": Narrowing(argument="band", list_key="bands", item_key="band", cleared=("total_inr",)),
}


def _small(value: object, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < minimum


def _containers(node: Any, path: tuple[str, ...], trail: str) -> Iterator[tuple[Any, str, str]]:
    """`(parent, key, printable path)` of every node `path` reaches (a `*` steps into each list element)."""
    if not path or not isinstance(node, dict):
        return
    head, rest = path[0], path[1:]
    if head == "*":
        return
    child = node.get(head)
    here = f"{trail}.{head}" if trail else head
    if not rest:
        yield node, head, here
        return
    if rest[0] == "*":
        if isinstance(child, list):
            for index, item in enumerate(child):
                yield from _containers(item, rest[1:], f"{here}[{index}]")
        return
    yield from _containers(child, rest, here)


def _complement(
    sizes: list[dict[str, int]], hidden: set[int], additive: tuple[str, ...], minimum: int
) -> set[int]:
    """Add the next smallest groups while any additive count of what is hidden is 1 to minimum - 1."""

    def short() -> bool:
        return any(0 < sum(sizes[i].get(name, 0) for i in hidden) < minimum for name in additive)

    while hidden and len(hidden) < len(sizes) and short():
        rest = [i for i in range(len(sizes)) if i not in hidden]
        hidden.add(min(rest, key=lambda i: (sum(sizes[i].get(name, 0) for name in additive), i)))
    return hidden


def _counts(item: Any, names: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(item, dict):
        return {}
    return {
        name: item[name]
        for name in names
        if isinstance(item.get(name), int) and not isinstance(item[name], bool)
    }


def _apply(rule: Rule, output: dict[str, Any], minimum: int, withheld: dict[str, int]) -> None:
    for parent, key, where in list(_containers(output, rule.path, "")):
        node = parent.get(key)
        if isinstance(rule, Whole):
            if isinstance(node, dict) and any(_small(node.get(name), minimum) for name in rule.counts):
                parent[key] = None
                withheld[where] = 1
        elif isinstance(rule, Groups):
            if not isinstance(node, list):
                continue
            sizes = [_counts(item, rule.counts) for item in node]
            hidden = {i for i, size in enumerate(sizes) if any(_small(v, minimum) for v in size.values())}
            if rule.additive:
                hidden = _complement(sizes, hidden, rule.additive, minimum)
            if hidden:
                parent[key] = [item for i, item in enumerate(node) if i not in hidden]
                withheld[where] = len(hidden)
        elif isinstance(node, dict):  # CountMap
            names = sorted(node)
            sizes = [{"rows": int(node[name])} if isinstance(node[name], int) else {} for name in names]
            hidden = {i for i, size in enumerate(sizes) if _small(size.get("rows"), minimum)}
            hidden = _complement(sizes, hidden, ("rows",), minimum)
            if hidden:
                parent[key] = {name: node[name] for i, name in enumerate(names) if i not in hidden}
                withheld[where] = len(hidden)


def suppress(
    tool: str, output: dict[str, Any], *, minimum: int = MIN_GROUP_ROWS
) -> tuple[dict[str, Any], dict[str, int]]:
    """`output` with every small group withheld, and what was withheld by path. The input is not changed."""
    result = copy.deepcopy(output)
    withheld: dict[str, int] = {}
    for rule in RULES.get(tool, ()):
        _apply(rule, result, minimum, withheld)
    return result, withheld


def widen(tool: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    """The arguments to call with (the narrowing argument removed) and the value it asked for (None if none)."""
    rule = NARROWING.get(tool)
    if rule is None or arguments.get(rule.argument) is None:
        return arguments, None
    wide = {name: value for name, value in arguments.items() if name != rule.argument}
    return wide, arguments[rule.argument]


def narrow(tool: str, output: dict[str, Any], wanted: Any) -> dict[str, Any]:
    """The suppressed full answer narrowed to `wanted`, shaped as the tool shapes a narrowed answer.

    A value that is not there and a value that was withheld are answered alike, so the answer says nothing
    about which it was.
    """
    rule = NARROWING[tool]
    result = dict(output)
    items = result.get(rule.list_key)
    kept = [item for item in items if item.get(rule.item_key) == wanted] if isinstance(items, list) else []
    result[rule.list_key] = kept
    for name in rule.cleared:
        result[name] = None
    if isinstance(items, list) and not kept and result.get("reason") is None:
        result["reason"] = (
            f"No {rule.item_key} of that name is shown: there is none, or it holds fewer than "
            f"{MIN_GROUP_ROWS} customers."
        )
    return result
