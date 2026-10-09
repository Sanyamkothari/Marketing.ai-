"""Plan J M104 through the product's own API: the Value Proof Pack (`GET /pilot/proof/{campaign_id}`, DEC-1314).

Every campaign here is built by the real code - the Phase 1 scoring stages (`apply_actions`, `write_scores`),
the uplift actions, the campaign routes and the engine's simulators for the outcomes - never by writing an
artefact by hand. What is checked is the milestone's acceptance list:

* every number of the pack resolves to the measured artefact field it was read from (an independent
  resolver, not the engine's own), and every digit printed on the page is one of those numbers;
* a missing artefact (no test plan, no contact file, no value inputs) renders "not measured" with its
  reason, never a zero;
* a group planted to be harmed by the campaign is flagged as backfiring, with a suggestion an Analyst
  approves (audited; nothing is applied), and the neutral group beside it is not;
* naive credit (every outcome among contacted customers) is at least the measured credit on the planted
  campaign;
* the HTML and PDF render, `jargon_in` finds nothing in either, and the pack holds no customer id;
* a campaign on generated data is refused (`PROOF_SYNTHETIC_DATA`) and one without a final result too
  (`PROOF_NOT_MATURE`), with the day it can be read;
* a campaign random only by the person's statement is never shown as proven; a descriptive-only one
  credits nothing; a programme readout has no groups and says so.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.measurement.simulate import (
    AS_OF,
    OUTCOME_WINDOW_DAYS,
    multi_arm_campaign,
    population,
    segment_outcomes,
)
from engine.pilot.plain import jargon_in
from engine.storage import LocalStorage
from tests.integration.measurement.support import MATURE, ScoredRun, ok, propensity_run, uplift_run, upload

pytestmark = pytest.mark.integration

PROPENSITY_RUN = "r_20261009_31000001"
UPLIFT_RUN = "r_20261009_31000002"
HARMED_BAND = "Medium"
NEUTRAL_BAND = "Low"
VALUE_INPUTS = {"value_per_outcome": 2000.0, "offer_cost": 150.0, "contact_cost": 1.5}
FORMULA = re.compile(r"[s0-9+\-*/() .]+")
TOKEN = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?")


@dataclass(frozen=True)
class World:
    client: TestClient
    storage: LocalStorage
    data_dir: Path
    propensity: ScoredRun
    banded: str
    """The propensity campaign whose Medium band was harmed."""
    uplift: str
    """The uplift campaign with the planted positive effect, value inputs entered on its run."""


def _measured_campaign(
    client: TestClient, run_id: str, outcomes: pd.DataFrame, *, synthetic: bool = False
) -> str:
    created = ok(client.post("/campaigns", json={"run_id": run_id}), 201)
    campaign_id = str(created["campaign"]["campaign_id"])
    payload = outcomes.to_csv(index=False, lineterminator="\n").encode()
    response = client.post(
        "/uploads",
        files={"file": ("outcomes.csv", payload, "text/csv")},
        data={
            "use_case": "win-back-campaign",
            "mode": "score",
            **({"synthetic": "true"} if synthetic else {}),
        },
    )
    upload_id = ok(response, 201)["upload_id"]
    ok(
        client.post(
            f"/campaigns/{campaign_id}/outcomes", json={"upload_id": upload_id, "outcome_column": "converted"}
        )
    )
    ok(client.post(f"/campaigns/{campaign_id}/measure", json={"as_of": MATURE.isoformat()}))
    return campaign_id


def _banded_outcomes(scores: pd.DataFrame, *, seed: int) -> pd.DataFrame:
    """The campaign helped nobody in Low and harmed Medium by ten points (planted)."""
    contacted = scores["suppressed_reason"].isna().to_numpy() & ~scores["control_group"].to_numpy(dtype=bool)
    return segment_outcomes(
        scores["customer_id"],
        scores["band"],
        contacted,
        base_rate=0.15,
        effects={NEUTRAL_BAND: 0.0, HARMED_BAND: -0.10},
        seed=seed,
    )


@pytest.fixture(scope="module")
def world(config_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    data_dir = tmp_path_factory.mktemp("proof") / "data"
    data_dir.mkdir()
    storage = LocalStorage(data_dir)
    propensity = propensity_run(storage, PROPENSITY_RUN, rows=12_000)
    scored = uplift_run(storage, UPLIFT_RUN, rows=12_000)
    with TestClient(create_app(config_root=config_root, data_dir=data_dir)) as client:
        banded = _measured_campaign(client, PROPENSITY_RUN, _banded_outcomes(propensity.scores, seed=21))
        uplift = _measured_campaign(
            client, UPLIFT_RUN, scored.outcomes.rename(columns={"reactivated_90d": "converted"})
        )
        ok(client.put(f"/pilot/roi/{UPLIFT_RUN}", json=VALUE_INPUTS))
        yield World(client, storage, data_dir, propensity, banded, uplift)


def _proof(world: World, campaign_id: str) -> dict[str, Any]:
    return dict(ok(world.client.get(f"/pilot/proof/{campaign_id}", params={"format": "json"})))


def _section(view: dict[str, Any], key: str) -> dict[str, Any]:
    return next(section for section in view["sections"] if section["key"] == key)


def _figures(item: Any) -> Iterator[dict[str, Any]]:
    if isinstance(item, dict):
        if {"value", "text", "format", "sources"} <= set(item):
            yield item
            return
        for value in item.values():
            yield from _figures(value)
    elif isinstance(item, list):
        for value in item:
            yield from _figures(value)


def _loose_numbers(item: Any, path: str = "") -> Iterator[str]:
    """Every number of the view that is not a figure's value: none may exist (the contract's own
    `schema_version`, never printed, is the one exception)."""
    if isinstance(item, dict):
        if {"value", "text", "format", "sources"} <= set(item):
            return
        for key, value in item.items():
            if path == "" and key == "schema_version":
                continue
            yield from _loose_numbers(value, f"{path}.{key}")
    elif isinstance(item, list):
        for index, value in enumerate(item):
            yield from _loose_numbers(value, f"{path}.{index}")
    elif isinstance(item, int | float) and not isinstance(item, bool):
        yield path


def _resolve(data_dir: Path, figure: dict[str, Any]) -> Any:
    """An independent resolver: read each source's JSON from disk, walk its path, apply the formula."""
    values = []
    for source in figure["sources"]:
        current: Any = json.loads((data_dir / source["artefact"]).read_text(encoding="utf-8"))
        for part in source["field"].split("."):
            current = current[int(part)] if isinstance(current, list) else current[part]
        values.append(current)
    if figure["formula"] is None:
        return values[0]
    assert FORMULA.fullmatch(figure["formula"]), figure["formula"]
    names = {f"s{index}": float(value) for index, value in enumerate(values)}
    expression = re.sub(r"s\d+", lambda match: repr(names[match.group(0)]), figure["formula"])
    # Only digits, the four operations and brackets remain (checked above), so eval computes arithmetic.
    return eval(expression, {"__builtins__": {}}, {})


def assert_traced(data_dir: Path, view: dict[str, Any]) -> None:
    loose = list(_loose_numbers(view))
    assert not loose, f"numbers not traced to an artefact: {loose}"
    figures = list(_figures(view))
    assert figures, "a pack prints numbers"
    for figure in figures:
        expected = _resolve(data_dir, figure)
        if isinstance(expected, str):
            assert figure["value"] == expected, figure
        else:
            assert figure["value"] == pytest.approx(expected, rel=1e-9, abs=1e-9), figure
        for source in figure["sources"]:
            assert source["artefact"].endswith(".json"), "a figure is read from an aggregate file"
            assert "assignment" not in source["artefact"] and "outcomes.parquet" not in source["artefact"]


def page_text(html: str) -> str:
    """The page's words without its markup, styles or the generated-at footer."""
    body = re.sub(r"<style>.*?</style>", " ", html, flags=re.S)
    body = re.sub(r"<footer>.*?</footer>", " ", body, flags=re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())


def pdf_text(pdf: bytes) -> str:
    from pypdf import PdfReader

    return " ".join(
        " ".join((page.extract_text() or "").split()) for page in PdfReader(io.BytesIO(pdf)).pages
    )


def pdf_body(pdf: bytes) -> str:
    """The PDF's words without its generated-at footer (the one line that is not the pack's)."""
    text = pdf_text(pdf)
    cut = text.rfind("Generated ")
    return text[:cut] if cut >= 0 else text


def assert_no_stray_digits(client: TestClient, campaign_id: str, view: dict[str, Any]) -> None:
    """Every number printed on the HTML page and in the PDF is the text of one of the view's figures, and
    neither holds a word of jargon."""
    import html as html_lib

    allowed = {token for figure in _figures(view) for token in TOKEN.findall(figure["text"])}
    page = html_lib.unescape(page_text(client.get(f"/pilot/proof/{campaign_id}").text))
    stray = sorted(set(TOKEN.findall(page)) - allowed)
    assert not stray, f"digits on the page that no figure printed: {stray}"
    pdf = client.get(f"/pilot/proof/{campaign_id}", params={"format": "pdf"})
    assert pdf.status_code == 200
    printed = pdf_body(pdf.content)
    stray = sorted(set(TOKEN.findall(printed)) - allowed)
    assert not stray, f"digits in the PDF that no figure printed: {stray}"
    assert jargon_in(page) == () and jargon_in(printed) == (), "the pack is in plain words"


# --- provenance ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("which", ["banded", "uplift"])
def test_every_number_of_the_pack_resolves_to_a_measured_artefact_field(world: World, which: str) -> None:
    view = _proof(world, getattr(world, which))
    assert_traced(world.data_dir, view)
    assert all(key.endswith(".json") for key in view["artefacts"])


@pytest.mark.parametrize("which", ["banded", "uplift"])
def test_every_digit_on_the_page_and_in_the_pdf_is_a_traced_figure(world: World, which: str) -> None:
    campaign_id = getattr(world, which)
    assert_no_stray_digits(world.client, campaign_id, _proof(world, campaign_id))


def test_a_changed_artefact_makes_the_build_fail_rather_than_show_an_untraced_number(world: World) -> None:
    from engine.pilot.proof import ProvenanceError, build_proof, verify_provenance

    view = build_proof(world.storage, world.uplift)
    key = f"campaigns/{world.uplift}/incrementality_report.json"
    original = world.storage.read_bytes(key)
    document = json.loads(original)
    try:
        document["treated_conversions"] += 1
        world.storage.write_bytes(key, json.dumps(document).encode())
        with pytest.raises(ProvenanceError):
            verify_provenance(view, world.storage)
    finally:
        world.storage.write_bytes(key, original)
    verify_provenance(view, world.storage)


# --- not measured -----------------------------------------------------------------------------------------
def test_a_missing_artefact_renders_not_measured_with_its_reason(world: World) -> None:
    view = _proof(world, world.banded)
    for key, words in (
        ("plan", "No test plan was registered"),
        ("delivery", "No contact file was added"),
        ("net_value", "No value inputs were entered"),
    ):
        section = _section(view, key)
        assert section["status"] == "not_measured", key
        assert words in section["reason"], key
        assert not list(_figures(section)), f"{key}: nothing is printed for what was not measured"
    page = page_text(world.client.get(f"/pilot/proof/{world.banded}").text)
    assert page.count("Not measured") >= 3
    assert "No value inputs were entered" in page


# --- backfire ---------------------------------------------------------------------------------------------
def test_the_planted_harmed_band_is_flagged_and_the_neutral_band_is_not(world: World) -> None:
    view = _proof(world, world.banded)
    backfire = _section(view, "backfire")
    assert backfire["status"] == "measured"
    results = {row[1]["value"]: row[-1] for row in backfire["table"]["rows"] if row[0] == "Band"}
    assert results == {HARMED_BAND: "backfired", NEUTRAL_BAND: "no backfire shown"}
    proposals = view["proposals"]
    assert [(p["dimension"], p["segment"]["value"], p["status"]) for p in proposals] == [
        ("band", HARMED_BAND, "proposed")
    ]
    assert proposals[0]["worst_case"]["value"] < 0
    page = page_text(world.client.get(f"/pilot/proof/{world.banded}").text)
    assert f"Leave {HARMED_BAND} out of the next cycle" in page


def test_the_suggestion_is_approved_by_an_analyst_recorded_and_never_applied(world: World) -> None:
    scores_before = world.storage.read_bytes(f"runs/{PROPENSITY_RUN}/scores.parquet")
    refused = world.client.post(
        f"/pilot/proof/{world.banded}/suppressions", json={"dimension": "band", "segment": NEUTRAL_BAND}
    )
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "PROOF_SUPPRESSION_INVALID"
    approved = ok(
        world.client.post(
            f"/pilot/proof/{world.banded}/suppressions", json={"dimension": "band", "segment": HARMED_BAND}
        ),
        201,
    )
    assert approved["segment"] == HARMED_BAND and approved["approved_by"]
    again = ok(
        world.client.post(
            f"/pilot/proof/{world.banded}/suppressions", json={"dimension": "band", "segment": HARMED_BAND}
        ),
        201,
    )
    assert again == approved, "approving twice records one approval"
    view = _proof(world, world.banded)
    assert [p["status"] for p in view["proposals"]] == ["approved"]
    assert_traced(world.data_dir, view)
    assert world.storage.read_bytes(f"runs/{PROPENSITY_RUN}/scores.parquet") == scores_before
    assert "approved by" in page_text(world.client.get(f"/pilot/proof/{world.banded}").text)


def test_approving_a_suggestion_is_an_analyst_action_and_is_audited() -> None:
    from api.access_policy import policy_for
    from engine.access.roles import Role

    approve = policy_for("POST", "/pilot/proof/{campaign_id}/suppressions")
    read = policy_for("GET", "/pilot/proof/{campaign_id}")
    assert approve is not None and approve.role is Role.ANALYST
    assert read is not None and read.role is Role.VIEWER


# --- naive and measured -----------------------------------------------------------------------------------
def test_naive_credit_is_at_least_the_measured_credit_on_the_planted_campaign(world: World) -> None:
    view = _proof(world, world.uplift)
    credit = _section(view, "credit")
    naive, measured = credit["lines"][0], credit["lines"][1]
    assert naive["label"].startswith("Naive credit") and measured["label"].startswith("Measured credit")
    assert measured["low"]["value"] > 0, "the planted effect is measured"
    assert naive["value"]["value"] >= measured["value"]["value"]
    assert naive["value"]["value"] >= measured["high"]["value"]
    report = json.loads(world.storage.read_bytes(f"campaigns/{world.uplift}/incrementality_report.json"))
    assert (
        naive["value"]["value"] == report["treated_conversions"]
    ), "naive: every outcome among the contacted"
    money = {line["label"]: line for line in credit["lines"]}
    assert (
        money["Naive credit in rupees"]["value"]["value"]
        >= money["Measured credit in rupees"]["value"]["value"]
    )


def test_the_net_value_is_a_range_from_the_measured_interval_and_the_value_inputs(world: World) -> None:
    view = _proof(world, world.uplift)
    net = _section(view, "net_value")
    assert net["status"] == "measured"
    line = next(line for line in net["lines"] if line["label"] == "Net value")
    report = json.loads(world.storage.read_bytes(f"campaigns/{world.uplift}/incrementality_report.json"))
    campaign = json.loads(world.storage.read_bytes(f"campaigns/{world.uplift}/campaign.json"))
    # Every customer meant to be contacted was paid for, whether or not the outcomes file had them.
    paid = campaign["counts"]["intended_treated"]
    assert paid >= report["treated_rows"]
    spent = paid * 1.5 + report["treated_conversions"] * 150.0
    interval = report["incremental_conversions"]
    assert line["low"]["value"] == pytest.approx(interval["ci_low"] * 2000.0 - spent)
    assert line["high"]["value"] == pytest.approx(interval["ci_high"] * 2000.0 - spent)
    assert line["low"]["value"] < line["value"]["value"] < line["high"]["value"]
    assert view["headline"].endswith(f"Net value {line['low']['text']} to {line['high']['text']}.")
    entered = {
        line["label"]: line["value"]["text"] for line in net["lines"] if line["label"].endswith("entered")
    }
    assert entered == {
        "Value of one extra outcome, as entered": "₹2,000",
        "Cost of one contact, as entered": "₹1.50",
        "Cost of one offer taken, as entered": "₹150",
    }, "a value per unit keeps its paise: ₹1.50 never prints as ₹2"
    assert any("entered for the scoring run" in note for note in net["notes"]), "the inputs are the run's"


def test_offer_money_on_sure_things_and_sleeping_dogs_reads_the_predicted_groups(world: World) -> None:
    view = _proof(world, world.uplift)
    section = _section(view, "offer_money")
    assert section["status"] == "measured"
    groups = [row[0] for row in section["table"]["rows"]]
    assert "Persuadables" in groups
    banded = _section(_proof(world, world.banded), "offer_money")
    assert banded["status"] == "not_measured" and "campaign-effect model" in banded["reason"]


# --- rendering and privacy --------------------------------------------------------------------------------
@pytest.mark.parametrize("which", ["banded", "uplift"])
def test_html_and_pdf_render_in_plain_words_and_hold_no_customer_id(world: World, which: str) -> None:
    campaign_id = getattr(world, which)
    html = world.client.get(f"/pilot/proof/{campaign_id}")
    assert html.status_code == 200 and html.headers["content-type"].startswith("text/html")
    pdf = world.client.get(f"/pilot/proof/{campaign_id}", params={"format": "pdf"})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    page, printed = page_text(html.text), pdf_text(pdf.content)
    assert "Value Proof Pack" in page and "Value Proof Pack" in printed
    assert jargon_in(page) == () and jargon_in(printed) == ()
    raw = json.dumps(_proof(world, campaign_id))
    keys = set(world.propensity.scores["customer_id"].astype(str))
    for text in (raw, html.text, printed):
        assert not [key for key in keys if re.search(rf"\b{re.escape(key)}\b", text)], "a customer id leaked"


def test_segment_effects_are_written_beside_the_report_and_hold_no_customer_id(world: World) -> None:
    raw = world.storage.read_bytes(f"campaigns/{world.banded}/segment_effects.json").decode()
    document = json.loads(raw)
    report = json.loads(world.storage.read_bytes(f"campaigns/{world.banded}/incrementality_report.json"))
    assert document["report_computed_at"] == report["computed_at"]
    assert {cell["segment"] for cell in document["cells"]} == {HARMED_BAND, NEUTRAL_BAND}
    keys = set(world.propensity.scores["customer_id"].astype(str))
    assert not [key for key in keys if re.search(rf"\b{re.escape(key)}\b", raw)]


def test_the_list_gives_each_newest_campaigns_headline_or_refusal(world: World) -> None:
    listed = ok(world.client.get("/pilot/proof"))["proofs"]
    by_id = {entry["campaign_id"]: entry for entry in listed}
    assert by_id[world.uplift]["status"] == "ready" and by_id[world.uplift]["headline"]
    assert by_id[world.banded]["status"] == "ready"


# --- refusals ---------------------------------------------------------------------------------------------
def test_a_campaign_on_generated_data_is_refused(world: World) -> None:
    scores = world.propensity.scores
    synthetic_run = "r_20261009_31000003"
    propensity_run(world.storage, synthetic_run, rows=4_000, seed=31)
    run_scores = pd.read_parquet(io.BytesIO(world.storage.read_bytes(f"runs/{synthetic_run}/scores.parquet")))
    campaign_id = _measured_campaign(
        world.client, synthetic_run, _banded_outcomes(run_scores, seed=4), synthetic=True
    )
    del scores
    for fmt in ("json", "html", "pdf"):
        response = world.client.get(f"/pilot/proof/{campaign_id}", params={"format": fmt})
        assert response.status_code == 409, fmt
        assert response.json()["detail"]["code"] == "PROOF_SYNTHETIC_DATA"


def test_a_generated_contact_file_is_refused_too(world: World) -> None:
    frame, outcomes = _random_file(47, details=True)
    campaign_id = _audit(world, frame=frame, outcomes=outcomes, basis="random")
    contacts = pd.DataFrame({"customer_id": frame["customer_id"], "contacted": frame["group"]})
    payload = contacts.to_csv(index=False, lineterminator="\n").encode()

    def add(synthetic: bool) -> str:
        response = world.client.post(
            "/uploads",
            files={"file": ("contacts.csv", payload, "text/csv")},
            data={
                "use_case": "win-back-campaign",
                "mode": "score",
                **({"synthetic": "true"} if synthetic else {}),
            },
        )
        upload_id = str(ok(response, 201)["upload_id"])
        body = {"upload_id": upload_id, "contacted_column": "contacted"}
        ok(world.client.post(f"/campaigns/{campaign_id}/contacts", json=body))
        return upload_id

    client_file = add(synthetic=False)
    view = _proof(world, campaign_id)
    assert _section(view, "delivery")["status"] == "measured", "a client's own contact file is read"
    readout = json.loads(world.storage.read_bytes(f"campaigns/{campaign_id}/contact_readout.json"))
    assert readout["contact_upload_id"] == client_file and readout["synthetic"] is False
    generated = add(synthetic=True)
    readout = json.loads(world.storage.read_bytes(f"campaigns/{campaign_id}/contact_readout.json"))
    assert readout["contact_upload_id"] == generated and readout["synthetic"] is True
    for fmt in ("json", "html", "pdf"):
        response = world.client.get(f"/pilot/proof/{campaign_id}", params={"format": fmt})
        assert response.status_code == 409, fmt
        assert response.json()["detail"]["code"] == "PROOF_SYNTHETIC_DATA"
    # A readout that only names its upload (the flag lost) is refused all the same, from the upload's record.
    readout["synthetic"] = False
    world.storage.write_bytes(f"campaigns/{campaign_id}/contact_readout.json", json.dumps(readout).encode())
    response = world.client.get(f"/pilot/proof/{campaign_id}", params={"format": "json"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "PROOF_SYNTHETIC_DATA"


def test_a_campaign_without_a_final_result_is_refused_with_the_day_it_can_be_read(world: World) -> None:
    created = ok(world.client.post("/campaigns", json={"run_id": PROPENSITY_RUN}), 201)
    campaign_id = created["campaign"]["campaign_id"]
    response = world.client.get(f"/pilot/proof/{campaign_id}")
    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["code"] == "PROOF_NOT_MATURE"
    assert body["results_available_on"] is not None


def test_an_early_look_is_refused_until_the_planned_date(world: World) -> None:
    run_scores = world.propensity.scores
    created = ok(world.client.post("/campaigns", json={"run_id": PROPENSITY_RUN}), 201)
    campaign_id = created["campaign"]["campaign_id"]
    analysis = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    ok(
        world.client.post(
            f"/campaigns/{campaign_id}/plan",
            json={"metric": "Came back", "outcome_column": "converted", "analysis_date": analysis},
        ),
        201,
    )
    upload_id = upload(world.client, _banded_outcomes(run_scores, seed=8))
    ok(
        world.client.post(
            f"/campaigns/{campaign_id}/outcomes", json={"upload_id": upload_id, "outcome_column": "converted"}
        )
    )
    measured = ok(world.client.post(f"/campaigns/{campaign_id}/measure", json={"as_of": MATURE.isoformat()}))
    assert measured["report"]["early_look"] is True
    response = world.client.get(f"/pilot/proof/{campaign_id}")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PROOF_NOT_MATURE"
    assert response.json()["results_available_on"] == analysis


def test_an_unknown_campaign_is_not_found(world: World) -> None:
    response = world.client.get("/pilot/proof/c_20260101_00000000")
    assert response.status_code == 404 and response.json()["detail"]["code"] == "CAMPAIGN_NOT_FOUND"


# --- what the numbers may claim -----------------------------------------------------------------------------
def _audit(world: World, *, frame: pd.DataFrame, outcomes: pd.DataFrame, basis: str) -> str:
    who = upload(world.client, frame, name="assignment.csv")
    what = upload(world.client, outcomes, name="outcomes.csv")
    body = {
        "primary_key": "customer_id",
        "assignment": {"upload_id": who, "arm_column": "group"},
        "outcomes": {
            "upload_id": what,
            "outcome_column": "converted",
            "treatment_date_column": "treatment_date",
        },
        "assignment_basis": basis,
        "treatment_start": "2026-04-01T00:00:00Z",
        "outcome_window_days": OUTCOME_WINDOW_DAYS,
        "as_of": AS_OF.isoformat(),
    }
    return str(ok(world.client.post("/campaigns/audit", json=body), 201)["campaign"]["campaign_id"])


def _random_file(seed: int, *, details: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    campaign = population(8_000, 0.10, 0.04, seed=seed, control_share=0.2)
    scores = campaign.scores
    frame = pd.DataFrame(
        {"customer_id": scores["customer_id"], "group": np.where(scores["control_group"], 0, 1)}
    )
    if details:
        rng = np.random.default_rng(seed)
        frame["age"] = rng.integers(18, 80, len(frame))
    return frame, campaign.outcomes


def _offers_audit(world: World, sim: Any, frame: pd.DataFrame, *, basis: str = "random") -> str:
    """`POST /campaigns/audit` of a campaign of several offers against one shared control group."""
    who = upload(world.client, frame, name="assignment.csv")
    what = upload(world.client, sim.outcomes, name="outcomes.csv")
    body = {
        "primary_key": "customer_id",
        "assignment": {
            "upload_id": who,
            "arm_column": "group",
            "control_value": sim.levels[0],
            "treated_values": list(sim.levels[1:]),
        },
        "outcomes": {
            "upload_id": what,
            "outcome_column": "converted",
            "treatment_date_column": "treatment_date",
        },
        "assignment_basis": basis,
        "treatment_start": "2026-04-01T00:00:00Z",
        "outcome_window_days": OUTCOME_WINDOW_DAYS,
        "as_of": AS_OF.isoformat(),
    }
    return str(ok(world.client.post("/campaigns/audit", json=body), 201)["campaign"]["campaign_id"])


def test_a_campaign_random_only_by_statement_is_never_shown_as_proven(world: World) -> None:
    # Two offers, the second planted to harm (twelve in a hundred come back untouched, five when given it);
    # the file has no customer details, so the random assignment is the person's statement only.
    sim = multi_arm_campaign(9_000, 0.12, (0.04, -0.07), seed=4104)
    frame = pd.DataFrame(
        {
            "customer_id": sim.scores["customer_id"],
            "group": np.where(sim.scores["control_group"], sim.levels[0], sim.scores["offer"]),
        }
    )
    campaign_id = _offers_audit(world, sim, frame)
    harmful = sim.levels[2]
    view = _proof(world, campaign_id)
    assert view["claim"] == "stated_random"
    assert view["claim_label"] == "Random by your statement, not verified"
    assert view["headline"].startswith("If the groups were random as you said")
    page = page_text(world.client.get(f"/pilot/proof/{campaign_id}").text)
    assert "proven" not in page.replace("not as proven value", "").replace("are proven:", "")
    assert "Causal:" not in page
    for key in ("incremental", "credit", "net_value"):
        for line in _section(view, key)["lines"]:
            if line["label"].startswith(("Measured credit", "Extra outcomes", "Net value", "Value of what")):
                assert line["label"].startswith("If the groups were random"), line["label"]
    # The planted harm is found, but shown under the statement it rests on, never as proven harm.
    backfire = _section(view, "backfire")
    verdicts = {row[1]["value"]: row[-1] for row in backfire["table"]["rows"] if row[0] == "Offer"}
    assert verdicts[harmful] == "If the groups were random as you said: backfired"
    assert "backfired" not in verdicts.values()
    assert [(p["dimension"], p["segment"]["value"]) for p in view["proposals"]] == [("offer", harmful)]
    assert f"If the groups were random as you said, leave {harmful} out of the next cycle" in page
    assert f"Leave {harmful} out" not in page
    assert_traced(world.data_dir, view)
    assert_no_stray_digits(world.client, campaign_id, view)


def test_a_descriptive_only_campaign_credits_nothing_to_the_campaign(world: World) -> None:
    frame, outcomes = _random_file(43, details=True)
    campaign_id = _audit(world, frame=frame, outcomes=outcomes, basis="not_random")
    ok(world.client.put(f"/pilot/proof/{campaign_id}/value", json=VALUE_INPUTS))
    view = _proof(world, campaign_id)
    assert view["claim"] == "descriptive"
    assert view["headline"].startswith("Descriptive only")
    assert _section(view, "net_value")["status"] == "not_measured"
    assert _section(view, "backfire")["status"] == "not_measured"
    credit = _section(view, "credit")
    assert [line["missing"] is not None for line in credit["lines"]][1] is True, "no measured credit"
    assert_traced(world.data_dir, view)
    assert_no_stray_digits(world.client, campaign_id, view)


def test_an_audited_campaign_of_several_offers_shows_each_offer_and_reads_each_against_the_shared_control(
    world: World,
) -> None:
    sim = multi_arm_campaign(9_000, 0.10, (0.03, 0.06), seed=104)
    rng = np.random.default_rng(104)
    frame = pd.DataFrame(
        {
            "customer_id": sim.scores["customer_id"],
            "group": np.where(sim.scores["control_group"], sim.levels[0], sim.scores["offer"]),
            "age": rng.integers(18, 80, len(sim.scores)),
        }
    )
    campaign_id = _offers_audit(world, sim, frame)
    view = _proof(world, campaign_id)
    offers = _section(view, "incremental")["table"]
    assert offers is not None and [row[0]["value"] for row in offers["rows"]] == list(sim.levels[1:])
    groups = json.loads(world.storage.read_bytes(f"campaigns/{campaign_id}/segment_effects.json"))
    report = json.loads(world.storage.read_bytes(f"campaigns/{campaign_id}/incrementality_report.json"))
    by_offer = {cell["segment"]: cell for cell in groups["cells"] if cell["dimension"] == "offer"}
    for arm in report["arms"]:
        assert by_offer[arm["arm"]]["comparison"] == "shared_control"
        assert by_offer[arm["arm"]]["effect"] == arm["effect"]
    assert _section(view, "backfire")["status"] == "measured", "the offers are the groups judged"
    assert_traced(world.data_dir, view)
    assert_no_stray_digits(world.client, campaign_id, view)


def test_a_verified_random_audit_is_proven_and_its_value_inputs_are_its_own(world: World) -> None:
    frame, outcomes = _random_file(45, details=True)
    campaign_id = _audit(world, frame=frame, outcomes=outcomes, basis="random")
    ok(world.client.put(f"/pilot/proof/{campaign_id}/value", json=VALUE_INPUTS))
    view = _proof(world, campaign_id)
    assert view["claim"] == "proven"
    assert _section(view, "net_value")["status"] == "measured"
    assert any(key.endswith(f"campaigns/{campaign_id}/pilot_roi_inputs.json") for key in view["artefacts"])
    backfire = _section(view, "backfire")
    assert backfire["status"] == "not_measured" and "No group" in backfire["reason"]
    # The reason for each kind of group is the one the measurement recorded, read from its file, never made up.
    recorded = json.loads(world.storage.read_bytes(f"campaigns/{campaign_id}/segment_effects.json"))
    assert [line["value"]["value"] for line in backfire["lines"]] == [
        note["reason"] for note in recorded["not_measured"]
    ]
    assert any("has a band" in line["value"]["value"] for line in backfire["lines"])
    assert_traced(world.data_dir, view)
    assert_no_stray_digits(world.client, campaign_id, view)
