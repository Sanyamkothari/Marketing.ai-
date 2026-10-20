"""Sleeping dogs, per person: the contact-harm posterior of each customer, shrunk to their band and
segment, read against the holdout since the agent started, and the rule that leaves someone alone.

Pure maths on synthetic records; no Gym. Each test runs in well under a second.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from autonomy.contracts import (
    Action,
    Channel,
    ChannelStats,
    CustomerState,
    Decision,
    DecisionContext,
    DecisionKind,
    Event,
    EventType,
    Group,
)
from autonomy.contracts.events import CHANNEL_REQUIRED
from autonomy.decide.bidders import (
    LEAVE_ALONE_CREDIBLE,
    LEAVE_ALONE_RETRY,
    ContactHarm,
    GoalBidder,
    contact_harm_of,
    leave_alone,
    leave_alone_retry,
)
from autonomy.decide.models import BetaResponseModel, ValueModel
from autonomy.fakes.samples import sample_brief, sample_content
from autonomy.learning import build_bundle, update_models
from autonomy.learning.observations import ContactEvidence, contact_evidence

T0 = datetime(2026, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)
REACHABLE = ChannelStats(address_known=True, consent=True)


# --- the evidence: history and the time since the start ------------------------------------------


def ev(kind: EventType, days: float, *, decision: str | None = None, customer: str = "c1") -> Event:
    return Event.model_validate(
        {
            "event_id": f"e_{customer}_{kind.value}_{days}",
            "customer_id": customer,
            "type": kind,
            "ts": T0 + timedelta(days=days),
            "source": "test",
            "decision_id": decision,
            "channel": Channel.EMAIL if kind in CHANNEL_REQUIRED else None,
            "value": 50.0 if kind is EventType.PURCHASE else None,
        }
    )


EVENTS = [
    ev(EventType.SIGNUP, 0),
    ev(EventType.SEND, 5, decision="h1"),
    ev(EventType.CLICK, 5.1, decision="h1"),
    ev(EventType.SEND, 12, decision="h2"),
    ev(EventType.SEND, 20, decision="h3"),
    ev(EventType.PURCHASE, 22),  # history, inside h3's week
    ev(EventType.PURCHASE, 35),  # history, open day
    ev(EventType.SEND, 41, decision="d1"),  # the agent started on day 40
    ev(EventType.PURCHASE, 43),  # since the start, exposed
    ev(EventType.PURCHASE, 55),  # since the start, open
]
START = T0 + timedelta(days=40)


def test_evidence_splits_the_history_from_the_time_since_the_start():
    now = T0 + timedelta(days=60)
    whole = contact_evidence(EVENTS, now, 7, split_at=START)
    assert whole.split_at == START
    assert whole.post_days == pytest.approx(20.0)
    assert whole.post_exposed_days == pytest.approx(7.0)
    assert (whole.post_purchases, whole.post_exposed_purchases) == (2.0, 1.0)
    # the whole-record counts are unchanged by the split
    plain = contact_evidence(EVENTS, now, 7)
    assert (whole.exposed_days, whole.open_days) == pytest.approx((plain.exposed_days, plain.open_days))
    assert whole.purchases == plain.purchases == 4.0
    assert plain.post_days == 0.0 and plain.split_at is None
    # counted over two nights, the split is the same; one counted before the start stays history
    night1 = contact_evidence(EVENTS, T0 + timedelta(days=30), 7)
    night2 = contact_evidence(EVENTS, now, 7, night1, split_at=START)
    assert night2.post_days == pytest.approx(whole.post_days)
    assert night2.post_purchases == whole.post_purchases


def test_the_quiet_streak_counts_messages_since_the_last_click():
    got = contact_evidence(EVENTS, T0 + timedelta(days=60), 7)
    assert got.sends_since_click == 3  # h2, h3 and d1 after the click on h1
    clicked = contact_evidence(
        [*EVENTS, ev(EventType.CLICK, 41.2, decision="d1")], T0 + timedelta(days=60), 7
    )
    assert clicked.sends_since_click == 0
    # not weighted by recency: a count of unanswered messages, however old
    weighted = contact_evidence(EVENTS, T0 + timedelta(days=60), 7, half_life_days=10.0)
    assert weighted.sends_since_click == 3


# --- synthetic shops ----------------------------------------------------------------------------------


def shop(
    n: int = 1200,
    *,
    harmed_share: float = 0.0,
    harmed_ratio: float = 0.5,
    ordinary_ratio: float = 1.3,
    trend: float = 1.0,
    holdout_share: float = 0.15,
    seed: int = 7,
) -> tuple[dict[str, ContactEvidence], set[str], set[str]]:
    """Customers with a history (randomised past campaigns) and 60 days since the agent started.

    Agent-group customers are messaged nearly all the time since the start; holdout customers never.
    Everyone's buying since the start moves by `trend` (a season), which only the holdout shows
    apart from what messages do. Harmed customers buy `harmed_ratio` as much while a message works
    on them, almost never click, and visit directly more. Returns (records, harmed, holdout)."""
    rng = np.random.default_rng(seed)
    out: dict[str, ContactEvidence] = {}
    harmed: set[str] = set()
    holdout: set[str] = set()
    for i in range(n):
        cid = f"p{i:04d}"
        bad = bool(rng.random() < harmed_share)
        held = bool(rng.random() < holdout_share)
        rate = float(rng.gamma(2.0, 0.01))
        ratio = harmed_ratio if bad else ordinary_ratio
        sends = int(rng.integers(8, 20))
        h_exposed = sends * 7.0 * 0.6
        h_open = 300.0 - h_exposed
        h_xp = float(rng.poisson(rate * ratio * h_exposed))
        h_op = float(rng.poisson(rate * h_open))
        post_days, post_exposed = 60.0, (0.0 if held else 54.0)
        post_rate = rate * trend
        p_xp = float(rng.poisson(post_rate * ratio * post_exposed))
        p_op = float(rng.poisson(post_rate * (post_days - post_exposed)))
        agent_sends = 0 if held else 9
        click = 0.005 if bad else float(rng.beta(4.0, 16.0))
        out[cid] = ContactEvidence(
            exposed_days=h_exposed + post_exposed,
            open_days=h_open + post_days - post_exposed,
            exposed_purchases=h_xp + p_xp,
            open_purchases=h_op + p_op,
            sends=float(sends + agent_sends),
            clicks=float(rng.binomial(sends + agent_sends, click)),
            direct_visits=float(rng.poisson(360 * (0.04 if bad else 0.02))),
            split_at=START,
            post_days=post_days,
            post_exposed_days=post_exposed,
            post_purchases=p_xp + p_op,
            post_exposed_purchases=p_xp,
        )
        if bad:
            harmed.add(cid)
        if held:
            holdout.add(cid)
    return out, harmed, holdout


def fitted(
    evidence: dict[str, ContactEvidence], holdout: set[str] = frozenset(), **kw: object  # type: ignore[assignment]
) -> ContactHarm:
    harm = ContactHarm(**kw)  # type: ignore[arg-type]
    harm.set_groups(holdout=holdout)
    harm.observe(evidence)
    harm.refit()
    return harm


# --- the holdout is the organic baseline ----------------------------------------------------------


def test_a_season_the_holdout_shares_is_not_blamed_on_messages():
    # Since the start everyone buys half as much (a slow season). Messages harm no one.
    evidence, _, holdout = shop(trend=0.5)
    harm = fitted(evidence, holdout)
    assert harm.fit is not None and harm.fit.holdout == len(holdout)
    assert harm.fit.trend == pytest.approx(0.5, abs=0.12)
    assert harm.fit.post_rows > 0.7 * len(evidence)
    assert harm.fit.ratio > 1.0  # messages still read as helping, as they do
    assert harm.share == 0.0 and not harm.likely(LEAVE_ALONE_CREDIBLE)


def test_without_a_holdout_a_season_would_read_as_harm():
    # The same slow season, but nobody is known to be held out: the agent's customers, messaged
    # nearly every day since the start, buy less then than on their (earlier) open days, and that
    # reads as messages costing them buying. The holdout is what tells the season from the harm.
    evidence, _, holdout = shop(trend=0.5)
    blind = fitted(evidence)
    seen = fitted(evidence, holdout)
    assert blind.fit is not None and seen.fit is not None
    assert blind.fit.holdout == 0 and blind.fit.trend == 1.0
    assert blind.fit.ratio < 0.8 * seen.fit.ratio
    assert seen.fit.ratio == pytest.approx(1.3, abs=0.15)  # the planted ratio
    # a holdout too small to be an honest baseline is not read
    few = fitted(evidence, set(sorted(holdout)[:10]))
    assert few.fit is not None and few.fit.holdout == 0 and few.fit.trend == 1.0


def test_harm_that_began_at_the_start_is_found_from_the_time_since():
    # In the history the harmed buy like everyone else (their annoyance began at the start); since
    # the start they buy half as much while messaged as on their own.
    evidence, harmed, holdout = shop(harmed_share=0.2, harmed_ratio=1.3)
    for cid in harmed - holdout:
        e = evidence[cid]
        cut = float(
            np.random.default_rng(len(cid) + int(cid[1:])).binomial(int(e.post_exposed_purchases), 0.5)
        )
        e.exposed_purchases -= cut
        e.post_purchases -= cut
        e.post_exposed_purchases -= cut
        e.clicks = 0.0  # and they stopped answering
    harm = fitted(evidence, holdout)
    contacted = [c for c in evidence if c not in holdout]
    bad = [c for c in contacted if c in harmed]
    ok = [c for c in contacted if c not in harmed]
    seen_bad, seen_ok = np.mean([harm.p_harm(c) for c in bad]), np.mean([harm.p_harm(c) for c in ok])
    assert seen_bad > 2.0 * seen_ok and seen_bad > seen_ok + 0.1


def test_each_segment_is_read_against_its_own_holdout():
    # A slow season in one segment only (buying since the start falls to 40% there), none in the
    # other; messages harm no one. Read per segment, each segment's holdout sets its own level, so
    # both read the planted ratio. Pooled, segment "a" is held to the milder average season and its
    # contacted customers look more harmed.
    a, _, held_a = shop(n=800, trend=0.4, seed=11)
    b, _, held_b = shop(n=800, trend=1.0, seed=12)
    evidence = {**{f"a{c}": e for c, e in a.items()}, **{f"b{c}": e for c, e in b.items()}}
    holdout = {f"a{c}" for c in held_a} | {f"b{c}" for c in held_b}

    def read(segments: dict[str, str]) -> ContactHarm:
        harm = ContactHarm()
        harm.set_groups(holdout=holdout, segments=segments)
        harm.observe(evidence)
        harm.refit()
        return harm

    by_segment = read({c: c[0] for c in evidence})
    pooled = read(dict.fromkeys(evidence, "all"))
    assert by_segment.fit is not None and set(by_segment.fit.segment_ratios) == {"a", "b"}
    for ratio in by_segment.fit.segment_ratios.values():
        assert ratio == pytest.approx(1.3, abs=0.15)
    in_a = [c for c in evidence if c.startswith("a") and c not in holdout]
    assert np.mean([by_segment.p_harm(c) for c in in_a]) < np.mean([pooled.p_harm(c) for c in in_a])
    assert by_segment.likely(LEAVE_ALONE_CREDIBLE) == 0


def test_the_baseline_uses_the_segment_each_customer_started_in():
    harm = ContactHarm()
    harm.set_groups(segments={"a": "lapsed"})
    harm.set_groups(segments={"a": "light", "b": None})  # "a" bought since: still read as lapsed
    assert harm.segments == {"a": "lapsed"}


# --- per-person shrinkage -------------------------------------------------------------------------------


def test_a_thin_record_stays_near_its_band_and_a_long_one_is_mostly_the_person():
    evidence, harmed, holdout = shop(harmed_share=0.15)
    one = next(c for c in sorted(harmed - holdout))
    twin = ContactEvidence(**{**vars(evidence[one])})
    # a look-alike with the same clues but a tenth of the record
    for name in ("exposed_days", "open_days", "exposed_purchases", "open_purchases", "post_days"):
        setattr(twin, name, getattr(twin, name) / 10.0)
    twin.post_exposed_days /= 10.0
    twin.post_purchases = twin.post_exposed_purchases = 0.0
    evidence["twin"] = twin
    harm = fitted(evidence, holdout, taus=(0.5,))
    _, long_sd = harm.posterior(one) or (0.0, 0.0)
    _, thin_sd = harm.posterior("twin") or (0.0, 0.0)
    assert thin_sd > long_sd  # less record, more of the band's doubt
    assert harm.posterior("nobody") is None and harm.p_below("nobody", 2.0) == 0.0


def test_the_person_to_person_spread_is_read_from_the_records():
    # Everyone alike: a narrow spread. Two kinds of people the clues do not tell apart: a wider one.
    alike, _, holdout = shop(harmed_share=0.0, n=800)
    narrow = fitted(alike, holdout).fit
    mixed, _, holdout = shop(harmed_share=0.3, harmed_ratio=0.2, n=800)
    for e in mixed.values():
        e.clicks, e.direct_visits = 2.0, 7.0  # no clue to sort them by
    wide = fitted(mixed, holdout).fit
    assert narrow is not None and wide is not None
    assert narrow.tau <= 0.2 < wide.tau


def test_a_shop_where_messages_harm_no_one_has_no_one_left_alone():
    for seed in (1, 2, 3):
        evidence, _, holdout = shop(seed=seed)
        harm = fitted(evidence, holdout)
        assert harm.share == 0.0
        assert harm.likely(LEAVE_ALONE_CREDIBLE) < 0.01 * len(evidence)


# --- leaving someone alone ---------------------------------------------------------------------------


class FixedHarm(ContactHarm):
    """A harm model with one known posterior: log ratio ~ Normal(mean, sd) for every customer."""

    def __init__(self, ratio: float, sd: float) -> None:
        super().__init__()
        self._post = {"c1": (float(np.log(ratio)), sd)}

    def p_below(self, customer_id: str, ratio: float) -> float:
        return super().p_below(customer_id, ratio)


def test_leave_alone_when_messages_credibly_lower_their_buying():
    clear = FixedHarm(ratio=0.5, sd=0.2)
    # buying worth $10 is at stake in the window
    assert clear.p_below("c1", 1.0) >= LEAVE_ALONE_CREDIBLE
    assert leave_alone(clear, "c1", at_stake=10.0)
    # doubtful harm is only charged, not acted on; a helped customer never is
    doubtful = FixedHarm(ratio=0.85, sd=0.3)
    assert doubtful.p_below("c1", 1.0) < LEAVE_ALONE_CREDIBLE
    assert not leave_alone(doubtful, "c1", at_stake=10.0)
    assert not leave_alone(FixedHarm(ratio=1.4, sd=0.2), "c1", at_stake=10.0)
    # a stranger is never left alone, nor someone with nothing at stake
    assert not leave_alone(clear, "stranger", at_stake=10.0)
    assert not leave_alone(clear, "c1", at_stake=0.0)


def test_the_exploration_floor_lets_a_left_alone_customer_back_now_and_then():
    weeks = [NOW + timedelta(days=7 * w) for w in range(52)]
    people = [f"c{i}" for i in range(400)]
    share = np.mean([leave_alone_retry(c, t) for c in people for t in weeks])
    assert share == pytest.approx(LEAVE_ALONE_RETRY, abs=0.006)
    # fixed per customer and calendar week: the same answer all week, and on every call
    assert leave_alone_retry("c7", NOW) == leave_alone_retry("c7", NOW)
    assert any(leave_alone_retry(c, NOW) for c in people) and not all(
        leave_alone_retry(c, NOW) for c in people
    )
    assert leave_alone_retry("c7", NOW, rate=0.0) is False and leave_alone_retry("c7", NOW, rate=1.0) is True


def state(customer_id: str = "c1") -> CustomerState:
    return CustomerState.model_validate(
        {
            "customer_id": customer_id,
            "as_of": NOW,
            "purchases": 2,
            "revenue": 200.0,
            "margin": 60.0,
            "last_purchase_at": NOW - timedelta(days=90),
            "channels": {Channel.EMAIL: REACHABLE},
        }
    )


def test_the_bidder_leaves_a_clearly_harmed_customer_alone_except_in_a_retry_week():
    m = BetaResponseModel(
        BetaResponseModel.priors_from_rates({"treated/*/email|*|*": 0.22, "control/*": 0.20}, strength=1e6)
    )
    harm = FixedHarm(ratio=0.4, sd=0.15)
    bidder = GoalBidder(
        sample_brief(NOW), sample_content(NOW), m, ValueModel(), {Channel.EMAIL: 0.0}, harm=harm, prune_z=2.0
    )
    days = [NOW + timedelta(days=7 * w) for w in range(150)]
    got = {t: bidder.bids(state(), DecisionContext(now=t, briefs=(sample_brief(t),))) for t in days}
    retry = [t for t in days if leave_alone_retry("c1", t)]
    assert retry and len(retry) < 0.1 * len(days)
    assert all(got[t] for t in retry)  # let back in: bid for as usual
    assert all(not got[t] for t in days if t not in retry)  # otherwise left alone
    # someone else, with no record, is bid for
    assert bidder.bids(state("c2"), DecisionContext(now=NOW, briefs=(sample_brief(NOW),)))
    # without pruning (explaining a decision) nothing is left out
    assert bidder.quote(state(), DecisionContext(now=NOW, briefs=(sample_brief(NOW),)), prune=False)


# --- the learner gets the holdout -------------------------------------------------------------------


def test_the_learner_hands_the_holdout_and_the_start_to_the_harm_model():
    bundle = build_bundle()
    held = Decision(
        decision_id="dh",
        customer_id="h1",
        made_at=START,
        kind=DecisionKind.NOTHING,
        group=Group.HOLDOUT,
        probability=1.0,
        reason="Holdout",
        policy_version="test",
    )
    sent = Decision(
        decision_id="d1",
        customer_id="c1",
        made_at=START,
        kind=DecisionKind.ACT,
        group=Group.AGENT,
        action=Action(goal_id="g", channel=Channel.EMAIL, content_id="x", send_at=START),
        probability=1.0,
        reason="Sent",
        policy_version="test",
    )
    log = {
        "c1": EVENTS,
        "h1": [ev(EventType.SIGNUP, 0, customer="h1"), ev(EventType.PURCHASE, 50, customer="h1")],
    }
    update_models(bundle, [], log, {}, START, 7)  # the history, before any decision
    harm = contact_harm_of(bundle.response)
    assert harm is not None and harm.split_at is None
    update_models(bundle, [held, sent], log, {}, T0 + timedelta(days=60), 7, harm_holdout=["h2"])
    assert harm.split_at == START  # the first decision
    assert harm.holdout == {"h1", "h2"}
    got = harm.evidence["c1"]
    assert isinstance(got, ContactEvidence) and got.split_at == START and got.post_purchases > 0
