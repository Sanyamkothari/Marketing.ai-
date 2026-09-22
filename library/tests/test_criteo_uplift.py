"""Criteo Uplift: the dataset that was deliberately not run, checked as such.

There is nothing to train here - see ../criteo-uplift/README.md and run_report.md for the two
independent reasons - so this module asserts the *absence* is intact rather than accidental. If
somebody later commits a sample, installs the use case, or drops the non-commercial warning, one
of these fails and the library stops quietly implying a run that never happened.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from conftest import LIBRARY, LIBRARY_CONFIGS

DATASET: Path = LIBRARY / "criteo-uplift"


def test_the_paperwork_is_all_there() -> None:
    for name in ("README.md", "LICENSE.txt", "fetch.py", "mapping.yaml", "use_case.yaml", "run_report.md"):
        assert (DATASET / name).is_file(), f"library/criteo-uplift/{name} is missing"


def test_no_data_and_no_sample_are_committed() -> None:
    """No byte of this dataset was ever fetched; nothing derived from it may appear here."""
    assert not (DATASET / "sample.csv").exists(), (
        "a sample.csv appeared for a dataset that was never downloaded - "
        "if it is now available, replace run_report.md with a real run"
    )


def test_the_use_case_is_drafted_and_not_installed() -> None:
    """Installing it would let the engine train a propensity model and call it uplift."""
    draft = yaml.safe_load((DATASET / "use_case.yaml").read_text(encoding="utf-8"))
    assert draft["id"] == "criteo-uplift"
    assert not (LIBRARY_CONFIGS / "use_cases" / "criteo_uplift.yaml").exists()


def test_the_industry_file_lists_it_as_planned() -> None:
    industry = yaml.safe_load((LIBRARY_CONFIGS / "industries" / "ad_tech.yaml").read_text(encoding="utf-8"))
    refs = [ref for stage in industry["stages"] for ref in stage["use_cases"]]
    criteo = next(ref for ref in refs if ref["id"] == "criteo-uplift")
    assert criteo["status"] == "planned"
    assert criteo["name"] and criteo["description"], "a planned entry carries its own copy"


def test_exposure_is_excluded_rather_than_used_as_a_feature() -> None:
    """`exposure` is an outcome of the auction, not the randomised lever. Using it is the trap."""
    draft = yaml.safe_load((DATASET / "use_case.yaml").read_text(encoding="utf-8"))
    assert "exposure" in draft["prepare"]["exclude_columns"]
    mapping = yaml.safe_load((DATASET / "mapping.yaml").read_text(encoding="utf-8"))
    exposure = next(c for c in mapping["columns"] if c["standard"] == "exposure")
    assert exposure["role"] == "excluded"


def test_the_non_commercial_licence_is_stated_where_it_will_be_seen() -> None:
    licence = (DATASET / "LICENSE.txt").read_text(encoding="utf-8")
    assert "CC BY-NC-SA 4.0" in licence
    assert "NonCommercial" in licence or "non-commercial" in licence.lower()
    demo = (LIBRARY / "DEMO_SCRIPT.md").read_text(encoding="utf-8")
    assert "do not demo" in demo.lower(), "the demo script must keep warning people off it"


def test_the_files_say_no_run_happened() -> None:
    """The claim has to survive somebody skimming. Both files state it in their opening lines."""
    fetch = (DATASET / "fetch.py").read_text(encoding="utf-8")
    assert "never been run" in fetch, "fetch.py must say it has never been run"

    report = (DATASET / "run_report.md").read_text(encoding="utf-8")
    opening = report.split("\n\n", 2)[1]
    assert "No run happened" in opening, "run_report.md must open by saying no run happened"
