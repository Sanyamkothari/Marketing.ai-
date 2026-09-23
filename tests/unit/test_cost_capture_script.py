"""`scripts/measure_costs.py`: one command writes a sourced file, and the guide changes only when whole.

The module under test is the operator's end of `engine/aws/cost_capture.py`, so these tests drive it
the way an operator would - argv in, files out - with Cost Explorer stubbed (the stub and the fixture
documents are `tests/unit/test_cost_capture.py`'s). Every test works on a *copy* of
`docs/AWS_DEPLOYMENT.md` in a temporary directory: the real guide is never written, and the headline
test here is that `--write-doc` with partial data leaves the copy byte-for-byte as it was.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.aws.cost_capture import (
    IDLE_HEADING,
    PER_RUN_HEADING,
    CellStatus,
    CostMeasurement,
    TableState,
    table_state,
)
from scripts.measure_costs import EXIT_BAD_INPUT, EXIT_OK, EXIT_WITHHELD, main
from tests.unit.test_cost_capture import (
    AFTER,
    BEFORE,
    IDLE_LINES,
    StubCostExplorer,
    manifest,
)
from tests.unit.test_docs_honesty import CITATION, CURRENCY, MARKER

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

MANIFESTS: dict[str, dict[str, object]] = {
    "train-template": {"rows": 50},
    "train-reference": {},
    "score-reference": {"entrypoint": "score", "billable": None},
    "train-fast": {"strategy": "fast"},
    "train-balanced": {},
    "train-exhaustive": {"strategy": "exhaustive"},
    "score-per-100k": {"entrypoint": "score", "billable": None, "rows": 100_000},
}


def responder(request: Mapping[str, Any]) -> tuple[tuple[tuple[str, ...], float], ...]:
    """Idle lines for a grouped-by-usage-type query; one SageMaker or Bedrock line otherwise."""
    keys = [group["Key"] for group in request["GroupBy"]]
    if keys == ["SERVICE", "USAGE_TYPE"]:
        return IDLE_LINES
    if "REGION" not in json.dumps(request["Filter"]):
        return ((("Amazon Bedrock",), 0.1),)
    return ((("Amazon SageMaker",), 0.2),)


@pytest.fixture
def doc(tmp_path: Path, repo_root: Path) -> Path:
    """A copy of the guide to write into; the real one is never touched."""
    copy = tmp_path / "AWS_DEPLOYMENT.md"
    copy.write_text((repo_root / "docs" / "AWS_DEPLOYMENT.md").read_text(encoding="utf-8"), encoding="utf-8")
    return copy


@pytest.fixture
def manifests(tmp_path: Path) -> dict[str, Path]:
    """One downloaded manifest per per-run row, as `curl ... > manifests/<row>.json` leaves them."""
    from engine.config import Strategy
    from engine.contracts import JobEntrypoint

    folder = tmp_path / "manifests"
    folder.mkdir()
    paths: dict[str, Path] = {}
    for row, built in MANIFESTS.items():
        options: dict[str, object] = dict(built)
        if "strategy" in options:
            options["strategy"] = Strategy(str(options["strategy"]))
        if "entrypoint" in options:
            options["entrypoint"] = JobEntrypoint(str(options["entrypoint"]))
        path = folder / f"{row}.json"
        path.write_text(manifest(f"run-{row}", **options).model_dump_json(), encoding="utf-8")  # type: ignore[arg-type]
        paths[row] = path
    return paths


@pytest.fixture
def snapshots(tmp_path: Path) -> tuple[Path, Path]:
    """The assistant's before/after usage, the 'after' one wrapped as `GET /indexes/{id}` returns it."""
    before = tmp_path / "usage-before.json"
    before.write_text(BEFORE.model_dump_json(), encoding="utf-8")
    after = tmp_path / "usage-after.json"
    after.write_text(json.dumps({"index_id": "idx-1", "llm_usage": json.loads(AFTER.model_dump_json())}))
    return before, after


@pytest.fixture
def priced_configs(tmp_path: Path) -> Path:
    """A configuration root whose `llm_prices.yaml` is filled, as an operator fills it before day D5.

    The prices are test inputs for the two fixture model ids, which no provider publishes; they are
    here so the assistant's list-price cell can be exercised, not as a claim about any real model.
    """
    root = tmp_path / "configs"
    root.mkdir()
    (root / "llm_prices.yaml").write_text(
        "schema_version: 1\n"
        "as_of: 2026-10-01\n"
        'source: "test fixture"\n'
        "currency: USD\n"
        "models:\n"
        "  some.generation-model-v1:0: {input_per_1m: 1.0, output_per_1m: 2.0}\n"
        "  some.embedding-model-v2:0: {input_per_1m: 0.5, output_per_1m: 0.0}\n",
        encoding="utf-8",
    )
    return root


def written(out_dir: Path) -> CostMeasurement:
    (path,) = sorted(out_dir.glob("*.json"))
    return CostMeasurement.model_validate_json(path.read_text(encoding="utf-8"))


def test_measure_writes_a_dated_sourced_file_and_leaves_the_guide_alone_by_default(
    tmp_path: Path, doc: Path, manifests: dict[str, Path]
) -> None:
    before = doc.read_bytes()
    out = tmp_path / "costs"
    code = main(
        [
            "measure",
            "--manifest",
            f"train-fast={manifests['train-fast']}",
            "--no-cost-explorer",
            "--out-dir",
            str(out),
            "--doc",
            str(doc),
            "--label",
            "runs-dev",
        ],
        now=NOW,
    )
    assert code == EXIT_OK
    assert doc.read_bytes() == before
    (path,) = out.glob("*.json")
    assert path.name == "2026-10-20-runs-dev.json"
    measurement = written(out)
    assert measurement.measured_at == NOW
    assert "--no-cost-explorer" in measurement.command
    billed = next(cell for cell in measurement.cells if cell.column == "billed")
    assert billed.status == CellStatus.NOT_MEASURED and billed.text == MARKER
    assert billed.reason == "Cost Explorer was not queried."
    assert all(cell.source for cell in measurement.cells)


def test_write_doc_with_partial_data_changes_nothing(
    tmp_path: Path, doc: Path, manifests: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Five of the eight per-run rows and the dev idle column: neither table is whole, so neither moves."""
    before = doc.read_bytes()
    argv = ["measure", "--idle-start", "2026-10-05", "--idle-end", "2026-10-06"]
    for row in ("train-template", "train-reference", "score-reference", "train-fast", "train-balanced"):
        argv += ["--manifest", f"{row}={manifests[row]}"]
    argv += ["--out-dir", str(tmp_path / "costs"), "--doc", str(doc), "--write-doc"]
    code = main(argv, client=StubCostExplorer(responder), now=NOW)
    assert code == EXIT_WITHHELD
    assert doc.read_bytes() == before, "a table with a gap must not be partly written"
    printed = capsys.readouterr().out
    assert f"{IDLE_HEADING}: left unchanged" in printed
    assert f"{PER_RUN_HEADING}: left unchanged" in printed
    assert "ap-south-1, prod" in printed and "strategy `exhaustive`" in printed


def test_one_command_fills_the_per_run_table_and_the_idle_table_waits_for_prod(
    tmp_path: Path,
    doc: Path,
    manifests: dict[str, Path],
    snapshots: tuple[Path, Path],
    priced_configs: Path,
) -> None:
    out = tmp_path / "costs"
    idle_code = main(
        [
            "measure",
            "--idle-start",
            "2026-10-05",
            "--idle-end",
            "2026-10-06",
            "--label",
            "idle-dev",
            "--out-dir",
            str(out),
            "--doc",
            str(doc),
        ],
        client=StubCostExplorer(responder),
        now=NOW,
    )
    assert idle_code == EXIT_OK
    (idle_file,) = out.glob("*-idle-dev.json")

    argv = ["measure", "--label", "runs-dev"]
    for row, path in manifests.items():
        argv += ["--manifest", f"{row}={path}"]
    before, after = snapshots
    argv += [
        "--assistant-before",
        str(before),
        "--assistant-after",
        str(after),
        "--assistant-questions",
        "20",
        "--assistant-day",
        "2026-10-09",
        "--configs",
        str(priced_configs),
        "--merge",
        str(idle_file),
        "--out-dir",
        str(out),
        "--doc",
        str(doc),
        "--write-doc",
    ]
    code = main(argv, client=StubCostExplorer(responder), now=NOW)
    assert code == EXIT_WITHHELD, "the idle table's prod column is still missing"
    text = doc.read_text(encoding="utf-8")
    assert table_state(text, PER_RUN_HEADING) == TableState.MEASURED
    assert table_state(text, IDLE_HEADING) == TableState.UNMEASURED
    for line in text.splitlines():
        if CURRENCY.search(line) and line.startswith("|"):
            assert CITATION.search(line), f"a written row carries no date or list-price citation: {line}"

    # The prod idle column arrives later; `render` over both idle files completes the idle table.
    prod_code = main(
        [
            "measure",
            "--env",
            "prod",
            "--idle-start",
            "2026-10-05",
            "--idle-end",
            "2026-10-06",
            "--label",
            "idle-prod",
            "--out-dir",
            str(out),
            "--doc",
            str(doc),
        ],
        client=StubCostExplorer(responder),
        now=NOW,
    )
    assert prod_code == EXIT_OK
    files = [str(path) for path in sorted(out.glob("*.json"))]
    assert main(["render", *files, "--doc", str(doc), "--write-doc"]) == EXIT_OK
    assert table_state(doc.read_text(encoding="utf-8"), IDLE_HEADING) == TableState.MEASURED


def test_render_without_write_doc_only_reports(
    tmp_path: Path, doc: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "costs"
    main(
        [
            "measure",
            "--idle-start",
            "2026-10-05",
            "--idle-end",
            "2026-10-06",
            "--out-dir",
            str(out),
            "--doc",
            str(doc),
        ],
        client=StubCostExplorer(responder),
        now=NOW,
    )
    before = doc.read_bytes()
    capsys.readouterr()
    assert main(["render", *[str(path) for path in out.glob("*.json")], "--doc", str(doc)]) == EXIT_OK
    assert doc.read_bytes() == before
    assert "left unchanged" in capsys.readouterr().out


def test_a_second_file_on_the_same_day_does_not_overwrite_the_first(tmp_path: Path, doc: Path) -> None:
    out = tmp_path / "costs"
    argv = ["measure", "--idle-start", "2026-10-05", "--idle-end", "2026-10-06", "--out-dir", str(out)]
    argv += ["--doc", str(doc)]
    for _ in range(2):
        assert main(argv, client=StubCostExplorer(responder), now=NOW) == EXIT_OK
    assert sorted(path.name for path in out.glob("*.json")) == [
        "2026-10-20-dev-2.json",
        "2026-10-20-dev.json",
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["measure", "--manifest", "train-turbo=x.json", "--no-cost-explorer"],
        ["measure", "--manifest", "train-fast", "--no-cost-explorer"],
        ["measure", "--idle-start", "2026-10-05", "--no-cost-explorer"],
        ["measure", "--assistant-questions", "20", "--no-cost-explorer"],
        ["measure", "--no-cost-explorer"],
        ["measure", "--manifest", "train-fast=/nonexistent/manifest.json", "--no-cost-explorer"],
    ],
)
def test_unusable_inputs_write_nothing(tmp_path: Path, argv: list[str]) -> None:
    out = tmp_path / "costs"
    assert main([*argv, "--out-dir", str(out)], now=NOW) == EXIT_BAD_INPUT
    assert not out.exists()


def test_a_missing_guide_is_refused_before_anything_is_measured(tmp_path: Path) -> None:
    out = tmp_path / "costs"
    client = StubCostExplorer(responder)
    argv = ["measure", "--idle-start", "2026-10-05", "--idle-end", "2026-10-06", "--out-dir", str(out)]
    code = main([*argv, "--doc", str(tmp_path / "nowhere.md")], client=client, now=NOW)
    assert code == EXIT_BAD_INPUT
    assert not out.exists() and not client.requests


def test_absent_idle_row_reaches_the_measurement(tmp_path: Path, doc: Path) -> None:
    out = tmp_path / "costs"
    argv = ["measure", "--idle-start", "2026-10-05", "--idle-end", "2026-10-06", "--out-dir", str(out)]
    argv += ["--doc", str(doc), "--absent-idle-row", "network"]
    no_nat = [line for line in IDLE_LINES if line[0][0] != "EC2 - Other"]
    assert main(argv, client=StubCostExplorer(lambda request: no_nat), now=NOW) == EXIT_OK
    network = next(cell for cell in written(out).cells if cell.row == "network")
    assert network.status == CellStatus.NOT_APPLICABLE
    assert main([*argv[:-1], "nat"], client=StubCostExplorer(responder), now=NOW) == EXIT_BAD_INPUT


def test_the_scoring_file_repeats_the_source_under_fresh_keys(tmp_path: Path) -> None:
    source = tmp_path / "reference.csv"
    source.write_text("customerID,tenure,Churn\nA-1,1,No\nB-2,02,Yes\nC-3,,No\n", encoding="utf-8")
    out = tmp_path / "score.csv"
    code = main(
        [
            "scoring-file",
            "--source",
            str(source),
            "--primary-key",
            "customerID",
            "--rows",
            "8",
            "--out",
            str(out),
            "--drop",
            "Churn",
        ]
    )
    assert code == EXIT_OK
    frame = pd.read_csv(out, dtype=str, keep_default_na=False)
    assert list(frame.columns) == ["customerID", "tenure"]
    assert len(frame) == 8 and frame["customerID"].is_unique
    assert list(frame["customerID"][:4]) == ["A-1", "B-2", "C-3", "A-1-x1"]
    assert list(frame["tenure"][:3]) == ["1", "02", ""], "values are copied exactly, never re-typed"
