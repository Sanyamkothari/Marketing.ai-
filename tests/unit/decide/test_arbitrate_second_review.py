"""Arbitration after the second review (Plan J M101, DEC-1311).

* `comparable_keys` cuts the treated and the held-back arm of a use case with one rule, checked against an
  oracle that ranks customer by customer;
* a binding channel cap keeps a subset of the explore rows that does not depend on their value;
* `customers_decided_by_*` count the customers who end with an action, and match the reasons on the rows;
* a settings file that cannot be read is refused, never replaced by the defaults.

The lists are the product's own (`tests/fixtures/decide/treat_runs.py`, `build_treat_list`).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.decide.arbitrate import (
    ARBITRATION_CONFIG_FILE,
    ARBITRATION_CONFIG_INVALID,
    ArbitrationConfig,
    ArbitrationError,
    arbitrate_treat_lists,
    comparable_keys,
    load_arbitration_config,
)
from engine.decide.treat_list import TREAT_LIST_PARQUET, build_treat_list, policy_intended
from engine.storage import LocalStorage, run_key
from tests.fixtures.decide.arbitration_runs import KEY, run_for_use_case


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path)


def _run(storage: LocalStorage, use_case: str, **options: Any) -> tuple[pd.DataFrame, pd.Series[Any]]:
    """A real scoring run's treat list, and whom its policy intended to contact."""
    run_id = run_for_use_case(storage, use_case, **options)
    build_treat_list(storage, run_id)
    treat_list = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, TREAT_LIST_PARQUET))))
    return treat_list, policy_intended(storage, run_id)


def _oracle(
    lists: list[pd.DataFrame],
    intended: list[pd.Series[Any]],
    priorities: dict[str, float],
) -> list[set[str]]:
    """Customer by customer, as written in the docstring of `comparable_keys` (cap 1)."""
    candidates: dict[str, list[tuple[int, Any]]] = {}
    for index, (frame, flags) in enumerate(zip(lists, intended, strict=True)):
        for row in frame.itertuples():
            wanted = bool(row.treat) or (bool(row.holdout) and bool(flags.get(row.customer_id, False)))
            if wanted:
                candidates.setdefault(row.customer_id, []).append((index, row))
    winners: list[set[str]] = [set() for _ in lists]
    for customer, rows in candidates.items():
        explored = [item for item in rows if bool(item[1].explore) and bool(item[1].treat)]
        if explored:
            winners[explored[0][0]].add(customer)
            continue
        valued = [pd.notna(row.net_value) for _, row in rows]
        if all(valued):
            best = max(
                rows, key=lambda item: (priorities.get(item[1].use_case, 1.0) * item[1].net_value, -item[0])
            )
        else:
            best = max(rows, key=lambda item: (priorities.get(item[1].use_case, 1.0), -item[0]))
        winners[best[0]].add(customer)
    return winners


# ---------------------------------------------------------------------------
# Blocker: the two arms are cut by one rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("priorities", [{}, {"uc-b": 2.0}], ids=["a-wins", "b-wins"])
def test_comparable_keys_match_the_oracle_on_real_uplift_runs(
    storage: LocalStorage, priorities: dict[str, float]
) -> None:
    options = {"kind": "uplift", "rows": 300, "value": True, "holdout": True}
    list_a, flags_a = _run(storage, "uc-a", **options, config_overrides={"uplift.policy.margin_pct": 60.0})
    list_b, flags_b = _run(storage, "uc-b", **options, config_overrides={"uplift.policy.margin_pct": 45.0})
    assert list_a["holdout"].sum() > 20, "the fixture holds customers back"
    config = ArbitrationConfig(use_cases={n: {"priority": p} for n, p in priorities.items()})

    scopes = comparable_keys([list_a, list_b], [flags_a, flags_b], config, KEY)
    expected = _oracle([list_a, list_b], [flags_a, flags_b], priorities)

    assert [set(scope) for scope in scopes] == expected
    held_back = set(list_a.loc[list_a["holdout"], "customer_id"]) & set(flags_a[flags_a].index)
    compared = held_back & set(scopes[0])
    left_out = held_back - set(scopes[0])
    # Held-back customers are cut by the same rule as treated ones: A keeps those it would have won ...
    if not priorities:
        assert compared, "a held-back customer A would have won is compared"
    # ... and loses those B would have won (B's priority makes it win the contested ones).
    else:
        assert left_out, "a held-back customer B would have won is left out"


def test_the_held_back_arm_loses_the_customers_a_rival_would_have_won_as_the_treated_arm_does(
    storage: LocalStorage,
) -> None:
    """Fails on cb43c05, whose campaign cut only the treated arm to the winners."""
    options = {"kind": "uplift", "rows": 400, "value": True, "holdout": True}
    list_a, flags_a = _run(storage, "uc-a", **options)
    list_b, flags_b = _run(storage, "uc-b", **options, config_overrides={"uplift.policy.margin_pct": 90.0})
    scope_a = set(comparable_keys([list_a, list_b], [flags_a, flags_b], ArbitrationConfig(), KEY)[0])
    arbitrated, _ = arbitrate_treat_lists([list_a, list_b], ArbitrationConfig(), KEY)

    intended_a = flags_a[flags_a].index
    treated_a = list_a[list_a["treat"] & ~list_a["explore"] & list_a["customer_id"].isin(intended_a)]
    held_a = list_a[list_a["holdout"] & list_a["customer_id"].isin(intended_a)]
    lost_treated = set(treated_a["customer_id"]) - scope_a
    lost_held = set(held_a["customer_id"]) - scope_a
    assert lost_treated and lost_held, "the rival wins customers in both arms"
    # The share of each arm the rival takes is the same, up to chance.
    share_treated = len(lost_treated) / len(treated_a)
    share_held = len(lost_held) / len(held_a)
    assert abs(share_treated - share_held) < 0.15, (share_treated, share_held)
    # Every treated customer the rival took really went to the rival in the arbitrated list.
    winners = arbitrated[arbitrated["treat"]].set_index("customer_id")["winning_use_case"]
    assert set(winners.reindex(sorted(lost_treated)).dropna()) <= {"uc-b"}


def test_with_one_list_the_comparable_customers_are_whom_the_policy_intended(storage: LocalStorage) -> None:
    list_a, flags_a = _run(storage, "uc-only", kind="propensity", rows=200, value=True, holdout=True)
    (scope,) = comparable_keys([list_a], [flags_a], ArbitrationConfig(), KEY)
    treated = set(list_a.loc[list_a["treat"], "customer_id"])
    held = set(list_a.loc[list_a["holdout"], "customer_id"]) & set(flags_a[flags_a].index)
    assert set(scope) == treated | held
    assert held and not (held & treated)


def test_a_list_with_no_intent_competes_with_its_treated_rows_only(storage: LocalStorage) -> None:
    list_a, flags_a = _run(storage, "uc-a", kind="propensity", rows=120, holdout=True)
    list_b, _ = _run(storage, "uc-b", kind="propensity", rows=120)
    scopes = comparable_keys([list_a, list_b], [flags_a, None], ArbitrationConfig(), KEY)
    assert set(scopes[1]) <= set(list_b.loc[list_b["treat"], "customer_id"])
    with pytest.raises(ValueError, match="one entry"):
        comparable_keys([list_a, list_b], [flags_a], ArbitrationConfig(), KEY)


@pytest.mark.parametrize("kind", ["propensity", "uplift"])
def test_policy_intended_is_the_policys_selection_without_the_holdout(
    storage: LocalStorage, kind: str
) -> None:
    run_id = run_for_use_case(storage, "uc-x", kind=kind, rows=200, value=True, holdout=True)
    build_treat_list(storage, run_id)
    treat_list = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, TREAT_LIST_PARQUET))))
    intended = policy_intended(storage, run_id)
    assert intended.index.is_unique and len(intended) == len(treat_list)
    flags = intended.reindex(treat_list["customer_id"]).to_numpy()
    # Everyone treated at the policy's choice is intended; so are the held-back customers it would have treated.
    chosen = treat_list["treat"] & ~treat_list["explore"]
    assert flags[chosen.to_numpy()].all()
    held = treat_list["holdout"].to_numpy()
    assert flags[held].any() and not flags[held].all()
    # Nobody suppressed is intended.
    assert not flags[treat_list["suppression_reason"].notna().to_numpy()].any()


# ---------------------------------------------------------------------------
# Minor: channel capacity does not select the explore sample by value
# ---------------------------------------------------------------------------


def _explore_list(values: np.ndarray, channel: str = "sms") -> pd.DataFrame:
    n = len(values)
    return pd.DataFrame(
        {
            "customer_id": [f"C-{i:04d}" for i in range(n)],
            "use_case": "uc-explore",
            "model_version": "m",
            "segment": "persuadable",
            "treat": True,
            "holdout": False,
            "explore": True,
            "suppression_reason": None,
            "offer": "Offer",
            "channel": channel,
            "net_value": values,
            "expected_gross_value": None,
            "reason_1": None,
            "reason_2": None,
            "reason_3": None,
        }
    )


def test_a_binding_channel_cap_keeps_a_value_independent_subset_of_the_explore_rows() -> None:
    """Fails on cb43c05, whose channel capacity went to the explore rows in the order of their value."""
    n, cap = 600, 150
    values = np.arange(1.0, n + 1.0)
    config = ArbitrationConfig(channel_caps={"sms": cap})

    kept = arbitrate_treat_lists([_explore_list(values)], config, KEY)[0]
    kept = kept[kept["treat"]]
    assert len(kept) == cap
    # Not the top of the values (cb43c05 kept exactly the 150 largest: a mean of 525) ...
    assert abs(kept["net_value"].mean() - values.mean()) < 0.2 * n
    # ... and the same customers are kept whatever their values are.
    reversed_values = arbitrate_treat_lists([_explore_list(values[::-1].copy())], config, KEY)[0]
    reversed_kept = reversed_values[reversed_values["treat"]]
    assert set(kept["customer_id"]) == set(reversed_kept["customer_id"])
    # The draw is fixed: the same call keeps the same customers.
    again = arbitrate_treat_lists([_explore_list(values)], config, KEY)[0]
    assert set(again.loc[again["treat"], "customer_id"]) == set(kept["customer_id"])


def test_non_explore_rows_still_get_channel_capacity_by_value() -> None:
    n, cap = 300, 100
    values = np.arange(1.0, n + 1.0)
    frame = _explore_list(values).assign(explore=False)
    out, _ = arbitrate_treat_lists([frame], ArbitrationConfig(channel_caps={"sms": cap}), KEY)
    kept = out[out["treat"]]
    assert len(kept) == cap and kept["net_value"].min() == values[-cap]


# ---------------------------------------------------------------------------
# Minor: how customers were decided is what the rows say
# ---------------------------------------------------------------------------


def test_the_decided_by_counts_are_the_customers_who_end_with_an_action(storage: LocalStorage) -> None:
    """Fails on cb43c05, which counted customers whose action a channel cap then removed."""
    options = {"rows": 300, "value": True, "holdout": True, "channels": True}
    lists = [
        _run(storage, "uc-a", kind="uplift", **options)[0],
        _run(storage, "uc-b", kind="uplift", **options, config_overrides={"uplift.policy.margin_pct": 60.0})[
            0
        ],
        _run(storage, "uc-c", kind="propensity", **options)[0],
    ]
    config = ArbitrationConfig(use_cases={"uc-c": {"priority": 2.0}}, channel_caps={"sms": 15})
    out, summary = arbitrate_treat_lists(lists, config, KEY)

    treated = out[out["treat"]]
    by_reason = treated.groupby("arbitration_reason")["customer_id"].nunique()

    def count(*reasons: str) -> int:
        return int(sum(by_reason.get(reason, 0) for reason in reasons))

    assert summary.customers_decided_by_value == count("net_value", "expected_gross_value")
    assert summary.customers_decided_by_priority == count("priority")
    assert summary.customers_decided_by_request_order == count("request_order", "explore_request_order")
    assert summary.customers_decided_by_explore == count("explore_treated")
    # Every contested customer either ends with an action or was left with none by a channel cap.
    assert summary.contested_customers_channel_capped > 0, "the cap binds on a contested customer"
    assert summary.customers_with_conflicts == (
        summary.customers_decided_by_value
        + summary.customers_decided_by_priority
        + summary.customers_decided_by_request_order
        + summary.customers_decided_by_explore
        + summary.contested_customers_channel_capped
    )
    capped_none = out[(out["arbitration_reason"] == "channel_cap")]
    assert summary.contested_customers_channel_capped <= len(capped_none)


# ---------------------------------------------------------------------------
# Major: a settings file that cannot be read is refused
# ---------------------------------------------------------------------------


def _write_config(root: Path, text: str) -> Path:
    path = root / ARBITRATION_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("contact_cap_per_customer: 2\nchannel_caps:\n  sms: -1\n  email: 500\n", "sms"),
        ("contact_cap_per_customer: 0\n", "contact_cap_per_customer"),
        ("priorities:\n  uc-a: 2\n", "priorities"),
        ("use_cases:\n  uc-a:\n    priorty: 2\n", "priorty"),
        ("channel_caps: [sms, 3]\n", "channel_caps"),
        ("channel_caps: {sms: [", "YAML"),
        ("- just\n- a list\n", "list settings by name"),
    ],
    ids=[
        "negative-cap",
        "zero-contact-cap",
        "unknown-key",
        "typo",
        "wrong-type",
        "bad-yaml",
        "not-a-mapping",
    ],
)
def test_a_settings_file_that_cannot_be_read_is_refused(tmp_path: Path, text: str, names: str) -> None:
    """Fails on cb43c05, which logged the problem and ran on the defaults (the valid caps lost too)."""
    root = _write_config(tmp_path, text)
    with pytest.raises(ArbitrationError) as caught:
        load_arbitration_config(root)
    assert caught.value.code == ARBITRATION_CONFIG_INVALID
    assert "fix it or remove it to use the defaults" in caught.value.message
    assert names in caught.value.message
    assert "Traceback" not in caught.value.message and "pydantic" not in caught.value.message


def test_a_valid_settings_file_is_read_and_an_absent_one_gives_the_defaults(tmp_path: Path) -> None:
    root = _write_config(
        tmp_path,
        "contact_cap_per_customer: 2\nchannel_caps:\n  sms: 0\n  email: 500\nuse_cases:\n  uc-a:\n    priority: 3\n",
    )
    config = load_arbitration_config(root)
    assert config.contact_cap_per_customer == 2
    assert config.channel_caps == {"sms": 0, "email": 500}
    assert config.priority_for("uc-a") == 3.0
    assert load_arbitration_config(tmp_path / "nothing-here") == ArbitrationConfig()
    empty = _write_config(tmp_path / "empty", "")
    assert load_arbitration_config(empty) == ArbitrationConfig()
