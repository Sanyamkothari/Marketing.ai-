"""The typed menu: settings in safe ranges, nothing outside the menu, and the fitted policies."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from autonomy.contracts import CustomerState
from autonomy.scientist import (
    BanditAgent,
    BlanketPolicy,
    MenuError,
    SegmentRule,
    UpliftPolicy,
    candidate_from_spec,
    default_candidates,
)
from autonomy.scientist.menu import KINDS, SETTING_RANGES, CandidateFailed, frame_from_preferences
from tests.v2.scientist.test_menu_campaign import make_campaign

ARMS = ("no message", "A", "B")
COLS = ("x1", "x2", "x3")


def spec(kind: str, **settings: object) -> dict[str, object]:
    return {"kind": kind, "settings": settings}


def build(kind: str, **settings: object) -> object:
    return candidate_from_spec(spec(kind, **settings), arm_names=ARMS, columns=COLS)


# --- typed settings ---------------------------------------------------------------------------------


def test_the_menu_has_four_kinds() -> None:
    assert KINDS == ("blanket", "segment", "uplift", "bandit")
    kinds = {
        build("blanket", arm=1).kind,  # type: ignore[attr-defined]
        build("segment").kind,  # type: ignore[attr-defined]
        build("uplift").kind,  # type: ignore[attr-defined]
        build("bandit").kind,  # type: ignore[attr-defined]
    }
    assert kinds == set(KINDS)


@pytest.mark.parametrize(
    ("kind", "settings"),
    [
        ("segment", {"exploration_floor": 0.5}),
        ("segment", {"exploration_floor": -0.01}),
        ("segment", {"lookback_days": 3}),
        ("segment", {"lookback_days": 400}),
        ("segment", {"weekly_contact_budget": 99}),
        ("segment", {"weekly_contact_budget": -1}),
        ("segment", {"max_columns": 4}),
        ("segment", {"n_bins": 1}),
        ("segment", {"shrinkage": 1e6}),
        ("uplift", {"exploration_floor": 0.21}),
        ("uplift", {"lookback_days": 6}),
        ("bandit", {"exploration_floor": 0.3}),
        ("blanket", {"arm": 1, "weekly_contact_budget": 8}),
    ],
)
def test_out_of_range_settings_are_refused(kind: str, settings: dict[str, object]) -> None:
    with pytest.raises(MenuError):
        build(kind, **settings)


@pytest.mark.parametrize(
    ("kind", "settings"),
    [
        ("segment", {"exploration_floor": "0.05"}),  # text is not a number
        ("segment", {"exploration_floor": True}),
        ("segment", {"max_columns": 1.5}),
        ("segment", {"exploration_floor": float("nan")}),
        ("segment", {"exploration_floor": float("inf")}),
        ("segment", {"unknown_setting": 1}),
        ("uplift", {"columns": ["x1"]}),  # a setting of another kind
        ("blanket", {"arm": 7}),
        ("blanket", {"arm": -1}),
        ("blanket", {"arm": 1.0}),
        ("blanket", {}),  # no arm
        ("segment", {"columns": ["nope"]}),  # a column the campaign does not have
        ("segment", {"columns": []}),
        ("segment", {"columns": "x1"}),
    ],
)
def test_wrong_types_unknown_keys_and_unknown_columns_are_refused(
    kind: str, settings: dict[str, object]
) -> None:
    with pytest.raises(MenuError):
        build(kind, **settings)


def test_unknown_kinds_and_spec_keys_are_refused() -> None:
    bad_specs: list[dict[str, object]] = [
        {"kind": "custom", "settings": {}},
        {"kind": "__import__('os')", "settings": {}},
        {"kind": 3, "settings": {}},
        {"settings": {}},
        {"kind": "uplift", "settings": {}, "code": "print(1)"},
        {"kind": "uplift", "settings": [1, 2]},
    ]
    for bad in bad_specs:
        with pytest.raises(MenuError):
            candidate_from_spec(bad, arm_names=ARMS, columns=COLS)


def test_direct_construction_is_validated_too() -> None:
    with pytest.raises(MenuError):
        SegmentRule(exploration_floor=0.9)
    with pytest.raises(MenuError):
        UpliftPolicy(lookback_days=1)
    with pytest.raises(MenuError):
        BlanketPolicy(-1)
    with pytest.raises(MenuError):
        BlanketPolicy(1, weekly_contact_budget=100)
    with pytest.raises(MenuError):
        SegmentRule(columns=("a", "b", "c", "d"))


def test_defaults_sit_inside_their_ranges_and_specs_round_trip() -> None:
    lo, hi = SETTING_RANGES["exploration_floor"]
    for cand in (BlanketPolicy(1), SegmentRule(), UpliftPolicy(), BanditAgent()):
        settings = cand.spec()["settings"]
        assert isinstance(settings, dict)
        floor = settings.get("exploration_floor", lo)
        assert lo <= floor <= hi
        again = candidate_from_spec(cand.spec(), arm_names=ARMS, columns=COLS)
        assert again == cand
        assert hash(again) == hash(cand)  # frozen: hashable, comparable
    rule = SegmentRule(columns=("x1", "x2"), max_columns=2, n_bins=3, shrinkage=10.0, lookback_days=30)
    assert candidate_from_spec(rule.spec(), arm_names=ARMS, columns=COLS) == rule


def test_default_list_is_fixed_and_at_most_three() -> None:
    assert [c.kind for c in default_candidates()] == ["segment", "uplift"]
    runner = lambda fit, held: np.tile([1.0, 0.0, 0.0], (held.n, 1))  # noqa: E731
    full = default_candidates(runner)
    assert [c.kind for c in full] == ["segment", "uplift", "bandit"]
    assert len(full) <= 3


# --- blanket -----------------------------------------------------------------------------------------


def test_blanket_probabilities_floor_and_budget() -> None:
    c = make_campaign(300)
    fit = BlanketPolicy(1, exploration_floor=0.0).fit(c)
    p = fit.probabilities(c.features, None)
    assert p.shape == (300, 3) and (p[:, 1] == 1.0).all()
    with_floor = BlanketPolicy(1, exploration_floor=0.2).fit(c).probabilities(c.features, None)
    assert np.allclose(with_floor[0], [0.2 / 3, 0.8 + 0.2 / 3, 0.2 / 3])
    assert np.allclose(with_floor.sum(axis=1), 1.0)
    nobody = BlanketPolicy(1, weekly_contact_budget=0).fit(c).probabilities(c.features, None)
    assert (nobody[:, 0] == 1.0).all()  # a budget of 0 contacts nobody, floor or not
    assert BlanketPolicy(0).label(ARMS) == "send nothing"
    assert BlanketPolicy(2).label(ARMS) == "send to all: B"
    with pytest.raises(MenuError):
        BlanketPolicy(5).fit(c)


# --- the fitted policies ----------------------------------------------------------------------------


def test_segment_rule_finds_the_split_that_pays() -> None:
    c = make_campaign(8000, seed=1)
    fitted = SegmentRule(exploration_floor=0.0).fit(c)
    assert "x1" in fitted.label
    hi = pd.DataFrame({"x1": [0.9] * 20, "x2": ["a"] * 20, "x3": [0.0] * 20})
    lo = pd.DataFrame({"x1": [0.1] * 20, "x2": ["a"] * 20, "x3": [0.0] * 20})
    assert (fitted.probabilities(hi, None).argmax(axis=1) == 1).all()  # send A where x1 is high
    assert (fitted.probabilities(lo, None).argmax(axis=1) == 0).all()  # nothing where it loses money
    named = SegmentRule(columns=("x1",), exploration_floor=0.0).fit(c)
    assert named.label == "segment rule (x1)"


def test_segment_rule_refuses_unknown_columns_at_fit() -> None:
    c = make_campaign(500)
    with pytest.raises(MenuError):
        SegmentRule(columns=("nope",)).fit(c)


def test_uplift_policy_sends_where_the_model_says_it_pays() -> None:
    c = make_campaign(9000, seed=2)
    fitted = UpliftPolicy(exploration_floor=0.0).fit(c)
    hi = pd.DataFrame({"x1": np.linspace(0.8, 0.99, 30), "x2": ["b"] * 30, "x3": [0.0] * 30})
    lo = pd.DataFrame({"x1": np.linspace(0.01, 0.2, 30), "x2": ["b"] * 30, "x3": [0.0] * 30})
    send_hi = (fitted.probabilities(hi, None).argmax(axis=1) == 1).mean()
    send_lo = (fitted.probabilities(lo, None).argmax(axis=1) == 1).mean()
    assert send_hi > 0.8 and send_lo < 0.2, (send_hi, send_lo)


def test_the_matrix_scoring_equals_the_models_own_score() -> None:
    """The batch path must give what `FittedUplift.score` gives one customer at a time."""
    c = make_campaign(5000, seed=3)
    fitted = UpliftPolicy().fit(c)
    rows = c.features.iloc[:25].reset_index(drop=True)
    batch = fitted.uplift(rows, None)  # type: ignore[attr-defined]
    model = fitted.model  # type: ignore[attr-defined]
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(len(rows)):
        prefs = {k: str(v) for k, v in rows.iloc[i].items()}
        state = CustomerState(customer_id=f"z{i}", as_of=now, preferences=prefs)
        for j, key in enumerate(model.keys):
            one = model.score(state, key, now)
            assert one is not None
            assert batch[i, j] == pytest.approx(one.mean, abs=1e-3)


def test_failures_are_reported_not_hidden() -> None:
    c = make_campaign(400)
    unlogged = type(c)(**{**c.__dict__, "propensity": None})  # same rows, no logged probabilities
    with pytest.raises(CandidateFailed):
        SegmentRule().fit(unlogged)
    with pytest.raises(CandidateFailed):
        UpliftPolicy().fit(unlogged)
    with pytest.raises(CandidateFailed):
        BanditAgent().held_out_probabilities(c, c)  # no runner
    bad = BanditAgent(runner=lambda fit, held: np.zeros((3, 3)))
    with pytest.raises(CandidateFailed):
        bad.held_out_probabilities(c, c)
    with pytest.raises(CandidateFailed):
        BanditAgent().fit(c)


def test_lookback_uses_only_the_recent_rows() -> None:
    c = make_campaign(6000, seed=4)
    times = np.concatenate([np.full(3000, 1.0e9), np.full(3000, 1.0e9 + 20 * 86_400.0)])
    # the first 3,000 rows are 20 days old; poison their outcomes
    converted = c.converted.copy()
    margin = c.margin.copy()
    converted[:3000] = 1.0 - converted[:3000]
    margin[:3000] = 10.0 * converted[:3000]
    fields = {**c.__dict__, "times": times, "converted": converted, "margin": margin}
    fields.pop("attributes", None)
    fields.pop("behaviour_matrix", None)
    poisoned = type(c)(**fields)
    recent_only = SegmentRule(columns=("x1",), lookback_days=7, exploration_floor=0.0)
    all_rows = SegmentRule(columns=("x1",), exploration_floor=0.0)
    probe = c.features.iloc[:400].reset_index(drop=True)
    a = recent_only.fit(poisoned).probabilities(probe, None)
    clean_recent = poisoned.take(np.arange(3000, 6000))
    b = all_rows.fit(clean_recent).probabilities(probe, None)
    assert np.array_equal(
        a, b
    )  # the 7-day look-back fits on the recent rows, as if the old ones did not exist


def test_preferences_become_a_frame_with_the_fitted_dtypes() -> None:
    dtypes = (("flag", "int64"), ("score", "float64"), ("city", "object"))
    f = frame_from_preferences({"flag": "1", "score": "2.5", "city": "Pune"}, dtypes)
    assert f.iloc[0].to_dict() == {"flag": 1, "score": 2.5, "city": "Pune"}
    assert str(f["flag"].dtype) == "int64"  # so a segment label reads 'flag=1', as it did in training
    missing = frame_from_preferences({}, dtypes)
    assert np.isnan(missing.iloc[0]["flag"]) and np.isnan(missing.iloc[0]["score"])
    assert missing.iloc[0]["city"] is None
