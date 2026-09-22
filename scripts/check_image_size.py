"""Report the built image's size and fail when it exceeds the budget.

Phase 4a sets a ceiling of 4 GiB, because that is the size at which a Fargate task's image pull and
a SageMaker job's start stop being background noise. The ceiling is a BUDGET - a decision about what
is acceptable - and not a measurement of anything; the actual size is measured here, printed, and is
the only size figure anyone should quote.

`docker image inspect` reports `Size`, the sum of the image's own layers. That is the number a
registry stores and a node downloads, so it is the one compared against the budget; the "virtual"
figure some tools print counts shared base layers twice.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from typing import Final

COMMAND: Final[str] = "python -m scripts.check_image_size"
DEFAULT_REF: Final[str] = "marketing-ai:local"
DEFAULT_MAX_BYTES: Final[int] = 4 * 1024**3
"""4 GiB, chosen (plan section 5) rather than measured; `--max-bytes` overrides it."""

GIB: Final[float] = float(1024**3)


class ImageSizeError(Exception):
    """The image could not be inspected."""


def image_size_bytes(ref: str, *, runner: object = None) -> int:
    """`docker image inspect <ref>` -> the image's own size in bytes."""
    run = subprocess.run if runner is None else runner  # type: ignore[assignment]
    try:
        completed = run(  # type: ignore[operator]
            ["docker", "image", "inspect", ref, "--format", "{{json .Size}}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ImageSizeError(f"Could not run docker: {type(exc).__name__}.") from exc
    if completed.returncode != 0:
        raise ImageSizeError(f"No such image {ref!r}. Build it first (`make image`).")
    try:
        return int(json.loads(completed.stdout.strip()))
    except (ValueError, TypeError) as exc:
        raise ImageSizeError(f"docker reported a size that is not a number for {ref!r}.") from exc


def verdict(size: int, *, max_bytes: int, ref: str) -> tuple[str, int]:
    """The line to print and the exit code, so the decision is testable without docker."""
    share = size / max_bytes * 100.0
    measured = (
        f"{ref} is {size / GIB:.3f} GiB ({size} bytes), {share:.1f}% of the {max_bytes / GIB:.0f} GiB budget"
    )
    if size > max_bytes:
        return f"{measured} - OVER BUDGET", 1
    return measured, 0


def main(argv: Sequence[str] | None = None) -> int:
    """`--ref` and `--max-bytes`; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog=COMMAND, description="Measure the image and compare it to the budget."
    )
    parser.add_argument("--ref", default=DEFAULT_REF, help=f"image reference (default: {DEFAULT_REF})")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="budget in bytes")
    args = parser.parse_args(argv)
    try:
        size = image_size_bytes(str(args.ref))
    except ImageSizeError as exc:
        print(f"{COMMAND}: {exc}", file=sys.stderr)
        return 1
    line, code = verdict(size, max_bytes=int(args.max_bytes), ref=str(args.ref))
    print(line, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
