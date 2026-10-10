"""The read tools on a real run and real campaigns: the same numbers as the product's own routes, read once.

Each tool reads what the product already computed and stored (`compute once`); what is not computed yet is
null with a plain reason, never a 0. Every output is aggregate and fits the 16 KB budget.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agentic.canonical import canonical_bytes
from engine.agentic.errors import AgenticError
from engine.agentic.registry import DEFAULT_OUTPUT_BUDGET
from engine.agentic.tools import default_registry
from engine.storage import run_key
from tests.agentic.conftest import upload_frame
from tests.agentic.scenarios.world import TRAIN_RUN, World

pytestmark = pytest.mark.integration


def _call(world: World, tool: str, given: dict[str, Any]) -> Any:
    result = default_registry().call(tool, given, world.context(principal="viewer"))
    assert len(canonical_bytes(result.output)) <= DEFAULT_OUTPUT_BUDGET
    return result


def test_check_contactable_reads_the_runs_own_counts(agentic_world: World) -> None:
    result = _call(agentic_world, "check_contactable", {"run_id": agentic_world.run_id})
    stored = json.loads(
        agentic_world.storage.read_bytes(run_key(agentic_world.run_id, "channel_contactability.json"))
    )
    out = result.output
    assert out["recorded"] is True and out["eligible_rows"] == stored["eligible_rows"]
    by_channel = {item["channel"]: item for item in out["channels"]}
    assert by_channel["sms"]["not_contactable_eligible_rows"] == stored["channel_counts"]["sms"] > 0
    assert by_channel["email"]["not_contactable_eligible_rows"] == 0
    assert by_channel["sms"]["reasons"] == {"not_contactable": stored["channel_counts"]["sms"]}
    assert by_channel["sms"]["columns_missing"] == [] and by_channel["email"]["columns_missing"] == []
    assert result.label == "Descriptive" and result.artefact_refs == (agentic_world.run_id,)
    unrecorded = _call(agentic_world, "check_contactable", {"run_id": TRAIN_RUN}).output
    assert unrecorded["recorded"] is False and unrecorded["reason"] and unrecorded["channels"] == []


def test_measure_returns_the_stored_report_and_never_measures_again(
    agentic_world: World, measured: str
) -> None:
    with agentic_world.client() as client:
        view = client.get(f"/campaigns/{measured}").json()
    first = _call(agentic_world, "measure", {"campaign_id": measured})
    again = _call(agentic_world, "measure", {"campaign_id": measured})
    assert first.output["measured"] is True and first.output["report"] == view["report"]
    assert first.evidence_id == again.evidence_id, "the same stored report, the same evidence"
    assert first.output["report"]["computed_at"] == view["report"]["computed_at"], "read, not recomputed"
    assert first.label == "Causal"


def test_measure_says_not_measured_instead_of_inventing_a_number(agentic_world: World) -> None:
    out = _call(agentic_world, "measure", {"campaign_id": agentic_world.open_campaign}).output
    assert out["measured"] is False and out["report"] is None and "not been measured" in out["reason"]
    with pytest.raises(AgenticError) as caught:
        _call(agentic_world, "measure", {"campaign_id": "c_20260101_00000000"})
    assert caught.value.code == "CAMPAIGN_NOT_FOUND"


def test_the_proof_pack_tool_says_what_the_proof_route_says(agentic_world: World, measured: str) -> None:
    result = _call(agentic_world, "get_proof_pack", {"campaign_id": measured})
    with agentic_world.client() as client:
        response = client.get(f"/pilot/proof/{measured}", params={"format": "json"})
    out = result.output
    if response.status_code == 200:
        pack = response.json()
        assert (
            out["available"] is True and out["headline"] == pack["headline"] and out["claim"] == pack["claim"]
        )
        assert [section["key"] for section in out["sections"]] == [
            section["key"] for section in pack["sections"]
        ]
        assert out["artefacts"] == pack["artefacts"]
    else:
        assert out["available"] is False and out["code"] == response.json()["detail"]["code"]


def test_audit_value_and_policy_tools_say_what_is_not_there(agentic_world: World, measured: str) -> None:
    audit = _call(agentic_world, "audit_campaign", {"campaign_id": measured}).output
    assert audit["audited"] is False and audit["audit"] is None and audit["reason"]
    value = _call(agentic_world, "value_segment", {"run_id": agentic_world.run_id}).output
    assert value["valued"] is False and value["total_inr"] is None and value["reason"]
    policy = _call(agentic_world, "evaluate_policy", {"run_id": agentic_world.run_id}).output
    assert policy["ope"] is None and policy["ope_reason"] and policy["risk_comparison"] is None
    with pytest.raises(AgenticError) as caught:
        _call(agentic_world, "evaluate_policy", {"run_id": "r_20260101_00000000"})
    assert caught.value.code == "RUN_NOT_FOUND"


# --- the tools on real stored artefacts ------------------------------------------------------------------
VALUE_RUN = "r_20261010_a1320003"
NOT_RANDOM_RUN = "r_20261010_a1320004"


@pytest.fixture(scope="module")
def valued(agentic_world: World) -> str:
    """A propensity scoring run with `uplift.policy.value_column`: M97 writes its `expected_gross_value.json`
    (`tests/fixtures/decide/treat_runs.write_run`, the product's own actions stage and value report)."""
    from tests.fixtures.decide.treat_runs import write_run

    write_run(agentic_world.storage, VALUE_RUN, kind="propensity", rows=60, value=True)
    return VALUE_RUN


@pytest.fixture(scope="module")
def evaluated(agentic_world: World) -> dict[str, Any]:
    """A candidate policy evaluated on the training run's randomised rows (`POST /runs/{id}/uplift/ope`)."""
    with agentic_world.client() as client:
        response = client.post(f"/runs/{TRAIN_RUN}/uplift/ope", json={"top_share": 0.3})
    assert response.status_code == 200, response.text
    return dict(response.json())


@pytest.fixture(scope="module")
def audited(agentic_world: World) -> dict[str, Any]:
    """Another tool's randomised campaign audited through `POST /campaigns/audit` (M103's simulator)."""
    from engine.measurement.simulate import AS_OF, OUTCOME_WINDOW_DAYS, population

    campaign = population(6_000, 0.10, 0.04, seed=13201, control_share=0.2)
    scores = campaign.scores
    rng = np.random.default_rng(13201)
    who = pd.DataFrame(
        {
            "customer_id": scores["customer_id"],
            "group": np.where(scores["control_group"], 0, 1),
            "age": rng.integers(18, 80, len(scores)),
            "region": rng.choice(list("ABCD"), len(scores)),
        }
    )
    with agentic_world.client() as client:
        body = {
            "primary_key": "customer_id",
            "assignment": {
                "upload_id": upload_frame(client, who, name="assignment.csv"),
                "arm_column": "group",
            },
            "outcomes": {
                "upload_id": upload_frame(client, campaign.outcomes),
                "outcome_column": "converted",
                "treatment_date_column": "treatment_date",
            },
            "assignment_basis": "random",
            "treatment_start": "2026-04-01T00:00:00Z",
            "outcome_window_days": OUTCOME_WINDOW_DAYS,
            "as_of": AS_OF.isoformat(),
        }
        response = client.post("/campaigns/audit", json=body)
        assert response.status_code == 201, response.text
        view = dict(response.json())
        shown = client.get(f"/campaigns/{view['campaign']['campaign_id']}").json()
    return {"view": view, "shown": shown}


def test_value_segment_reads_the_runs_stored_value(agentic_world: World, valued: str) -> None:
    """No route serves `expected_gross_value.json` by name (the treat list summary carries its total), so
    the tool's figures are held to the stored artefact itself, field by field."""
    from engine.decide.value import EXPECTED_GROSS_VALUE_FILENAME

    stored = json.loads(agentic_world.storage.read_bytes(run_key(valued, EXPECTED_GROSS_VALUE_FILENAME)))
    result = _call(agentic_world, "value_segment", {"run_id": valued})
    out = result.output
    assert out["valued"] is True and out["reason"] is None and out["label"] == stored["label"]
    assert out["total_inr"] == stored["total"] and out["contactable_rows"] == stored["contactable_rows"]
    assert out["rows_missing_value"] == stored["rows_missing_value"]
    assert out["value_column"] == stored["value_column"] and out["note"] == stored["note"]
    assert [(b["band"], b["rows"], b["rows_valued"], b["total_inr"]) for b in out["bands"]] == [
        (b["band"], b["rows"], b["rows_valued"], b["total"]) for b in stored["by_band"]
    ]
    assert result.label == "Descriptive" and result.artefact_refs == (valued,)
    band = stored["by_band"][0]["band"]
    one = _call(agentic_world, "value_segment", {"run_id": valued, "band": band}).output
    assert [b["band"] for b in one["bands"]] == [band] and one["total_inr"] is None
    absent = _call(agentic_world, "value_segment", {"run_id": valued, "band": "nope"}).output
    assert absent["bands"] == [] and "nope" in absent["reason"]


def test_evaluate_policy_reads_the_stored_estimate_and_comparison(
    agentic_world: World, evaluated: dict[str, Any]
) -> None:
    with agentic_world.client() as client:
        comparison = client.get(f"/runs/{TRAIN_RUN}/risk-comparison")
        ope = client.get(f"/runs/{TRAIN_RUN}/uplift/ope_report.json")
    assert comparison.status_code == 200 and ope.status_code == 200, (comparison.text, ope.text)
    result = _call(agentic_world, "evaluate_policy", {"run_id": TRAIN_RUN})
    out = result.output
    assert out["ope"] == ope.json() == evaluated and out["ope_reason"] is None
    assert out["risk_comparison"] == comparison.json() and out["risk_comparison_reason"] is None
    assert out["ope"]["causal"] is True and out["risk_comparison"]["causal"] is True
    assert result.label == "Estimated"


def test_a_comparison_on_treatment_not_random_is_labelled_not_proven(agentic_world: World) -> None:
    """The stored comparison says the treatment was not random: the label must not claim an estimate."""
    from engine.runs import RUN_FILENAME
    from engine.uplift.contracts import RISK_COMPARISON_FILENAME, RiskComparison

    storage = agentic_world.storage
    stored = storage.read_model(run_key(TRAIN_RUN, RISK_COMPARISON_FILENAME), RiskComparison)
    storage.write_bytes(
        run_key(NOT_RANDOM_RUN, RUN_FILENAME), storage.read_bytes(run_key(TRAIN_RUN, RUN_FILENAME))
    )
    storage.write_model(
        run_key(NOT_RANDOM_RUN, RISK_COMPARISON_FILENAME),
        stored.model_copy(update={"causal": False, "run_id": NOT_RANDOM_RUN}),
    )
    result = _call(agentic_world, "evaluate_policy", {"run_id": NOT_RANDOM_RUN})
    assert result.output["ope"] is None and result.output["risk_comparison"]["causal"] is False
    assert result.label == "Not proven"


def test_audit_campaign_reads_the_stored_audit_and_report(
    agentic_world: World, audited: dict[str, Any]
) -> None:
    view, shown = audited["view"], audited["shown"]
    campaign_id = view["campaign"]["campaign_id"]
    result = _call(agentic_world, "audit_campaign", {"campaign_id": campaign_id})
    out = result.output
    assert out["audited"] is True and out["reason"] is None
    assert out["audit"] == view["audit"] and out["report"] == shown["report"]
    assert out["audit"]["causal_basis"] == "verified_random" and result.label == "Causal"


def test_a_stored_report_that_does_not_read_is_not_called_unmeasured(agentic_world: World) -> None:
    from engine.measurement.campaign import REPORT_FILENAME, campaign_key

    with agentic_world.client() as client:
        campaign_id = agentic_world.new_campaign(client)
    agentic_world.storage.write_bytes(campaign_key(campaign_id, REPORT_FILENAME), b'{"campaign_id": ')
    with pytest.raises(AgenticError) as caught:
        _call(agentic_world, "measure", {"campaign_id": campaign_id})
    assert caught.value.code == "TOOL_ARTEFACT_UNREADABLE" and REPORT_FILENAME in caught.value.message
