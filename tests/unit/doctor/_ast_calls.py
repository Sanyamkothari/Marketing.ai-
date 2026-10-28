"""The names a module calls, with import aliases and simple name aliases resolved (architecture tests)."""

from __future__ import annotations

import ast
from pathlib import Path


def _aliases(tree: ast.AST) -> dict[str, str]:
    """`from m import run_recipe as go` -> {"go": "run_recipe"}; `again = go` -> {"again": "run_recipe"}."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            for item in node.names:
                if item.asname:
                    names[item.asname] = item.name.rsplit(".", 1)[-1]
    for _ in range(3):  # `a = b` chains
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Name | ast.Attribute)
            ):
                value = node.value
                source = value.id if isinstance(value, ast.Name) else value.attr
                names[node.targets[0].id] = names.get(source, source)
    return names


def called_names(node: ast.AST, aliases: dict[str, str] | None = None) -> set[str]:
    """Every function or method name called under `node`, as the original name when it was imported under another."""
    known = _aliases(node) if aliases is None else aliases
    found: set[str] = set()
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            found.add(known.get(name, name))
    return found


def module_calls(path: Path) -> set[str]:
    return called_names(ast.parse(path.read_text(encoding="utf-8")))


def function_calls(path: Path, function: str) -> set[str]:
    """The names called inside the one function `function` of the module at `path` (empty when it is not there)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = _aliases(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            found |= called_names(node, aliases)
    return found
