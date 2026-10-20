"""Public-data replay: the three readers, the evaluation harness and the segment policy.

The fixtures in `fixtures/` are tiny synthetic files with exactly the published column layout of
each dataset (the Open Bandit files keep their unnamed index column and all 80 affinity columns);
Hillstrom uses the repo's committed sample. The harness is checked against hand computations and
against a small log with a planted best action per segment.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from autonomy.proof.replay import (
    HILLSTROM_SAMPLE,
    LoggedBandit,
    evaluate,
    fit_segment_policy,
    fixed_action_policy,
    read_criteo_uplift,
    read_hillstrom,
    read_open_bandit,
    replay,
)
from autonomy.proof.replay.fetch import DATASETS, sha256_file

FIXTURES = Path(__file__).parent / "fixtures"


# --- readers -------------------------------------------------------------------------------------


def test_open_bandit_random_reads_into_the_common_shape() -> None:
    log = read_open_bandit(FIXTURES / "obd_random_all.csv")
    assert log.source == "open_bandit/random/all"
    assert log.n_rows == 40
    assert log.n_actions == 80
    assert np.allclose(log.propensity, 1 / 80)
    assert set(np.unique(log.reward)) <= {0.0, 1.0}
    cols = set(log.contexts.columns)
    assert {"position", "user_feature_0", "user-item_affinity_79"} <= cols
    assert not cols & {"click", "propensity_score", "item_id", "timestamp"}


def test_open_bandit_bts_from_the_dataset_folder(tmp_path: Path) -> None:
    folder = tmp_path / "open_bandit_dataset" / "bts" / "all"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURES / "obd_bts_all.csv", folder / "all.csv")
    log = read_open_bandit(tmp_path / "open_bandit_dataset", behavior_policy="bts")
    assert log.source == "open_bandit/bts/all"
    raw = pd.read_csv(FIXTURES / "obd_bts_all.csv", index_col=0)
    assert np.allclose(log.propensity, raw["propensity_score"].to_numpy())
    assert np.array_equal(log.actions, raw["item_id"].to_numpy())
    # The file name alone also says which policy logged it.
    assert read_open_bandit(FIXTURES / "obd_bts_all.csv").source == "open_bandit/bts/all"


def test_open_bandit_needs_a_known_policy(tmp_path: Path) -> None:
    anon = tmp_path / "log.csv"
    shutil.copy(FIXTURES / "obd_random_all.csv", anon)
    with pytest.raises(ValueError, match="behaviour policy"):
        read_open_bandit(anon)
    with pytest.raises(ValueError, match="outside"):
        read_open_bandit(anon, behavior_policy="random", n_items=5)


def test_open_bandit_campaign_comes_from_the_documented_path(tmp_path: Path) -> None:
    raw = pd.read_csv(FIXTURES / "obd_bts_all.csv", index_col=0)
    folder = tmp_path / "bts" / "men"
    folder.mkdir(parents=True)
    raw[raw["item_id"] < 34].to_csv(folder / "men.csv")
    log = read_open_bandit(folder / "men.csv")
    assert log.source == "open_bandit/bts/men"
    assert log.n_actions == 34
    # The folder form takes the campaign as given.
    assert read_open_bandit(tmp_path, behavior_policy="bts", campaign="men").n_actions == 34


def test_hillstrom_sample_has_three_arms_at_one_third() -> None:
    log = read_hillstrom()
    raw = pd.read_csv(HILLSTROM_SAMPLE)
    assert log.n_rows == len(raw)
    assert log.action_names == ("No E-Mail", "Mens E-Mail", "Womens E-Mail")
    assert np.allclose(log.propensity, 1 / 3)
    assert np.array_equal(log.reward, raw["conversion"].to_numpy(dtype=float))
    assert not set(log.contexts.columns) & {"segment", "visit", "conversion", "spend", "customer_id"}
    assert [log.action_names[a] for a in log.actions[:5]] == raw["segment"].head().tolist()


def test_hillstrom_net_spend_charges_only_emailed_customers() -> None:
    log = read_hillstrom(reward="spend", email_cost=0.5)
    raw = pd.read_csv(HILLSTROM_SAMPLE)
    emailed = raw["segment"].to_numpy() != "No E-Mail"
    assert np.allclose(log.reward, raw["spend"].to_numpy() - 0.5 * emailed)
    assert log.reward_name == "spend - 0.5 per e-mail"


def test_criteo_uses_the_known_treatment_rate() -> None:
    log = read_criteo_uplift(FIXTURES / "criteo_uplift.csv")
    treated = log.actions == 1
    assert log.action_names == ("control", "treatment")
    assert np.allclose(log.propensity[treated], 0.85)
    assert np.allclose(log.propensity[~treated], 0.15)
    assert list(log.contexts.columns) == [f"f{i}" for i in range(12)]

    empirical = read_criteo_uplift(FIXTURES / "criteo_uplift.csv", reward="visit", treatment_rate=None)
    rate = treated.mean()
    assert np.allclose(empirical.propensity, np.where(treated, rate, 1 - rate))
    assert empirical.reward_name == "visit"


def test_readers_refuse_the_wrong_file() -> None:
    with pytest.raises(ValueError, match="missing column"):
        read_criteo_uplift(HILLSTROM_SAMPLE)
    with pytest.raises(ValueError, match="missing column"):
        read_hillstrom(FIXTURES / "criteo_uplift.csv")


# --- the harness ---------------------------------------------------------------------------------


def _planted(n: int = 2000, seed: int = 7) -> LoggedBandit:
    """Two segments, two actions at 50/50: 'b' customers respond to action 1, 'a' customers to nothing."""
    rng = np.random.default_rng(seed)
    seg = rng.choice(["a", "b"], size=n)
    actions = rng.integers(0, 2, size=n).astype(np.int64)
    rate = np.where(seg == "b", np.where(actions == 1, 0.6, 0.1), np.where(actions == 1, 0.05, 0.3))
    return LoggedBandit(
        source="planted",
        contexts=pd.DataFrame({"seg": seg}),
        actions=actions,
        propensity=np.full(n, 0.5),
        reward=(rng.random(n) < rate).astype(np.float64),
        action_names=("nothing", "send"),
        reward_name="conversion",
    )


def test_ips_of_a_fixed_action_matches_the_hand_computation() -> None:
    log = read_hillstrom()
    est = evaluate(fixed_action_policy(1), log, policy_name="always mens")
    expected = np.mean(3.0 * log.reward * (log.actions == 1))
    assert est.ips.point == pytest.approx(expected)
    assert est.ips.low < est.ips.point < est.ips.high
    assert est.logging_value.point == pytest.approx(log.reward.mean())
    assert est.ips_vs_logging.point == pytest.approx(expected - log.reward.mean())
    assert est.max_weight == pytest.approx(3.0)


def test_the_logging_policy_scores_exactly_the_logged_mean() -> None:
    log = read_hillstrom()
    est = evaluate(lambda lg: np.full((lg.n_rows, 3), 1 / 3), log)
    assert est.ips.point == pytest.approx(log.reward.mean())
    assert est.snips is not None
    assert est.snips.point == pytest.approx(log.reward.mean())
    assert est.ips_vs_logging.point == pytest.approx(0.0, abs=1e-12)
    assert not est.beats_logging
    assert "cannot be told apart" in est.sentence


def test_evaluate_rejects_malformed_policies() -> None:
    log = _planted(50)
    with pytest.raises(ValueError, match="shape"):
        evaluate(lambda lg: np.ones((lg.n_rows, 3)) / 3, log)
    with pytest.raises(ValueError, match="sum to 1"):
        evaluate(lambda lg: np.ones((lg.n_rows, 2)), log)
    with pytest.raises(ValueError, match="two"):
        evaluate(fixed_action_policy(0), log.take(np.array([0])))


def test_segment_policy_finds_the_planted_best_action_and_beats_logging() -> None:
    report = replay(_planted(), segment_columns=["seg"], seed=3)
    assert report.policy.choice == {"seg=a": 0, "seg=b": 1}
    assert report.beats_logging
    target = report.target
    assert target.dr_vs_logging.low > 0
    # Truth: logging earns ~0.5*(0.175 + 0.35) = 0.26; the planted best earns ~0.5*(0.3 + 0.6) = 0.45.
    assert target.dr.point == pytest.approx(0.45, abs=0.06)
    assert [e.policy_name for e in report.fixed_actions] == ["always nothing", "always send"]
    assert all(e.dr.point < target.dr.point for e in report.fixed_actions)
    assert "beats the logging policy" in report.summary


def test_segment_policy_bins_numbers_on_train_and_explores_with_epsilon() -> None:
    log = read_hillstrom()
    policy = fit_segment_policy(log, segment_columns=["history", "womens"], n_bins=3, epsilon=0.3)
    assert len(policy.edges["history"]) == 2  # three bins from train quantiles
    assert "womens" not in policy.edges  # only two values: used as a category
    probs = policy(log)
    assert np.allclose(probs.sum(axis=1), 1.0)
    assert probs.min() == pytest.approx(0.1)
    assert probs.max() == pytest.approx(0.8)


def test_segment_policy_never_picks_an_action_it_never_saw() -> None:
    # Every logged action loses money; action 2 was never logged, so nothing says it would not.
    n = 40
    log = LoggedBandit(
        source="x",
        contexts=pd.DataFrame({"seg": ["a", "b"] * (n // 2)}),
        actions=np.array([0, 1] * (n // 2), dtype=np.int64),
        propensity=np.full(n, 0.5),
        reward=np.where(np.arange(n) % 2 == 0, -1.0, -2.0),
        action_names=("nothing", "send", "never logged"),
        reward_name="net",
    )
    for shrinkage in (0.0, 50.0):
        policy = fit_segment_policy(log, segment_columns=["seg"], shrinkage=shrinkage)
        assert policy.default_action == 0
        assert set(policy.choice.values()) == {0}


def test_segment_policy_shrinks_by_rows_not_by_inverse_propensity() -> None:
    # Segment 'a' has only 3 control rows (logged at p=0.15) that look great, beside 30 treated
    # rows. Three rows must not outvote 50 pseudo-rows of shrinkage toward treatment's overall
    # lead; counting each control row as ~5 (Σ1/p times the mean propensity) would let them.
    seg = ["a"] * 33 + ["b"] * 350
    actions = np.array([0] * 3 + [1] * 30 + [0] * 50 + [1] * 300, dtype=np.int64)
    reward = np.array([3.0] * 3 + [0.5] * 30 + [0.0] * 50 + [0.5] * 300)
    log = LoggedBandit(
        source="x",
        contexts=pd.DataFrame({"seg": seg}),
        actions=actions,
        propensity=np.where(actions == 1, 0.85, 0.15),
        reward=reward,
        action_names=("control", "treatment"),
        reward_name="r",
    )
    policy = fit_segment_policy(log, segment_columns=["seg"], shrinkage=50.0)
    assert policy.choice == {"seg=a": 1, "seg=b": 1}


def test_hillstrom_replay_is_deterministic_and_reports_honestly() -> None:
    log = read_hillstrom(reward="spend", email_cost=0.1)
    first = replay(log, segment_columns=["history_segment", "womens"], seed=11)
    again = replay(log, segment_columns=["history_segment", "womens"], seed=11)
    assert first.target.dr == again.target.dr
    assert first.n_train + first.n_test == log.n_rows
    assert len(first.fixed_actions) == 3
    assert first.summary.startswith("On hillstrom")
    if not first.beats_logging:
        assert "Honest note" in first.summary


def test_open_bandit_and_criteo_fixtures_replay_end_to_end() -> None:
    obd = replay(read_open_bandit(FIXTURES / "obd_bts_all.csv"), segment_columns=["position"], seed=1)
    assert obd.fixed_actions == ()  # 80 items: blanket rules are skipped
    assert obd.n_test == 20
    criteo = replay(read_criteo_uplift(FIXTURES / "criteo_uplift.csv"), segment_columns=["f0"], seed=1)
    assert len(criteo.fixed_actions) == 2
    assert np.isfinite(criteo.target.dr.point)


def test_split_is_seeded_and_disjoint() -> None:
    log = _planted(100)
    train, test = log.split(0.3, seed=5)
    train2, test2 = log.split(0.3, seed=5)
    assert (train.n_rows, test.n_rows) == (70, 30)
    assert train.contexts.equals(train2.contexts) and np.array_equal(test.reward, test2.reward)
    with pytest.raises(ValueError):
        log.split(1.0)


def test_logged_bandit_rejects_impossible_propensities() -> None:
    with pytest.raises(ValueError, match="above 0"):
        LoggedBandit(
            source="x",
            contexts=pd.DataFrame({"a": [1, 2]}),
            actions=np.array([0, 1], dtype=np.int64),
            propensity=np.array([0.5, 0.0]),
            reward=np.array([1.0, 0.0]),
            action_names=("a", "b"),
            reward_name="r",
        )


# --- fetch (no network) --------------------------------------------------------------------------


def test_fetch_specs_cover_every_dataset_and_hash_files(tmp_path: Path) -> None:
    assert {"open_bandit", "hillstrom", "criteo_uplift"} <= set(DATASETS)
    assert all(spec.urls for spec in DATASETS.values())
    assert DATASETS["hillstrom"].sha256 is not None
    target = tmp_path / "f.txt"
    target.write_bytes(b"abc")
    assert sha256_file(target) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
