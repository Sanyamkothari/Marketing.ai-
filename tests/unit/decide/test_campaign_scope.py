"""Plan M M184 (DEC-1614 (a)): an approved campaign plan's limits on its run's treat list.

The policy checks a campaign plan's payload - one channel, a number of contacts - so the list that is executed
must keep to them. `campaign_scope.json` (written by the planner's EXECUTE) limits every build of the run's treat
list: other channels and anyone beyond the number are not contacted, the number taken from the highest value of
the run's own ranking column down (net value, else predicted uplift, on an uplift run; the run's score on a
propensity run) and never by key order alone; the held-back rows keep their flags. A run without the file builds
exactly what it built before.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from engine.decide.campaign_scope import (
    CAMPAIGN_SCOPE_FILENAME,
    OTHER_CHANNEL,
    OVER_LIMIT,
    CampaignScope,
    CampaignScopeError,
    apply_campaign_scope,
    priority_order,
    write_campaign_scope,
)
from engine.decide.treat_list import (
    TREAT_LIST_PARQUET,
    TREAT_LIST_SUMMARY_FILENAME,
    TreatListError,
    build_treat_list,
    ensure_treat_list,
)
from engine.storage import LocalStorage, run_key
from engine.uplift.contracts import RANKING_CHOICE_FILENAME, RankingChoice
from tests.fixtures.decide.treat_runs import write_run

RUN = "r_20261011_0c184001"
AT = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


def _scope(channel: str, limit: int | None, run_id: str = RUN) -> CampaignScope:
    return CampaignScope(run_id=run_id, channel=channel, max_contacts=limit, proposal_id="prop_1", set_at=AT)


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------
def test_other_channels_are_cut_then_the_highest_scores_are_kept_up_to_the_limit() -> None:
    treat = np.array([True, True, True, True, False, True])
    channel = np.array(["sms", "email", "sms", "sms", None, None], dtype=object)
    score = np.array([0.2, 0.9, 0.8, 0.5, 0.99, 0.7])
    keys = np.array(["a", "b", "c", "d", "e", "f"], dtype=object)

    cut = apply_campaign_scope(_scope("sms", 2), treat=treat, channel=channel, priority=score, keys=keys)

    assert cut.treat.tolist() == [False, False, True, True, False, False]
    assert cut.other_channel.tolist() == [False, True, False, False, False, True]  # email, and no channel
    assert cut.over_limit.tolist() == [True, False, False, False, False, False]  # the lowest sms score
    assert cut.channel.tolist() == [None, None, "sms", "sms", None, None]
    assert cut.withheld() == {OTHER_CHANNEL: 2, OVER_LIMIT: 1}
    assert not cut.treat[4], "a customer the list did not contact is never contacted by the limits"


def test_no_limit_keeps_every_customer_on_the_channel() -> None:
    treat = np.array([True, True, True])
    channel = np.array(["sms", "sms", "email"], dtype=object)
    cut = apply_campaign_scope(
        _scope("sms", None), treat=treat, channel=channel, priority=[0.1, 0.2, 0.3], keys=["a", "b", "c"]
    )
    assert cut.treat.tolist() == [True, True, False]
    assert cut.withheld() == {OTHER_CHANNEL: 1, OVER_LIMIT: 0}


def test_the_order_is_the_score_down_a_missing_score_last_and_ties_by_key() -> None:
    order = priority_order([0.5, np.nan, 0.9, 0.5, 0.5], ["z", "a", "m", "b", "y"])
    assert order.tolist() == [2, 3, 4, 0, 1]
    # The same input always gives the same cut.
    again = priority_order([0.5, np.nan, 0.9, 0.5, 0.5], ["z", "a", "m", "b", "y"])
    assert again.tolist() == order.tolist()


def test_a_limit_of_zero_contacts_nobody() -> None:
    cut = apply_campaign_scope(
        _scope("sms", 0), treat=[True, True], channel=["sms", "sms"], priority=[0.4, 0.6], keys=["a", "b"]
    )
    assert not cut.treat.any() and cut.withheld() == {OTHER_CHANNEL: 0, OVER_LIMIT: 2}


@pytest.mark.parametrize("priority", [None, [np.nan, np.nan, np.nan]], ids=["no-column", "all-null"])
def test_a_cut_with_nothing_to_rank_by_is_refused_not_made_by_key(priority: list[float] | None) -> None:
    with pytest.raises(CampaignScopeError, match="not cut by customer key"):
        apply_campaign_scope(
            _scope("sms", 1),
            treat=[True, True, True],
            channel=["sms", "sms", "sms"],
            priority=priority,
            keys=["a", "b", "c"],
            no_ranking="the run keeps no score",
        )
    # No cut needed: nothing to rank, nothing refused.
    cut = apply_campaign_scope(
        _scope("sms", 3),
        treat=[True, True, True],
        channel=["sms", "sms", "sms"],
        priority=priority,
        keys="abc",
    )
    assert cut.treat.all()


# ---------------------------------------------------------------------------
# The treat list, built under the limits
# ---------------------------------------------------------------------------
def _parquet(storage: LocalStorage, run_id: str) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, TREAT_LIST_PARQUET))))


@pytest.fixture
def channel_run(tmp_path: Path) -> tuple[LocalStorage, str]:
    """A propensity run with a holdout and two channels: some customers are sent by SMS, others by email."""
    storage = LocalStorage(tmp_path)
    run = write_run(storage, RUN, rows=120, holdout=True, channels=True)
    return storage, run.run_id


def test_without_limits_the_list_is_unchanged_and_says_nothing_of_them(
    channel_run: tuple[LocalStorage, str],
) -> None:
    storage, run_id = channel_run
    summary = build_treat_list(storage, run_id)
    first = storage.read_bytes(run_key(run_id, TREAT_LIST_PARQUET))
    raw = storage.read_bytes(run_key(run_id, TREAT_LIST_SUMMARY_FILENAME)).decode("utf-8")
    assert "campaign_scope" not in raw
    assert summary.campaign_scope_channel is None and summary.campaign_scope_withheld is None
    assert not storage.exists(run_key(run_id, CAMPAIGN_SCOPE_FILENAME))
    build_treat_list(storage, run_id)
    assert storage.read_bytes(run_key(run_id, TREAT_LIST_PARQUET)) == first


def test_the_limits_keep_one_channel_and_the_highest_scores_and_leave_the_holdout_alone(
    channel_run: tuple[LocalStorage, str],
) -> None:
    storage, run_id = channel_run
    whole = build_treat_list(storage, run_id)
    before = _parquet(storage, run_id)
    assert whole.channel_rows is not None and set(whole.channel_rows) == {
        "sms",
        "email",
    }, "the fixture must send some customers on each channel"
    on_sms = before.loc[before["treat"] & (before["channel"] == "sms")]
    limit = len(on_sms) - 3
    assert limit > 0

    write_campaign_scope(storage, _scope("sms", limit, run_id))
    summary = build_treat_list(storage, run_id)
    after = _parquet(storage, run_id)

    assert summary.treat_rows == limit
    assert summary.channel_rows == {"sms": limit}
    assert set(after.loc[after["treat"], "channel"]) == {"sms"}
    assert summary.campaign_scope_channel == "sms" and summary.campaign_scope_max_contacts == limit
    assert summary.campaign_scope_proposal_id == "prop_1"
    assert summary.campaign_scope_withheld == {
        OTHER_CHANNEL: int(whole.channel_rows["email"]),
        OVER_LIMIT: 3,
    }
    # The kept customers are the highest scores among those sent by SMS (ties by key).
    scores = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run_id, "scores.parquet"))))
    score_of = dict(zip(scores["customer_id"].astype(str), scores["winback_prob"], strict=True))
    ranked = sorted(on_sms["customer_id"], key=lambda key: (-score_of[key], key))
    assert sorted(after.loc[after["treat"], "customer_id"]) == sorted(ranked[:limit])
    # The comparison group is untouched: the same held-back and control rows, flagged as before.
    for column in ("holdout", "control_group", "explore", "suppression_reason"):
        assert after[column].equals(before[column]), column
    assert summary.holdout_rows == whole.holdout_rows
    # A customer the limits took off says why, and has no offer or channel.
    cut = after.loc[before["treat"] & ~after["treat"]]
    assert len(cut) == int(whole.channel_rows["email"]) + 3
    assert cut["channel"].isna().all() and cut["offer"].isna().all()
    assert cut["offer_reason"].str.startswith("Not contacted: the approved campaign").all()
    assert summary.campaign_scope_ranked_by == "winback_prob"
    over = cut.loc[cut["customer_id"].isin(ranked[limit:])]
    assert over["offer_reason"].str.contains("highest score (winback_prob) down", regex=False).all()


# ---------------------------------------------------------------------------
# An uplift run: ranked by its own net value or uplift, never by key text (review of M184)
# ---------------------------------------------------------------------------
def _cut_by(storage: LocalStorage, run_id: str, column: str, over: int = 5) -> tuple[pd.DataFrame, int]:
    """The list built under an SMS limit `over` below its SMS contacts; (the list, the limit)."""
    build_treat_list(storage, run_id)
    before = _parquet(storage, run_id)
    on_sms = int((before["treat"] & (before["channel"] == "sms")).sum())
    limit = on_sms - over
    assert limit > 0, on_sms
    write_campaign_scope(storage, _scope("sms", limit, run_id))
    summary = build_treat_list(storage, run_id)
    after = _parquet(storage, run_id)
    assert summary.treat_rows == limit and summary.campaign_scope_ranked_by == column
    assert summary.campaign_scope_withheld is not None and summary.campaign_scope_withheld[OVER_LIMIT] == over
    return after, limit


@pytest.mark.parametrize(
    ("value", "column"), [(True, "net_value"), (False, "uplift")], ids=["value", "no-value"]
)
def test_an_uplift_list_keeps_its_highest_value_customers(tmp_path: Path, value: bool, column: str) -> None:
    storage = LocalStorage(tmp_path)
    run = write_run(storage, RUN, kind="uplift", rows=400, holdout=True, channels=True, value=value)
    after, limit = _cut_by(storage, run.run_id, column)
    scores = pd.read_parquet(io.BytesIO(storage.read_bytes(run_key(run.run_id, "scores.parquet"))))
    assert "winback_prob" not in scores.columns, "the run's actions.score_field is not in uplift scores"
    rank = (
        after if column == "net_value" else after.merge(scores[["customer_id", "uplift"]], on="customer_id")
    )
    over = rank["offer_reason"].fillna("").str.contains("at most")
    kept = rank.loc[rank["treat"], column]
    assert len(kept) == limit and int(over.sum()) == 5
    assert (
        kept.min() >= rank.loc[over, column].max()
    ), "the kept customers are the highest-ranked, not key order"
    label = "net value" if column == "net_value" else "predicted uplift"
    assert rank.loc[over, "offer_reason"].str.contains(f"highest {label} down", regex=False).all()


def _ranked_by_propensity(storage: LocalStorage, run_id: str) -> None:
    choice = RankingChoice(
        run_id=run_id,
        model_version_id="m_x_2",
        ranking="propensity_model",
        code="UPLIFT_NOT_BETTER_THAN_RISK",
        beats_risk=False,
        propensity_model_id="m_x_1",
        contacts=10,
        reason="Ranked by the approved propensity model.",
        computed_at=AT,
    )
    storage.write_model(run_key(run_id, RANKING_CHOICE_FILENAME), choice)


def test_an_uplift_list_ranked_by_a_propensity_model_is_refused_when_it_must_be_cut(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    run = write_run(storage, RUN, kind="uplift", rows=400, holdout=True, channels=True, value=True)
    _ranked_by_propensity(storage, run.run_id)
    build_treat_list(storage, run.run_id)
    on_sms = int((lambda f: (f["treat"] & (f["channel"] == "sms")).sum())(_parquet(storage, run.run_id)))
    write_campaign_scope(storage, _scope("sms", on_sms - 1, run.run_id))
    with pytest.raises(TreatListError) as refused:
        build_treat_list(storage, run.run_id)
    assert refused.value.code == "CAMPAIGN_LIST_NOT_AS_APPROVED"
    assert "m_x_1" in refused.value.message and "not cut by customer key" in refused.value.message
    # Within the number, nothing needs ranking: the channel limit alone applies.
    write_campaign_scope(storage, _scope("sms", on_sms, run.run_id))
    summary = build_treat_list(storage, run.run_id)
    assert summary.treat_rows == on_sms and summary.campaign_scope_ranked_by is None


def test_a_list_stored_before_the_limits_is_built_again_under_them(
    channel_run: tuple[LocalStorage, str],
) -> None:
    storage, run_id = channel_run
    whole = ensure_treat_list(storage, run_id)
    assert ensure_treat_list(storage, run_id) == whole, "no limits: the stored list is served as it is"
    write_campaign_scope(storage, _scope("email", None, run_id))
    limited = ensure_treat_list(storage, run_id)
    assert limited.campaign_scope_channel == "email"
    assert limited.channel_rows is not None and set(limited.channel_rows) == {"email"}


def test_limits_that_cannot_be_read_stop_the_build(channel_run: tuple[LocalStorage, str]) -> None:
    storage, run_id = channel_run
    storage.write_bytes(run_key(run_id, CAMPAIGN_SCOPE_FILENAME), b"{not json")
    with pytest.raises(TreatListError) as refused:
        build_treat_list(storage, run_id)
    assert refused.value.code == "CAMPAIGN_LIST_NOT_AS_APPROVED"
