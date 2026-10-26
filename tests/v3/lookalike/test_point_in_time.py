"""No outcome from after a decision reaches the features it was decided on, or a fit run before it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import numpy as np
import pytest

from autonomy.contracts import Action, Channel, Decision, DecisionKind, Event, EventType, Group
from autonomy.decide.lookalike import (
    LookalikeResponseModel,
    Outcome,
    OutcomeBook,
    Profile,
    WindowLookup,
    profile_of,
)
from autonomy.learning.observations import Timeline

from .synth import KEY, T0, WINDOW, make_state, outcomes


def _profile() -> Profile:
    return profile_of(make_state(0, np.random.default_rng(0)))


def test_a_profile_taken_after_its_window_opened_is_refused() -> None:
    model = LookalikeResponseModel()
    late = make_state(0, np.random.default_rng(0)).model_copy(update={"as_of": T0 + timedelta(days=2)})
    got = model.observe([Outcome("c0", profile_of(late), KEY, True, T0, T0 + WINDOW)])
    assert got == 0 and model.rows == 0 and model.refused == 1


def test_fit_ignores_windows_that_have_not_closed() -> None:
    states, rows = outcomes(2500, seed=6)
    cutoff = T0 + timedelta(days=8)
    future = cutoff + timedelta(days=30)
    flipped = [
        Outcome(r.customer_id, r.profile, r.action_key, not r.converted, r.window_start, future) for r in rows
    ]
    clean = LookalikeResponseModel()
    clean.observe(rows)
    dirty = LookalikeResponseModel()
    dirty.observe(rows + flipped)  # twice the rows, the extra ones with the answer flipped, but in the future
    assert clean.fit(cutoff) and dirty.fit(cutoff)
    for st in states[:20]:
        assert dirty.predict(st, KEY) == clean.predict(st, KEY)
    assert dirty.person(states[0].customer_id) == clean.person(states[0].customer_id)


# --- the book that turns decisions and events into outcomes ---------------------------------------


def _decision(did: str, cid: str, *, group: Group = Group.AGENT, act: bool = True) -> Decision:
    action = Action(goal_id="g", channel=Channel.EMAIL, send_at=T0, content_id="x") if act else None
    return Decision(
        decision_id=did,
        customer_id=cid,
        made_at=T0,
        kind=DecisionKind.ACT if act else DecisionKind.NOTHING,
        action=action,
        group=group,
        probability=0.5,
        reason="test",
        policy_version="t",
    )


def _events(cid: str, did: str | None, purchase_at: timedelta | None) -> list[Event]:
    out: list[Event] = []
    if did is not None:
        out.append(
            Event(
                event_id=f"s{cid}",
                customer_id=cid,
                type=EventType.SEND,
                ts=T0,
                source="t",
                channel=Channel.EMAIL,
                decision_id=did,
            )
        )
        out.append(
            Event(
                event_id=f"v{cid}",
                customer_id=cid,
                type=EventType.DELIVER,
                ts=T0,
                source="t",
                channel=Channel.EMAIL,
                decision_id=did,
            )
        )
    if purchase_at is not None:
        out.append(
            Event(
                event_id=f"p{cid}",
                customer_id=cid,
                type=EventType.PURCHASE,
                ts=T0 + purchase_at,
                source="t",
                value=10.0,
            )
        )
    return out


def _lookup(table: dict[str, list[Event]]) -> Callable[[str], WindowLookup | None]:
    return lambda cid: Timeline(table.get(cid, []))


def test_only_a_purchase_inside_the_window_counts() -> None:
    book = OutcomeBook(WINDOW)
    when = {"in": timedelta(days=3), "late": timedelta(days=9), "before": timedelta(days=-2)}
    for cid in when:
        assert book.remember(_decision(f"d_{cid}", cid), _profile(), KEY)
    table = {cid: _events(cid, f"d_{cid}", at) for cid, at in when.items()}
    # a week and a day later every window has closed; the purchase on day 9 is after its window
    got = {o.customer_id: o.converted for o in book.close(T0 + timedelta(days=8), _lookup(table))}
    assert got == {"in": True, "late": False, "before": False}


def test_a_window_still_open_is_left_alone_and_a_never_sent_one_is_dropped() -> None:
    book = OutcomeBook(WINDOW)
    book.remember(_decision("d_open", "open"), _profile(), KEY)
    book.remember(_decision("d_unsent", "unsent"), _profile(), KEY)
    table = {"open": _events("open", "d_open", None)}
    assert book.close(T0 + timedelta(days=3), _lookup(table)) == []  # nothing has closed yet
    assert len(book) == 2
    got = book.close(T0 + timedelta(days=9), _lookup(table))
    assert [o.customer_id for o in got] == ["open"]
    assert book.dropped == {"never_sent": 1}


def test_holdout_windows_with_a_send_are_dropped_and_consecutive_ones_share_weight() -> None:
    book = OutcomeBook(WINDOW)
    first = _decision("h1", "held", group=Group.HOLDOUT, act=False)
    second = first.model_copy(update={"decision_id": "h2", "made_at": T0 + timedelta(days=1)})
    sent_to = _decision("h3", "oops", group=Group.HOLDOUT, act=False)
    for d in (first, second, sent_to):
        assert book.remember(d, _profile(), None)
    table = {"oops": _events("oops", "d_x", None)}
    got = book.close(T0 + timedelta(days=9), _lookup(table))
    assert sorted(o.weight for o in got) == [
        pytest.approx(1 / 7),
        1.0,
    ]  # the second is mostly the first again
    assert book.dropped == {"contaminated": 1}


def test_decisions_that_teach_nothing_are_not_kept() -> None:
    book = OutcomeBook(WINDOW)
    assert not book.remember(
        _decision("d1", "c", act=False), _profile(), None
    )  # the agent's own "do nothing"
    assert not book.remember(_decision("d2", "c"), _profile(), None)  # a send with no action key
    assert len(book) == 0
