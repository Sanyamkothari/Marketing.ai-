"""The scoring model beats chance on a planted signal, and falls back to theme labels when there is none."""

from __future__ import annotations

import pytest

from autonomy.content.config import CopyConfig
from autonomy.content.scoring import (
    FEATURE_NAMES,
    DraftResult,
    ScoringModel,
    ThemeBaseline,
    draft_features,
    merge_cells,
    tally_sends,
    within_type_auc,
)
from autonomy.content.types import CustomerTypes
from autonomy.contracts import Theme

from .synth import make_item, make_states, synth_results

CFG = CopyConfig(seed=1)


def test_within_type_auc_counts_only_pairs_inside_a_type() -> None:
    # Two types, two drafts each. The better draft scores higher in type a, lower in type b.
    types = ["a", "a", "b", "b"]
    sends, buys = [100, 100, 100, 100], [20, 5, 20, 5]
    perfect = within_type_auc(types, [0.9, 0.1, 0.9, 0.1], sends, buys)[0]
    worst = within_type_auc(types, [0.1, 0.9, 0.1, 0.9], sends, buys)[0]
    mixed = within_type_auc(types, [0.9, 0.1, 0.1, 0.9], sends, buys)[0]
    # In one type: 20 buyers of the better draft vs 95 non-buyers of the other (1,900 pairs the right way)
    # against 5 buyers of the other draft vs 80 non-buyers of the better one (400 the wrong way).
    assert perfect == pytest.approx(1900 / 2300)
    assert worst == pytest.approx(400 / 2300)
    assert mixed == pytest.approx(0.5)
    assert within_type_auc(types, [0.5] * 4, sends, buys)[0] == pytest.approx(0.5)
    assert within_type_auc(["a", "b"], [0.1, 0.9], [10, 10], [5, 1])[1] == 0.0  # no pair inside a type


def test_draft_features_are_fixed_width_and_read_the_text() -> None:
    short = draft_features(make_item("s", words=5))
    long = draft_features(make_item("l", words=40, theme=Theme.URGENCY))
    assert short.shape == long.shape == (len(FEATURE_NAMES),)
    col = {n: i for i, n in enumerate(FEATURE_NAMES)}
    assert long[col["words"]] > short[col["words"]] and long[col["log_chars"]] > short[col["log_chars"]]
    assert long[col["theme:urgency"]] == 1.0 and short[col["theme:urgency"]] == 0.0
    assert short[col["slot_first_name"]] == 1.0 and short[col["offer:none"]] == 1.0
    assert short[col["greeting"]] == 1.0


def test_a_planted_signal_is_found_and_beats_theme_labels_on_held_out_drafts() -> None:
    results, traits = synth_results(seed=11, signal=True)
    model = ScoringModel(CFG)
    report = model.fit(results, traits)
    assert report.used and model.usable, report.sentence()
    assert report.auc is not None and report.theme_auc is not None
    assert report.auc > 0.6  # chance is 0.5
    assert report.auc > report.theme_auc + 0.05  # theme labels cannot see copy length
    assert report.test_cells > 0 and report.test_purchases >= CFG.min_test_purchases
    # And it predicts in the planted direction: short wins where the first trait is high, long where low.
    short, long_ = make_item("short", words=8), make_item("long", words=42)
    assert model.predict(short, "ct005") > model.predict(long_, "ct005")  # trait +1.5
    assert model.predict(long_, "ct000") > model.predict(short, "ct000")  # trait -1.5
    ranked = model.rank([long_, short], "ct005")
    assert [i.content_id for i, _ in ranked] == ["short", "long"]
    assert "used" in report.sentence() and "held-out AUC" in report.sentence()


def test_without_a_signal_it_falls_back_to_theme_labels() -> None:
    for seed in (21, 22, 23, 24, 25):
        results, traits = synth_results(seed=seed, signal=False)
        model = ScoringModel(CFG)
        report = model.fit(results, traits)
        assert not report.used and not model.usable, (seed, report.sentence())
        assert report.auc is not None  # the held-out numbers are still reported
        baseline = ThemeBaseline(merge_cells(results))
        for item in (make_item("p", words=8), make_item("q", words=40, theme=Theme.SAVINGS)):
            assert model.predict(item, "ct002") == baseline.rate("ct002", item.theme)


def test_too_little_history_falls_back_with_a_reason() -> None:
    results, traits = synth_results(seed=5, signal=True, n_types=2, drafts_per_type=6, sends=20)
    model = ScoringModel(CFG)
    report = model.fit(results, traits)
    assert not report.used
    assert "too little history" in report.reason
    assert report.auc is None
    empty = ScoringModel(CFG)
    assert not empty.fit([], {}).used
    assert empty.predict(make_item("z"), "ct000") == pytest.approx(0.05)  # no data: a plain 5% starting guess


def test_the_theme_baseline_shrinks_towards_the_theme_and_the_shop() -> None:
    reminder, savings = make_item("r", theme=Theme.REMINDER), make_item("s", theme=Theme.SAVINGS)
    cells = [
        DraftResult(reminder, "a", 1000, 100),
        DraftResult(savings, "a", 1000, 20),
        DraftResult(savings, "b", 10, 5),
    ]
    base = ThemeBaseline(cells)
    assert base.rate("a", Theme.REMINDER) > base.rate("a", Theme.SAVINGS)
    assert base.rate("b", Theme.SAVINGS) < 0.2  # 5 of 10 is shrunk hard towards the theme's rate
    assert 0.0 < base.rate("c", Theme.NOVELTY) < 1.0  # an unseen theme gets the shop's rate


def test_results_must_be_possible_and_repeats_are_added() -> None:
    item = make_item("a")
    with pytest.raises(ValueError):
        DraftResult(item, "t", 10, 11)
    merged = merge_cells([DraftResult(item, "t", 10, 1), DraftResult(item, "t", 30, 4)])
    assert [(c.sends, c.purchases) for c in merged] == [(40, 5)]


def test_past_sends_are_retyped_by_todays_types() -> None:
    states = make_states(90)
    types = CustomerTypes.fit(states, CopyConfig(min_types=3, max_types=3))
    item = make_item("only")
    cells = tally_sends([(item, s, i % 4 == 0) for i, s in enumerate(states)], types)
    assert sum(c.sends for c in cells) == 90
    assert sum(c.purchases for c in cells) == sum(1 for i in range(90) if i % 4 == 0)
    assert {c.type_id for c in cells} <= {t.type_id for t in types.types}
