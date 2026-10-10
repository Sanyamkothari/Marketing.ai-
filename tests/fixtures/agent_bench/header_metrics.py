"""Top-1 and top-3 accuracy of the header rules alone on the Hinglish cases (Plan K M142, DEC-1409).

`python -m tests.fixtures.agent_bench.header_metrics` prints the numbers; `--misses` lists each miss.
The rules are `engine.agent.headers.rank_roles`: no model is involved, no cell is read, only the
header and the profile's counts.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

from engine.agent.headers import ROLES, rank_roles
from tests.fixtures.agent_bench.make_hinglish import CASES, SPLIT, CaseSpec, hinglish_frame
from tests.unit.agent.helpers import context_for

__all__ = ["Miss", "Report", "accuracy"]


@dataclass(frozen=True)
class Miss:
    case: str
    role: str
    expected: str
    found: tuple[str, ...]


@dataclass(frozen=True)
class Report:
    """What the rules got right. A role a file does not have is scored apart (`false_suggestions`)."""

    asked: int
    top1: int
    top3: int
    absent: int
    false_suggestions: int
    misses: tuple[Miss, ...]
    wrong_top1: tuple[Miss, ...]

    @property
    def top1_rate(self) -> float:
        return self.top1 / self.asked if self.asked else 0.0

    @property
    def top3_rate(self) -> float:
        return self.top3 / self.asked if self.asked else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "roles_asked": self.asked,
            "top1": self.top1,
            "top3": self.top3,
            "top1_rate": round(self.top1_rate, 3),
            "top3_rate": round(self.top3_rate, 3),
            "roles_absent": self.absent,
            "false_suggestions": self.false_suggestions,
        }


def _ranked(spec: CaseSpec) -> dict[str, tuple[str, ...]]:
    ctx = context_for(hinglish_frame(spec))
    hints = ctx.config.agent.column_hints
    words = [
        w for w in (ctx.config.target.column, *hints.target_synonyms) if w
    ]  # as `find_roles` passes them
    found = rank_roles(ctx.profile.columns, rows=ctx.profile.row_count, extra_anchors={"outcome": words})
    return {role: tuple(c.column for c in found[role]) for role in ROLES}


def accuracy(split: str | None = None) -> Report:
    """Accuracy over every case, or over the `dev` / `held_out` ones."""
    asked = top1 = top3 = absent = false = 0
    misses: list[Miss] = []
    wrong: list[Miss] = []
    for spec in CASES:
        if split is not None and SPLIT[spec.name] != split:
            continue
        found = _ranked(spec)
        for role, header in spec.expected.items():
            names = found[role]
            if header is None:
                absent += 1
                false += 1 if names else 0
                continue
            asked += 1
            hit1 = bool(names) and names[0] == header
            hit3 = header in names[:3]
            top1 += hit1
            top3 += hit3
            if not hit3:
                misses.append(Miss(spec.name, role, header, names))
            elif not hit1:
                wrong.append(Miss(spec.name, role, header, names))
    return Report(asked, top1, top3, absent, false, tuple(misses), tuple(wrong))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--misses", action="store_true", help="list every miss and every wrong first pick")
    args = parser.parse_args()
    out: dict[str, Any] = {}
    for split in (None, "dev", "held_out"):
        report = accuracy(split)
        out[split or "all"] = report.as_dict()
        if args.misses:
            for miss in report.misses:
                print(
                    f"[{split or 'all'}] MISS   {miss.case} {miss.role}: want {miss.expected!r}, got {miss.found}"
                )
            for miss in report.wrong_top1:
                print(
                    f"[{split or 'all'}] TOP3   {miss.case} {miss.role}: want {miss.expected!r}, got {miss.found}"
                )
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
