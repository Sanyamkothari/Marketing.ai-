"""Every detail a route puts on its audit event is a key the event accepts.

`AuditEvent.details` is closed (`engine.audit.events.DETAIL_KEYS`): a route that passes another key makes the event
fail to build, so the request goes unaudited and the server logs "audit event could not be written". Found by reading
the routes, so a new route with a key of its own fails here, not in a log.
"""

from __future__ import annotations

import ast
from pathlib import Path

from engine.audit.events import DETAIL_KEYS

API = Path(__file__).resolve().parents[2] / "api"


def _detail_keys(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or getattr(node.func, "id", getattr(node.func, "attr", "")) != "set_audit_context"
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg == "details" and isinstance(keyword.value, ast.Dict):
                found += [
                    (key.lineno, str(key.value))
                    for key in keyword.value.keys
                    if isinstance(key, ast.Constant)
                ]
    return found


def test_every_audit_detail_a_route_sets_is_an_allowed_key() -> None:
    wrong = [
        f"{path.relative_to(API.parent)}:{line} {key}"
        for path in sorted(API.rglob("*.py"))
        for line, key in _detail_keys(ast.parse(path.read_text(encoding="utf-8")))
        if key not in DETAIL_KEYS
    ]
    assert not wrong, wrong
