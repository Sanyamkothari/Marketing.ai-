"""Build `dist/pilot-kit.zip`: what a client's analyst needs, and nothing more (Plan E M59).

    python -m scripts.build_pilot_kit [--out dist/pilot-kit.zip]

The kit holds the data request and its templates, the pre-flight checker, the parts of `engine/`
and `configs/` it runs on, and a README. The checker imports none of AutoGluon, scikit-learn,
FastAPI or DuckDB (`tests/unit/pilot/test_preflight.py` proves it), so the kit needs only a Python
3.11 with the four libraries in `requirements-preflight.txt`. The whole `engine/` package is
included rather than a hand-picked subset: its modules import each other lazily, and a subset that
misses one would fail on the client's laptop, not here. No data, no model and no credential is in it.
"""

from __future__ import annotations

import argparse
import zipfile
from collections.abc import Sequence
from pathlib import Path

COMMAND = "python -m scripts.build_pilot_kit"
ROOT = Path(__file__).resolve().parent.parent

REQUIREMENTS = """# The pre-flight checker's own needs, pinned to the versions the platform runs.
pandas==2.3.3
pyarrow==24.0.0
pydantic==2.13.5
PyYAML==6.0.3
"""

README = """# Pilot kit

1. Read `docs/pilot/DATA_REQUEST.md`: which files to export, which columns, how much history,
   how to pseudonymise the customer ID, and what never to send.
2. Put the files in one folder.
3. Install Python 3.11, then: `pip install -r requirements-preflight.txt`
4. From this folder: `python -m scripts.preflight <your folder> --use-case telco-churn`
5. Open `preflight_report.html` (written into your folder). Fix anything marked Problem and run it again.

The check runs on your computer only. Nothing is uploaded and no network connection is made.
"""

INCLUDE_DIRS = ("engine", "configs")
INCLUDE_FILES = (
    "scripts/__init__.py",
    "scripts/preflight.py",
    "docs/pilot/DATA_REQUEST.md",
)


def _members(root: Path) -> list[Path]:
    members: list[Path] = []
    for directory in INCLUDE_DIRS:
        members += sorted(
            path
            for path in (root / directory).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        )
    members += [root / name for name in INCLUDE_FILES]
    members += sorted((root / "docs" / "pilot" / "templates").glob("*.csv"))
    return members


def build(out: Path, root: Path = ROOT) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in _members(root):
            archive.write(path, f"pilot-kit/{path.relative_to(root).as_posix()}")
        archive.writestr("pilot-kit/requirements-preflight.txt", REQUIREMENTS)
        archive.writestr("pilot-kit/README.md", README)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=COMMAND, description="Build the client's pilot kit.")
    parser.add_argument("--out", type=Path, default=Path("dist/pilot-kit.zip"))
    args = parser.parse_args(argv)
    path = build(args.out)
    print(f"Pilot kit: {path} ({path.stat().st_size / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
