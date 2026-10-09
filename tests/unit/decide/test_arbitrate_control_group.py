"""A use case's own per-run control group is protected across use cases (Plan J, DEC-1311 (al)-(ap)).

Phase 1's actions stage keeps a share of each run back as its control group (`control_group` in the
scores). M92's persistent hold-out is the other kind of held-back customer, and arbitration already
protected that one (DEC-1311 (c)): no other use case treats a hold-out member. The control group of one
run was not: another use case could treat that customer. These tests build runs with the product's own
code (`tests/fixtures/decide/treat_runs.py`, `build_treat_list`), never a hand-made frame, and read who is
in a run's control group from that run's scores.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from engine.decide.arbitrate import (
    ArbitrationConfig,
    UseCasePriorityConfig,
    arbitrate_treat_lists,
    comparable_keys,
)
from engine.decide.treat_list import TREAT_LIST_PARQUET, build_treat_list, policy_intended
from engine.stages import export
from engine.storage import LocalStorage, run_key
from tests.fixtures.decide import arbitration_runs
from tests.fixtures.decide.arbitration_runs import KEY, run_for_use_case


@pytest.fixture(autouse=True)
def _fixed_run_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ids come from a counter shared by every test of the process, and the actions stage draws each run's
    control group from its run id. Start the counter at the same place in every test so that who is in a control
    group, and so the thresholds below, do not depend on the tests that ran before."""
    monkeypatch.setitem(arbitration_runs._COUNTER, "n", 0)


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path)


class _Run:
    """One real scoring run: its treat list, whom its policy intended, and who its control group is."""

    def __init__(self, storage: LocalStorage, use_case: str, **options: Any) -> None:
        self.run_id = run_for_use_case(storage, use_case, **options)
        build_treat_list(storage, self.run_id)
        self.treat_list = pd.read_parquet(
            io.BytesIO(storage.read_bytes(run_key(self.run_id, TREAT_LIST_PARQUET)))
        )
        self.intended = policy_intended(storage, self.run_id)
        scores = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(self.run_id, export.SCORES_PARQUET))))
        self.control = set(scores.loc[scores["control_group"].astype(bool), "customer_id"])
        self.use_case = use_case

    @property
    def treated(self) -> set[str]:
        return set(self.treat_list.loc[self.treat_list["treat"], "customer_id"])


def _config(**priorities: float) -> ArbitrationConfig:
    return ArbitrationConfig(
        use_cases={
            name.replace("_", "-"): UseCasePriorityConfig(priority=p) for name, p in priorities.items()
        }
    )


def _row(frame: pd.DataFrame, customer: str) -> pd.Series[Any]:
    rows = frame[frame["customer_id"] == customer]
    assert len(rows) == 1, f"{customer} has {len(rows)} rows"
    return rows.iloc[0]


def _pair(storage: LocalStorage) -> tuple[_Run, _Run]:
    """An uplift run and a propensity run of the same customers; each has its own per-run control group."""
    first = _Run(storage, "uc-a", kind="uplift", value=True)
    second = _Run(storage, "uc-b", kind="propensity", value=True)
    return first, second


# ---------------------------------------------------------------------------
# The treat list says who the run kept as its control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("holdout", [False, True], ids=["default-run", "persistent-holdout"])
@pytest.mark.parametrize("kind", ["propensity", "uplift"])
def test_the_treat_list_flags_the_runs_control_group_and_leaves_holdout_as_it_was(
    storage: LocalStorage, kind: str, holdout: bool
) -> None:
    run = _Run(storage, "uc-x", kind=kind, value=True, holdout=holdout)
    frame = run.treat_list
    assert len(run.control) > 10, "the fixture keeps customers back as a control group"
    assert set(frame.loc[frame["control_group"].astype(bool), "customer_id"]) == run.control
    assert frame["control_group"].notna().all()
    # A control customer is never treated, here or by the list.
    assert not frame.loc[frame["control_group"].astype(bool), "treat"].any()
    if (
        holdout
    ):  # M92's flag is as it was: it also records a member who was suppressed, the control group does not
        assert frame["holdout"].notna().all()
        assert run.control <= set(frame.loc[frame["holdout"].astype(bool), "customer_id"])
    else:  # no assignment file: holdout stays unknown (null), not false
        assert frame["holdout"].isna().all()
    # The column sits between offer_reason and net_value: no other column moves from the start or the end.
    columns = list(frame.columns)
    assert columns[columns.index("offer_reason") + 1] == "control_group"
    assert columns[columns.index("control_group") + 1] == "net_value"


# ---------------------------------------------------------------------------
# Arbitration: no other use case treats a customer in the control group of any selected use case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "priorities", [{}, {"uc_b": 5.0}, {"uc_a": 5.0}], ids=["equal", "b-first", "a-first"]
)
def test_a_customer_in_one_use_cases_control_group_is_never_treated_by_another(
    storage: LocalStorage, priorities: dict[str, float]
) -> None:
    """Fails on main: the rival's row stays treated, so the customer is contacted by B while A measures them."""
    first, second = _pair(storage)
    blocked_from_b = first.control & second.treated
    blocked_from_a = second.control & first.treated
    assert len(blocked_from_b) >= 5 and len(blocked_from_a) >= 5
    both = first.control & second.control

    out, summary = arbitrate_treat_lists(
        [first.treat_list, second.treat_list],
        _config(**priorities),
        KEY,
    )

    treated = set(out.loc[out["treat"], "customer_id"])
    assert not (treated & (first.control | second.control)), "nobody in a control group is contacted"
    for customer in blocked_from_b - both:
        row = _row(out, customer)
        assert not row["treat"] and pd.isna(row["winning_use_case"])
        assert row["use_case"] == "uc-a", "the row is the use case that held the customer back"
        assert bool(row["control_group"]) is True
        assert row["control_use_cases"] == "uc-a"
        assert pd.isna(row["holdout_use_cases"]), "M92's hold-out did not hold them"
        assert row["arbitration_reason"] == "held_out"
    for customer in blocked_from_a - both:
        row = _row(out, customer)
        assert row["use_case"] == "uc-b" and row["control_use_cases"] == "uc-b"
    for customer in both:
        assert _row(out, customer)["control_use_cases"] == "uc-a, uc-b"
    assert summary.control_blocked_actions == len(blocked_from_b | blocked_from_a)
    assert summary.holdout_blocked_actions == 0
    # Customers nobody holds back are untouched and name no one.
    free = out[~out["customer_id"].isin(first.control | second.control)]
    assert free["control_use_cases"].isna().all()
    assert len(out) == first.treat_list["customer_id"].nunique()


def test_a_persistent_holdout_member_is_named_by_the_holdout_only(storage: LocalStorage) -> None:
    """M92's hold-out keeps its column: its members are not also listed as a control group of the run."""
    holder = _Run(storage, "uc-holder", kind="uplift", value=True, holdout=True)
    rival = _Run(storage, "uc-rival", kind="propensity", value=True)
    out, summary = arbitrate_treat_lists([rival.treat_list, holder.treat_list], _config(), KEY)
    for customer in list(holder.control - rival.control)[:15]:
        row = _row(out, customer)
        assert row["holdout_use_cases"] == "uc-holder"
        assert pd.isna(row["control_use_cases"])
    blocked_by_holdout = holder.control & rival.treated
    blocked_by_control = (rival.control & holder.treated) - holder.control
    assert summary.holdout_blocked_actions == len(blocked_by_holdout) > 0
    assert summary.control_blocked_actions == len(blocked_by_control) > 0
    for customer in list(blocked_by_control)[:15]:
        row = _row(out, customer)
        assert row["control_use_cases"] == "uc-rival" and pd.isna(row["holdout_use_cases"])


def test_a_customer_in_a_control_group_and_another_use_cases_holdout_keeps_the_holdout_row(
    storage: LocalStorage,
) -> None:
    """M92's hold-out row wins over a control-group row, whichever use case is listed first (DEC-1311 (aq))."""
    controlled = _Run(storage, "uc-a", kind="propensity", value=True)
    holder = _Run(storage, "uc-b", kind="uplift", value=True, holdout=True)
    members = set(holder.treat_list.loc[holder.treat_list["holdout"].astype(bool), "customer_id"])
    both = sorted(controlled.control & members)
    assert len(both) >= 3, "the fixture puts customers in both"
    for lists in ([controlled.treat_list, holder.treat_list], [holder.treat_list, controlled.treat_list]):
        out, _ = arbitrate_treat_lists(lists, ArbitrationConfig(), KEY)
        for customer in both:
            row = _row(out, customer)
            assert row["use_case"] == "uc-b" and bool(row["holdout"]) is True
            assert row["holdout_use_cases"] == "uc-b" and row["control_use_cases"] == "uc-a"
            assert not row["treat"] and row["arbitration_reason"] == "held_out"


def test_a_treat_list_without_the_column_is_arbitrated_as_before(storage: LocalStorage) -> None:
    """A list written before the column existed protects nobody, and no run without a control group changes."""
    first, second = _pair(storage)
    old = [
        first.treat_list.drop(columns=["control_group"]),
        second.treat_list.drop(columns=["control_group"]),
    ]
    out, summary = arbitrate_treat_lists(old, ArbitrationConfig(), KEY)
    assert "control_group" not in out.columns
    assert summary.control_blocked_actions == 0
    assert out["control_use_cases"].isna().all()
    treated = set(out.loc[out["treat"], "customer_id"])
    assert treated & (
        first.control | second.control
    ), "as before, a control customer can be treated by a rival"


def test_runs_without_a_control_group_arbitrate_exactly_as_before(storage: LocalStorage) -> None:
    zero = {"actions.control_group_fraction": 0.0}
    first = _Run(storage, "uc-a", kind="uplift", value=True, config_overrides=zero)
    second = _Run(storage, "uc-b", kind="propensity", value=True, config_overrides=zero)
    assert not first.control and not second.control
    lists = [first.treat_list, second.treat_list]
    now, summary_now = arbitrate_treat_lists(lists, ArbitrationConfig(), KEY)
    before, summary_before = arbitrate_treat_lists(
        [frame.drop(columns=["control_group"]) for frame in lists], ArbitrationConfig(), KEY
    )
    assert summary_now.control_blocked_actions == 0
    shared = [c for c in now.columns if c in before.columns]
    pd.testing.assert_frame_equal(now[shared], before[shared])
    assert now["control_use_cases"].isna().all()
    skip = {"control_blocked_actions", "created_at"}
    assert summary_now.model_dump(exclude=skip) == summary_before.model_dump(exclude=skip)


# ---------------------------------------------------------------------------
# The two arms of each use case's campaign follow DEC-1311 (n) for them
# ---------------------------------------------------------------------------


def _expected_scopes(runs: list[_Run], priorities: dict[str, float]) -> list[set[str]]:
    """Customer by customer, as `comparable_keys` documents (cap 1): whom each use case would win if nobody
    were held back. A customer a run kept as its control competes if its policy intended them."""
    candidates: dict[str, list[tuple[int, _Run]]] = {}
    for index, run in enumerate(runs):
        flagged = run.treat_list["holdout"].fillna(False).astype(bool)
        held = run.control | set(run.treat_list.loc[flagged, "customer_id"])
        for customer in run.treated | {c for c in held if bool(run.intended.get(c, False))}:
            candidates.setdefault(customer, []).append((index, run))
    scopes: list[set[str]] = [set() for _ in runs]
    for customer, rows in candidates.items():
        best = max(rows, key=lambda item: (priorities.get(item[1].use_case, 1.0), -item[0]))
        scopes[best[0]].add(customer)
    return scopes


@pytest.mark.parametrize("priorities", [{}, {"uc-b": 5.0}], ids=["a-wins", "b-wins"])
def test_comparable_keys_treat_a_control_customer_like_a_held_back_one(
    storage: LocalStorage, priorities: dict[str, float]
) -> None:
    """Fails on main: A's control customers never compete for A, so a rival always takes them and A's
    control arm loses the customers its treated arm keeps."""
    first, second = _pair(storage)
    config = ArbitrationConfig(
        use_cases={n: UseCasePriorityConfig(priority=p) for n, p in priorities.items()}
    )
    scopes = comparable_keys(
        [first.treat_list, second.treat_list], [first.intended, second.intended], config, KEY
    )
    expected = _expected_scopes([first, second], priorities)
    assert [set(s) for s in scopes] == expected
    held_a = {c for c in first.control if bool(first.intended.get(c, False))}
    assert len(held_a) >= 5
    if priorities:
        contested = held_a & second.treated
        assert contested, "B also wants some of A's control customers"
        assert not (contested & set(scopes[0])), "B beats A on these customers, in the control arm too"
        assert contested <= set(scopes[1])
        b_wants = second.treated | {c for c in second.control if bool(second.intended.get(c, False))}
        assert (held_a - b_wants) <= set(scopes[0]), "A keeps the ones B does not want"
    else:
        assert held_a <= set(scopes[0]), "A keeps every control customer it would have won"
