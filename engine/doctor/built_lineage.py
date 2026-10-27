"""Which columns of a table a query builds read a personal column: the personal-data mark follows the data, not the name.

A query can give a personal column any name (``SELECT MAX(full_name) AS who``), so a mark carried by name alone would
let it through the gate. :func:`personal_outputs` reads the query with DuckDB's own parser and marks every output
column that reads, however deep, a column that is personal; it fails closed: a query that cannot be read marks every
column with every kind found.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["personal_outputs"]


def _walk(node: Any) -> Any:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _tree(sql: str) -> dict[str, Any] | None:
    from engine.agent.sql_step import _connect

    con = _connect()
    try:
        tree = json.loads(con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    except Exception:
        return None
    finally:
        con.close()
    return tree if isinstance(tree, dict) and not tree.get("error") else None


def _all_kinds(personal: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(k for kinds in personal.values() for k in kinds))


def personal_outputs(
    sql: str, columns: Sequence[str], personal: Mapping[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    """`{column: kinds}` for the output `columns` that are personal: by name, or because the expression behind them reads
    a personal column (through aliases and sub-selects too). `personal` is `{source column: kinds}`."""
    if not personal:
        return {}
    marked = {name.casefold(): kinds for name, kinds in personal.items()}
    tree = _tree(sql)
    if tree is None:
        everything = _all_kinds(personal)
        return dict.fromkeys(columns, everything)
    top = (tree.get("statements") or [{}])[0].get("node") or {}
    top_list = top.get("select_list") if top.get("type") == "SELECT_NODE" else None
    selects = [n for n in _walk(tree) if isinstance(n.get("select_list"), list)]
    positional = (
        isinstance(top_list, list)
        and len(top_list) == len(columns)
        and not any(i.get("type") == "STAR" for i in top_list)
    )
    unnamed: tuple[str, ...] = ()
    for _ in range(len(selects) * 4 + 4):  # an alias read by a later select: until nothing is added
        grew = False
        for select in selects:
            for position, item in enumerate(select["select_list"]):
                reads = [
                    str(n["column_names"][-1]).casefold()
                    for n in _walk(item)
                    if n.get("type") == "COLUMN_REF" and n.get("column_names")
                ]
                kinds = tuple(dict.fromkeys(k for r in reads for k in marked.get(r, ())))
                if not kinds:
                    continue
                if item.get("alias"):
                    name = str(item["alias"]).casefold()
                elif item.get("type") == "COLUMN_REF":
                    name = str(item["column_names"][-1]).casefold()
                elif select is top and positional:
                    name = columns[position].casefold()
                else:  # an expression the engine names itself, out of reach of its name: fail closed
                    unnamed = tuple(dict.fromkeys((*unnamed, *kinds)))
                    continue
                merged = tuple(dict.fromkeys((*marked.get(name, ()), *kinds)))
                if merged != marked.get(name):
                    marked[name] = merged
                    grew = True
        if not grew:
            break
    return {
        c: tuple(dict.fromkeys((*marked.get(c.casefold(), ()), *unnamed)))
        for c in columns
        if marked.get(c.casefold()) or unnamed
    }
