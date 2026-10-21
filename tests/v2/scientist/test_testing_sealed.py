"""Sealed datasets: refused without --sealed-final, a load check that reads no outcome, a recorded run."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from scripts.v2_realbench import adapters, run
from scripts.v2_realbench.sealed import (
    SEALED_DATASETS,
    SEALED_HEADING,
    SEALED_PLACEHOLDER,
    SEALED_TABLE_HEAD,
    load_check,
    record_sealed_run,
    refusal,
)

REPO = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 21, 18, 30, tzinfo=UTC)


def _doc(tmp_path: Path) -> Path:
    doc = tmp_path / "REAL_BENCH.md"
    doc.write_text(
        f"# Bench\n\ntext\n\n{SEALED_HEADING}\n\nintro\n\n{SEALED_TABLE_HEAD}\n|---|---|---|---|\n"
        f"{SEALED_PLACEHOLDER}\n\n## Next\n\nmore\n"
    )
    return doc


def _lenta_csv(path: Path, n: int = 40) -> None:
    rng = np.random.default_rng(1)
    frame = pd.DataFrame(
        {
            "age": rng.integers(18, 80, n),
            "cheque_count_12m_g20": rng.integers(0, 9, n),
            "children": rng.integers(0, 3, n),
            "gender": rng.choice(["M", "F", "U"], n),
            "group": np.where(np.arange(n) % 4 == 0, "control", "test"),
            "main_format": rng.integers(1, 3, n),
            "months_from_register": rng.integers(1, 90, n),
            "promo_share_15d": rng.random(n),
            "response_att": rng.integers(0, 2, n),
            "response_sms": rng.integers(0, 2, n),
            "response_viber": rng.integers(0, 2, n),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as fh:
        frame.to_csv(fh, index=False)


def _starbucks_csvs(root: Path, n: int = 30) -> None:
    root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(2)
    for fname, size in (("training.csv", n), ("Test.csv", n // 2)):
        pd.DataFrame(
            {
                "ID": np.arange(size),
                "Promotion": np.where(np.arange(size) % 2 == 0, "Yes", "No"),
                "purchase": rng.integers(0, 2, size),
                "V1": rng.integers(0, 4, size),
                "V2": rng.normal(30, 5, size),
                "V3": rng.normal(0, 1, size),
                "V4": rng.integers(1, 3, size),
                "V5": rng.integers(1, 5, size),
                "V6": rng.integers(1, 5, size),
                "V7": rng.integers(1, 3, size),
            }
        ).to_csv(root / fname, index=False)


# --- refusal ----------------------------------------------------------------------------------------


def test_refusal_rules() -> None:
    assert SEALED_DATASETS == ("lenta", "starbucks")
    for name in SEALED_DATASETS:
        assert refusal(name, False) is not None
        assert "--sealed-final" in str(refusal(name, False))
        assert refusal(name, True) is None
    for name in ("hillstrom", "x5", "criteo", "megafon", "all"):
        assert refusal(name, False) is None
        assert refusal(name, True) is not None  # the flag is for sealed data only


@pytest.mark.parametrize("name", SEALED_DATASETS)
def test_run_refuses_sealed_without_flag_and_reads_nothing(
    name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a sealed dataset was loaded without --sealed-final")

    monkeypatch.setattr(run, "load", boom)
    monkeypatch.setattr(run, "evaluate_dataset", boom)
    doc = _doc(tmp_path)
    before = doc.read_text()
    code = run.main(["--dataset", name, "--data-dir", str(tmp_path / "nowhere"), "--sealed-log", str(doc)])
    err = capsys.readouterr().err
    assert code == 2
    assert "SEALED" in err and "--sealed-final" in err
    assert doc.read_text() == before


def test_all_never_includes_sealed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    def fake_load(name: str, *_a: object, **_k: object) -> Any:
        seen.append(name)
        raise MemoryError("stop here")  # run.py keeps going after a MemoryError

    monkeypatch.setattr(run, "load", fake_load)
    assert run.main(["--dataset", "all", "--data-dir", str(tmp_path), "--quiet"]) == 0
    assert seen == list(adapters.DATASETS)
    assert not set(seen) & set(SEALED_DATASETS)


def test_the_report_folder_is_created_before_the_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The TARGETS commands write to `out/...`; a missing folder must not crash a run of many minutes
    (or a sealed one that cannot be repeated) after its first dataset."""

    def fake_load(name: str, *_a: object, **_k: object) -> Any:
        assert (tmp_path / "out" / "deep").is_dir()  # already there when the data is loaded
        raise MemoryError("stop here")

    monkeypatch.setattr(run, "load", fake_load)
    report = tmp_path / "out" / "deep" / "r.json"
    code = run.main(["--dataset", "hillstrom", "--data-dir", str(tmp_path), "--json", str(report), "--quiet"])
    assert code == 0 and report.is_file()


def test_sealed_final_is_recorded_before_data_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = _doc(tmp_path)
    order: list[str] = []

    def fake_load(name: str, *_a: object, **_k: object) -> Any:
        order.append(
            "load:" + name + ":" + ("recorded" if "| lenta |" in doc.read_text() else "NOT recorded")
        )
        raise MemoryError("stop here")

    monkeypatch.setattr(run, "load", fake_load)
    code = run.main(
        [
            "--dataset",
            "lenta",
            "--sealed-final",
            "--sealed-log",
            str(doc),
            "--data-dir",
            str(tmp_path),
            "--quiet",
        ]
    )
    assert code == 0
    assert order == ["load:lenta:recorded"]
    text = doc.read_text()
    assert SEALED_PLACEHOLDER not in text
    assert text.count("| lenta |") == 1
    assert "--sealed-final" in text  # the command is in the row


def test_sealed_final_refuses_a_smoke_or_changed_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(run, "load", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("loaded")))
    doc = _doc(tmp_path)
    before = doc.read_text()
    for extra in (["--limit", "100"], ["--seeds", "2"], ["--no-features"]):
        code = run.main(["--dataset", "lenta", "--sealed-final", "--sealed-log", str(doc), *extra])
        assert code == 2
    assert "refusing" in capsys.readouterr().err
    assert doc.read_text() == before


# --- the log ----------------------------------------------------------------------------------------


def test_record_sealed_run_replaces_the_placeholder_then_appends(tmp_path: Path) -> None:
    doc = _doc(tmp_path)
    row = record_sealed_run(
        doc, dataset="lenta", git_sha="abcdef1234567890", dirty=False, command="python -m x | y", now=NOW
    )
    assert row.startswith("| 2026-10-21 18:30:00 | abcdef123456 | lenta |")
    assert "\\|" in row  # a pipe in the command cannot break the table
    record_sealed_run(doc, dataset="starbucks", git_sha=None, dirty=True, command="c", now=NOW)
    lines = doc.read_text().split("\n")
    table = [ln for ln in lines if ln.startswith("| 2026")]
    assert len(table) == 2
    assert SEALED_PLACEHOLDER not in lines
    assert "unknown (dirty)" in table[1]
    assert lines[-3:] == ["## Next", "", "more"] or "## Next" in lines  # text after the table is kept


def test_record_sealed_run_needs_the_section(tmp_path: Path) -> None:
    doc = tmp_path / "d.md"
    doc.write_text("# no section\n")
    with pytest.raises(ValueError, match="Sealed runs"):
        record_sealed_run(doc, dataset="lenta", git_sha="a", dirty=False, command="c", now=NOW)
    doc.write_text(f"{SEALED_HEADING}\n\nno table\n")
    with pytest.raises(ValueError, match="table header"):
        record_sealed_run(doc, dataset="lenta", git_sha="a", dirty=False, command="c", now=NOW)


def test_real_bench_doc_has_the_sealed_runs_table() -> None:
    lines = (REPO / "docs" / "v2" / "REAL_BENCH.md").read_text().split("\n")
    assert SEALED_HEADING in [ln.strip() for ln in lines]
    start = [ln.strip() for ln in lines].index(SEALED_HEADING)
    head = next(i for i in range(start, len(lines)) if lines[i].strip() == SEALED_TABLE_HEAD)
    rows = []
    for ln in lines[head + 2 :]:
        if not ln.startswith("|"):
            break
        rows.append(ln)
    for ln in rows:  # none yet, or one dated row per sealed run
        assert ln == SEALED_PLACEHOLDER or ln.startswith("| 20"), ln
    assert len(rows) <= len(SEALED_DATASETS) + 2


# --- the load check reads no outcome -----------------------------------------------------------------


def test_load_check_reads_only_header_and_treatment_column(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _lenta_csv(tmp_path / "lenta" / "lenta_dataset.csv.gz")
    _starbucks_csvs(tmp_path / "starbucks_promo")
    calls: list[dict[str, Any]] = []
    real = pd.read_csv

    def spy(*args: Any, **kwargs: Any) -> pd.DataFrame:
        calls.append({"usecols": kwargs.get("usecols"), "nrows": kwargs.get("nrows")})
        frame: pd.DataFrame = real(*args, **kwargs)
        return frame

    monkeypatch.setattr(pd, "read_csv", spy)
    lenta = load_check("lenta", tmp_path)
    star = load_check("starbucks", tmp_path)
    for c in calls:
        assert c["nrows"] == 0 or c["usecols"] in (["group"], ["Promotion"]), c
    assert lenta["rows"] == 40
    assert lenta["treatment_share"] == pytest.approx(0.75)
    assert lenta["files"][0]["n_columns"] == 11
    assert "response_att" in lenta["files"][0]["outcome_columns_not_read"]
    assert star["rows"] == 45
    assert star["treatment_share"] == pytest.approx((15 + 8) / 45)
    flat = repr(lenta) + repr(star)
    assert "conversion" not in flat and "mean" not in flat  # no outcome statistic anywhere
    for result in (lenta, star):
        assert set(result) == {"dataset", "files", "rows", "treatment_share", "note"}
        for f in result["files"]:
            assert set(f) == {
                "file",
                "rows",
                "n_columns",
                "columns",
                "treatment_column",
                "treated_value",
                "treated_rows",
                "treatment_share",
                "outcome_columns_not_read",
            }


def test_load_check_rejects_a_dev_dataset(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a sealed dataset"):
        load_check("hillstrom", tmp_path)


def test_sealed_module_never_names_an_outcome_column_in_a_read() -> None:
    src = (REPO / "scripts" / "v2_realbench" / "sealed.py").read_text()
    assert "usecols=[f.treatment_column]" in src
    assert src.count("read_csv(") == 2  # the header and the treatment column, nothing else


# --- the adapters, on inline fixtures (never on the real sealed files) -------------------------------


def test_lenta_adapter_on_a_fixture(tmp_path: Path) -> None:
    path = tmp_path / "lenta" / "lenta_dataset.csv.gz"
    _lenta_csv(path, n=400)
    frame = adapters.lenta_frame(path, 120)
    again = adapters.lenta_frame(path, 120)
    assert len(frame) == 120 and list(frame["customer_id"]) == list(again["customer_id"])  # a fixed sample
    data = adapters.lenta_from_frame(frame, fixed=True)
    assert data.name == "lenta" and data.n == 120
    assert set(np.unique(data.arm)) <= {0, 1}
    assert data.arm.mean() == pytest.approx((frame["group"] == "test").mean())
    assert data.propensity[data.arm == 1][0] == pytest.approx(data.arm.mean())
    cols = set(data.features.columns)
    assert not cols & {"group", "response_att", "response_sms", "response_viber", "customer_id"}
    assert set(data.categorical) == {"gender", "main_format"}
    assert set(data.numeric) | set(data.categorical) == cols
    assert np.array_equal(data.conversion, frame["response_att"].to_numpy(dtype=float))
    assert data.arm_cost[0] == 0.0 and data.arm_cost[1] > 0.0
    ids = [str(c) for c in data.customer_ids[:5]]
    assert len(data.agent.pre_events(ids)) == 5 and len(data.agent.campaign_events(ids)) == 5
    assert all(set(c) <= cols for c in data.segment_candidates)


def test_starbucks_adapter_on_a_fixture(tmp_path: Path) -> None:
    root = tmp_path / "starbucks_promo"
    _starbucks_csvs(root, n=60)
    frame = adapters.starbucks_frame(root)
    assert len(frame) == 90 and frame["customer_id"].is_unique
    data = adapters.starbucks_from_frame(frame, fixed=True)
    assert data.name == "starbucks" and data.n == 90
    assert data.arm_cost == (0.0, adapters.STARBUCKS_COST)
    assert "$10" not in data.economics and "10 x conversion" in data.economics
    assert set(data.features.columns) == {f"V{i}" for i in range(1, 8)}
    assert data.margin_value.max() == adapters.STARBUCKS_VALUE
    assert float(data.propensity[0]) == 0.5
    ids = [str(c) for c in data.customer_ids[:3]]
    evs = data.agent.campaign_events(ids)
    purchases = [e for es in evs.values() for e in es if e.type.value == "purchase"]
    assert all(e.margin == adapters.STARBUCKS_VALUE for e in purchases)


def test_load_dispatches_sealed_names() -> None:
    src = (REPO / "scripts" / "v2_realbench" / "adapters.py").read_text()
    for name in SEALED_DATASETS:
        assert f'name == "{name}"' in src
    assert set(adapters.SIZES) == {*adapters.DATASETS, *SEALED_DATASETS}


def test_criteo_and_megafon_economics_are_unchanged_by_the_new_parameters() -> None:
    """`_feature_rct` gained keyword parameters; the defaults must give the old Criteo / MegaFon numbers."""
    rng = np.random.default_rng(3)
    n = 400
    ids = np.array([f"mf{i}" for i in range(n)], dtype=object)
    arm = (rng.random(n) < 0.5).astype(np.int64)
    conv = (rng.random(n) < 0.05 + 0.05 * arm).astype(np.float64)
    feats = pd.DataFrame({"X_1": rng.normal(size=n)})
    data = adapters._feature_rct("t", ids, arm, 0.5, feats, conv, None, (("X_1",),), (), {})
    assert data.margin_value.max() == adapters.VALUE_OF_CONVERSION
    ev = data.agent.campaign_events([str(ids[int(np.flatnonzero(conv)[0])])])
    buy = next(e for es in ev.values() for e in es if e.type.value == "purchase")
    assert buy.value == adapters.ORDER_VALUE and buy.margin == adapters.VALUE_OF_CONVERSION
    assert data.categorical == () and data.numeric == ("X_1",)
