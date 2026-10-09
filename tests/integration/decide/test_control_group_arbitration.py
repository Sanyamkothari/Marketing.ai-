"""`POST /decide/arbitrate` protects a use case's own per-run control group (Plan J, DEC-1311 (al)-(ap)).

Closes the gap `docs/handoff/M101.md` section 7 item 2 named: a customer in use case A's control group (Phase 1's
actions stage, not M92's persistent hold-out) could be treated by use case B. The runs are written by the
product's own code (`tests/fixtures/decide/treat_runs.py`) and their treat lists by `build_treat_list`; the
arbitration, the campaigns and their assignment files are what the API wrote.

The tests that fail on the commit before the fix: a customer in A's control group is treated by B (the first
two), and A's control arm loses the customers B would have won while A's treated arm keeps them (the campaign
arms).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.access.roles import Role
from engine.decide.arbitrate import ARBITRATED_TREAT_LIST_PARQUET
from engine.decide.treat_list import TREAT_LIST_PARQUET, ensure_treat_list, policy_intended
from engine.measurement.campaign import (
    ASSIGNMENT_FILENAME,
    InMemoryCampaignStore,
    campaign_key,
    read_frame,
)
from engine.stages import export
from engine.storage import LocalStorage, run_key
from tests.fixtures.decide import arbitration_runs
from tests.fixtures.decide.arbitration_runs import run_for_use_case
from tests.integration.production.access_support import bearer, local_app, make_user

pytestmark = pytest.mark.integration

FIRST = "uc-first"
SECOND = "uc-second"


@pytest.fixture(autouse=True)
def _fixed_run_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """The actions stage draws a run's control group from its run id, and the fixture takes run ids from a counter
    shared by every test of the process: start it at the same place in every test, so who is in a control
    group does not depend on the tests that ran before."""
    monkeypatch.setitem(arbitration_runs._COUNTER, "n", 0)


class _Cycle:
    """Two scoring runs, the API they were arbitrated through, and what it wrote."""

    def __init__(self, tmp_path: Path, priorities: dict[str, float], *, old_lists: bool = False) -> None:
        self.storage = LocalStorage(tmp_path)
        # An uplift run of a net value and a propensity run of a gross value: the values are of different
        # kinds, so priority alone decides the customers both want.
        self.run_ids = {
            FIRST: run_for_use_case(self.storage, FIRST, kind="uplift", rows=120, value=True),
            SECOND: run_for_use_case(self.storage, SECOND, kind="propensity", rows=120, value=True),
        }
        for run_id in self.run_ids.values():
            ensure_treat_list(self.storage, run_id)
        # A list written before the column existed: its stored files are what was handed off.
        self.stored: dict[str, bytes] = {}
        if old_lists:
            for run_id in self.run_ids.values():
                key = run_key(run_id, TREAT_LIST_PARQUET)
                frame = pd.read_parquet(io.BytesIO(self.storage.read_bytes(key)))
                buffer = io.BytesIO()
                frame.drop(columns=["control_group"]).to_parquet(buffer, index=False)
                self.storage.write_bytes(key, buffer.getvalue())
                self.stored[run_id] = buffer.getvalue()
        root = tmp_path / "config-root"
        (root / "decide").mkdir(parents=True)
        lines = "".join(f"  {name}:\n    priority: {value}\n" for name, value in priorities.items())
        (root / "decide" / "arbitration.yaml").write_text(f"use_cases:\n{lines}", encoding="utf-8")
        self.store = InMemoryCampaignStore()
        self.app: FastAPI = local_app(tmp_path)
        self.app.state.campaign_store = self.store
        self.app.state.config_root = root
        client = TestClient(self.app, raise_server_exceptions=False)
        analyst = make_user(self.app, "arbitrator", [Role.ANALYST])
        response = client.post(
            "/decide/arbitrate", json={"use_cases": [FIRST, SECOND]}, headers=bearer(self.app, analyst)
        )
        assert response.status_code == 200, response.text
        self.body: dict[str, Any] = response.json()
        self.arbitrated = pd.read_parquet(
            io.BytesIO(self.storage.read_bytes(run_key(self.run_ids[FIRST], ARBITRATED_TREAT_LIST_PARQUET)))
        )

    def control(self, use_case: str) -> set[str]:
        scores = pd.read_parquet(
            io.BytesIO(self.storage.read_bytes(run_key(self.run_ids[use_case], export.SCORES_PARQUET)))
        )
        return set(scores.loc[scores["control_group"].astype(bool), "customer_id"])

    def treat_list(self, use_case: str) -> pd.DataFrame:
        return pd.read_parquet(
            io.BytesIO(self.storage.read_bytes(run_key(self.run_ids[use_case], TREAT_LIST_PARQUET)))
        )

    def treated(self, use_case: str) -> set[str]:
        frame = self.treat_list(use_case)
        return set(frame.loc[frame["treat"], "customer_id"])

    def intended(self, use_case: str) -> set[str]:
        flags = policy_intended(self.storage, self.run_ids[use_case])
        return set(flags.index[flags.to_numpy(dtype=bool)])

    def assignment(self, use_case: str) -> pd.DataFrame:
        entry = next(c for c in self.body["campaigns"] if c["use_case_id"] == use_case)
        assert entry["outcome"] == "created", entry
        campaign = self.store.get(entry["campaign_id"])
        assert campaign is not None
        return read_frame(self.storage, campaign_key(campaign.campaign_id, ASSIGNMENT_FILENAME))


@pytest.mark.parametrize("priorities", [{SECOND: 3.0}, {FIRST: 3.0}], ids=["second-first", "first-first"])
def test_a_customer_in_one_use_cases_control_group_is_not_treated_by_another_and_the_row_names_it(
    tmp_path: Path, priorities: dict[str, float]
) -> None:
    cycle = _Cycle(tmp_path, priorities)
    first_control, second_control = cycle.control(FIRST), cycle.control(SECOND)
    blocked_from_second = (first_control & cycle.treated(SECOND)) - second_control
    blocked_from_first = (second_control & cycle.treated(FIRST)) - first_control
    assert len(blocked_from_second) >= 3 and len(blocked_from_first) >= 3

    table = cycle.arbitrated.set_index("customer_id")
    treated = set(table.index[table["treat"]])
    assert not (treated & (first_control | second_control)), "nobody in a control group is contacted"
    for customer in blocked_from_second:
        row = table.loc[customer]
        assert row["use_case"] == FIRST and row["control_use_cases"] == FIRST
        assert row["arbitration_reason"] == "held_out" and not row["treat"]
        assert pd.isna(row["holdout_use_cases"])
    for customer in blocked_from_first:
        row = table.loc[customer]
        assert row["use_case"] == SECOND and row["control_use_cases"] == SECOND
        assert row["arbitration_reason"] == "held_out"
    summary = cycle.body["summary"]
    assert summary["control_blocked_actions"] == len(
        (first_control & cycle.treated(SECOND)) | (second_control & cycle.treated(FIRST))
    )
    assert summary["holdout_blocked_actions"] == 0
    # Every other customer one of them treats is still treated by exactly one use case.
    wanted = (cycle.treated(FIRST) | cycle.treated(SECOND)) - first_control - second_control
    assert treated == wanted


@pytest.mark.parametrize("priorities", [{SECOND: 3.0}, {FIRST: 3.0}], ids=["second-first", "first-first"])
def test_the_campaign_arms_of_both_use_cases_follow_dec_1311_n_for_a_control_customer(
    tmp_path: Path, priorities: dict[str, float]
) -> None:
    """Each campaign compares the customers its use case would win if nobody were held back, in both arms.

    The use case that holds a customer back as its control compares them in its control arm when it would
    have won them; the other use case, when it would have won them, has them in its treated arm although no
    one contacted them (intent to treat, as for a hold-out member, DEC-1311 (n)). A customer the rival would
    have won is in neither arm of the use case that held them back.
    """
    cycle = _Cycle(tmp_path, priorities)
    preferred = SECOND if SECOND in priorities else FIRST
    other = FIRST if preferred == SECOND else SECOND
    assert priorities[preferred] > 1.0

    def would_win(use_case: str) -> set[str]:
        """Customers a use case wins when nobody is held back: it asks for them and the rival does not win."""
        asks = {
            name: cycle.treated(name) | (cycle.control(name) & cycle.intended(name))
            for name in (FIRST, SECOND)
        }
        return asks[use_case] if use_case == preferred else asks[use_case] - asks[preferred]

    for use_case in (FIRST, SECOND):
        assignment = cycle.assignment(use_case)
        scope = would_win(use_case)
        intended = set(assignment.loc[assignment["intended"], "customer_id"])
        # The population is the use case's own policy, cut to the customers it would win.
        assert intended == scope & cycle.intended(use_case), use_case
        arms = assignment.set_index("customer_id")["arm"]
        control = cycle.control(use_case)
        # Its control customers it would win are in the control arm, ...
        held_in = intended & control
        assert held_in and (arms[sorted(held_in)] == "holdout").all(), use_case
        # ... and the ones the rival would win are in neither arm.
        lost = (control & cycle.intended(use_case)) - scope
        if use_case == other:
            assert lost, "the preferred use case takes some of the other's control customers"
        assert not (lost & intended), use_case
        assert (arms[sorted(lost)] == "suppressed").all() if lost else True

    # The preferred use case compares the other's control customers it asked for, in its treated arm.
    taken = cycle.control(other) & cycle.treated(preferred) & cycle.intended(preferred)
    assert taken
    arms = cycle.assignment(preferred).set_index("customer_id")["arm"]
    assert (arms[sorted(taken)] == "treated").all()


def test_an_old_treat_list_is_protected_in_memory_and_its_stored_files_are_left_alone(tmp_path: Path) -> None:
    """A list written before the `control_group` column is not rebuilt (it may already be handed off): the column
    is read from the run's scores for this arbitration, and the stored list stays byte for byte as it was."""
    cycle = _Cycle(tmp_path, {SECOND: 3.0}, old_lists=True)
    for run_id, before in cycle.stored.items():
        assert cycle.storage.read_bytes(run_key(run_id, TREAT_LIST_PARQUET)) == before
        assert "control_group" not in pd.read_parquet(io.BytesIO(before)).columns
    first_control, second_control = cycle.control(FIRST), cycle.control(SECOND)
    table = cycle.arbitrated.set_index("customer_id")
    treated = set(table.index[table["treat"]])
    assert not (treated & (first_control | second_control)), "nobody in a control group is contacted"
    held = first_control - second_control
    assert held and (table.loc[sorted(held), "control_use_cases"] == FIRST).all()
