"""What your file already says (DEC-1596): the first look, counted with no AI and no model.

Every rate below is recomputed here with plain pandas, independently of `rate_by`; the ranking rule
(spread = sum of rows_g / rows * |rate_g - base|) is recomputed too.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.agent.tools import AgentContext
from engine.config import RunMode, UseCaseConfig, get_roles, load_use_case
from engine.doctor.contracts import UseCaseReadiness
from engine.doctor.diagnose import LoadedFile, Offer, diagnose_file, readiness
from engine.doctor.first_look import (
    FIRST_LOOK_COLUMNS,
    FIRST_LOOK_LABEL,
    FirstLook,
    LookColumn,
    LookGroup,
    attach_recency,
    outcome_look,
    separation,
    spread,
)
from engine.pilot.plain import jargon_in
from engine.stages.ingest import profile_dataset

USE_CASE = "targeted-advertisement"
OUTCOME = "converted_30d"
ROWS = 3000
RATES = {"basic": 0.05, "standard": 0.12, "premium": 0.30}


def history(rows: int = ROWS, seed: int = 7) -> pd.DataFrame:
    """A customer history with a strong column, a weak one, personal data, two leaks, an ID and a date."""
    rng = np.random.default_rng(seed)
    tiers = rng.choice(list(RATES), size=rows, p=[0.5, 0.3, 0.2]).astype(object)
    tiers[:5] = "legacy"  # a value of 5 rows: never named
    rate = np.array([RATES.get(tier, 0.1) for tier in tiers])
    outcome = (rng.random(rows) < rate).astype(int)
    spend = np.round(rng.gamma(2.0, 50.0, size=rows) + outcome * 40.0, 2)
    flip = rng.random(rows) < 0.003
    return pd.DataFrame(
        {
            "customer_id": [f"C{index:06d}" for index in range(rows)],
            "signup_date": pd.date_range("2023-01-01", periods=rows, freq="h").strftime("%Y-%m-%d"),
            "plan_tier": tiers,
            "region": rng.choice(["north", "south", "east", "west"], size=rows),
            "monthly_spend": spend,
            "email": [f"person{index}@example.com" for index in range(rows)],
            "outcome_status": np.where(outcome == 1, "won", "lost"),
            "stage_code": np.where((outcome == 1) ^ flip, "A", "B"),
            "channel": rng.choice(["app", "web", "store"], size=rows),
            OUTCOME: outcome,
        }
    )


def config(config_root: Path, hide: tuple[str, ...] = ()) -> UseCaseConfig:
    base = load_use_case(USE_CASE, config_root)
    if not hide:
        return base
    return base.model_copy(update={"agent": base.agent.model_copy(update={"always_hide_columns": hide})})


def loaded(
    frame: pd.DataFrame, cfg: UseCaseConfig, name: str = "history.csv", upload_id: str = "u_1"
) -> LoadedFile:
    profile = profile_dataset(
        frame,
        cfg,
        upload_id=upload_id,
        file_name=name,
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame),
    )
    return LoadedFile(upload_id=upload_id, file_name=name, frame=frame, profile=profile)


def verdict_for(frame: pd.DataFrame, cfg: UseCaseConfig, config_root: Path) -> UseCaseReadiness:
    (verdict,) = readiness([loaded(frame, cfg)], [Offer(config=cfg, stage="Awareness")], config_root)
    return verdict


def context(frame: pd.DataFrame, cfg: UseCaseConfig, config_root: Path) -> AgentContext:
    file = loaded(frame, cfg)
    return AgentContext(
        use_case_id=cfg.id,
        config=cfg,
        config_root=config_root,
        upload_id=file.upload_id,
        mode=RunMode.TRAIN,
        profile=file.profile,
        frame=frame,
    )


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    return history()


@pytest.fixture(scope="module")
def look(frame: pd.DataFrame, repo_root: Path) -> FirstLook:
    root = repo_root / "configs"
    verdict = verdict_for(frame, config(root, hide=("channel",)), root)
    assert verdict.status in {"ready", "check"}, verdict.summary
    assert verdict.first_look is not None
    return verdict.first_look


def by_column(look: FirstLook) -> dict[str, LookColumn]:
    return {column.column: column for column in look.columns}


def test_the_base_rate_is_the_files_rate_of_yes(look: FirstLook, frame: pd.DataFrame) -> None:
    assert look.kind == "outcome" and look.label == FIRST_LOOK_LABEL
    assert look.label == "Describes your file. Not a cause and not a prediction."
    assert look.outcome_column == OUTCOME and look.positive_label == "1"
    assert look.base_rate == pytest.approx(frame[OUTCOME].mean(), abs=1e-6)
    assert look.rows == len(frame) and look.file_rows == len(frame) and not look.sampled
    assert f"{frame[OUTCOME].mean():.1%}" in look.summary
    assert not jargon_in(look.summary) and not jargon_in(look.rule or "")


def test_rates_by_value_are_recomputed_with_pandas(look: FirstLook, frame: pd.DataFrame) -> None:
    tier = by_column(look)["plan_tier"]
    assert tier.kind == "values"
    counts = frame["plan_tier"].value_counts()
    rates = frame.groupby("plan_tier")[OUTCOME].mean()
    for group in tier.groups:
        if group.group == "(other)":
            continue
        assert group.rows == counts[group.group]
        assert group.rate == pytest.approx(rates[group.group], abs=1e-6)
        assert group.share == pytest.approx(counts[group.group] / len(frame), abs=1e-6)
    # "legacy" (5 rows) cannot stand alone, so the smallest named value joins it in (other): the rate of
    # "premium" cannot be worked back from the totals.
    assert {g.group for g in tier.groups} == {"basic", "standard", "(other)"}
    (other,) = [g for g in tier.groups if g.group == "(other)"]
    merged = frame[frame["plan_tier"].isin(["legacy", "premium"])]
    assert other.rows == len(merged)
    assert other.rate == pytest.approx(merged[OUTCOME].mean(), abs=1e-6)
    assert not jargon_in(tier.summary)


def test_rates_by_number_range_are_recomputed_with_pandas(look: FirstLook, frame: pd.DataFrame) -> None:
    spend = by_column(look)["monthly_spend"]
    assert spend.kind == "number_ranges"
    bins = pd.qcut(frame["monthly_spend"], q=5, duplicates="drop")
    expected = frame.groupby(bins, observed=True)[OUTCOME].agg(["size", "mean"])
    assert [g.rows for g in spend.groups] == expected["size"].tolist()
    assert [g.rate for g in spend.groups] == pytest.approx(expected["mean"].tolist(), abs=1e-6)


def test_a_group_under_ten_rows_is_never_named(look: FirstLook) -> None:
    for column in look.columns:
        assert all(group.rows >= 10 for group in column.groups)
        assert "legacy" not in {group.group for group in column.groups}
    tier = by_column(look)["plan_tier"]
    other = [g for g in tier.groups if g.group == "(other)"]
    assert other and other[0].rows >= 10  # "legacy" was merged, with the smallest named value if needed


def test_personal_and_hidden_columns_are_never_used(look: FirstLook) -> None:
    used = {column.column for column in look.columns}
    left = {item.column: item.reason for item in look.left_out}
    assert "email" not in used and left["email"] == "personal"
    assert "channel" not in used and left["channel"] == "personal"  # agent.always_hide_columns


def test_columns_that_may_hold_the_answer_are_left_out(look: FirstLook) -> None:
    used = {column.column for column in look.columns}
    left = {item.column: item.reason for item in look.left_out}
    assert "outcome_status" not in used and left["outcome_status"] == "may_hold_answer"
    assert "stage_code" not in used and left["stage_code"] == "may_hold_answer"


def test_a_near_perfect_column_is_left_out_without_the_advisor(
    frame: pd.DataFrame, config_root: Path
) -> None:
    """With no advisor findings at all, a column whose groups split yes from no is still left out."""
    cfg = config(config_root)
    found = outcome_look(
        context(frame, cfg, config_root), outcome=OUTCOME, file_name="h.csv", file_rows=len(frame)
    )
    assert found is not None
    left = {item.column: item.reason for item in found.left_out}
    assert left["stage_code"] == "may_hold_answer" and left["outcome_status"] == "may_hold_answer"
    assert {"stage_code", "outcome_status"}.isdisjoint(column.column for column in found.columns)


def test_id_and_date_columns_are_not_grouped(look: FirstLook) -> None:
    used = {column.column for column in look.columns}
    left = {item.column: item.reason for item in look.left_out}
    assert left["customer_id"] == "id" and left["signup_date"] == "date"
    assert {"customer_id", "signup_date", OUTCOME}.isdisjoint(used)


def test_the_most_informative_columns_come_first_by_the_documented_rule(
    look: FirstLook, frame: pd.DataFrame
) -> None:
    assert 0 < len(look.columns) <= FIRST_LOOK_COLUMNS
    base = frame[OUTCOME].mean()
    for column in look.columns:
        rated = [g for g in column.groups if g.rate is not None]
        total = sum(g.rows for g in rated)
        expected = sum(g.rows / total * abs(g.rate - base) for g in rated if g.rate is not None)
        assert column.spread == pytest.approx(expected, abs=1e-5)
    spreads = [column.spread for column in look.columns]
    assert spreads == sorted(spreads, reverse=True)
    assert look.columns[0].column == "plan_tier"  # the strong column leads, the noise columns trail
    tier = frame.groupby("plan_tier")[OUTCOME].agg(["size", "mean"])
    region = frame.groupby("region")[OUTCOME].agg(["size", "mean"])
    tier_spread = (tier["size"] / len(frame) * (tier["mean"] - base).abs()).sum()
    region_spread = (region["size"] / len(frame) * (region["mean"] - base).abs()).sum()
    assert tier_spread > region_spread


def test_the_first_look_is_deterministic(frame: pd.DataFrame, config_root: Path) -> None:
    cfg = config(config_root)
    ctx = context(frame, cfg, config_root)
    first = outcome_look(ctx, outcome=OUTCOME, file_name="h.csv", file_rows=len(frame))
    again = outcome_look(
        context(frame.copy(), cfg, config_root), outcome=OUTCOME, file_name="h.csv", file_rows=len(frame)
    )
    assert first is not None and again is not None
    assert first.model_dump() == again.model_dump()


def test_rows_read_fewer_than_the_file_say_so(frame: pd.DataFrame, config_root: Path) -> None:
    cfg = config(config_root)
    found = outcome_look(
        context(frame, cfg, config_root), outcome=OUTCOME, file_name="h.csv", file_rows=10 * len(frame)
    )
    assert found is not None and found.sampled and found.file_rows == 10 * len(frame)


def test_an_outcome_that_is_not_yes_or_no_has_no_first_look(config_root: Path) -> None:
    frame = pd.DataFrame({"customer_id": range(300), "plan": ["a", "b", "c"] * 100, "spend": range(300)})
    cfg = config(config_root)
    assert (
        outcome_look(context(frame, cfg, config_root), outcome="spend", file_name="h.csv", file_rows=300)
        is None
    )


def test_a_verdict_that_is_not_ready_has_no_first_look(config_root: Path) -> None:
    cfg = config(config_root)
    verdict = verdict_for(history(rows=200), cfg, config_root)
    assert verdict.status in {"too_small", "missing"} and verdict.first_look is None


def test_spread_and_separation_by_hand() -> None:
    groups = [
        LookGroup(group="a", rows=50, share=0.5, rate=0.1),
        LookGroup(group="b", rows=50, share=0.5, rate=0.3),
    ]
    assert spread(groups, 0.2) == pytest.approx(0.1)
    perfect = [
        LookGroup(group="a", rows=40, share=0.4, rate=0.0),
        LookGroup(group="b", rows=60, share=0.6, rate=1.0),
    ]
    assert separation(perfect) == pytest.approx(1.0)
    same = [
        LookGroup(group="a", rows=40, share=0.4, rate=0.2),
        LookGroup(group="b", rows=60, share=0.6, rate=0.2),
    ]
    assert separation(same) == pytest.approx(0.5)
    assert separation([LookGroup(group="a", rows=10, share=1.0, rate=0.0)]) is None


# ---------------------------------------------------------------------------
# Recency and frequency, for a file with no outcome
# ---------------------------------------------------------------------------
def recharges(rows: int = 4000, customers: int = 500, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = rng.integers(0, 500, size=rows)
    frame = pd.DataFrame(
        {
            "customer_id": [f"c{index:05d}" for index in rng.integers(0, customers, size=rows)],
            "recharge_date": (pd.Timestamp("2024-03-01") + pd.to_timedelta(days, unit="D")).strftime(
                "%Y-%m-%d"
            ),
            "amount": rng.integers(10, 500, size=rows),
        }
    )
    lonely = pd.DataFrame(  # three customers seen once, long ago: under 10, never shown alone
        {"customer_id": ["z1", "z2", "z3"], "recharge_date": ["2023-01-05"] * 3, "amount": [10, 20, 30]}
    )
    return pd.concat([frame, lonely], ignore_index=True)


def recency_of(frame: pd.DataFrame, config_root: Path) -> FirstLook | None:
    cfg = config(config_root)
    file = loaded(frame, cfg, name="recharges.csv", upload_id="u_log")
    diagnosis = diagnose_file(file, cfg, get_roles(config_root))
    (out,) = attach_recency([diagnosis], [frame], [file.profile], [cfg])
    return out.first_look


def test_recency_counts_are_recomputed_with_pandas(config_root: Path) -> None:
    frame = recharges()
    look = recency_of(frame, config_root)
    assert look is not None and look.kind == "recency" and look.label == FIRST_LOOK_LABEL
    assert look.id_column == "customer_id" and look.date_column == "recharge_date"
    times = pd.to_datetime(frame["recharge_date"])
    latest = times.max()
    assert look.latest == latest.date()
    last = times.groupby(frame["customer_id"]).max()
    days = (latest - last).dt.days
    assert look.customers == len(last)
    expected = {
        "In the last 30 days": int((days <= 30).sum()),
        "31 to 90 days before": int(((days > 30) & (days <= 90)).sum()),
        "91 to 180 days before": int(((days > 90) & (days <= 180)).sum()),
    }
    assert look.recency is not None
    shown = {group.group: group.rows for group in look.recency.groups}
    for name, count in expected.items():
        assert shown[name] == count
    assert all(group.rows >= 10 for group in look.recency.groups)
    assert look.frequency is not None
    sizes = frame.groupby("customer_id").size()
    shown_frequency = {group.group: group.rows for group in look.frequency.groups}
    assert shown_frequency["4 to 10 rows"] == int(((sizes >= 4) & (sizes <= 10)).sum())
    assert "1 row" not in shown_frequency  # the three customers seen once are fewer than 10
    assert all(group.rows >= 10 for group in look.frequency.groups)
    assert not jargon_in(look.summary + look.recency.summary + look.frequency.summary)


def test_a_file_with_an_outcome_gets_no_recency_picture(frame: pd.DataFrame, config_root: Path) -> None:
    assert recency_of(frame, config_root) is None


def test_a_personal_id_or_no_date_means_no_recency_picture(config_root: Path) -> None:
    frame = recharges().rename(columns={"customer_id": "email"})
    frame["email"] = frame["email"] + "@example.com"
    assert recency_of(frame, config_root) is None
    assert recency_of(recharges().drop(columns=["recharge_date"]), config_root) is None
