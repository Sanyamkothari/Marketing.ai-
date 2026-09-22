"""Criteo Uplift: the dataset that was deliberately not run, checked as such.

There is nothing to train here - see ../criteo-uplift/README.md and run_report.md for the two
independent reasons - so this module asserts the *absence* is intact rather than accidental. If
somebody later commits a sample, installs the use case, or drops the non-commercial warning, one
of these fails and the library stops quietly implying a run that never happened.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
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


# ---------------------------------------------------------------------------
# The sampler, exercised on a synthetic file
# ---------------------------------------------------------------------------
# `fetch.py` has never been run against the real dataset - the host is blocked here - which is
# exactly how it came to carry a docstring describing behaviour its code did not have. These tests
# build a small file with the published schema and run `prepare()` over it, so the claims in
# fetch.py, mapping.yaml, README.md and run_report.md are checked by something rather than by
# nobody. They do NOT need the real download.
def _criteo_fetch() -> ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location("criteo_fetch", DATASET / "fetch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _synthetic_file(module: ModuleType, tmp_path: Path, rows: int) -> tuple[Any, Path]:
    """A `rows`-row gzip with the published schema, deliberately skewed 95/5 on treatment."""
    import gzip

    import pandas as pd

    frame = pd.DataFrame(
        {
            **{name: [i * 0.001 for i in range(rows)] for name in module.FEATURES},
            "treatment": [1 if i % 20 else 0 for i in range(rows)],
            "conversion": [1 if i % 500 == 0 else 0 for i in range(rows)],
            "visit": [1 if i % 50 == 0 else 0 for i in range(rows)],
            "exposure": [1 if i % 7 == 0 else 0 for i in range(rows)],
        }
    )[list(module.EXPECTED_COLUMNS)]
    path = tmp_path / "synthetic.csv.gz"
    with gzip.open(path, "wt") as handle:
        frame.to_csv(handle, index=False)
    return frame, path


@pytest.fixture()
def prepared(tmp_path: Path) -> tuple[ModuleType, Any, Any]:
    """`prepare()` run over a 20,000-row synthetic file in ten chunks, sampled down to 5,000."""
    module = _criteo_fetch()
    frame, path = _synthetic_file(module, tmp_path, 20_000)
    module.RAW = path
    module.EXPECTED_ROWS = 20_000  # the share is computed from this
    module.CHUNK_ROWS = 2_000  # ten chunks
    return module, frame, module.prepare(5_000)


def test_the_sample_respects_its_ceiling_and_the_published_column_order(prepared: tuple) -> None:
    module, _, out = prepared
    assert len(out) <= 5_000
    assert list(out.columns) == [module.PRIMARY_KEY, *module.EXPECTED_COLUMNS]


def test_the_derived_key_is_the_row_number_in_the_published_order(prepared: tuple) -> None:
    """mapping.yaml calls impression_id "the 1-based row number in the published order"."""
    module, frame, out = prepared
    assert out[module.PRIMARY_KEY].is_unique
    assert out[module.PRIMARY_KEY].min() >= 1
    assert out[module.PRIMARY_KEY].max() <= len(frame)
    for _, row in out.head(50).iterrows():
        published = frame.iloc[int(row[module.PRIMARY_KEY]) - 1]
        assert published["f0"] == pytest.approx(row["f0"])
        assert published["treatment"] == row["treatment"]


def test_sampling_is_proportional_per_chunk_not_global(prepared: tuple) -> None:
    """The claim four documents make: every chunk contributes the same share of its rows.

    A global sample would leave this roughly even too, but only by luck of large numbers; per-chunk
    sampling makes it exact. Ten equal slices of the key range must each hold about a tenth.
    """
    import pandas as pd

    module, _frame, out = prepared
    per_slice = pd.cut(out[module.PRIMARY_KEY], bins=10, labels=False).value_counts().sort_index().tolist()
    assert len(per_slice) == 10
    expected = len(out) / 10
    for count in per_slice:
        assert abs(count - expected) <= 0.05 * expected, per_slice


def test_the_treated_control_ratio_survives_sampling(prepared: tuple) -> None:
    """README.md and run_report.md both promise the 85/15 split survives; here it is 95/5."""
    _, frame, out = prepared
    assert out["treatment"].mean() == pytest.approx(frame["treatment"].mean(), abs=0.01)


def test_a_file_smaller_than_the_ceiling_is_kept_whole(tmp_path: Path) -> None:
    module = _criteo_fetch()
    frame, path = _synthetic_file(module, tmp_path, 3_000)
    module.RAW = path
    module.EXPECTED_ROWS = 3_000
    module.CHUNK_ROWS = 1_000
    out = module.prepare(5_000)
    assert len(out) == len(frame)


def test_verify_refuses_a_file_whose_schema_moved() -> None:
    """The guard that exists because nobody here could check the download against its docs."""
    import pandas as pd

    module = _criteo_fetch()
    with pytest.raises(SystemExit):
        module.verify(pd.DataFrame({"f0": [1.0], "treatment": [1]}))
