"""Seed a store with the validated public dataset instead of planted data (Plan J M111, DEC-1321).

    python -m scripts.seed_validated [--data-dir DIR] [--csv PATH] [--fetch] [--force]

`scripts/seed_demo.py` (Plan E) seeds a synthetic "Demo Company" whose effect is planted. The manager demo does
not use it: it shows the product on **real randomised data**, the MineThatData e-mail test (Hillstrom, M110).
This command runs the whole M110 journey through the product's own API, in-process, into a store the app then
serves, so the screens a visitor clicks show artefacts the engine made on that file: the uploads, a risk model and
a campaign-effect model with their approval checks, a treat list with an offer on every row, and two campaigns
measured against the engine's own random control group, each with its Value Proof Pack.

**Which file it reads, in this order, and it always says which:**

1. `--csv PATH`: a prepared file you give it (the full file's `prepared.csv`).
2. `library/hillstrom-email/data/prepared.csv`, when `library/hillstrom-email/fetch.py` has been run: **the full
   file**, 64,000 customers. Its numbers are the validated ones in `library/hillstrom-email/DEMO_SUMMARY.md`.

Neither 1 nor 2 is called validated by where it came from: the file's SHA-256 is compared with the validated
digest (`scripts.demo_summary.VALIDATED_FILE_SHA256`). A file with any other digest is announced as UNVERIFIED, its
kind is `unverified`, and the summary written beside the store carries a notice in its first lines (it is read from
the digest the journey recorded, so a 64,000-row file that is not the validated one cannot pass for it).
3. With `--fetch`: it runs `fetch.py`, which downloads the file and refuses any copy whose SHA-256 is not the
   validated one. If that fails (no network, a blocked host) it says why and falls back to 4.
4. `library/hillstrom-email/sample.csv`: **a tenth of the file**, committed to git. The journey runs on it with the
   smaller arm floors a tenth of the conversions needs, and every screen works. Its numbers are *not* the
   validated results (the ranges are wide and the verdicts may fall differently); the summary written beside the
   store says so in its first lines, and so does this command.

**A clean store.** The journey starts from an empty store. The command refuses a store that already holds anything
unless `--force` is given, and then replaces it; it never touches a directory outside `DIR`.

When it finishes it prints the command that serves the store, the screens to open (`DEMO_SCRIPT.md` has the
clicks), and where the generated one-page summary is. A full-file run takes about two to three minutes on four
cores; the sample, less.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "COMMAND",
    "SAMPLE_RISK_OVERRIDES",
    "SAMPLE_UPLIFT_OVERRIDES",
    "SeedError",
    "SeedResult",
    "choose_source",
    "main",
    "seed",
    "sha256_of",
]

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
FOLDER: Final = REPO_ROOT / "library" / "hillstrom-email"
FULL_FILE: Final = FOLDER / "data" / "prepared.csv"
SAMPLE_FILE: Final = FOLDER / "sample.csv"
DEFAULT_DATA_DIR: Final = Path("data") / "validated-demo"
COMMAND: Final = "python -m scripts.seed_validated"
DATASET: Final = "hillstrom-email"

SAMPLE_RISK_OVERRIDES: Final[dict[str, Any]] = {"validation.min_positive": 20}
SAMPLE_UPLIFT_OVERRIDES: Final[dict[str, Any]] = {
    "validation.min_positive": 20,
    "uplift.min_arm_positives": 5,
    "uplift.min_arm_rows": 500,
    "uplift.bootstrap_samples": 50,
}
"""A tenth of the file has a tenth of the conversions: the floors the committed sample needs (the same the
library's own sample journey test uses)."""


class SeedError(RuntimeError):
    """The store could not be seeded; the message says which step and why."""


@dataclass
class SeedResult:
    """What a seed produced: where the store is, what it was made from, and the ids a demo opens."""

    store: Path
    """The data directory the app serves (`MARKETING_AI_DATA_DIR`)."""
    journey_dir: Path
    results_path: Path
    summary_path: Path | None
    source: Path
    source_kind: str
    """`given`, `full file`, `unverified` (a file whose SHA-256 is not the validated one) or `sample`."""
    rows: int
    is_sample: bool
    is_validated: bool = False
    """True only when the file's SHA-256 is the validated one; a sample is never validated."""
    campaign_ids: dict[str, str] = field(default_factory=dict)
    """Outcome measured (`conversion`, `spend`) -> campaign id."""
    notes: list[str] = field(default_factory=list)


def _load_fetch() -> Any:
    spec = importlib.util.spec_from_file_location("hillstrom_fetch", FOLDER / "fetch.py")
    if spec is None or spec.loader is None:
        raise SeedError(f"cannot load {FOLDER / 'fetch.py'}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def choose_source(
    csv: Path | None,
    *,
    fetch: bool,
    echo: Callable[[str], None],
    full_file: Path | None = None,
    sample_file: Path | None = None,
    fetcher: Callable[[], Any] | None = None,
) -> tuple[Path, str]:
    """`(path, kind)` of the file to seed from: the order is in the module docstring, and each fallback is announced."""
    full_file = full_file or FULL_FILE
    sample_file = sample_file or SAMPLE_FILE
    if csv is not None:
        if not csv.is_file():
            raise SeedError(f"{csv} is not a file")
        return csv.resolve(), "given"
    if full_file.is_file():
        return full_file, "full file"
    if fetch:
        echo("fetching the full file with library/hillstrom-email/fetch.py (verified by SHA-256) ...")
        try:
            (fetcher or (lambda: _load_fetch().main([])))()
        except (
            Exception
        ) as error:  # a blocked host, a bad checksum: say so and use the sample, never half a file
            echo(
                f"the full file could not be fetched ({type(error).__name__}: {error}); using the sample instead"
            )
        else:
            if full_file.is_file():
                return full_file, "full file"
    return sample_file, "sample"


def sha256_of(path: Path) -> str:
    """The SHA-256 of the file at `path`, read in blocks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _store_is_empty(directory: Path) -> bool:
    return not directory.exists() or not any(directory.iterdir())


def seed(
    *,
    data_dir: Path,
    csv: Path | None = None,
    fetch: bool = False,
    force: bool = False,
    config_root: Path | None = None,
    expected_sha256: str | None = None,
    echo: Callable[[str], None] = print,
) -> SeedResult:
    """Seed `data_dir` with the Hillstrom journey; see the module docstring."""
    from library.journey import HILLSTROM, run_journey

    config = config_root or REPO_ROOT / "configs"
    journey_dir = data_dir / DATASET / "journey"
    store = journey_dir / "data"
    if not _store_is_empty(store) and not force:
        raise SeedError(
            f"{store} already holds a store; --force replaces it (the journey always starts from an empty one)"
        )
    from scripts.demo_summary import VALIDATED_FILE_SHA256

    source, kind = choose_source(csv, fetch=fetch, echo=echo)
    sample = kind == "sample"
    validated = False
    if not sample:
        validated = sha256_of(source) == (expected_sha256 or VALIDATED_FILE_SHA256)
        if not validated:
            kind = "unverified"
            echo(
                f"UNVERIFIED: {source} does not have the validated SHA-256, so its numbers are not shown to be "
                "the validated results. The journey runs, and the summary says so; do not quote it. For the "
                "validated file: python library/hillstrom-email/fetch.py, then run "
                f"{COMMAND} --force again without --csv."
            )
    if sample:
        echo(
            "SAMPLE: no full file found, so this is the committed sample, a tenth of the file. Every screen works, "
            "but the numbers are NOT the validated results. For those: python library/hillstrom-email/fetch.py, "
            f"then run {COMMAND} --force again."
        )
    elif validated:
        echo(f"seeding from {source} ({kind}, SHA-256 verified)")
    echo("running the whole journey through the product's API; this takes a few minutes ...")
    outcome = run_journey(
        HILLSTROM,
        csv_path=source,
        config_root=config,
        runs_dir=data_dir,
        extra_risk_overrides=SAMPLE_RISK_OVERRIDES if sample else None,
        extra_uplift_overrides=SAMPLE_UPLIFT_OVERRIDES if sample else None,
    )
    results = outcome.results
    if source.is_relative_to(REPO_ROOT):  # the artefact names the file as a checkout spells it
        results["csv"] = source.relative_to(REPO_ROOT).as_posix()
    results_path = outcome.directory / "journey.results.json"
    results_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    campaigns = results["steps"].get("campaigns", {})
    unverified = [
        name for name, item in campaigns.items() if not item.get("proof", {}).get("provenance_verified")
    ]
    if not campaigns or unverified:
        raise SeedError(
            "the journey ran but did not leave a verified Value Proof Pack for "
            + (", ".join(unverified) if unverified else "any campaign")
        )
    result = SeedResult(
        store=store,
        journey_dir=outcome.directory,
        results_path=results_path,
        summary_path=None,
        source=source,
        source_kind=kind,
        rows=int(results["rows"]),
        is_sample=sample,
        is_validated=validated,
        campaign_ids={name: str(item["campaign_id"]) for name, item in campaigns.items()},
    )
    from scripts import demo_summary

    summary_path = outcome.directory / "DEMO_SUMMARY.md"
    if demo_summary.main(["--results", str(results_path), "--out", str(summary_path)], echo=echo) == 0:
        result.summary_path = summary_path
    else:
        result.notes.append(
            "the one-page summary could not be generated from this run (see the message above)"
        )
    return result


def describe(result: SeedResult, *, port: int = 8000) -> str:
    """What to do next: the command that serves the store, the screens to open, and what the numbers are."""
    base = f"http://localhost:{port}/ui"
    lines = [
        "",
        f"Seeded {result.store}",
        f"  made from  {result.source} ({result.source_kind}, {result.rows:,} customers)",
        "",
        "Serve it:",
        f"  MARKETING_AI_DATA_DIR={result.store} .venv/bin/uvicorn api.main:app --port {port}",
        "",
        "Screens (see library/DEMO_SCRIPT.md for the clicks):",
        f"  Results                  {base}#/results",
    ]
    for outcome, campaign_id in result.campaign_ids.items():
        lines.append(f"  Campaign ({outcome})  {base}#/campaigns/{campaign_id}")
        lines.append(f"  Value Proof Pack ({outcome})  {base}#/pilot/proof/{campaign_id}")
    if result.summary_path is not None:
        lines.append(f"  One-page summary         {result.summary_path}")
    if result.is_sample:
        lines += [
            "",
            "These are the numbers of the SAMPLE, not the validated results. Quote only library/hillstrom-email/"
            "DEMO_SUMMARY.md, which is from the full file.",
        ]
    elif not result.is_validated:
        lines += [
            "",
            "UNVERIFIED: the file is not the validated one (its SHA-256 differs), so these numbers are not shown to "
            "be the validated results. Quote only library/hillstrom-email/DEMO_SUMMARY.md.",
        ]
    else:
        lines += [
            "",
            "These should equal library/hillstrom-email/DEMO_SUMMARY.md: compare the generated summary with "
            "`diff` before a demo.",
        ]
    lines += [f"  note: {note}" for note in result.notes]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, echo: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(
        prog=COMMAND, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"where the store goes, under <dir>/{DATASET}/journey/data (default {DEFAULT_DATA_DIR})",
    )
    parser.add_argument("--csv", type=Path, default=None, help="a prepared file to seed from")
    parser.add_argument(
        "--fetch", action="store_true", help="run fetch.py for the full file when it is absent"
    )
    parser.add_argument("--force", action="store_true", help="replace a store that already holds something")
    parser.add_argument("--port", type=int, default=8000, help="the port the printed serve command uses")
    args = parser.parse_args(argv)
    try:
        result = seed(data_dir=args.data_dir, csv=args.csv, fetch=args.fetch, force=args.force, echo=echo)
    except SeedError as error:
        echo(f"not seeded: {error}")
        return 2 if "already holds" in str(error) else 1
    echo(describe(result, port=args.port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
