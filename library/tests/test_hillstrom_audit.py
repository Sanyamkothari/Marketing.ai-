"""The audit readout on Hillstrom's own e-mail test, through `POST /campaigns/audit` (Plan J, DEC-1322).

Nothing here downloads anything. The tests read the committed `sample.csv` (a tenth of the file, the same
share of each group): they check that the original campaign is posted the way a client would post it, that
the route labels it by what the engine could verify, that the numbers are the file's own, that each Value
Proof Pack is traced, and that the report is a function of the results. The full file's numbers are in
`run_report.md`; the one test that compares with them skips, saying why, when the full file or its run is
absent (it is never committed: see the dataset's README).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from conftest import CONFIGS, LIBRARY

FOLDER = LIBRARY / "hillstrom-email"
SAMPLE = FOLDER / "sample.csv"
PREPARED = FOLDER / "data" / "prepared.csv"
REPORT = FOLDER / "run_report.md"
AUDIT_RESULTS = FOLDER / "audit.results.json"
JOURNEY_RESULTS = FOLDER / "journey.results.json"

OUTCOMES = ("visit", "conversion", "spend")


def _sample() -> pd.DataFrame:
    return pd.read_csv(SAMPLE)


# ---------------------------------------------------------------------------
# The two files a client would send
# ---------------------------------------------------------------------------
def test_the_assignment_file_has_the_customer_details_and_no_outcome() -> None:
    from library.audit import AUDIT_CASES, HILLSTROM_AUDIT, assignment_file, outcomes_file

    frame = _sample()
    details = {"recency", "history_segment", "history", "mens", "womens", "zip_code", "newbie", "channel"}
    for case in AUDIT_CASES:
        who = assignment_file(frame, HILLSTROM_AUDIT, case)
        assert set(who.columns) == {"customer_id", case.arm_column} | details, case.key
        assert not set(who.columns) & set(OUTCOMES), "the outcomes are in their own file"
        assert who["customer_id"].is_unique
    whole = assignment_file(frame, HILLSTROM_AUDIT, next(c for c in AUDIT_CASES if c.key == "spend_any"))
    assert (
        "segment" not in whole.columns
    ), "the three-valued column would give the groups away beside the 0/1 one"
    held = (frame["segment"] == "No E-Mail").to_numpy()
    assert (whole["emailed"].to_numpy() == (~held).astype(int)).all()
    mens = assignment_file(frame, HILLSTROM_AUDIT, next(c for c in AUDIT_CASES if c.key == "spend_mens"))
    assert set(mens["segment"]) == {"No E-Mail", "Mens E-Mail"}
    what = outcomes_file(frame, HILLSTROM_AUDIT)
    assert list(what.columns) == ["customer_id", *OUTCOMES] and len(what) == len(frame)


def test_the_bootstrap_of_a_difference_in_means_is_seeded_and_percentile() -> None:
    from library.audit import bootstrap_mean_difference

    rng = np.random.default_rng(7)
    treated = rng.exponential(2.0, 400)
    control = rng.exponential(1.0, 500)
    first = bootstrap_mean_difference(treated, control, samples=300, seed=11)
    again = bootstrap_mean_difference(treated, control, samples=300, seed=11)
    other = bootstrap_mean_difference(treated, control, samples=300, seed=12)
    assert first == again and first != other
    assert first["estimate"] == pytest.approx(treated.mean() - control.mean())
    assert first["ci_low"] < first["estimate"] < first["ci_high"]
    assert first["resamples"] == 300 and first["seed"] == 11


# ---------------------------------------------------------------------------
# The audit, on the sample, through the API
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def audit(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from library.audit import HILLSTROM_AUDIT, run_audit

    return run_audit(
        HILLSTROM_AUDIT,
        csv_path=SAMPLE,
        config_root=CONFIGS,
        runs_dir=tmp_path_factory.mktemp("runs"),
    )


@pytest.mark.slow
@pytest.mark.integration
def test_the_original_campaign_is_labelled_causal_because_the_engine_verified_it(audit: Any) -> None:
    cases = audit.results["cases"]
    assert [
        c["status"] for c in (cases[k] for k in ("conversion", "spend_any", "spend_mens", "spend_womens"))
    ] == [201] * 4
    for key in ("conversion", "spend_any", "spend_mens", "spend_womens"):
        readout = cases[key]["audit"]
        assert readout["label"] == "Causal" and readout["causal"] is True, key
        assert readout["causal_basis"] == "verified_random" and readout["stated_basis"] == "random"
        check = readout["randomness"]
        assert check["status"] == "passed" and check["columns_used"] == 8
        assert 0.4 < check["auc"] <= check["threshold"] == 0.6
        assert cases[key]["campaign"]["causal_basis"] == "verified_random"
    assert cases["conversion"]["audit"]["offers"] == ["Mens E-Mail", "Womens E-Mail"]
    assert cases["conversion"]["audit"]["control_level"] == "No E-Mail"


@pytest.mark.slow
@pytest.mark.integration
def test_the_groups_and_effects_are_the_files_own_numbers(audit: Any) -> None:
    frame = _sample()
    cases = audit.results["cases"]
    held = frame[frame["segment"] == "No E-Mail"]
    arms = {a["arm"]: a for a in cases["conversion"]["report"]["arms"]}
    for offer in ("Mens E-Mail", "Womens E-Mail"):
        sent = frame[frame["segment"] == offer]
        arm = arms[offer]
        assert (arm["treated_rows"], arm["control_rows"]) == (len(sent), len(held))
        assert arm["treated_conversions"] == int(sent["conversion"].sum())
        assert arm["control_conversions"] == int(held["conversion"].sum())
        assert arm["effect"]["value"] == pytest.approx(sent["conversion"].mean() - held["conversion"].mean())
        assert arm["effect"]["ci_low"] < arm["effect"]["value"] < arm["effect"]["ci_high"]
    whole = cases["spend_any"]["report"]
    sent_all = frame[frame["segment"] != "No E-Mail"]
    assert (whole["treated_rows"], whole["control_rows"]) == (len(sent_all), len(held))
    assert whole["mean_difference"] == pytest.approx(sent_all["spend"].mean() - held["spend"].mean())
    for key, offer in (("spend_mens", "Mens E-Mail"), ("spend_womens", "Womens E-Mail")):
        sent = frame[frame["segment"] == offer]
        report = cases[key]["report"]
        assert report["treated_rows"] == len(sent) and report["control_rows"] == len(held)
        assert report["mean_difference"] == pytest.approx(sent["spend"].mean() - held["spend"].mean())
        boot = cases[key]["bootstrap"]
        assert boot["resamples"] == 2000 and boot["ci_low"] < boot["estimate"] < boot["ci_high"]
    assert audit.results["groups"] == {k: int(v) for k, v in frame["segment"].value_counts().items()}


@pytest.mark.slow
@pytest.mark.integration
def test_several_offers_on_an_amount_are_refused_and_the_refusal_is_the_result(audit: Any) -> None:
    refused = audit.results["cases"]["spend_offers"]
    assert refused["status"] == 422 and "report" not in refused
    detail = refused["refusal"]["detail"]
    assert detail["code"] == "CAMPAIGN_INVALID" and "yes/no outcome only" in detail["message"]


@pytest.mark.slow
@pytest.mark.integration
def test_every_pack_is_traced_and_says_it_is_a_retrospective_audit_of_a_public_dataset(audit: Any) -> None:
    from engine.pilot.proof import ProofView, verify_provenance
    from engine.storage import LocalStorage

    storage = LocalStorage(audit.directory / "data")
    for key in ("conversion", "spend_any", "spend_mens", "spend_womens"):
        case = audit.results["cases"][key]
        proof = case["proof"]
        assert proof["status"] == 200 and proof["provenance_verified"] is True, key
        assert proof["html_status"] == 200 and proof["pdf_status"] == 200 and proof["pdf_bytes"] > 1000
        view = ProofView.model_validate(proof["view"])
        verify_provenance(view, storage)  # again, from the stored record
        assert view.claim == "proven"
        assert "public dataset, retrospective audit" in case["name"]
        assert view.campaign_name.text == case["name"]
        assert "public dataset, retrospective audit" in (audit.directory / f"proof_{key}.html").read_text(
            encoding="utf-8"
        )


@pytest.mark.slow
@pytest.mark.integration
def test_the_campaign_ids_are_pinned_and_nothing_but_the_files_enters_the_store(audit: Any) -> None:
    ids = [
        audit.results["cases"][k]["campaign_id"]
        for k in ("conversion", "spend_any", "spend_mens", "spend_womens")
    ]
    assert ids == [f"c_20261010_1120000{n}" for n in (1, 2, 3, 4)]
    stored = {p.name for p in (audit.directory / "data" / "campaigns").iterdir()}
    assert stored == set(ids), "a refused audit leaves nothing behind"


@pytest.mark.slow
@pytest.mark.integration
def test_the_programme_readout_does_not_apply_and_the_route_says_so(audit: Any) -> None:
    programme = audit.results["programme"]
    assert programme["applies"] is False
    assert programme["status"] == 409 and programme["code"] == "PROGRAMME_NO_HOLDOUT"


@pytest.mark.slow
@pytest.mark.integration
def test_an_assignment_chosen_from_the_customers_history_is_not_called_causal(
    tmp_path: Path,
) -> None:
    """The check bites: the same file, with the e-mail given to the big spenders, is Descriptive only."""
    from library.audit import (
        AUDIT_CASES,
        HILLSTROM_AUDIT,
        _audit_body,
        _upload,
        assignment_file,
        outcomes_file,
    )
    from library.journey import _app

    frame = _sample()
    rigged = frame.copy()
    big = rigged["history"] > rigged["history"].median()
    rigged["segment"] = np.where(big, "Mens E-Mail", "No E-Mail")
    case = next(c for c in AUDIT_CASES if c.key == "spend_mens")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with _app(CONFIGS, data_dir) as client:
        a = _upload(client, HILLSTROM_AUDIT, assignment_file(rigged, HILLSTROM_AUDIT, case), "assignment")
        o = _upload(client, HILLSTROM_AUDIT, outcomes_file(rigged, HILLSTROM_AUDIT), "outcomes")
        body = _audit_body(HILLSTROM_AUDIT, case, assignment_id=a, outcomes_id=o, name="rigged")
        response = client.post("/campaigns/audit", json=body)
    assert response.status_code == 201, response.text
    readout = response.json()["audit"]
    assert readout["stated_basis"] == "random", "the person said so"
    assert readout["randomness"]["status"] == "failed" and readout["randomness"]["auc"] > 0.6
    assert readout["label"] == "Descriptive only" and readout["causal"] is False
    assert "history" in readout["randomness"]["signals"]
    assert response.json()["verdict"] is None, "no 'the campaign added' sentence is drawn from it"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
@pytest.mark.slow
@pytest.mark.integration
def test_the_audit_section_is_rendered_from_the_results_alone(audit: Any) -> None:
    from library.audit_report import render_audit

    first = render_audit(audit.results, results_path="a.json", command="cmd")
    second = render_audit(
        json.loads(json.dumps(audit.results, default=str)), results_path="a.json", command="cmd"
    )
    assert first == second
    assert first.startswith("## 8. The audit readout on the original campaign")
    assert "public dataset, retrospective audit" in first and "**Causal**" in first
    assert "### 8.5 The programme readout does not apply" in first and "`PROGRAMME_NO_HOLDOUT`" in first
    assert "was refused by the route: `CAMPAIGN_INVALID`" in first
    assert "**Finding (`conversion`).**" in first, "the Pack's first-offer scope is said where it is met"
    assert "Caution: the amount is skewed" in first or "no skew warning" in first


def test_the_audit_section_says_what_a_failed_check_means_and_does_not_hide_a_refusal() -> None:
    """The wording follows the stored label: a descriptive result is not written up as causal."""
    from library.audit_report import render_audit

    check = {"status": "failed", "auc": 0.71, "threshold": 0.6, "columns_used": 3, "reason": None}
    case = {
        "key": "conversion",
        "title": "Conversion",
        "contrast": "x",
        "outcome": "conversion",
        "outcome_kind": "binary",
        "assignment_columns": ["customer_id", "segment", "history"],
        "status": 201,
        "campaign_id": "c_1",
        "campaign": {"primary_key": "customer_id"},
        "audit": {
            "label": "Descriptive only",
            "explanation": "The groups differ.",
            "randomness": check,
            "notes": [],
        },
        "report": {
            "treated_rows": 10,
            "treated_conversions": 2,
            "treated_rate": 0.2,
            "control_rows": 10,
            "control_conversions": 1,
            "control_rate": 0.1,
            "absolute_lift": {"value": 0.1, "ci_low": -0.2, "ci_high": 0.4},
            "incremental_conversions": {"value": 1.0, "ci_low": -2.0, "ci_high": 4.0},
            "p_value": 0.5,
        },
        "verdict": None,
        "proof": {"status": 409, "refusal": {"detail": {"code": "PROOF_NOT_MATURE"}}},
    }
    results = {
        "label": "public dataset, retrospective audit",
        "csv": "x.csv",
        "csv_sha256": "0" * 64,
        "rows": 20,
        "groups": {"No E-Mail": 10, "Mens E-Mail": 10},
        "outcomes_file": {"rows": 20, "columns": ["customer_id", "conversion"]},
        "treatment_start": "2008-03-20T00:00:00Z",
        "start_note": "dated",
        "outcome_window_days": 14,
        "wall_clock_seconds": 1.0,
        "value": {
            "value_per_conversion_inr": 1.0,
            "basis": "b",
            "buyers": 1,
            "mean_order_usd": 1.0,
            "fx_inr_per_usd": 83.0,
            "fx_note": "n",
        },
        "contact_cost_inr": 0.05,
        "programme": {"applies": False, "status": 409, "code": "PROGRAMME_NO_HOLDOUT", "message": "m"},
        "cases": {"conversion": case},
    }
    text = render_audit(results, results_path="a.json", command="cmd")
    assert "The label is **Causal**" not in text
    assert "- `conversion`: **Descriptive only**. The groups differ." in text
    assert "refused: `PROOF_NOT_MATURE`" in text
    assert "includes zero" in text and "draws no" in text


# ---------------------------------------------------------------------------
# The committed report against the full file's run
# ---------------------------------------------------------------------------
def test_the_committed_report_is_the_committed_journey_then_the_committed_audit() -> None:
    """A clean checkout can check the report: both results files are committed beside it (aggregates only)."""
    from library.run_engine import journey_report_text

    journey = json.loads(JOURNEY_RESULTS.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT_RESULTS.read_text(encoding="utf-8"))
    expected = journey_report_text(journey, audit)
    assert REPORT.read_text(encoding="utf-8") == expected
    assert journey_report_text(journey) == expected, "the committed audit results are the default"
    assert expected.startswith(journey_report_text(journey, committed_audit=False).rstrip("\n"))
    assert AUDIT_RESULTS.stat().st_size < 600_000, "aggregates only: no customer row"


def test_the_committed_report_carries_the_audit_section_and_its_label() -> None:
    text = REPORT.read_text(encoding="utf-8")
    assert "## 8. The audit readout on the original campaign" in text
    assert "Public dataset, retrospective audit." in text
    assert "### 8.5 The programme readout does not apply" in text
    assert "python -m library.run_engine audit --dataset hillstrom-email" in text


def _numbers(results: dict[str, Any]) -> dict[str, Any]:
    """Every number of an audit that a re-run on the same file must reproduce (not times, not dates)."""
    out: dict[str, Any] = {
        "groups": results["groups"],
        "value": results["value"],
        "programme": results["programme"],
    }
    for key, case in results["cases"].items():
        if case["status"] != 201:
            out[key] = {"status": case["status"], "code": case["refusal"]["detail"]["code"]}
            continue
        report = case["report"]
        out[key] = {
            "label": case["audit"]["label"],
            "randomness": case["audit"]["randomness"],
            "arms": [(a["arm"], a["effect"], a["incremental_conversions"]) for a in report.get("arms") or []],
            "mean_difference_ci": report.get("mean_difference_ci"),
            "treated_rows": report["treated_rows"],
            "control_rows": report["control_rows"],
            "bootstrap": case.get("bootstrap"),
            "headline": case["proof"]["view"]["headline"],
        }
    return out


@pytest.mark.slow
@pytest.mark.integration
def test_a_fresh_audit_of_the_full_file_reproduces_the_committed_numbers(tmp_path: Path) -> None:
    if not (PREPARED.is_file() and AUDIT_RESULTS.is_file()):
        pytest.skip("needs the full file (fetch.py) and its audit run (run_engine audit); neither is in git")
    from library.audit import HILLSTROM_AUDIT, run_audit

    fresh = run_audit(HILLSTROM_AUDIT, csv_path=PREPARED, config_root=CONFIGS, runs_dir=tmp_path)
    committed = json.loads(AUDIT_RESULTS.read_text(encoding="utf-8"))

    def same_shape(numbers: dict[str, Any]) -> Any:
        return json.loads(json.dumps(numbers, default=str))

    assert same_shape(_numbers(fresh.results)) == same_shape(_numbers(committed))
