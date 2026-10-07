"""Holdout membership and the explore slice (Plan J M92 acceptance).

* Stability: on 10,000 keys with universal scope, membership is identical across 12 monthly runs, two
  use cases, 20% of rows becoming suppressed and a shuffled row order.
* Share and nesting: on 1,000,000 keys the realised share is inside the binomial 99.9% band, and the
  5% members are a strict subset of the 10% members.
* Composite keys: one membership per entity.
* Propensity and uplift runs give the same membership.
* No sleeping dog is ever treated, explore rows included.

Every expectation is computed from the rule itself (`sha256(f"{salt}:{scope_key}:{entity}")`), never
read back from the code under test.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from engine.holdout.assign import (
    ASSIGNMENT_COLUMNS,
    ActiveHoldout,
    active_holdout,
    assignment_frame,
    holdout_context,
    member_flags,
)
from engine.stages.actions import CONTROL_GROUP_COLUMN, SUPPRESSED_REASON_COLUMN, apply_actions
from engine.uplift.actions import TREAT_ACTION, apply_uplift_actions
from engine.uplift.contracts import Segment, SegmentThresholds
from tests.unit.holdout.support import CONSENT, SALT, active, binomial_band, keys, scored, use_case

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
MONTHS = tuple(f"r_2026{month:02d}01_0000{month:04x}" for month in range(1, 13))
THRESHOLDS = SegmentThresholds(
    persuadable_min_uplift=0.02,
    sleeping_dog_max_uplift=-0.01,
    sure_thing_min_probability=0.40,
    sure_thing_from_base_rate=False,
)


def by_rule(entity: str, *, salt: str, scope_key: str, fraction: float) -> bool:
    """The plan's formula, spelled exactly as written: hex digest, first 16 characters, base 16."""
    return (
        int(hashlib.sha256(f"{salt}:{scope_key}:{entity}".encode()).hexdigest()[:16], 16) < fraction * 2**64
    )


def act(
    frame: pd.DataFrame, config: object, run_id: str, holdout: ActiveHoldout | None, **kwargs: object
) -> pd.DataFrame:
    with holdout_context(holdout):
        return apply_actions(frame, config, run_id=run_id, primary_key="customer_id", now=NOW, **kwargs)  # type: ignore[arg-type]


def assignment(
    banded: pd.DataFrame, config: object, run_id: str, holdout: ActiveHoldout | None, **kwargs: object
) -> pd.DataFrame:
    return assignment_frame(
        banded,
        config,  # type: ignore[arg-type]
        primary_key=str(kwargs.get("primary_key", "customer_id")),
        row_key=str(kwargs.get("row_key", "customer_id")),
        entity_key=kwargs.get("entity_key"),  # type: ignore[arg-type]
        run_id=run_id,
        active=holdout,
        explore_fraction=float(kwargs.get("explore", 0.0)),  # type: ignore[arg-type]
    ).table


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------
def test_membership_is_the_plans_threshold_rule() -> None:
    ids = keys(2_000)
    flags = member_flags(ids, salt=SALT, scope_key="universal", fraction=0.10)
    assert flags.tolist() == [by_rule(key, salt=SALT, scope_key="universal", fraction=0.10) for key in ids]


def test_nothing_is_active_unless_a_flow_makes_it_so() -> None:
    assert active_holdout() is None
    holdout = ActiveHoldout(salt=SALT, scope_key="universal", fraction=0.1)
    with holdout_context(holdout):
        assert active_holdout() is holdout
    assert active_holdout() is None
    assert SALT not in repr(holdout)


def test_share_on_a_million_keys_is_binomial_and_smaller_fractions_nest() -> None:
    ids = keys(1_000_000)
    ten = member_flags(ids, salt=SALT, scope_key="universal", fraction=0.10)
    five = member_flags(ids, salt=SALT, scope_key="universal", fraction=0.05)
    for flags, fraction in ((ten, 0.10), (five, 0.05)):
        low, high = binomial_band(len(ids), fraction)
        assert low <= int(flags.sum()) <= high, (fraction, int(flags.sum()), low, high)
    assert not bool((five & ~ten).any()), "every 5% member is a 10% member"
    assert int(five.sum()) < int(ten.sum()), "and the subset is strict"


# ---------------------------------------------------------------------------
# Stability: 12 runs, 2 use cases, 20% newly suppressed, shuffled rows
# ---------------------------------------------------------------------------
def test_universal_membership_never_moves() -> None:
    ids = keys(10_000)
    expected = {key: by_rule(key, salt=SALT, scope_key="universal", fraction=0.10) for key in ids}
    configs = [
        use_case("targeted-advertisement", scope="universal", fraction=0.10),
        use_case("telco-churn", scope="universal", fraction=0.10),
    ]
    for month, run_id in enumerate(MONTHS):
        for config in configs:
            frame = scored(ids, seed=month, suppressed_share=0.0 if month == 0 else 0.20)
            frame = frame.sample(frac=1.0, random_state=month).reset_index(drop=True)  # shuffled order
            banded = act(frame, config, run_id, active(config))
            table = assignment(banded, config, run_id, active(config))
            members = dict(zip(table["customer_id"], table["holdout_member"], strict=True))
            assert members == expected, (run_id, config.id)
            eligible = banded[SUPPRESSED_REASON_COLUMN].isna()
            wanted = eligible & banded["customer_id"].map(expected).astype(bool)
            assert banded[CONTROL_GROUP_COLUMN].tolist() == wanted.tolist(), "control = eligible and member"


def test_the_use_case_scope_is_stable_per_use_case_and_differs_between_them() -> None:
    ids = keys(10_000)
    first = use_case("targeted-advertisement", scope="use_case", fraction=0.10)
    second = use_case("telco-churn", scope="use_case", fraction=0.10)
    tables = {}
    for config in (first, second):
        seen = []
        for run_id in MONTHS[:3]:
            banded = act(scored(ids, seed=7), config, run_id, active(config))
            seen.append(assignment(banded, config, run_id, active(config))["holdout_member"].tolist())
        assert seen[0] == seen[1] == seen[2]
        tables[config.id] = seen[0]
    assert tables[first.id] != tables[second.id]


def test_without_a_holdout_today_s_run_seeded_draw_is_unchanged() -> None:
    """The control mask under `scope: run` is the Phase 1 draw: two run ids draw two holdouts."""
    config = use_case()
    frame = scored(keys(400), seed=3)
    one = act(frame, config, MONTHS[0], None)
    two = act(frame, config, MONTHS[1], None)
    assert one[CONTROL_GROUP_COLUMN].tolist() != two[CONTROL_GROUP_COLUMN].tolist()
    eligible = int(one[SUPPRESSED_REASON_COLUMN].isna().sum())
    assert int(one[CONTROL_GROUP_COLUMN].sum()) == int(eligible * 0.10 + 0.5)


# ---------------------------------------------------------------------------
# Composite keys
# ---------------------------------------------------------------------------
def test_a_two_column_key_has_one_membership_per_entity() -> None:
    config = use_case(scope="universal", fraction=0.20)
    entities = keys(600)
    rows = []
    for snapshot in ("2026-07-31", "2026-08-31", "2026-09-30"):
        for index, entity in enumerate(entities):
            rows.append({"account_id": entity, "snapshot": snapshot, CONSENT: index % 9 != 0})
    frame = pd.DataFrame(rows)
    frame["customer_id"] = frame["account_id"] + "|" + frame["snapshot"]
    frame["propensity"] = np.linspace(0.0, 1.0, len(frame.index))
    with holdout_context(active(config)):
        banded = apply_actions(
            frame, config, run_id=MONTHS[0], primary_key="customer_id", entity_key="account_id", now=NOW
        )
    table = assignment_frame(
        banded,
        config,
        primary_key="customer_id",
        row_key="customer_id",
        entity_key="account_id",
        run_id=MONTHS[0],
        active=active(config),
        explore_fraction=0.0,
    ).table
    per_entity = pd.DataFrame(
        {
            "entity": banded["account_id"],
            "member": table["holdout_member"],
            "control": banded[CONTROL_GROUP_COLUMN],
        }
    ).groupby("entity")
    assert int(per_entity["member"].nunique().max()) == 1, "one membership per entity"
    assert int(per_entity["control"].nunique().max()) == 1, "no entity is in both arms"
    for entity, member in per_entity["member"].first().items():
        assert member == by_rule(str(entity), salt=SALT, scope_key="universal", fraction=0.20)


# ---------------------------------------------------------------------------
# Propensity and uplift agree
# ---------------------------------------------------------------------------
def uplift_frame(ids: list[str], *, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    uplift = np.round(rng.uniform(-0.3, 0.4, size=len(ids)), 4)
    p_control = np.round(rng.uniform(0.01, 0.8, size=len(ids)), 4)
    return pd.DataFrame(
        {
            "customer_id": ids,
            "uplift": uplift,
            "p_treated": np.clip(p_control + uplift, 0.0, 1.0),
            "p_control": p_control,
            CONSENT: [index % 10 != 0 for index in range(len(ids))],
        }
    )


def uplift_run(
    frame: pd.DataFrame, config: object, holdout: ActiveHoldout, run_id: str = MONTHS[0]
) -> pd.DataFrame:
    with holdout_context(holdout):
        result, _ = apply_uplift_actions(
            frame,
            config,  # type: ignore[arg-type]
            run_id=run_id,
            primary_key="customer_id",
            causal=True,
            thresholds=THRESHOLDS,
            now=NOW,
        )
    return result


def test_propensity_and_uplift_runs_hold_out_the_same_customers() -> None:
    config = use_case(scope="universal", fraction=0.10, policy={})
    frame = uplift_frame(keys(3_000))
    uplift = uplift_run(frame, config, active(config))
    propensity = act(frame.assign(propensity=0.5), config, MONTHS[5], active(config))
    assert uplift[CONTROL_GROUP_COLUMN].tolist() == propensity[CONTROL_GROUP_COLUMN].tolist()
    left = assignment(uplift, config, MONTHS[0], active(config))["holdout_member"]
    right = assignment(propensity, config, MONTHS[5], active(config))["holdout_member"]
    assert left.tolist() == right.tolist()


# ---------------------------------------------------------------------------
# The explore slice
# ---------------------------------------------------------------------------
def test_no_sleeping_dog_is_treated_or_explored() -> None:
    config = use_case(scope="universal", fraction=0.10, explore=0.10, policy={})
    frame = uplift_frame(keys(5_000), seed=11)
    result = uplift_run(frame, config, active(config))
    found = assignment_frame(
        result,
        config,
        primary_key="customer_id",
        row_key="customer_id",
        entity_key=None,
        run_id=MONTHS[0],
        active=active(config),
        explore_fraction=0.10,
    )
    table = found.table
    sleeping = (result["segment"] == Segment.SLEEPING_DOG.value).to_numpy()
    treated = (result["action"] == TREAT_ACTION).to_numpy()
    explore = table["explore"].to_numpy(dtype=bool)
    assert sleeping.any() and explore.any(), "the fixture has both"
    assert not bool((sleeping & treated).any())
    assert not bool((sleeping & explore).any())
    eligible = result[SUPPRESSED_REASON_COLUMN].isna().to_numpy()
    member = table["holdout_member"].to_numpy(dtype=bool)
    assert not bool((explore & ~eligible).any()), "only eligible rows are explored"
    assert not bool((explore & member).any()), "never a holdout member"
    assert not bool((explore & treated).any()), "never a row the policy already treats"
    candidate = eligible & ~member & ~treated & ~sleeping
    assert table["explore_probability"].tolist() == [0.10 if flag else 0.0 for flag in candidate.tolist()]
    low, high = binomial_band(int(candidate.sum()), 0.10)
    assert low <= int(explore.sum()) <= high
    assert found.candidates == int(candidate.sum()) and found.explored == int(explore.sum())


def test_a_propensity_run_explores_outside_its_top_bands() -> None:
    config = use_case(explore=0.10)
    frame = scored(keys(4_000), seed=2, suppressed_share=0.1)
    banded = act(frame, config, MONTHS[0], None)
    table = assignment(banded, config, MONTHS[0], None, explore=0.10)
    explore = table["explore"].to_numpy(dtype=bool)
    assert explore.any()
    lowest = config.actions.bands[-1].name
    assert set(banded.loc[explore, "band"]) == {lowest}
    assert not bool((explore & banded[CONTROL_GROUP_COLUMN].to_numpy()).any())
    assert (
        table["holdout_member"].tolist() == banded[CONTROL_GROUP_COLUMN].tolist()
    ), "under run, member = control"


def test_the_explore_draw_changes_each_run_at_the_same_probability() -> None:
    config = use_case(scope="universal", fraction=0.10, explore=0.10)
    frame = scored(keys(4_000), seed=4)
    tables = [
        assignment(act(frame, config, run, active(config)), config, run, active(config), explore=0.10)
        for run in MONTHS[:2]
    ]
    assert tables[0]["explore"].tolist() != tables[1]["explore"].tolist()
    assert tables[0]["explore_probability"].tolist() == tables[1]["explore_probability"].tolist()
    assert tables[0]["holdout_member"].tolist() == tables[1]["holdout_member"].tolist()


def test_zero_explore_marks_nobody() -> None:
    config = use_case(scope="universal", fraction=0.10)
    frame = scored(keys(1_000), seed=4)
    table = assignment(act(frame, config, MONTHS[0], active(config)), config, MONTHS[0], active(config))
    assert not table["explore"].any()
    assert set(table["explore_probability"]) == {0.0}


def test_the_table_has_every_row_and_the_contract_columns() -> None:
    config = use_case(scope="universal", fraction=0.10)
    frame = scored(keys(500), seed=1, suppressed_share=0.3)
    table = assignment(act(frame, config, MONTHS[0], active(config)), config, MONTHS[0], active(config))
    assert list(table.columns) == ["customer_id", *ASSIGNMENT_COLUMNS]
    assert len(table.index) == 500, "suppressed rows included"
    suppressed = ~frame[CONSENT].to_numpy()
    assert bool(table.loc[suppressed, "holdout_member"].any()), "a suppressed member is still recorded as one"


def test_a_control_group_that_is_not_the_holdout_is_refused() -> None:
    config = use_case(scope="universal", fraction=0.10)
    frame = scored(keys(1_000), seed=1)
    banded = act(frame, config, MONTHS[0], None)  # forgot the context: today's per-run draw
    with pytest.raises(RuntimeError, match="not the persistent holdout"):
        assignment(banded, config, MONTHS[0], active(config))
