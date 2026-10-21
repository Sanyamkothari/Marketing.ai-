"""Pre-registered targets: `docs/v2/TARGETS.json` and a mechanical check of them.

    python -m scripts.v2_realbench.targets                       # validate the file, list every claim
    python -m scripts.v2_realbench.targets --base-dir OUT        # judge every claim whose report exists in OUT
    python -m scripts.v2_realbench.targets --base-dir OUT --write   # also set each claim's `passes`

Each claim is `{id, claim, command, threshold, passes}`. `passes` is `null` until the command has been
run and the threshold judged, then `true` or `false`; it is never edited by hand to flip a result.

A threshold is either text for a person (for example "exit status 0") or a machine-checkable rule:

    <report file> :: <dotted.path.in.the.report> <op> <literal>

with `op` one of `==`, `!=`, `>=`, `<=`, `>`, `<` and the literal `true`, `false`, `null` or a
number. The report file is relative to `--base-dir`. A claim whose report file is missing stays
`null` (not run); a path missing from an existing report is a FAIL (the report did not measure it).

The claims were written before the numbers they judge (the first time the file was committed, nothing
had been run), and the tolerance in the bench claims is fixed here, not tuned.
"""

from __future__ import annotations

import argparse
import json
import operator
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

DEFAULT_TARGETS = Path(__file__).resolve().parents[2] / "docs" / "v2" / "TARGETS.json"
KEYS = ("id", "claim", "command", "threshold", "passes")
RULE = re.compile(r"^(?P<file>\S+)\s*::\s*(?P<path>[\w.\[\]-]+)\s*(?P<op>==|!=|>=|<=|>|<)\s*(?P<value>\S+)$")
OPS: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}
_MISSING = object()


def load_targets(path: Path = DEFAULT_TARGETS) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a list of claims")
    return [dict(x) for x in data]


def validate(targets: Sequence[dict[str, Any]]) -> list[str]:
    """Shape problems: exactly the five keys, unique ids, text where text is expected, a parseable
    machine rule where the threshold has a `::`."""
    problems: list[str] = []
    seen: set[str] = set()
    for i, t in enumerate(targets):
        where = f"claim #{i} ({t.get('id', '?')})"
        if tuple(t.keys()) != KEYS:
            problems.append(f"{where}: keys must be exactly {KEYS}, in that order; got {tuple(t.keys())}")
            continue
        for k in ("id", "claim", "command", "threshold"):
            if not isinstance(t[k], str) or not t[k].strip():
                problems.append(f"{where}: {k} must be a non-empty string")
        if t["passes"] not in (None, True, False):
            problems.append(f"{where}: passes must be null, true or false")
        if t["id"] in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(t["id"])
        if isinstance(t["threshold"], str) and "::" in t["threshold"] and not RULE.match(t["threshold"]):
            problems.append(f"{where}: threshold {t['threshold']!r} is not '<file> :: <path> <op> <value>'")
    return problems


def _literal(text: str) -> Any:
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    return float(text)


def _lookup(report: Any, path: str) -> Any:
    cur = report
    for part in re.findall(r"[^.\[\]]+", path):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return _MISSING
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def judge(threshold: str, base_dir: Path) -> bool | None:
    """True / False for a machine rule whose report exists; None if it cannot be judged (a rule for a
    person, or the report file is missing)."""
    m = RULE.match(threshold)
    if m is None:
        return None
    report_file = base_dir / m["file"]
    if not report_file.exists():
        return None
    value = _lookup(json.loads(report_file.read_text()), m["path"])
    if value is _MISSING:
        return False
    want = _literal(m["value"])
    if value is None or isinstance(value, bool) or isinstance(want, bool) or want is None:
        return bool(OPS[m["op"]](value, want)) if m["op"] in ("==", "!=") else False
    return bool(OPS[m["op"]](float(value), float(want)))


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate and judge docs/v2/TARGETS.json.")
    ap.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    ap.add_argument("--base-dir", type=Path, help="where the commands wrote their reports")
    ap.add_argument("--write", action="store_true", help="store each judged claim's result in `passes`")
    args = ap.parse_args(argv)
    targets = load_targets(args.targets)
    problems = validate(targets)
    for p in problems:
        print("INVALID " + p)
    if problems:
        return 1
    judged = 0
    for t in targets:
        result = judge(t["threshold"], args.base_dir) if args.base_dir else None
        if result is not None:
            judged += 1
            if args.write:
                t["passes"] = result
        state = {True: "PASS", False: "FAIL", None: "not run"}[result if args.base_dir else t["passes"]]
        print(f"[{state}] {t['id']}: {t['claim']}")
    if args.write:
        args.targets.write_text(json.dumps(targets, indent=2) + "\n")
    print(f"\n{len(targets)} claims, {judged} judged now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
