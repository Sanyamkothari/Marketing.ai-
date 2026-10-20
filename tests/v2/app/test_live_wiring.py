"""The marketer app's live wiring: the approval-batch sample data, and the node tests of the batch flow.

`ui/v2/mock/batches.json` is what the app shows in mock mode for "approve each batch", so it must be exactly the
`Batch` contract and agree with the sample decisions it points at (who, how many, how much). The node half
(`live_wiring.test.mjs`) draws the Live, Plan and Proof screens in a tiny fake DOM and checks the API client
against fake `/v2` answers; without node it skips with a reason, as everywhere else in the suite.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from autonomy.contracts import Batch, BatchStatus, Decision, DecisionKind, Group
from tests.fixtures.node import skip_without_node

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
MOCK = ROOT / "ui" / "v2" / "mock"
GOAL_ID = "goal_winback"


def load(name: str) -> Any:
    return json.loads((MOCK / name).read_text(encoding="utf-8"))


def batches() -> list[Batch]:
    return TypeAdapter(list[Batch]).validate_python(load("batches.json"))


def decisions() -> dict[str, Decision]:
    rows = TypeAdapter(list[Decision]).validate_python(load("decisions.json"))
    return {d.decision_id: d for d in rows}


def chosen_cost(d: Decision) -> float:
    """Offer plus channel cost of the bid the decision acted on (not always the top bid: exploration)."""
    match = [b for b in d.bids if b.action == d.action]
    assert match, f"{d.decision_id} acts without a bid for its action"
    return match[0].offer_cost + match[0].channel_cost


def test_batches_file_is_exactly_the_contract_shape() -> None:
    data = load("batches.json")
    adapter = TypeAdapter(list[Batch])
    assert adapter.dump_python(adapter.validate_python(data), mode="json") == data


def test_batches_belong_to_the_sample_goal_and_show_both_states() -> None:
    rows = batches()
    assert {b.goal_id for b in rows} == {GOAL_ID}
    assert len({b.batch_id for b in rows}) == len(rows)
    statuses = {b.status for b in rows}
    assert BatchStatus.PENDING in statuses, "the Live screen needs something to approve"
    assert statuses - {BatchStatus.PENDING}, "and something already decided"


@pytest.mark.parametrize("batch", batches(), ids=lambda b: b.batch_id)
def test_batch_agrees_with_the_decisions_it_holds(batch: Batch) -> None:
    known = decisions()
    held = [known[i] for i in batch.decision_ids]
    for d in held:
        assert d.kind is DecisionKind.ACT and d.action is not None, "only decisions that act are held"
        assert d.group is Group.AGENT, "the holdout and the simple rule never wait for a person"
        assert d.action.goal_id == batch.goal_id
        assert d.made_at <= batch.created_at, "a batch is made after its decisions"
    assert batch.n_contacts == len({d.customer_id for d in held})
    assert batch.expected_net == pytest.approx(sum(d.expected_net for d in held), abs=0.005)
    # Spend is what sending costs: the chosen bid's expected offer cost plus its channel cost (the same sum the
    # budget rule checks), so the "expected spend" a marketer approves is the money the batch puts at risk.
    assert batch.expected_spend == pytest.approx(sum(chosen_cost(d) for d in held), abs=0.005)
    assert batch.summary, "the marketer reads the summary, so there must be one"
    if batch.status is BatchStatus.PENDING:
        assert batch.decided_by is None and batch.decided_at is None
    else:
        assert batch.decided_by and batch.decided_at is not None
        assert batch.decided_at >= batch.created_at


def test_no_decision_is_in_two_batches() -> None:
    ids = [i for b in batches() for i in b.decision_ids]
    assert len(ids) == len(set(ids))


def test_live_wiring_in_node() -> None:
    node = skip_without_node()
    done = subprocess.run(
        [node, "--test", str(HERE / "live_wiring.test.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-6000:] + done.stderr[-2000:]
