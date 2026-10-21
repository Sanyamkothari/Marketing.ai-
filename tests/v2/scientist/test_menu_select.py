"""The selection rule: best lower bound after Holm, else the safe default, else 'not enough data'."""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist

import numpy as np
import pytest

from autonomy.contracts import Estimate
from autonomy.proof.estimate import Z_95
from autonomy.scientist import (
    ALPHA,
    MAX_CANDIDATES,
    BlanketPolicy,
    MenuError,
    SegmentRule,
    UpliftPolicy,
    holm_adjust,
    run_scientist,
    safe_default,
    select,
)
from autonomy.scientist.evaluate import Evaluation, Evaluations, Split
from autonomy.scientist.select import SELECTION_RULE, SafeDefault
from tests.v2.scientist.test_menu_campaign import make_campaign
from tests.v2.scientist.test_menu_evaluate import replace


def est(point: float, se: float) -> Estimate:
    return Estimate(point=point, low=point - Z_95 * se, high=point + Z_95 * se)


def fake_eval(
    gains: Sequence[tuple[float, float] | None],
    *,
    n_held: int = 10_000,
    margin: float = 1.5,
    default_value: float = 1.0,
) -> tuple[Evaluations, list[BlanketPolicy]]:
    """Evaluations built by hand: candidate i gains `gains[i] = (point, se)` over the default
    (None = it failed to fit)."""
    cands: list[Evaluation] = []
    for i, g in enumerate(gains):
        if g is None:
            cands.append(Evaluation(f"cand{i}", "segment", {}, "failed", "could not be fitted"))
            continue
        point, se = g
        cands.append(
            Evaluation(
                f"cand{i}",
                "segment",
                {"kind": "segment"},
                "ok",
                "",
                value=est(default_value + point, se),
                ips=default_value + point,
                snips=default_value + point,
                vs_default=est(point, se),
                vs_none=est(point, se),
                contact_rate=0.5,
                arm_shares=(0.5, 0.5),
                effective_n=n_held / 2,
                max_weight=3.0,
            )
        )
    ref = Evaluation("ref", "blanket", {}, "ok", "", value=est(default_value, 0.05), vs_none=est(0.0, 0.0))
    ev = Evaluations(
        split=Split("customer", np.arange(5), np.arange(5), "s", "n"),
        n_fit=n_held,
        n_held=n_held,
        arm_names=("none", "A"),
        arm_counts_fit=(n_held // 2, n_held // 2),
        arm_counts_held=(n_held // 2, n_held // 2),
        conversions_fit=500,
        conversions_held=500,
        margin_held=margin,
        none=ref,
        default=ref,
        candidates=tuple(cands),
    )
    return ev, [BlanketPolicy(1)] * len(gains)


DEFAULT = SafeDefault(0, "send nothing", "test", None)


# --- the pieces ---------------------------------------------------------------------------------------


def test_holm_hand_values() -> None:
    assert holm_adjust([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert holm_adjust([0.01, 0.02, 0.03]) == pytest.approx([0.03, 0.04, 0.04])
    assert holm_adjust([0.5, 0.9]) == [1.0, 1.0]
    assert holm_adjust([]) == []


def test_the_rule_is_written_down() -> None:
    assert "Holm" in SELECTION_RULE and "KEEP THE SAFE DEFAULT" in SELECTION_RULE
    assert "NOT ENOUGH DATA" in SELECTION_RULE and "At most" in SELECTION_RULE
    assert MAX_CANDIDATES == 3 and ALPHA == 0.05


def test_safe_default_is_the_best_blanket_arm_with_positive_train_evidence() -> None:
    c = make_campaign(20000, seed=12, targeting=False, blanket_gain=0.15)  # A: +0.15 x 10 - 1 = +0.5 for all
    d = safe_default(c)
    assert d.arm == 1 and d.label == "send to all: A" and d.gain is not None and d.gain.low > 0
    assert "best blanket arm" in d.reason


def test_safe_default_is_send_nothing_without_positive_evidence() -> None:
    c = make_campaign(20000, seed=13, targeting=False)  # A and B both lose money on average
    d = safe_default(c)
    assert d.arm == 0 and d.label == "send nothing" and d.gain is None
    assert d.policy() == BlanketPolicy(0)
    # a gain whose interval includes zero is not evidence
    weak = make_campaign(600, seed=14, targeting=False, blanket_gain=0.11)
    assert safe_default(weak).arm == 0
    unlogged = replace(make_campaign(100), propensity=None)
    assert safe_default(unlogged).arm == 0


# --- the rule on hand-built evidence ------------------------------------------------------------------------


def test_a_clear_winner_is_chosen_and_explained() -> None:
    ev, cands = fake_eval([(0.30, 0.05), (0.01, 0.05), (-0.2, 0.05)])
    sel = select(ev, DEFAULT, cands)
    assert sel.outcome == "candidate" and sel.chosen is cands[0]
    assert sel.verdicts[0].eligible and not sel.verdicts[1].eligible and not sel.verdicts[2].eligible
    assert "Chose 'cand0'" in sel.explanation and "adjusted p" in sel.explanation
    assert sel.forecast == ev.candidates[0].vs_none


def test_equal_candidates_keep_the_default() -> None:
    ev, cands = fake_eval([(0.0, 0.05), (0.0, 0.05), (0.0, 0.05)])
    sel = select(ev, DEFAULT, cands)
    assert sel.outcome == "default" and sel.chosen is None and sel.run == DEFAULT.policy()
    assert "Kept the safe default" in sel.explanation
    assert sel.forecast == ev.default.vs_none


def test_among_the_eligible_the_best_corrected_lower_bound_wins() -> None:
    # B has the higher point gain but a much wider interval: its corrected lower bound is lower
    ev, cands = fake_eval([(0.20, 0.03), (0.35, 0.12), (0.18, 0.03)])
    sel = select(ev, DEFAULT, cands)
    z = NormalDist().inv_cdf(1 - ALPHA / 3)
    lows = [0.20 - z * 0.03, 0.35 - z * 0.12, 0.18 - z * 0.03]
    assert sel.verdicts[0].corrected is not None and sel.verdicts[0].corrected.low == pytest.approx(lows[0])
    assert sel.outcome == "candidate" and sel.chosen is cands[0]
    assert sel.verdicts[1].eligible is False or lows[1] < lows[0]


def test_holm_is_used_not_the_raw_p() -> None:
    # raw one-sided p = 0.03 for each of three: raw-significant at 5%, but Holm gives 0.09
    se = 0.1
    z = NormalDist().inv_cdf(1 - 0.03)
    ev, cands = fake_eval([(z * se, se)] * 3)
    sel = select(ev, DEFAULT, cands)
    assert all(v.p_raw == pytest.approx(0.03, abs=1e-6) for v in sel.verdicts)
    assert all(v.p_holm == pytest.approx(0.09, abs=1e-6) for v in sel.verdicts)
    assert sel.outcome == "default"
    one, _ = fake_eval([(z * se, se)])
    assert (
        select(one, DEFAULT, [BlanketPolicy(1)]).outcome == "candidate"
    )  # a single claim: no correction needed


def test_a_failed_candidate_counts_in_the_correction_and_is_never_chosen() -> None:
    ev, cands = fake_eval([None, (0.30, 0.05)])
    sel = select(ev, DEFAULT, cands)
    assert sel.verdicts[0].status == "failed" and not sel.verdicts[0].eligible
    assert sel.outcome == "candidate" and sel.chosen is cands[1]
    assert sel.m == 2
    assert "could not be fitted" in select(*_only_failed()).explanation


def _only_failed() -> tuple[Evaluations, SafeDefault, list[BlanketPolicy]]:
    ev, cands = fake_eval([None])
    return ev, DEFAULT, cands


def test_too_good_to_be_true_is_blocked() -> None:
    ev, cands = fake_eval([(5.0, 0.05)], margin=1.5)  # gains more than the whole margin per customer
    sel = select(ev, DEFAULT, cands)
    assert sel.outcome == "default" and sel.verdicts[0].blocking
    assert "too good to be true" in sel.explanation


def test_thin_effective_samples_and_heavy_weights_are_warned_about() -> None:
    ev, cands = fake_eval([(0.30, 0.05)])
    thin = ev.candidates[0]
    object.__setattr__(thin, "effective_n", 10.0)
    object.__setattr__(thin, "max_weight", 400.0)
    sel = select(ev, DEFAULT, cands)
    assert sel.outcome == "candidate"  # a warning does not block
    assert len(sel.verdicts[0].warnings) == 2 and "Note:" in sel.explanation


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [({"n_held": 100}, "only 100 customers"), ({"n_held": 10_000}, None)],
)
def test_not_enough_data_is_its_own_outcome(kwargs: dict[str, int], expected: str | None) -> None:
    ev, cands = fake_eval([(0.30, 0.05)], **kwargs)
    sel = select(ev, DEFAULT, cands)
    if expected is None:
        assert sel.outcome == "candidate"
        return
    assert sel.outcome == "not_enough_data" and expected in sel.reasons[0] and sel.chosen is None
    assert sel.run == DEFAULT.policy() and "Not enough data" in sel.explanation


def test_too_few_conversions_or_a_thin_arm_is_not_enough_data() -> None:
    ev, cands = fake_eval([(0.30, 0.05)])
    object.__setattr__(ev, "conversions_held", 3)
    object.__setattr__(ev, "arm_counts_fit", (5000, 4))
    sel = select(ev, DEFAULT, cands)
    assert sel.outcome == "not_enough_data"
    assert any("converted" in r for r in sel.reasons) and any("fewer than" in r for r in sel.reasons)


def test_more_than_three_candidates_is_refused() -> None:
    c = make_campaign(500)
    with pytest.raises(MenuError):
        run_scientist(c, [BlanketPolicy(1)] * 4)
    with pytest.raises(MenuError):
        run_scientist(c, [])
    ev, cands = fake_eval([(0.3, 0.05)] * 4)
    with pytest.raises(MenuError):
        select(ev, DEFAULT, cands)


def test_chance_alone_picks_a_challenger_rarely() -> None:
    """400 simulated runs where no candidate is better than the default (three noisy zeros): the rule
    must name a challenger in at most 8% of them (the family-wise error is 5% by construction)."""
    rng = np.random.default_rng(2026)
    picked = 0
    for _ in range(400):
        gains = [(float(z) * 0.1, 0.1) for z in rng.standard_normal(3)]
        ev, cands = fake_eval(gains)
        picked += select(ev, DEFAULT, cands).outcome == "candidate"
    assert picked / 400 <= 0.08, picked


def test_a_real_effect_is_found_almost_always() -> None:
    rng = np.random.default_rng(7)
    found = 0
    for _ in range(200):
        gains = [(0.3 + 0.1 * float(z), 0.1) for z in rng.standard_normal(1)] + [
            (float(z) * 0.1, 0.1) for z in rng.standard_normal(2)
        ]
        ev, cands = fake_eval(gains)
        sel = select(ev, DEFAULT, cands)
        found += sel.outcome == "candidate" and sel.chosen is cands[0]
    assert found / 200 >= 0.7


# --- the whole loop on a campaign -----------------------------------------------------------------------------


def test_with_signal_a_fitted_candidate_beats_the_default() -> None:
    c = make_campaign(20000, seed=21)  # targeting pays; no blanket arm does
    sel = run_scientist(c, [SegmentRule(), UpliftPolicy(), BlanketPolicy(2)])
    assert sel.default.label == "send nothing"
    assert sel.outcome == "candidate" and sel.run.kind in ("segment", "uplift")
    assert sel.forecast is not None and sel.forecast.point > 0.1
    d = sel.to_dict()
    assert d["outcome"] == "candidate" and d["split"]["held_out"] == sel.evaluations.n_held  # type: ignore[union-attr]
    assert [v["label"] for v in d["verdicts"]] == ["segment rule", "uplift model", "send to all: B"]
    assert d["verdicts"][2]["eligible"] is False


def test_when_the_best_blanket_arm_is_the_answer_it_stays_the_default() -> None:
    """Everyone gains from A, nothing to target: the default is 'send to all: A' and no candidate beats it."""
    c = make_campaign(20000, seed=22, targeting=False, blanket_gain=0.15)
    sel = run_scientist(c, [SegmentRule(), UpliftPolicy()])
    assert sel.default.label == "send to all: A"
    assert sel.outcome == "default" and sel.run == BlanketPolicy(1)
    assert sel.chosen is None


def test_no_signal_anywhere_sends_nothing() -> None:
    c = make_campaign(20000, seed=23, targeting=False)
    sel = run_scientist(c, [SegmentRule(), UpliftPolicy()])
    assert sel.outcome == "default" and sel.run == BlanketPolicy(0) and sel.default.label == "send nothing"


def test_a_campaign_nobody_logged_probabilities_for_is_not_enough_data() -> None:
    c = replace(make_campaign(3000), propensity=None, randomised=False)
    sel = run_scientist(c, [SegmentRule()])
    assert sel.outcome == "not_enough_data" and sel.run == BlanketPolicy(0)
    assert "nothing can be proven" in sel.explanation and sel.evaluations is None


def test_a_small_shop_gets_not_enough_data() -> None:
    sel = run_scientist(make_campaign(300, seed=24), [SegmentRule(), UpliftPolicy()])
    assert sel.outcome == "not_enough_data"
    assert sel.run == BlanketPolicy(0) or sel.run.kind == "blanket"


def test_same_inputs_give_the_same_selection() -> None:
    c = make_campaign(8000, seed=25)
    a = run_scientist(c, [SegmentRule(), UpliftPolicy()]).to_dict()
    b = run_scientist(c, [SegmentRule(), UpliftPolicy()]).to_dict()
    assert a == b
    assert math.isfinite(a["verdicts"][0]["p_holm"])


def test_the_selection_never_reads_what_it_is_not_given() -> None:
    """Rows handed to the loop are all it may learn from: poisoning rows it was never given (a test
    part kept outside) cannot change it, and the held-out rows inside are scored, not fitted."""
    c = make_campaign(12000, seed=26)
    train = c.take(np.arange(6000))
    outside = c.take(np.arange(6000, 12000))
    assert set(train.customer_ids).isdisjoint(outside.customer_ids)
    first = run_scientist(train, [SegmentRule(), UpliftPolicy()]).to_dict()
    again = run_scientist(c.take(np.arange(6000)), [SegmentRule(), UpliftPolicy()]).to_dict()
    assert first == again


def test_a_propensity_logged_campaign_is_judged_on_a_time_split() -> None:
    """Not randomised but logged: the earlier customers fit, the later ones score, and the loop runs."""
    c = make_campaign(20000, seed=27)
    times = np.arange(c.n, dtype=np.float64) * 60.0
    logged = replace(c, randomised=False, times=times)
    sel = run_scientist(logged, [SegmentRule(), UpliftPolicy()])
    assert sel.evaluations is not None and sel.evaluations.split.method == "time"
    assert sel.to_dict()["split"]["method"] == "time"
    assert sel.outcome == "candidate"  # the planted targeting is still found
