"""The campaign planner's safety fixes from the autonomy audit (Plan M M184, DEC-1614).

(a) What EXECUTE carries out is what the policy checked: the run's treat list is limited to the approved channel
    and at most `planned_contacts`, the held-back group untouched, or nothing is opened.
(b) A scoring run whose model was never approved is not planned, and not carried out, while the use case
    requires approval.
(e) With `agentic.require_sign_in` on, an approval by the local operator (sign-in off) is not carried out; with
    it off (the default) it is, and the ledger says the approver's identity was not verified.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.agentic.errors import AgenticError
from engine.agentic.ledger import DecisionLedger
from engine.agentic.planner import (
    BuiltList,
    ExecuteActions,
    ListLimits,
    PlannerDeps,
    PlanRequest,
    execute_plan,
    start_plan,
)
from engine.agentic.policy import CampaignPlanPayload
from engine.agentic.proposals import ProposalStore
from engine.agentic.tools import default_registry
from engine.contracts import ModelStatus, RunRecord
from engine.decide.campaign_scope import CAMPAIGN_SCOPE_FILENAME
from engine.decide.treat_list import TREAT_LIST_PARQUET, TREAT_LIST_SUMMARY_FILENAME, TreatListSummary
from engine.platform_db import PLATFORM_DB_FILENAME, sqlite_engine
from engine.registry import LocalModelRegistry
from engine.runs import RUN_FILENAME
from engine.storage import run_key
from tests.agentic.scenarios.planner_world import PlannerWorld

pytestmark = pytest.mark.integration

FORM = {"goal": {"use_case_id": "win-back-campaign", "channel": "sms"}}


def _entries(pw: PlannerWorld, proposal_id: str) -> list[Any]:
    ledger = DecisionLedger(
        sqlite_engine(pw.world.data_dir / PLATFORM_DB_FILENAME), signer=None, policy_version="unused"
    )
    return [entry for entry in ledger.entries() if entry.body.get("proposal_id") == proposal_id]


def _draft(client: TestClient) -> dict[str, Any]:
    drafted = client.post("/agentic/plans", json=FORM)
    assert drafted.status_code == 201, drafted.text
    proposal = drafted.json()["proposal"]
    assert proposal["status"] == "pending"
    return dict(proposal)


def _treat_list(pw: PlannerWorld) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(pw.world.storage.read_bytes(run_key(pw.run_id, TREAT_LIST_PARQUET))))


# ---------------------------------------------------------------------------
# (a) the executed list is the checked one
# ---------------------------------------------------------------------------
def test_an_approved_plan_sends_only_on_its_channel_at_most_its_contacts_and_keeps_the_holdout(
    fresh_planner_world: PlannerWorld,
) -> None:
    from engine.decide.treat_list import build_treat_list

    pw = fresh_planner_world
    storage = pw.world.storage
    # What the run's list holds on its own (the M98 download), before anything is approved.
    whole = build_treat_list(storage, pw.run_id, config_root=pw.world.config_root)
    unlimited = _treat_list(pw)
    with pw.world.client() as client:
        proposal = _draft(client)
        payload = CampaignPlanPayload.model_validate(proposal["payload"])
        approved = client.post(f"/agentic/proposals/{proposal['proposal_id']}/approve", json={})
        assert approved.status_code == 200, approved.text
        campaigns = client.get("/campaigns", params={"run_id": pw.run_id}).json()["campaigns"]
        assert len(campaigns) == 1

    summary = storage.read_model(run_key(pw.run_id, TREAT_LIST_SUMMARY_FILENAME), TreatListSummary)
    limited = _treat_list(pw)
    assert payload.planned_contacts is not None
    assert summary.campaign_scope_channel == payload.channel == "sms"
    assert summary.campaign_scope_max_contacts == payload.planned_contacts
    assert summary.campaign_scope_proposal_id == proposal["proposal_id"]
    assert summary.treat_rows <= payload.planned_contacts
    assert set(limited.loc[limited["treat"], "channel"]) <= {payload.channel}
    assert (summary.channel_rows or {}).keys() <= {payload.channel}
    # Nobody the run's own list left out is contacted, and the comparison group is exactly what it was.
    assert not (limited["treat"] & ~unlimited["treat"]).any()
    for column in ("holdout", "control_group", "suppression_reason"):
        assert limited[column].equals(unlimited[column]), column
    assert summary.holdout_rows == whole.holdout_rows
    assert int(limited["control_group"].sum()) == campaigns[0]["counts"]["holdout"] > 0
    plan = storage.read_model(run_key(pw.run_id, RUN_FILENAME), RunRecord)
    assert plan.run_id == pw.run_id
    assert storage.exists(run_key(pw.run_id, CAMPAIGN_SCOPE_FILENAME))


def _proposal(pw: PlannerWorld) -> Any:
    state = start_plan(
        PlanRequest.model_validate(FORM), pw.world.context(), PlannerDeps(registry=default_registry())
    )
    assert state.proposal_id is not None, state.stopped
    return ProposalStore(pw.world.storage).get(state.proposal_id)


@pytest.mark.parametrize(
    ("channel_rows", "treat_rows"),
    [
        ({"sms": 10, "email": 5}, 15),  # another channel
        ({"sms": 10}, 12),  # treated customers with no channel
        ({"sms": 10_000}, 10_000),  # more than were approved
    ],
    ids=["another-channel", "no-channel", "over-the-number"],
)
def test_execute_refuses_a_list_that_is_not_what_was_approved(
    planner_world: PlannerWorld, channel_rows: dict[str, int], treat_rows: int
) -> None:
    pw = planner_world
    proposal = _proposal(pw)
    payload = CampaignPlanPayload.model_validate(proposal.payload)
    asked: list[ListLimits] = []
    opened: list[str] = []

    def build(run_id: str, limits: ListLimits) -> BuiltList:
        asked.append(limits)
        return BuiltList(total_rows=20_000, treat_rows=treat_rows, channel_rows=channel_rows)

    def never(*args: Any) -> Any:
        opened.append(str(args))
        raise AssertionError("nothing is opened for a list that is not the approved one")

    actions = ExecuteActions(build_treat_list=build, open_campaign=never, register_plan=never)
    with pytest.raises(AgenticError) as refused:
        execute_plan(proposal, pw.world.context(), actions)
    assert refused.value.code == "CAMPAIGN_LIST_NOT_AS_APPROVED" and refused.value.status == 409
    assert asked == [
        ListLimits(channel="sms", max_contacts=payload.planned_contacts, proposal_id=proposal.proposal_id)
    ]
    assert opened == []


# ---------------------------------------------------------------------------
# (b) a model that was never approved
# ---------------------------------------------------------------------------
class _Registry:
    """The world's model registry, answering every version with another status (never approved)."""

    def __init__(self, real: LocalModelRegistry, status: ModelStatus) -> None:
        self._real = real
        self._status = status

    def get(self, model_id: str) -> Any:
        version = self._real.get(model_id)
        return version.model_copy(
            update={"status": self._status, "approved_at": None, "promoted_at": None, "promoted_by": None}
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_a_run_whose_model_was_never_approved_is_not_planned(planner_world: PlannerWorld) -> None:
    pw = planner_world
    real = LocalModelRegistry(pw.world.data_dir / "registry.db")
    for status in (ModelStatus.PENDING_APPROVAL, ModelStatus.CANDIDATE):
        state = start_plan(
            PlanRequest.model_validate(FORM),
            pw.world.context(),
            PlannerDeps(registry=default_registry(), model_registry=_Registry(real, status)),
        )
        assert state.stopped is not None and state.stopped.code == "PLANNER_NO_CANDIDATE"
        assert "never approved" in state.stopped.message and state.proposal_id is None
    # The champion's runs are planned as before.
    approved = start_plan(
        PlanRequest.model_validate(FORM),
        pw.world.context(),
        PlannerDeps(registry=default_registry(), model_registry=real),
    )
    assert approved.stopped is None and approved.proposal_id is not None


def test_an_approval_of_a_run_whose_model_is_not_approved_carries_nothing_out(
    fresh_planner_world: PlannerWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    import engine.model_approval as model_approval

    pw = fresh_planner_world
    with pw.world.client() as client:
        proposal = _draft(client)
        monkeypatch.setattr(
            model_approval,
            "unapproved_model_message",
            lambda _registry, _model: "The model was never approved.",
        )
        refused = client.post(f"/agentic/proposals/{proposal['proposal_id']}/approve", json={})
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "MODEL_VERSION_NOT_APPROVED"
        assert client.get(f"/agentic/proposals/{proposal['proposal_id']}").json()["status"] == "pending"
        assert client.get("/campaigns", params={"run_id": pw.run_id}).json()["campaigns"] == []
    assert not pw.world.storage.exists(run_key(pw.run_id, CAMPAIGN_SCOPE_FILENAME)), "nothing was built"
    failed = [e for e in _entries(pw, proposal["proposal_id"]) if e.kind == "proposal.execution_failed"]
    assert [e.body["codes"] for e in failed] == [["MODEL_VERSION_NOT_APPROVED"]]


# ---------------------------------------------------------------------------
# (e) who approved, with sign-in off
# ---------------------------------------------------------------------------
def test_with_sign_in_off_an_approval_is_carried_out_and_marked_unverified(
    fresh_planner_world: PlannerWorld,
) -> None:
    pw = fresh_planner_world
    with pw.world.client() as client:
        proposal = _draft(client)
        approved = client.post(f"/agentic/proposals/{proposal['proposal_id']}/approve", json={})
        assert approved.status_code == 200, approved.text
    (entry,) = [e for e in _entries(pw, proposal["proposal_id"]) if e.kind == "proposal.approved"]
    assert entry.body["identity_verified"] is False
    assert entry.signature, "still signed"


def test_require_sign_in_refuses_the_local_operators_approval(
    fresh_planner_world: PlannerWorld, tmp_path: Path
) -> None:
    pw = fresh_planner_world
    root = tmp_path / "configs"
    shutil.copytree(pw.world.config_root, root)
    (root / "agentic" / "agentic.yaml").write_text(
        "agentic:\n  enabled: true\n  require_sign_in: true\n", encoding="utf-8"
    )
    with TestClient(create_app(config_root=root, data_dir=pw.world.data_dir)) as client:
        proposal = _draft(client)
        refused = client.post(f"/agentic/proposals/{proposal['proposal_id']}/approve", json={})
        assert refused.status_code == 503, refused.text
        assert refused.json()["detail"]["code"] == "AUTH_NOT_CONFIGURED"
        assert client.get(f"/agentic/proposals/{proposal['proposal_id']}").json()["status"] == "pending"
        assert client.get("/campaigns", params={"run_id": pw.run_id}).json()["campaigns"] == []
    failed = [e for e in _entries(pw, proposal["proposal_id"]) if e.kind == "proposal.execution_failed"]
    assert [e.body["codes"] for e in failed] == [["AUTH_NOT_CONFIGURED"]]
