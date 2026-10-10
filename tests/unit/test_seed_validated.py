"""The validated-data seed command (Plan J M111, DEC-1321): which file it reads, what it refuses, what it prints.

The fast tests replace `library.journey.run_journey` with one that returns the committed full-file results, so
they check the command's own decisions (the order of sources, the sample floors, the refusal of a store that is not
empty, the verified Packs, the summary written beside the store) without training anything. The one test that does
run the whole journey, on the committed sample into a clean store, and then serves that store through the app, is
marked slow and integration like the library's own journey test.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import library.journey as journey_module
import pytest

from scripts import seed_validated as seed_module
from scripts.seed_validated import SeedError, choose_source, seed

FOLDER = Path(__file__).resolve().parents[2] / "library" / "hillstrom-email"
COMMITTED = json.loads((FOLDER / "journey.results.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Which file it reads
# ---------------------------------------------------------------------------
def test_a_file_given_is_used_and_a_missing_one_is_refused(tmp_path: Path) -> None:
    given = tmp_path / "prepared.csv"
    given.write_text("customer_id\n1\n", encoding="utf-8")
    assert choose_source(given, fetch=False, echo=lambda _line: None) == (given.resolve(), "given")
    with pytest.raises(SeedError, match="is not a file"):
        choose_source(tmp_path / "nope.csv", fetch=False, echo=lambda _line: None)


def test_the_full_file_is_preferred_when_it_is_there(tmp_path: Path) -> None:
    full = tmp_path / "prepared.csv"
    full.write_text("customer_id\n1\n", encoding="utf-8")
    sample = tmp_path / "sample.csv"
    assert choose_source(None, fetch=False, echo=lambda _line: None, full_file=full, sample_file=sample) == (
        full,
        "full file",
    )


def test_without_the_full_file_the_committed_sample_is_used(tmp_path: Path) -> None:
    sample = tmp_path / "sample.csv"
    path, kind = choose_source(
        None, fetch=False, echo=lambda _line: None, full_file=tmp_path / "absent.csv", sample_file=sample
    )
    assert (path, kind) == (sample, "sample")


def test_fetch_that_fails_says_why_and_falls_back_to_the_sample(tmp_path: Path) -> None:
    said: list[str] = []

    def blocked() -> None:
        raise OSError("403 from the egress proxy")

    path, kind = choose_source(
        None,
        fetch=True,
        echo=said.append,
        full_file=tmp_path / "absent.csv",
        sample_file=tmp_path / "sample.csv",
        fetcher=blocked,
    )
    assert kind == "sample" and path.name == "sample.csv"
    assert any("403 from the egress proxy" in line and "using the sample" in line for line in said)


def test_fetch_that_works_gives_the_full_file(tmp_path: Path) -> None:
    full = tmp_path / "prepared.csv"

    def fetched() -> None:
        full.write_text("customer_id\n1\n", encoding="utf-8")

    path, kind = choose_source(
        None,
        fetch=True,
        echo=lambda _line: None,
        full_file=full,
        sample_file=tmp_path / "sample.csv",
        fetcher=fetched,
    )
    assert (path, kind) == (full, "full file")


# ---------------------------------------------------------------------------
# What it does with the file, with the journey replaced
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_journey(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`run_journey` replaced by one that writes nothing and returns the committed full-file results."""
    seen: dict[str, Any] = {"results": copy.deepcopy(COMMITTED)}

    def fake(spec: Any, *, csv_path: Path, config_root: Path, runs_dir: Path, **extra: Any) -> Any:
        seen.update(csv_path=csv_path, extra=extra)
        directory = runs_dir / spec.dataset / "journey"
        (directory / "data").mkdir(parents=True, exist_ok=True)
        (directory / "data" / "marker").write_text("a store", encoding="utf-8")
        return journey_module.JourneyOutcome(results=seen["results"], directory=directory)

    monkeypatch.setattr(journey_module, "run_journey", fake)
    return seen


def test_the_full_file_runs_with_no_floors_changed_and_the_summary_is_the_validated_one(
    tmp_path: Path, fake_journey: dict[str, Any]
) -> None:
    csv = tmp_path / "prepared.csv"
    csv.write_text("customer_id\n1\n", encoding="utf-8")
    # this stand-in is "the validated file" for the test: its own digest is the one expected
    result = seed(
        data_dir=tmp_path / "store",
        csv=csv,
        expected_sha256=seed_module.sha256_of(csv),
        echo=lambda _line: None,
    )
    assert fake_journey["extra"] == {"extra_risk_overrides": None, "extra_uplift_overrides": None}
    assert result.source_kind == "given" and not result.is_sample and result.is_validated
    assert result.results_path.is_file() and result.summary_path is not None
    committed = (FOLDER / "DEMO_SUMMARY.md").read_text(encoding="utf-8")
    assert result.summary_path.read_text(encoding="utf-8") == committed
    assert set(result.campaign_ids) == {"conversion", "spend"}


def test_the_sample_runs_with_the_smaller_floors_and_says_it_is_not_the_validated_result(
    tmp_path: Path, fake_journey: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_module, "FULL_FILE", tmp_path / "absent.csv")
    fake_journey["results"]["rows"] = 6400
    said: list[str] = []
    result = seed(data_dir=tmp_path / "store", echo=said.append)
    assert result.source_kind == "sample" and result.source == seed_module.SAMPLE_FILE and result.is_sample
    assert fake_journey["extra"]["extra_uplift_overrides"] == seed_module.SAMPLE_UPLIFT_OVERRIDES
    assert fake_journey["extra"]["extra_risk_overrides"] == seed_module.SAMPLE_RISK_OVERRIDES
    assert any(line.startswith("SAMPLE:") and "NOT the validated results" in line for line in said)
    assert result.summary_path is not None
    assert result.summary_path.read_text(encoding="utf-8").splitlines()[2].startswith("> **SAMPLE RUN.")


def test_a_store_that_already_holds_something_is_refused_unless_forced(
    tmp_path: Path, fake_journey: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_module, "FULL_FILE", tmp_path / "absent.csv")
    store = tmp_path / "store" / "hillstrom-email" / "journey" / "data"
    store.mkdir(parents=True)
    (store / "keep.txt").write_text("somebody's store", encoding="utf-8")
    said: list[str] = []
    assert seed_module.main(["--data-dir", str(tmp_path / "store")], echo=said.append) == 2
    assert "already holds a store" in said[0]
    assert (store / "keep.txt").read_text(encoding="utf-8") == "somebody's store", "nothing was touched"
    assert "csv_path" not in fake_journey, "the journey must not have started"
    seed(data_dir=tmp_path / "store", force=True, echo=lambda _line: None)
    assert "csv_path" in fake_journey


def test_a_journey_that_leaves_no_verified_pack_is_not_reported_as_seeded(
    tmp_path: Path, fake_journey: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_module, "FULL_FILE", tmp_path / "absent.csv")
    fake_journey["results"]["steps"]["campaigns"]["spend"]["proof"]["provenance_verified"] = False
    with pytest.raises(SeedError, match="verified Value Proof Pack for spend"):
        seed(data_dir=tmp_path / "store", echo=lambda _line: None)


def test_the_printed_next_steps_name_the_serve_command_the_screens_and_the_caveat(
    tmp_path: Path, fake_journey: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_module, "FULL_FILE", tmp_path / "absent.csv")
    result = seed(data_dir=tmp_path / "store", echo=lambda _line: None)
    text = seed_module.describe(result, port=8123)
    assert f"MARKETING_AI_DATA_DIR={result.store}" in text and "--port 8123" in text
    for campaign_id in result.campaign_ids.values():
        assert f"#/campaigns/{campaign_id}" in text and f"#/pilot/proof/{campaign_id}" in text
    sample = copy.deepcopy(result)
    sample.is_sample = True
    assert "SAMPLE, not the validated results" in seed_module.describe(sample)


# ---------------------------------------------------------------------------
# A file is validated by its digest, not by where it was found or what it was called
# ---------------------------------------------------------------------------
def test_the_pinned_digest_is_the_one_the_committed_full_file_run_recorded() -> None:
    from scripts.demo_summary import VALIDATED_FILE_SHA256

    assert COMMITTED["csv_sha256"] == VALIDATED_FILE_SHA256 and COMMITTED["rows"] == 64_000


def test_sha256_of_reads_the_whole_file(tmp_path: Path) -> None:
    import hashlib

    big = tmp_path / "big.csv"
    big.write_bytes(b"customer_id\n" + b"1\n" * 600_000)  # more than one read block
    assert seed_module.sha256_of(big) == hashlib.sha256(big.read_bytes()).hexdigest()


def test_a_full_size_file_that_is_not_the_validated_one_is_unverified_and_the_page_says_so(
    tmp_path: Path, fake_journey: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tampered 64,000-row file sits where fetch.py puts the full file; the journey records its own digest."""
    tampered = tmp_path / "prepared.csv"
    tampered.write_text("customer_id\n1\n", encoding="utf-8")
    monkeypatch.setattr(seed_module, "FULL_FILE", tampered)
    fake_journey["results"]["csv_sha256"] = seed_module.sha256_of(tampered)  # what the journey records
    assert fake_journey["results"]["rows"] == 64_000
    said: list[str] = []
    result = seed(data_dir=tmp_path / "store", echo=said.append)
    assert result.source == tampered and result.source_kind == "unverified"
    assert not result.is_validated and not result.is_sample
    assert any(line.startswith("UNVERIFIED:") and "validated SHA-256" in line for line in said)
    assert result.summary_path is not None
    page = result.summary_path.read_text(encoding="utf-8")
    assert page.splitlines()[2].startswith("> **UNVERIFIED FILE.")
    assert page != (FOLDER / "DEMO_SUMMARY.md").read_text(encoding="utf-8")
    text = seed_module.describe(result)
    assert "UNVERIFIED" in text and "These should equal" not in text


def test_a_file_given_with_csv_is_unverified_unless_its_digest_is_the_validated_one(
    tmp_path: Path, fake_journey: dict[str, Any]
) -> None:
    given = tmp_path / "mine.csv"
    given.write_text("customer_id\n1\n", encoding="utf-8")
    fake_journey["results"]["csv_sha256"] = seed_module.sha256_of(given)
    said: list[str] = []
    result = seed(data_dir=tmp_path / "a", csv=given, echo=said.append)
    assert result.source_kind == "unverified" and any(line.startswith("UNVERIFIED:") for line in said)
    # the same call with the file's digest taken as the validated one is the validated case
    fake_journey["results"]["csv_sha256"] = COMMITTED["csv_sha256"]
    ok = seed(
        data_dir=tmp_path / "b", csv=given, expected_sha256=seed_module.sha256_of(given), echo=said.append
    )
    assert ok.source_kind == "given" and ok.is_validated
    assert "These should equal" in seed_module.describe(ok)


def test_the_sample_is_never_called_validated(
    tmp_path: Path, fake_journey: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seed_module, "FULL_FILE", tmp_path / "absent.csv")
    result = seed(data_dir=tmp_path / "store", echo=lambda _line: None)
    assert result.is_sample and not result.is_validated


# ---------------------------------------------------------------------------
# The real thing: a clean store, the committed sample, served through the app
# ---------------------------------------------------------------------------
@pytest.mark.slow
@pytest.mark.integration
def test_the_seed_works_on_a_clean_store_from_the_committed_sample_and_the_app_serves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from api.main import create_app

    monkeypatch.setattr(
        seed_module, "FULL_FILE", tmp_path / "absent.csv"
    )  # whatever is in this checkout's data/
    said: list[str] = []
    result = seed(data_dir=tmp_path / "validated", echo=said.append)
    assert result.source_kind == "sample" and result.is_sample
    assert any(line.startswith("SAMPLE:") for line in said)
    assert result.store.is_dir() and result.results_path.is_file() and result.summary_path is not None
    assert result.summary_path.read_text(encoding="utf-8").splitlines()[2].startswith("> **SAMPLE RUN.")
    results = json.loads(result.results_path.read_text(encoding="utf-8"))
    assert results["rows"] == 6401 and set(result.campaign_ids) == {"conversion", "spend"}
    with TestClient(
        create_app(config_root=seed_module.REPO_ROOT / "configs", data_dir=result.store)
    ) as client:
        runs = client.get("/runs").json()["runs"]
        assert {run["use_case_id"] for run in runs} == {"hillstrom-email"} and len(runs) >= 4
        campaigns = client.get("/campaigns").json()["campaigns"]
        assert {c["campaign_id"] for c in campaigns} == set(result.campaign_ids.values())
        for campaign_id in result.campaign_ids.values():
            page = client.get(f"/pilot/proof/{campaign_id}")
            assert page.status_code == 200
    assert seed_module.main(["--data-dir", str(tmp_path / "validated")], echo=said.append) == 2
