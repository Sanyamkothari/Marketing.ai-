"""The pre-flight checker, for a client analyst to run on their own laptop (Plan E M59).

    python -m scripts.preflight <folder> [--use-case telco-churn] [--out report.html]

Reads every CSV and Parquet file in the folder, checks them against the pilot's data request and
writes one HTML page (default: `preflight_report.html` in the folder). Nothing is uploaded and no
network connection is made. The exit code is 0 when nothing blocks the files, 1 when a problem
does, and 2 when the command itself was used wrongly - so a script that prepares an extract can
stop before sending a broken one.

What it needs: Python 3.11 with pandas, pyarrow, pydantic and PyYAML, and this repository's
`engine/` and `configs/` - the pilot kit (`make pilot-kit`) is exactly those, with a
`requirements-preflight.txt` pinning the four libraries. AutoGluon and the rest of the platform are not needed:
`tests/unit/pilot/test_preflight.py` fails if running this imports them.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

COMMAND: str = "python -m scripts.preflight"
REPORT_FILENAME: str = "preflight_report.html"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=COMMAND,
        description="Check a folder of files against the pilot's data request, on this computer only.",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="a folder of files, or the files themselves")
    parser.add_argument(
        "--use-case",
        action="append",
        dest="use_cases",
        default=None,
        help="use case the files are for; repeat for several (default: the pilot's two)",
    )
    parser.add_argument(
        "--out", type=Path, default=None, help=f"where to write the page (default: {REPORT_FILENAME})"
    )
    parser.add_argument(
        "--configs", type=Path, default=None, help="configuration root (default: the kit's configs/)"
    )
    args = parser.parse_args(argv)

    missing = [str(path) for path in args.paths if not path.exists()]
    if missing:
        print(f"Not found: {', '.join(missing)}", file=sys.stderr)
        return 2

    from engine.config import ConfigError
    from engine.pilot.document import render_html
    from engine.pilot.preflight import preflight_document, run_preflight

    folder = args.paths[0] if args.paths[0].is_dir() else args.paths[0].parent
    out: Path = args.out if args.out is not None else folder / REPORT_FILENAME
    if not out.parent.is_dir():
        print(f"Folder not found for --out: {out.parent}", file=sys.stderr)
        return 2
    use_cases = tuple(args.use_cases) if args.use_cases else None
    try:
        result = run_preflight(list(args.paths), use_cases, root=args.configs)
    except ConfigError as exc:
        print(f"Cannot run the check: {exc.message}", file=sys.stderr)
        return 2
    out.write_text(render_html(preflight_document(result, args.configs)), encoding="utf-8")

    errors = [f for f in result.findings if f.severity == "error"]
    warnings = [f for f in result.findings if f.severity == "warning"]
    print(f"Checked {len(result.tables)} file(s): {len(errors)} problem(s), {len(warnings)} warning(s).")
    for finding in errors:
        print(f"  problem: {finding.code} {finding.file or ''}".rstrip())
    print(f"Report: {out}")
    return 1 if errors else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
