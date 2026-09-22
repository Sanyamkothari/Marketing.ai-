"""Print the requirement lines for a set of optional-dependency groups, one per line.

The image installs third-party wheels from `[project].dependencies` (plus whichever extras it wants)
with `requirements-freeze.txt` as a pip **constraints** file, so the versions in the image are the
repository's pinned versions rather than a second, independent resolution. Doing that needs the
dependency list as a file, and the list lives in `pyproject.toml`.

A three-line `python -c` inside a `RUN` would do the same thing, which is exactly why this is a
module instead: a script in the repository can be read, reviewed and unit-tested, and a string
inside a Dockerfile cannot.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Final

COMMAND: Final[str] = "python -m scripts.requirements_from_pyproject"
DEFAULT_PYPROJECT: Final[Path] = Path("pyproject.toml")


class UnknownExtraError(Exception):
    """A named optional-dependency group does not exist in `pyproject.toml`."""


def requirements(document: str, *, extras: Sequence[str] = ()) -> tuple[str, ...]:
    """`[project].dependencies`, then each named extra, in the order asked for.

    An unknown extra is an error rather than an empty list: a typo in a build argument would
    otherwise produce an image quietly missing a whole dependency group, and the first sign of it
    would be an ImportError inside a running container.
    """
    project = tomllib.loads(document)["project"]
    lines: list[str] = list(project["dependencies"])
    available = project.get("optional-dependencies", {})
    for extra in extras:
        name = extra.strip()
        if not name:
            continue
        if name not in available:
            raise UnknownExtraError(
                f"{name!r} is not an optional-dependency group; known groups are "
                f"{', '.join(sorted(available))}."
            )
        lines.extend(available[name])
    return tuple(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """`--extras aws,dev` (comma-separated, may be empty) and `--pyproject`."""
    parser = argparse.ArgumentParser(prog=COMMAND, description=__doc__)
    parser.add_argument("--extras", default="", help="comma-separated optional-dependency groups")
    parser.add_argument("--pyproject", type=Path, default=DEFAULT_PYPROJECT)
    args = parser.parse_args(argv)
    path: Path = args.pyproject
    try:
        lines = requirements(path.read_text(encoding="utf-8"), extras=str(args.extras).split(","))
    except (UnknownExtraError, KeyError, OSError) as exc:
        print(f"{COMMAND}: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
