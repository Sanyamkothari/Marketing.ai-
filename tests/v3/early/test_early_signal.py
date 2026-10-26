"""Early-signal model: censoring, point-in-time features, the backtest gate and the early read."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from autonomy.contracts import Channel, Event, EventType, Group
from autonomy.learning.early_signal import (
    FEATURE_NAMES,
    EarlySignalConfig,
    build_examples,
    early_read,
    fit,
)

T0 = datetime(2025, 1, 1, tzinfo=UTC)
CFG = EarlySignalConfig(enabled=True, min_train=60, min_cohort_size=10, min_cohorts=3)


def _buy(cid: str, at: datetime, value: float = 100.0, margin: float | None = None, n: int = 0) -> Event:
    return Event(
        event_id=f"{cid}-p-{at.isoformat()}-{n}",
        customer_id=cid,
        type=EventType.PURCHASE,
        ts=at,
        source="test",
        value=value,
        margin=margin,
    )


def _other(cid: str, at: datetime, kind: EventType) -> Event:
    return Event(
        event_id=f"{cid}-{kind}-{at.isoformat()}",
        customer_id=cid,
        type=kind,
        ts=at,
        source="test",
        channel=Channel.EMAIL,
    )


def _row(ex, cid):  # type: ignore[no-untyped-def]
    return ex.customer_ids.index(cid)


# --- censoring --------------------------------------------------------------------------------------


def test_unfinished_window_gets_no_label() -> None:
    cfg = EarlySignalConfig(enabled=True)
    # "done" bought on day 0 and again on day 50; "open" bought on day 0 only, 60 days before the end.
    ev = [
        _buy("done", T0, margin=10.0),
        _buy("done", T0 + timedelta(days=50), margin=20.0),
        _buy("open", T0 + timedelta(days=60), margin=10.0),
        _buy("late", T0 + timedelta(days=100), margin=10.0),
    ]
    ex = build_examples(ev, cfg, as_of=T0 + timedelta(days=130))
    assert ex.labels[_row(ex, "done")] == pytest.approx(20.0)
    # cut-off day 67 + 90 days runs past day 130: no label, not a label of zero
    assert math.isnan(ex.labels[_row(ex, "open")])
    assert math.isnan(ex.labels[_row(ex, "late")])
    assert int(ex.labelled.sum()) == 1


def test_window_closing_exactly_at_the_end_is_labelled() -> None:
    cfg = EarlySignalConfig(enabled=True)
    end = T0 + timedelta(days=7 + 90)
    ex = build_examples([_buy("a", T0, margin=5.0), _buy("a", end, margin=7.0)], cfg)
    assert ex.data_end_ts == end.timestamp()
    assert ex.labels[0] == pytest.approx(7.0)
    short = build_examples(
        [_buy("a", T0, margin=5.0), _buy("a", end - timedelta(seconds=1), margin=7.0)], cfg
    )
    assert math.isnan(
        short.labels[0]
    )  # the end of the data is the last event, one second short of the window


def test_first_week_not_over_is_not_an_example() -> None:
    cfg = EarlySignalConfig(enabled=True)
    ev = [_buy("old", T0), _buy("new", T0 + timedelta(days=200)), _buy("new", T0 + timedelta(days=202))]
    ex = build_examples(ev, cfg)
    assert ex.customer_ids == ("old",)
    assert ex.open_week == 1


def test_end_of_data_is_the_whole_log_not_the_customer() -> None:
    """A customer who stopped buying early is a real zero when the log runs on, not a censored row."""
    cfg = EarlySignalConfig(enabled=True)
    ev = [_buy("gone", T0, margin=5.0), _buy("other", T0 + timedelta(days=300), margin=5.0)]
    ex = build_examples(ev, cfg)
    assert ex.labels[_row(ex, "gone")] == 0.0


# --- point in time ----------------------------------------------------------------------------------


def test_features_ignore_everything_after_the_cutoff() -> None:
    cfg = EarlySignalConfig(enabled=True)
    base = [_buy("a", T0, value=50.0), _other("a", T0 + timedelta(days=2), EventType.VISIT)]
    future = [
        _buy("a", T0 + timedelta(days=8), value=9999.0),
        _buy("a", T0 + timedelta(days=9), value=9999.0),
        _other("a", T0 + timedelta(days=10), EventType.REPLY),
        _buy("z", T0 + timedelta(days=400)),
    ]
    a = build_examples([*base, _buy("z", T0 + timedelta(days=400))], cfg)
    b = build_examples(base + future, cfg)
    np.testing.assert_array_equal(a.features[_row(a, "a")], b.features[_row(b, "a")])
    assert b.labels[_row(b, "a")] > a.labels[_row(a, "a")]  # the later purchases belong to the label


def test_event_at_the_cutoff_is_a_feature_and_just_after_is_the_label() -> None:
    cfg = EarlySignalConfig(enabled=True)
    cut = T0 + timedelta(days=7)
    ev = [
        _buy("a", T0, margin=1.0),
        _buy("a", cut, margin=2.0),
        _buy("a", cut + timedelta(seconds=1), margin=4.0),
        _buy("z", T0 + timedelta(days=300)),
    ]
    ex = build_examples(ev, cfg)
    row = ex.features[_row(ex, "a")]
    assert row[FEATURE_NAMES.index("second_order")] == 1.0  # the purchase at exactly T is in the week
    assert ex.labels[_row(ex, "a")] == pytest.approx(4.0)  # and only the one after it is in the label


def test_messages_we_sent_do_not_start_the_clock() -> None:
    cfg = EarlySignalConfig(enabled=True)
    ev = [_other("a", T0, EventType.SEND), _buy("a", T0 + timedelta(days=3))]
    ev += [_buy("z", T0 + timedelta(days=300))]
    ex = build_examples(ev, cfg)
    assert ex.anchor_ts[_row(ex, "a")] == (T0 + timedelta(days=3)).timestamp()


def test_features_read_the_four_signals() -> None:
    cfg = EarlySignalConfig(enabled=True)
    ev = [
        _buy("a", T0, value=40.0),
        _buy("a", T0 + timedelta(days=1), value=60.0),
        _other("a", T0 + timedelta(days=2), EventType.REPLY),
        _buy("b", T0, value=10.0),
        _buy("z", T0 + timedelta(days=300)),
    ]
    ex = build_examples(ev, cfg)
    f = dict(zip(FEATURE_NAMES, ex.features[_row(ex, "a")], strict=True))
    assert f["second_visit"] == 1.0 and f["second_order"] == 1.0 and f["replied"] == 1.0
    assert f["log_basket"] == pytest.approx(math.log1p(50.0))
    g = dict(zip(FEATURE_NAMES, ex.features[_row(ex, "b")], strict=True))
    assert g["second_visit"] == 0.0 and g["second_order"] == 0.0 and g["replied"] == 0.0


# --- the backtest gate ------------------------------------------------------------------------------


def _shop(
    months: int = 14, per_month: int = 40, seed: int = 3, shift_after: int | None = None
) -> list[Event]:
    """Customers arrive through `months`; those who come back in week one go on to spend more."""
    rng = np.random.default_rng(seed)
    out: list[Event] = []
    for m in range(months):
        for i in range(per_month):
            cid = f"c{m:02d}-{i:03d}"
            start = T0 + timedelta(days=30 * m + int(rng.integers(0, 28)))
            keen = rng.random() < 0.4
            boost = 3.0 if shift_after is not None and m >= shift_after else 1.0  # the shop's level changes
            out.append(_buy(cid, start, value=float(rng.uniform(30, 60)), margin=15.0, n=0))
            if keen:
                out.append(_buy(cid, start + timedelta(days=3), value=45.0, margin=13.0, n=1))
            later = rng.poisson(boost * (4.0 if keen else 0.8))
            for k in range(later):
                at = start + timedelta(days=8 + float(rng.uniform(0, 80)))
                out.append(_buy(cid, at, value=45.0, margin=float(rng.uniform(10, 16)), n=10 + k))
    return out


def test_backtest_passes_when_the_first_week_carries_signal() -> None:
    ev = _shop()
    sig = fit(ev, CFG)
    assert sig.backtest is not None
    assert len(sig.backtest.cohorts) >= 3
    assert sig.backtest.passed, sig.backtest
    assert sig.usable
    assert sig.backtest.correlation is not None and sig.backtest.correlation > 0.5
    assert {r.cohort for r in sig.backtest.cohorts}.isdisjoint({c for c, _ in sig.backtest.skipped})
    got = sig.forecasts(ev)
    assert got and all(math.isfinite(v) for v in got.values())


def test_backtest_skips_months_it_cannot_score_and_says_why() -> None:
    sig = fit(_shop(), CFG)
    assert sig.backtest is not None
    reasons = dict(sig.backtest.skipped)
    first, last = "2025-01", "2026-02"
    assert "earlier customers" in reasons[first]  # nothing finished before the first month
    assert reasons[last] == "windows not over yet"  # the newest month's labels are still censored


def test_backtest_fails_when_the_shop_changes() -> None:
    ev = _shop(months=14, shift_after=7)
    sig = fit(ev, EarlySignalConfig(enabled=True, min_train=60, min_cohort_size=10, band=0.1))
    assert sig.backtest is not None
    assert not sig.backtest.passed
    assert not sig.usable
    assert sig.forecasts(ev) == {}
    assert "months were within" in sig.reason


def test_backtest_needs_enough_months() -> None:
    ev = _shop(months=6)
    sig = fit(ev, CFG)
    assert sig.backtest is not None and not sig.backtest.passed
    assert not sig.usable and "months could be checked" in sig.reason


def test_a_month_is_forecast_with_nothing_from_after_it() -> None:
    """Changing what later customers did moves their own labels, not an earlier month's forecast."""
    ev = _shop()
    base = fit(ev, CFG).backtest
    assert base is not None
    month = base.cohorts[1].cohort  # e.g. "2025-06"
    firsts: dict[str, datetime] = {}
    for e in ev:
        firsts[e.customer_id] = min(e.ts, firsts.get(e.customer_id, e.ts))
    later = [c for c, t in firsts.items() if t.strftime("%Y-%m") > month]
    assert later
    pert = ev + [_buy(c, firsts[c] + timedelta(days=20), value=1e5, margin=5e4, n=99) for c in later]
    again = fit(pert, CFG).backtest
    assert again is not None
    old = {r.cohort: r for r in base.cohorts}[month]
    new = {r.cohort: r for r in again.cohorts}[month]
    assert new.forecast == pytest.approx(old.forecast)
    assert new.n_train == old.n_train
    assert new.actual == pytest.approx(old.actual)


def test_off_by_default_changes_nothing() -> None:
    ev = _shop(months=12)
    sig = fit(ev, EarlySignalConfig())
    assert not sig.usable and sig.model is None and sig.backtest is None
    assert sig.forecasts(ev) == {}
    assert early_read(sig, {"a": 1.0}, {"a": Group.AGENT}) is None


# --- the early read ---------------------------------------------------------------------------------


def test_early_read_compares_agent_and_holdout_forecasts() -> None:
    ev = _shop()
    sig = fit(ev, CFG)
    assert sig.usable
    fc = sig.forecasts(ev)
    group = {c: (Group.AGENT if i % 2 else Group.HOLDOUT) for i, c in enumerate(sorted(fc))}
    read = early_read(sig, fc, group)
    assert read is not None
    assert read.n_agent + read.n_holdout == len(fc)
    assert read.difference.low <= read.difference.point <= read.difference.high
    assert read.horizon_days == 90
    small = dict(list(group.items())[:10])
    assert early_read(sig, fc, small) is None  # too few customers per group


def test_the_analyst_briefing_carries_the_early_read_with_backed_numbers() -> None:
    from autonomy.agents.analyst import EARLY_TITLE, template_briefing
    from autonomy.agents.claims import check_numbers
    from autonomy.contracts import Estimate, Goal, GoalKind, HealthStatus, ProofSummary
    from autonomy.learning.early_signal import EarlyRead

    now = T0 + timedelta(days=500)
    goal = Goal(
        goal_id="g", business_id="b", text="win back", kind=GoalKind.WIN_BACK, budget=100.0,
        starts_at=now, created_at=now,
    )  # fmt: skip
    proof = ProofSummary(
        goal_id="g", as_of=now, agent_n=0, baseline_n=0, holdout_n=0,
        incremental_revenue=None, incremental_profit=None, lift_vs_baseline=None,
    )  # fmt: skip
    read = EarlyRead(
        n_agent=900,
        n_holdout=100,
        agent_mean=31.0,
        holdout_mean=25.5,
        difference=Estimate(point=5.5, low=2.25, high=8.75),
        band=0.25,
        share_within=0.9,
        cohorts_checked=9,
    )
    plain = template_briefing(goal, proof, HealthStatus(as_of=now), {}, "weekly", now)
    withit = template_briefing(goal, proof, HealthStatus(as_of=now), {}, "weekly", now, early=read)
    assert EARLY_TITLE not in [s.title for s in plain.sections]
    assert [s.title for s in withit.sections[:-1]] == [s.title for s in plain.sections]
    section = withit.sections[-1]
    assert section.title == EARLY_TITLE and "forecast" in section.body
    assert check_numbers(section.body, section.numbers) == []
