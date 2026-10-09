"""Arbitration over the treat lists of real scoring runs (Plan J M101, DEC-1311; Claude Code's review).

Every list below is built by the product: a scoring run written by `tests/fixtures/decide/treat_runs.py`
(Phase 1's `apply_actions`, M97's `apply_uplift_actions`, M92's holdout and explore assignment, M99's
contactability) and `build_treat_list`. Nothing hand-makes a treat list, a flag or a value. The tests that
the review's findings required fail on `98b2959`:

* a made-up value of 1 decides the winner, and incremental and gross values are compared as one number;
* a row M92 treated at random loses to another use case (the flag survives, the decision does not);
* a hold-out member's row is marked as a hold-out on another use case's row;
* the repository ships invented priorities and channel caps.

Tests of the decision look at the decision (who is treated, with which action), not at a flag.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.decide.arbitrate import (
    ArbitrationConfig,
    arbitrate_treat_lists,
    load_arbitration_config,
)
from engine.storage import LocalStorage
from tests.fixtures.decide.arbitration_runs import KEY, treat_list_for_use_case

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path)


def _config(priorities: dict[str, float] | None = None, **extra: Any) -> ArbitrationConfig:
    return ArbitrationConfig(
        use_cases={name: {"priority": value} for name, value in (priorities or {}).items()}, **extra
    )


def _flag(frame: pd.DataFrame, column: str) -> pd.Series[bool]:
    return frame[column].fillna(False).astype(bool)


def _treated(frame: pd.DataFrame) -> set[str]:
    return set(frame.loc[frame["treat"], "customer_id"])


def _row(frame: pd.DataFrame, customer: str) -> pd.Series[Any]:
    rows = frame[frame["customer_id"] == customer]
    assert len(rows) == 1, f"{customer} has {len(rows)} rows"
    return rows.iloc[0]


def _contested(*lists: pd.DataFrame) -> list[str]:
    """Customers every list treats, in the first list's order."""
    common = set.intersection(*[_treated(frame) for frame in lists])
    return [c for c in lists[0]["customer_id"] if c in common]


# ---------------------------------------------------------------------------
# Blocking 1: no made-up value, no mixed units
# ---------------------------------------------------------------------------


def test_a_missing_value_is_never_replaced_by_a_made_up_one(storage: LocalStorage) -> None:
    """Fails on 98b2959, which gave a row with no value a value of 1 and compared that with real rupees."""
    without_value = treat_list_for_use_case(storage, "uc-no-value", kind="propensity")
    with_value = treat_list_for_use_case(storage, "uc-valued", kind="uplift", value=True)
    assert without_value["net_value"].isna().all() and without_value["expected_gross_value"].isna().all()
    contested = _contested(without_value, with_value)
    assert len(contested) >= 10

    out, summary = arbitrate_treat_lists([without_value, with_value], _config(), KEY)

    # Neither has a value to compare, so priority decides, and the tie goes to the first use case
    # requested: 98b2959 let the valued use case win each of these on its real rupees against a made-up 1.
    for customer in contested:
        row = _row(out, customer)
        assert row["treat"] and row["winning_use_case"] == "uc-no-value", customer
        assert row["arbitration_reason"] == "request_order"
        assert pd.isna(row["priority_score"]), "no made-up score for a row without a value"
    assert summary.customers_decided_by_request_order == len(contested)
    assert summary.customers_decided_by_value == 0

    # The request's order is the whole tie-break: reversed, the other use case wins every one of them.
    flipped, _ = arbitrate_treat_lists([with_value, without_value], _config(), KEY)
    assert {_row(flipped, c)["winning_use_case"] for c in contested} == {"uc-valued"}


def test_a_treated_row_without_a_value_has_no_priority_score(storage: LocalStorage) -> None:
    """Fails on 98b2959, which wrote priority x 1 for a customer whose value is not known."""
    only = treat_list_for_use_case(storage, "uc-no-value", kind="propensity")
    out, _ = arbitrate_treat_lists([only], _config({"uc-no-value": 3.0}), KEY)
    winners = out[out["treat"]]
    assert len(winners) > 0
    assert winners["priority_score"].isna().all()
    assert (winners["priority_weight"] == 3.0).all()
    assert set(winners["arbitration_reason"]) == {"only_action"}


def test_incremental_and_gross_values_are_never_compared(storage: LocalStorage) -> None:
    """Fails on 98b2959, which multiplied an uplift run's net value and a propensity run's gross value by a
    priority and compared the two numbers as rupees of one kind."""
    net = treat_list_for_use_case(storage, "uc-net", kind="uplift", value=True)
    gross = treat_list_for_use_case(storage, "uc-gross", kind="propensity", value=True)
    contested = _contested(net, gross)
    assert len(contested) >= 10
    both = net.set_index("customer_id").loc[contested, "net_value"]
    other = gross.set_index("customer_id").loc[contested, "expected_gross_value"]
    assert (other > both).any(), "the fixture must let a gross value beat a net value if they were compared"

    # Equal priority: the first use case requested wins every one, whichever number is bigger.
    out, summary = arbitrate_treat_lists([net, gross], _config(), KEY)
    assert {_row(out, c)["winning_use_case"] for c in contested} == {"uc-net"}
    assert {_row(out, c)["arbitration_reason"] for c in contested} == {"request_order"}
    assert summary.customers_decided_by_value == 0

    # A higher priority wins every one, whichever number is bigger.
    out, summary = arbitrate_treat_lists([net, gross], _config({"uc-gross": 2.0}), KEY)
    assert {_row(out, c)["winning_use_case"] for c in contested} == {"uc-gross"}
    assert {_row(out, c)["arbitration_reason"] for c in contested} == {"priority"}
    assert summary.customers_decided_by_priority == len(contested)
    assert summary.customers_decided_by_value == 0
    assert all(pd.isna(_row(out, c)["priority_score"]) for c in contested)


def test_values_of_one_kind_are_compared_as_priority_times_value(storage: LocalStorage) -> None:
    """Two uplift runs: every candidate carries a net value, so priority x net value decides."""
    low = treat_list_for_use_case(storage, "uc-low", kind="uplift", value=True)
    high = treat_list_for_use_case(
        storage, "uc-high", kind="uplift", value=True, config_overrides={"uplift.policy.margin_pct": 60.0}
    )
    contested = _contested(low, high)
    assert len(contested) >= 10
    left = low.set_index("customer_id").loc[contested, "net_value"]
    right = high.set_index("customer_id").loc[contested, "net_value"]
    weight = float(np.median(right / left)) * 1.001  # about half the customers go each way, none tie
    out, summary = arbitrate_treat_lists([low, high], _config({"uc-low": weight}), KEY)

    expected = np.where(weight * left > right, "uc-low", "uc-high")
    assert set(expected) == {"uc-low", "uc-high"}, "the priority must let each use case win somewhere"
    for customer, want in zip(contested, expected, strict=True):
        row = _row(out, customer)
        assert row["winning_use_case"] == want, customer
        assert row["arbitration_reason"] == "net_value"
        mine = weight * left[customer] if want == "uc-low" else right[customer]
        assert row["priority_score"] == pytest.approx(mine)
    assert summary.customers_decided_by_value == len(contested)
    assert summary.customers_decided_by_priority == 0


# ---------------------------------------------------------------------------
# Blocking 2: explore-treated actions survive
# ---------------------------------------------------------------------------


def test_an_action_treated_at_random_survives_a_use_case_that_would_win(storage: LocalStorage) -> None:
    """Fails on 98b2959, which dropped the explore row of a customer another use case wanted more.

    Use case A runs under the persistent holdout, so M92 treats some of its customers at random (explore).
    Use case B has ten times the priority and would win every customer both want.
    """
    explorer = treat_list_for_use_case(storage, "uc-explore", kind="uplift", value=True, holdout=True)
    rival = treat_list_for_use_case(storage, "uc-rival", kind="propensity", value=True)
    random_pick = explorer[_flag(explorer, "explore") & explorer["treat"]]
    wanted_by_rival = set(random_pick["customer_id"]) & _treated(rival)
    assert len(wanted_by_rival) >= 1

    out, summary = arbitrate_treat_lists([explorer, rival], _config({"uc-rival": 10.0}), KEY)

    for customer in wanted_by_rival:
        row = _row(out, customer)
        assert row["treat"] and row["winning_use_case"] == "uc-explore", customer
        assert row["offer"] == _row(explorer, customer)["offer"]
        assert row["arbitration_reason"] == "explore_treated"
        assert "uc-rival" in row["losing_actions"]
    assert summary.explore_kept_count == len(random_pick)
    assert summary.customers_decided_by_explore == len(wanted_by_rival)

    # The rival still wins the customers that were not treated at random.
    others = (_treated(explorer) - set(random_pick["customer_id"])) & _treated(rival)
    assert others
    assert {_row(out, c)["winning_use_case"] for c in others} == {"uc-rival"}


def test_two_use_cases_that_both_treat_a_customer_at_random(storage: LocalStorage) -> None:
    """Both are kept only if the contact cap allows it; otherwise the first requested is kept and it is recorded.

    The second use case's list is the first one's real treat list under another use case id, so the same
    customers are treated at random by both.
    """
    first = treat_list_for_use_case(storage, "uc-first", kind="uplift", value=True, holdout=True)
    second = first.assign(use_case="uc-second")
    random_pick = first[_flag(first, "explore") & first["treat"]]
    customers = list(random_pick["customer_id"])
    assert len(customers) >= 2

    out, summary = arbitrate_treat_lists([first, second], _config(), KEY)
    for customer in customers:
        row = _row(out, customer)
        assert row["winning_use_case"] == "uc-first"
        assert row["arbitration_reason"] == "explore_request_order"
        assert "uc-second" in row["losing_actions"] and "explore" in row["losing_actions"]
    assert summary.explore_dropped_count == len(customers)
    assert summary.explore_kept_count == len(customers)

    reversed_out, _ = arbitrate_treat_lists([second, first], _config(), KEY)
    assert {_row(reversed_out, c)["winning_use_case"] for c in customers} == {"uc-second"}

    # With a contact cap of two, both are kept: one row for each action.
    both, both_summary = arbitrate_treat_lists([first, second], _config(contact_cap_per_customer=2), KEY)
    for customer in customers:
        rows = both[both["customer_id"] == customer]
        assert sorted(rows["winning_use_case"]) == ["uc-first", "uc-second"]
        assert rows["treat"].all()
    assert both_summary.explore_dropped_count == 0
    assert both_summary.explore_kept_count == 2 * len(customers)


# ---------------------------------------------------------------------------
# Hold-out protection, and what the non-winning row says
# ---------------------------------------------------------------------------


def test_a_hold_out_member_is_never_treated_by_any_use_case(storage: LocalStorage) -> None:
    """The hold-out is in use case A only; B, which does not know it, treats those customers.

    Fails on 98b2959 for the row's fields: it kept B's row (use case B, holdout forced to True), so the
    row read as if B had held the customer back.
    """
    rival = treat_list_for_use_case(storage, "uc-rival", kind="propensity", value=True)
    holder = treat_list_for_use_case(storage, "uc-holder", kind="uplift", value=True, holdout=True)
    held = set(holder.loc[_flag(holder, "holdout"), "customer_id"])
    blocked = held & _treated(rival)
    assert len(blocked) >= 5

    out, summary = arbitrate_treat_lists(
        [rival, holder], _config({"uc-rival": 5.0}, contact_cap_per_customer=2), KEY
    )

    assert not (set(out.loc[out["treat"], "customer_id"]) & held)
    for customer in blocked:
        row = _row(out, customer)
        assert not row["treat"] and pd.isna(row["winning_use_case"])
        assert row["use_case"] == "uc-holder", "the row is the use case that held the customer back"
        assert row["holdout"] is True or row["holdout"] == True  # noqa: E712 - numpy bool
        assert row["holdout_use_cases"] == "uc-holder"
        assert row["arbitration_reason"] == "held_out"
    assert summary.holdout_blocked_actions == len(blocked)
    # A customer nobody holds back has no hold-out use case.
    free = out[~out["customer_id"].isin(held)]
    assert free["holdout_use_cases"].isna().all()


def test_every_use_case_that_held_a_customer_back_is_named(storage: LocalStorage) -> None:
    first = treat_list_for_use_case(storage, "uc-one", kind="uplift", value=True, holdout=True)
    second = first.assign(use_case="uc-two")  # the same persistent hold-out, seen by a second use case
    held = set(first.loc[_flag(first, "holdout"), "customer_id"])
    out, _ = arbitrate_treat_lists([first, second], _config(), KEY)
    assert len(out) == first["customer_id"].nunique()
    for customer in list(held)[:10]:
        row = _row(out, customer)
        assert row["holdout_use_cases"] == "uc-one, uc-two"
        assert row["holdout"] is True or row["holdout"] == True  # noqa: E712 - numpy bool


# ---------------------------------------------------------------------------
# Three overlapping use cases: the acceptance test, on real runs
# ---------------------------------------------------------------------------


def _expected_winners(
    lists: list[pd.DataFrame], priorities: dict[str, float], held: set[str], cap: int
) -> dict[str, list[str]]:
    """The rule written out customer by customer, for the test to hold the arbitration to.

    Candidates are treat rows of customers no hold-out keeps back. A row treated at random (explore) goes
    first. Priority x value ranks the rest when all of a customer's candidates carry a net value, or all an
    expected gross value; otherwise priority ranks them; the order of the lists breaks a tie.
    """
    candidates: dict[str, list[tuple[int, str, str, float, bool]]] = {}
    for order, frame in enumerate(lists):
        for row in frame[frame["treat"]].itertuples():
            if row.customer_id in held:
                continue
            if pd.notna(row.net_value):
                kind, value = "net", float(row.net_value)
            elif pd.notna(row.expected_gross_value):
                kind, value = "gross", float(row.expected_gross_value)
            else:
                kind, value = "none", 0.0
            explore = bool(row.explore) if pd.notna(row.explore) else False
            candidates.setdefault(row.customer_id, []).append((order, row.use_case, kind, value, explore))
    winners: dict[str, list[str]] = {}
    for customer, rows in candidates.items():
        kinds = {kind for _, _, kind, _, _ in rows}
        by_value = len(kinds) == 1 and "none" not in kinds

        def score(
            item: tuple[int, str, str, float, bool], by_value: bool = by_value
        ) -> tuple[bool, float, int]:
            order, use_case, _, value, explore = item
            weight = priorities.get(use_case, 1.0)
            # Treated at random: first, in the order of the lists; the rest by the rule.
            return (not explore, 0.0 if explore else -(weight * value if by_value else weight), order)

        winners[customer] = [use_case for _, use_case, _, _, _ in sorted(rows, key=score)[:cap]]
    return winners


def test_three_overlapping_use_cases_one_action_each_and_the_caps_hold(storage: LocalStorage) -> None:
    uplift_a = treat_list_for_use_case(storage, "uc-a", kind="uplift", value=True, holdout=True)
    uplift_b = treat_list_for_use_case(
        storage, "uc-b", kind="uplift", value=True, config_overrides={"uplift.policy.margin_pct": 60.0}
    )
    propensity = treat_list_for_use_case(storage, "uc-c", kind="propensity", value=True)
    lists = [uplift_a, uplift_b, propensity]
    priorities = {"uc-a": 1.5, "uc-b": 1.0, "uc-c": 2.0}
    held = set(uplift_a.loc[_flag(uplift_a, "holdout"), "customer_id"])
    on_lists = pd.concat([f.loc[f["treat"], "customer_id"] for f in lists]).value_counts()
    assert on_lists.max() == 3, "some customer is on all three lists"

    out, summary = arbitrate_treat_lists(lists, _config(priorities), KEY)
    expected = _expected_winners(lists, priorities, held, cap=1)

    treated = out[out["treat"]]
    assert treated["customer_id"].is_unique, "no customer has two actions"
    assert not (set(treated["customer_id"]) & held), "hold-out members are never treated"
    assert set(treated["customer_id"]) == set(expected), "everyone wanted and not held back is treated once"
    assert {
        c: [w] for c, w in zip(treated["customer_id"], treated["winning_use_case"], strict=True)
    } == expected
    assert summary.treated_customers == len(expected)
    assert summary.customers_with_conflicts == sum(1 for c, n in on_lists.items() if n > 1 and c not in held)
    assert summary.dropped_actions_count == sum(
        on_lists[c] - 1 for c in expected  # each treated customer loses all but one of the actions wanted
    )

    # A contact cap of two: at most two actions each, the two the rule ranks first.
    out2, _ = arbitrate_treat_lists(lists, _config(priorities, contact_cap_per_customer=2), KEY)
    expected2 = _expected_winners(lists, priorities, held, cap=2)
    per_customer = out2[out2["treat"]].groupby("customer_id")["winning_use_case"].apply(list)
    assert per_customer.map(len).max() == 2
    assert {c: sorted(w) for c, w in per_customer.items()} == {c: sorted(w) for c, w in expected2.items()}


# ---------------------------------------------------------------------------
# Channel caps, on runs that plan a channel for each customer (M99)
# ---------------------------------------------------------------------------


def test_channel_caps_hold_and_capacity_goes_to_the_most_valuable(storage: LocalStorage) -> None:
    first = treat_list_for_use_case(storage, "uc-one", kind="uplift", value=True, channels=True)
    second = treat_list_for_use_case(
        storage,
        "uc-two",
        kind="uplift",
        value=True,
        channels=True,
        config_overrides={"uplift.policy.margin_pct": 60.0},
    )
    assert set(first.loc[first["treat"], "channel"]) == {"sms", "email"}

    uncapped, _ = arbitrate_treat_lists([first, second], _config(), KEY)
    sms = uncapped[uncapped["treat"] & (uncapped["channel"] == "sms")]
    assert len(sms) > 6

    out, summary = arbitrate_treat_lists([first, second], _config(channel_caps={"sms": 5}), KEY)
    sent = out[out["treat"] & (out["channel"] == "sms")]
    assert len(sent) == 5
    assert summary.channel_capped_count == len(sms) - 5
    # The five kept are the five with the largest priority x net value of those the cap applied to.
    assert set(sent["customer_id"]) == set(sms.nlargest(5, "priority_score")["customer_id"])
    # Email was not capped, and a customer capped out of SMS is not treated on another channel.
    emailed = out[out["treat"] & (out["channel"] == "email")]
    assert len(emailed) == len(uncapped[uncapped["treat"] & (uncapped["channel"] == "email")])
    capped_out = set(sms["customer_id"]) - set(sent["customer_id"])
    dropped = out[out["customer_id"].isin(capped_out)]
    assert not dropped["treat"].any()
    assert set(dropped["arbitration_reason"]) == {"channel_cap"}
    assert dropped["offer"].isna().all() and dropped["channel"].isna().all()
    assert dropped["losing_actions"].str.contains("channel cap").all()


# ---------------------------------------------------------------------------
# One selected use case equals its treat list (M98)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [
        {"kind": "propensity"},
        {"kind": "propensity", "value": True, "holdout": True},
        {"kind": "uplift", "value": True, "holdout": True},
        {"kind": "uplift", "value": True, "holdout": True, "channels": True},
    ],
    ids=["propensity", "propensity-value-holdout", "uplift-value-holdout", "uplift-channels"],
)
def test_with_one_use_case_selected_the_output_is_its_treat_list(
    storage: LocalStorage, options: dict[str, Any]
) -> None:
    original = treat_list_for_use_case(storage, "uc-only", **options)
    out, summary = arbitrate_treat_lists([original], _config(), KEY)

    assert list(out["customer_id"]) == list(original["customer_id"])
    for column in original.columns:
        left = out[column].astype("object").where(out[column].notna(), None)
        right = original[column].astype("object").where(original[column].notna(), None)
        assert left.tolist() == right.tolist(), column
    assert summary.treated_customers == int(original["treat"].sum())
    assert summary.customers_with_conflicts == 0 and summary.dropped_actions_count == 0
    winners = out[out["treat"]]
    assert set(winners["winning_use_case"]) == {"uc-only"}
    assert set(winners["arbitration_reason"]) == {"only_action"}


# ---------------------------------------------------------------------------
# Blocking 3: no shipped configuration; the defaults
# ---------------------------------------------------------------------------


def test_the_repository_ships_no_arbitration_config_only_an_example() -> None:
    """Fails on 98b2959, which shipped invented priorities for four use cases and channel caps for three."""
    assert not (REPO / "configs" / "decide" / "arbitration.yaml").exists()
    example = (REPO / "configs" / "decide" / "arbitration.example.yaml").read_text(encoding="utf-8")
    assert "AN EXAMPLE, NEVER READ" in example
    # The example is never read: the configuration root of the repository gives the defaults.
    assert load_arbitration_config(REPO / "configs") == ArbitrationConfig()


def test_with_no_config_priority_is_one_the_cap_is_one_and_there_are_no_channel_caps(
    storage: LocalStorage, tmp_path: Path
) -> None:
    config = load_arbitration_config(tmp_path / "an-empty-config-root")
    assert config.contact_cap_per_customer == 1
    assert config.channel_caps == {}
    assert config.priority_for("any-use-case") == 1.0

    first = treat_list_for_use_case(storage, "uc-one", kind="uplift", value=True, channels=True)
    second = treat_list_for_use_case(storage, "uc-two", kind="propensity", value=True, channels=True)
    out, summary = arbitrate_treat_lists([first, second], config, KEY)
    treated = out[out["treat"]]
    assert treated["customer_id"].is_unique
    assert (treated["priority_weight"] == 1.0).all()
    assert summary.channel_capped_count == 0
    assert set(treated["channel"]) == {"sms", "email"}


def test_a_negative_channel_cap_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        ArbitrationConfig(channel_caps={"sms": -1})


def test_the_output_columns_carry_the_treat_lists_own_and_the_arbitrations(storage: LocalStorage) -> None:
    first = treat_list_for_use_case(storage, "uc-one", kind="uplift", value=True, channels=True)
    second = treat_list_for_use_case(storage, "uc-two", kind="propensity", value=True, channels=True)
    out, _ = arbitrate_treat_lists([first, second], _config(), KEY)
    # Every column of either treat list (M99's contactable_channels, M100's runner-up columns when the
    # lists carry them) comes through.
    for column in [*first.columns, *second.columns]:
        assert column in out.columns, column
    assert "contactable_channels" in out.columns
    # The group column of each kind of run is kept, null for the other kind.
    assert {"segment", "band"} <= set(out.columns)
    assert out.loc[out["use_case"] == "uc-one", "segment"].notna().any()
    assert out.loc[out["use_case"] == "uc-one", "band"].isna().all()
    assert out.loc[out["use_case"] == "uc-two", "band"].notna().any()
    for column in ("winning_use_case", "losing_actions", "priority_score", "arbitration_reason"):
        assert column in out.columns


def test_arbitration_of_a_list_with_no_candidates_is_a_valid_empty_decision(storage: LocalStorage) -> None:
    original = treat_list_for_use_case(storage, "uc-none", kind="propensity")
    nobody = original.assign(treat=False)
    out, summary = arbitrate_treat_lists([nobody], _config(channel_caps={"sms": 1}), KEY)
    assert len(out) == len(original) and not out["treat"].any()
    assert summary.treated_customers == 0 and summary.dropped_actions_count == 0
    assert set(out["arbitration_reason"]) == {"not_selected"}
