"""The agent's guides (`configs/doctor/guides/*.md`), read on demand by the action `read_guide` (Step 7b).

A guide holds the rules for one job (features, time and the as-of date, joins, ...). The system prompt carries only
the index (`configs/doctor/guides/index.md`, at most 150 words) and one line telling the model to read the guide for
a job before doing it; the guide itself comes back as a tool result, so the prompt stays under its word limit.

What the model gets back is the guide's `## Rules` plus the requested `## section`, or the whole guide when it names
no section. A guide is a file of rules written by us, never a client's data, so nothing here is masked; it is split
into lines (a line is at most `MAX_LINE_CHARS`, a guide at most `MAX_LINES`) because the prompt's tool results keep
at most that much of one text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

from engine.agent.tools import AgentToolError
from engine.agent.untrusted import MAX_PROMPT_LIST, MAX_TEXT_CHARS
from engine.config import config_root

__all__ = [
    "ALREADY_READ",
    "INDEX",
    "LEARNED",
    "LESSONS",
    "MAX_LINES",
    "MAX_LINE_CHARS",
    "RULES",
    "guide_names",
    "guide_sections",
    "index_text",
    "read_guide",
]

INDEX: Final[str] = "index"
RULES: Final[str] = "Rules"
LESSONS: Final[str] = "lessons"
LEARNED: Final[str] = "Learned"
MAX_LINE_CHARS: Final[int] = MAX_TEXT_CHARS
MAX_LINES: Final[int] = MAX_PROMPT_LIST - 1
ALREADY_READ: Final[str] = "already read above"
_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HEADING: Final = re.compile(r"^## (.+?)\s*$", flags=re.MULTILINE)


def _folder(root: Path | None) -> Path:
    return config_root(root) / "doctor" / "guides"


def guide_names(root: Path | None = None) -> tuple[str, ...]:
    """The guides that can be read (every file but the index), by name."""
    folder = _folder(root)
    return tuple(sorted(p.stem for p in folder.glob("*.md") if p.stem != INDEX)) if folder.is_dir() else ()


def index_text(root: Path | None = None) -> str:
    """The index's entries, for the prompts' `{{ guide_index }}` (the prompt has its own heading); empty when the
    repository ships none."""
    path = _folder(root) / f"{INDEX}.md"
    if not path.is_file():
        return ""
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- "))


def _sections(text: str) -> dict[str, str]:
    """Section title -> its text (heading line included), in file order."""
    found = list(_HEADING.finditer(text))
    return {
        m.group(1): text[m.start() : found[i + 1].start() if i + 1 < len(found) else len(text)].strip()
        for i, m in enumerate(found)
    }


def guide_sections(name: str, root: Path | None = None) -> tuple[str, ...]:
    """The section titles of one guide (the rules first)."""
    path = _folder(root) / f"{name}.md"
    return tuple(_sections(path.read_text(encoding="utf-8"))) if name in guide_names(root) else ()


def _lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def read_guide(
    name: object,
    section: object = None,
    root: Path | None = None,
    *,
    rules: bool = True,
    lessons: Sequence[str] = (),
) -> tuple[str, str | None, list[str]]:
    """`(name, section, lines)`: the guide's rules and the section asked for, or all of it when `section` is None.

    The `lessons` guide also holds `## Learned`: `lessons`, the owner-approved lessons the caller read from the store
    (`engine.doctor.lessons.approved_lessons`), one line each; with none, the section is left out.

    `rules=False` leaves the `## Rules` block out of a section read (the caller already sent it this turn). A
    section may be named by its title or by a unique start of it ("buckets").

    Raises `AgentToolError` (`AGENT_GUIDE_UNKNOWN`) naming the valid guides, or the valid sections of the guide.
    """
    names = guide_names(root)
    key = str(name or "").strip().lower().removesuffix(".md")
    if not _NAME.match(key) or key not in names:
        raise AgentToolError(
            "AGENT_GUIDE_UNKNOWN",
            f"There is no guide called {str(name or '')[:40]!r}. Guides: {', '.join(names) or 'none'}.",
        )
    text = (_folder(root) / f"{key}.md").read_text(encoding="utf-8").strip()
    if key == LESSONS and lessons:
        text += "\n\n## " + LEARNED + "\n" + "\n".join(f"- {item}" for item in lessons)
    if section is None or not str(section).strip():
        return key, None, _fit(_lines(text))
    sections = _sections(text)
    wanted = str(section).strip().lstrip("#").strip().lower().replace("_", " ")
    title = next((t for t in sections if t.lower() == wanted), None)
    if title is None:
        starts = [t for t in sections if t.lower().startswith(wanted)] if wanted else []
        title = starts[0] if len(starts) == 1 else None
    if title is None:
        raise AgentToolError(
            "AGENT_GUIDE_SECTION_UNKNOWN",
            f"The guide {key!r} has no section {str(section)[:40]!r}. Sections: {', '.join(sections)}.",
        )
    parts: Iterable[str] = [sections[RULES]] if rules and RULES in sections and title != RULES else []
    return key, title, _fit(_lines("\n".join([*parts, sections[title]])))


def _fit(lines: list[str]) -> list[str]:
    return [line[:MAX_LINE_CHARS] for line in lines][:MAX_LINES]
