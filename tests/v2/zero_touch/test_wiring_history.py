"""Connector history rebuilds customer states on its own: the engine's `history_hook` and the identity
linker's `on_change` are the runtime's `request_rebuild`."""

from __future__ import annotations

from datetime import timedelta

from autonomy.connectors import Lane, Stream
from autonomy.contracts import EventType
from tests.v2.zero_touch.wiring_support import SWEEP, T0, make_shop


def test_history_from_a_backfill_is_folded_by_a_rebuild_the_next_tick() -> None:
    shop = make_shop(linking=True, hold=False)
    assert shop.engine.history_hook == shop.runtime.request_rebuild
    assert shop.wiring.linker is not None and shop.wiring.linker.on_change == shop.runtime.request_rebuild
    connection = shop.connect_klaviyo()
    shop.runtime.tick(shop.at(T0))  # warm: from here on the runtime folds new events incrementally
    # A profile last changed 40 days ago arrives with the history lane, long after the late window.
    shop.klaviyo.add_profile("P9", email="old@example.com", updated=T0 - timedelta(days=40))
    report = shop.engine.sync(
        connection.connection_id, Stream.CONSENT, Lane.HISTORY, shop.at(T0 + timedelta(minutes=1))
    )
    assert report.status in ("ok", "done") and report.events > 0
    stored = [
        e for e in shop.runtime.events.for_customer("old@example.com") if e.type is EventType.CONSENT_GRANTED
    ]
    assert stored and stored[0].ts < T0 - timedelta(days=2)
    # The incremental fold would never see it (older than the late window); the requested rebuild does.
    early = shop.runtime.tick(shop.at(T0 + timedelta(minutes=5)))
    assert not early.rebuilt  # at most once per rebuild_every (15 minutes) after the last one
    assert shop.runtime.states.get("old@example.com") is None
    report = shop.runtime.tick(shop.at(T0 + timedelta(minutes=20)))
    assert report.rebuilt
    state = shop.runtime.states.get("old@example.com")
    assert state is not None and state.channels[next(iter(state.channels))].consent
    later = shop.runtime.tick(shop.at(SWEEP))
    assert not later.rebuilt  # nothing new was asked for
