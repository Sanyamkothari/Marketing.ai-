"""The pre-flight checker (Plan E M59): the client's files, checked on the client's laptop.

The tables are `tests/fixtures/raw/make_raw.py`'s, the same synthetic subscribers the onboarding
suites build from, plus its broken variants - one defect per directory.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from engine.pilot.document import render_html
from engine.pilot.preflight import PreflightResult, preflight_document, run_preflight
from scripts.preflight import main as preflight_main
from tests.fixtures.raw.make_raw import make_duplicate_entities, make_raw, make_unmatched_keys

CUSTOMERS = 1_100
USAGE_ROWS = 4_000
NOW = datetime(2026, 9, 23, tzinfo=UTC)


@pytest.fixture(scope="module")
def clean(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_raw(tmp_path_factory.mktemp("clean"), customers=CUSTOMERS, usage_rows=USAGE_ROWS).root


@pytest.fixture(scope="module")
def clean_result(clean: Path, config_root: Path) -> PreflightResult:
    return run_preflight([clean], ("telco-churn",), root=config_root, now=NOW)


def codes(result: PreflightResult, severity: str | None = None) -> set[str]:
    return {f.code for f in result.findings if severity is None or f.severity == severity}


def test_clean_tables_have_no_problem(clean_result: PreflightResult) -> None:
    assert codes(clean_result, "error") == set()
    assert clean_result.verdict in ("ready", "warnings")


def test_every_file_is_identified_with_its_key_and_date(clean_result: PreflightResult) -> None:
    roles = {t.file: t.role for t in clean_result.tables}
    assert roles == {
        "customers.csv": "entity",
        "bills.csv": "bills",
        "payments.csv": "payments",
        "complaints.csv": "complaints",
        "usage.csv": "usage",
        "activity.csv": "activity",
    }
    for table in clean_result.tables:
        assert table.key_column == "CUST_ID"
        if table.role != "entity":
            assert table.date_column is not None and table.first_date is not None
    complaints = next(t for t in clean_result.tables if t.role == "complaints")
    assert complaints.date_column == "TICKET_DT", "the ticket's own date, not its (often blank) resolution"


def test_row_counts_are_exact_and_coverage_is_measured(clean: Path, clean_result: PreflightResult) -> None:
    customers = next(t for t in clean_result.tables if t.role == "entity")
    assert customers.rows == CUSTOMERS
    for table in clean_result.tables:
        lines = sum(1 for _ in (clean / table.file).open(encoding="utf-8")) - 1
        assert table.rows == lines
        if table.role != "entity":
            assert table.key_coverage == pytest.approx(1.0)


def test_contacts_typed_into_notes_are_found_and_never_printed(
    clean: Path, clean_result: PreflightResult
) -> None:
    assert "PII_IN_FREE_TEXT" in codes(clean_result)
    html = render_html(preflight_document(clean_result))
    assert "@example.invalid" not in html, "a planted e-mail address reached the report"
    assert "REMARKS" in html


def test_a_repeated_customer_id_is_the_one_problem(tmp_path: Path, config_root: Path) -> None:
    broken = make_duplicate_entities(tmp_path, customers=CUSTOMERS, usage_rows=USAGE_ROWS).root
    result = run_preflight([broken], ("telco-churn",), root=config_root, now=NOW)
    assert codes(result, "error") == {"ENTITY_DUPLICATE_KEYS"}
    assert result.verdict == "not_ready"


def test_ids_that_do_not_link_are_reported(tmp_path: Path, config_root: Path) -> None:
    broken = make_unmatched_keys(tmp_path, customers=CUSTOMERS, usage_rows=USAGE_ROWS).root
    result = run_preflight([broken], ("telco-churn",), root=config_root, now=NOW)
    assert "JOIN_KEY_COVERAGE_LOW" in codes(result)
    activity = next(t for t in result.tables if t.role == "activity")
    assert activity.key_coverage is not None and activity.key_coverage < 0.8


def test_a_missing_outcome_table_and_an_unreadable_file(
    tmp_path: Path, clean: Path, config_root: Path
) -> None:
    folder = tmp_path / "files"
    shutil.copytree(clean, folder)
    (folder / "activity.csv").unlink()
    (folder / "notes.txt").write_bytes(b"\x00\x01\x02 not a table")
    (folder / "readme.pdf").write_bytes(b"%PDF-1.4")
    result = run_preflight([folder], ("telco-churn",), root=config_root, now=NOW)
    found = codes(result)
    assert "LABEL_ROLE_MISSING" in found
    assert "PREFLIGHT_FILE_SKIPPED" in found
    assert result.verdict == "not_ready"


def test_the_page_explains_every_finding_in_plain_words(tmp_path: Path, config_root: Path) -> None:
    broken = make_duplicate_entities(tmp_path, customers=CUSTOMERS, usage_rows=USAGE_ROWS).root
    result = run_preflight([broken], ("telco-churn",), root=config_root, now=NOW)
    html = render_html(preflight_document(result, config_root))
    assert "Not ready" in html and "The customer table repeats customer IDs" in html
    assert "Nothing was uploaded" in html
    assert "<script" not in html


def test_the_command_writes_one_page_and_exits_one_on_a_problem(tmp_path: Path, config_root: Path) -> None:
    broken = make_duplicate_entities(tmp_path / "broken", customers=CUSTOMERS, usage_rows=USAGE_ROWS).root
    out = tmp_path / "report.html"
    assert (
        preflight_main(
            [str(broken), "--use-case", "telco-churn", "--out", str(out), "--configs", str(config_root)]
        )
        == 1
    )
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert preflight_main([str(tmp_path / "missing")]) == 2


def test_the_checker_needs_none_of_the_platforms_heavy_libraries(
    clean: Path, tmp_path: Path, repo_root: Path
) -> None:
    """The pilot kit ships it to a laptop with pandas, pyarrow, pydantic and PyYAML only."""
    code = (
        "import sys\n"
        "from scripts.preflight import main\n"
        f"main([{str(clean)!r}, '--use-case', 'telco-churn', '--out', {str(tmp_path / 'r.html')!r}])\n"
        "heavy = {'autogluon', 'sklearn', 'fastapi', 'duckdb', 'boto3', 'lightgbm', 'sqlmodel', 'shap',\n"
        "         'torch', 'fpdf', 'matplotlib', 'rapidfuzz', 'httpx', 'starlette'}\n"
        "print(sorted({m.split('.')[0] for m in sys.modules} & heavy))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=repo_root, capture_output=True, text=True, check=True
    )
    assert done.stdout.strip().splitlines()[-1] == "[]", done.stdout


def test_nothing_is_written_beside_the_files_but_the_report(
    tmp_path: Path, clean: Path, config_root: Path
) -> None:
    folder = tmp_path / "files"
    shutil.copytree(clean, folder)
    before = {p.name for p in folder.iterdir()}
    preflight_main([str(folder), "--use-case", "telco-churn", "--configs", str(config_root)])
    assert {p.name for p in folder.iterdir()} - before == {"preflight_report.html"}


def test_misusing_the_command_is_exit_two_not_a_traceback(clean: Path, tmp_path: Path) -> None:
    assert preflight_main([str(clean), "--use-case", "no-such-use-case"]) == 2
    assert preflight_main([str(clean), "--out", str(tmp_path / "missing" / "r.html")]) == 2


def test_a_file_the_profiler_cannot_handle_is_a_finding(
    tmp_path: Path, clean: Path, config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import engine.onboarding.sources as sources

    real = sources.profile_source

    def fragile(frame, config, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs["file_name"] == "usage.csv":
            raise KeyError("1")
        return real(frame, config, **kwargs)

    monkeypatch.setattr(sources, "profile_source", fragile)
    result = run_preflight([clean], ("telco-churn",), root=config_root, now=NOW)
    assert any(f.code == "PREFLIGHT_FILE_UNREADABLE" and f.file == "usage.csv" for f in result.findings)
    assert len(result.tables) == 5, "the other files are still checked"
