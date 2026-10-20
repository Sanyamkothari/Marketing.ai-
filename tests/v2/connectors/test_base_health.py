"""Health: freshness against cadence, history readiness, row drops, one alert per problem, the act gate."""

from __future__ import annotations

from datetime import datetime, timedelta

from autonomy.connectors import (
    ConnectionStatus,
    Freshness,
    Lane,
    NeedsReconnect,
    Stream,
    SyncPosition,
    SyncRun,
)
from autonomy.contracts import Action, Channel, Decision, DecisionKind, stable_id

from .base_support import T0, Rig, connect, make_rig


def ready(rig: Rig, cid: str, *, synced_at: datetime, reached_days: int = 28) -> None:
    """Positions as if the live lanes last finished at `synced_at` and history reached `reached_days` back."""
    for stream in (Stream.ORDERS, Stream.CUSTOMERS):
        rig.store.put_position(
            SyncPosition(
                connection_id=cid,
                stream=stream,
                lane=Lane.LIVE,
                high_watermark=synced_at,
                last_success_at=synced_at,
            ),
            synced_at,
        )
    rig.store.put_position(
        SyncPosition(
            connection_id=cid,
            stream=Stream.ORDERS,
            lane=Lane.HISTORY,
            reached=T0 - timedelta(days=reached_days),
            done=reached_days >= 28,
        ),
        synced_at,
    )


def run(cid: str, at: datetime, rows: int, *, ok: bool = True) -> SyncRun:
    return SyncRun(
        run_id=stable_id("run", cid, at.isoformat()),
        connection_id=cid,
        stream=Stream.ORDERS,
        lane=Lane.LIVE,
        started_at=at,
        finished_at=at,
        ok=ok,
        complete=ok,
        rows=rows,
        new_records=rows,
    )


def test_freshness_against_the_streams_cadence() -> None:
    rig = make_rig()
    connection = connect(rig, seed=False)
    ready(rig, connection.connection_id, synced_at=T0)
    monitor = rig.engine.monitor
    spec = rig.engine.connector(connection).capabilities.stream(Stream.ORDERS)
    expect = {
        timedelta(minutes=4): Freshness.FRESH,
        timedelta(minutes=100): Freshness.FRESH,  # 1.5 h + 15 min grace
        timedelta(minutes=110): Freshness.LATE,
        timedelta(hours=3, minutes=16): Freshness.STALE,
    }
    for lag, freshness in expect.items():
        assert monitor.stream_health(connection, spec, T0 + lag).freshness is freshness, lag
    health = monitor.connection_health(connection, T0 + timedelta(minutes=4))
    assert health.state == "healthy" and health.can_act
    assert health.summary == "Demo Shop: healthy, 4 min behind"


def test_never_synced_and_short_history_hold_decisions() -> None:
    rig = make_rig()
    connection = connect(rig, seed=False)
    verdict = rig.engine.monitor.ready_to_act("biz_1", T0)
    assert not verdict.can_act and "out of date" in verdict.reasons[0]
    ready(rig, connection.connection_id, synced_at=T0, reached_days=10)
    health = rig.engine.monitor.connection_health(connection, T0)
    assert health.state == "catching_up" and not health.can_act
    assert health.summary == "Demo Shop: loading history (36%)"
    ready(rig, connection.connection_id, synced_at=T0, reached_days=14)  # min_history_days reached
    assert rig.engine.monitor.ready_to_act("biz_1", T0).can_act


def test_a_stale_critical_stream_holds_the_business_with_one_alert() -> None:
    rig = make_rig()
    connection = connect(rig, seed=False)
    ready(rig, connection.connection_id, synced_at=T0)
    later = T0 + timedelta(hours=5)
    for minutes in (0, 10, 60):
        rig.engine.monitor.evaluate(connection, later + timedelta(minutes=minutes))
    stale = [a for a in rig.alerts.list_alerts() if a.code == "connector.stale"]
    assert len(stale) == 2  # orders (critical) and customers (not critical): one each
    critical = [a for a in stale if a.severity == "critical"]
    assert len(critical) == 1 and critical[0].auto_paused and "on hold" in critical[0].message
    verdict = rig.engine.monitor.ready_to_act("biz_1", later)
    assert not verdict.can_act and verdict.reasons == ("Demo Shop orders is out of date",)
    # it catches up: incidents close, and a later outage alerts again
    ready(rig, connection.connection_id, synced_at=later)
    assert rig.engine.monitor.evaluate(connection, later) == []
    assert rig.store.open_incidents(connection.connection_id) == []
    rig.engine.monitor.evaluate(connection, later + timedelta(hours=5))
    assert len([a for a in rig.alerts.list_alerts() if a.code == "connector.stale"]) == 4


def test_needs_reconnect_hides_every_symptom() -> None:
    rig = make_rig()
    connection = connect(rig, seed=False)
    ready(rig, connection.connection_id, synced_at=T0)
    rig.engine._on_revoked(connection, NeedsReconnect("the refresh token was revoked"), T0)
    for hours in (5, 10, 24):
        rig.engine.daily(connection.connection_id, T0 + timedelta(hours=hours), rig.queue)
        refreshed = rig.store.get_connection(connection.connection_id)
        assert refreshed is not None and refreshed.status is ConnectionStatus.NEEDS_RECONNECT
        rig.engine.monitor.evaluate(refreshed, T0 + timedelta(hours=hours))
    assert [a.code for a in rig.alerts.list_alerts()] == ["connector.needs_reconnect"]


def test_row_count_drop_against_the_previous_week() -> None:
    rig = make_rig()
    connection = connect(rig, seed=False)
    cid = connection.connection_id
    now = T0 + timedelta(days=8)
    ready(rig, cid, synced_at=now)
    for day in range(1, 9):
        for hour in range(0, 24, 6):
            rig.store.record_run(run(cid, now - timedelta(days=day, hours=hour), rows=40))
    rig.store.record_run(run(cid, now - timedelta(hours=2), rows=3))
    health = rig.engine.monitor.stream_health(
        connection, rig.engine.connector(connection).capabilities.stream(Stream.ORDERS), now
    )
    assert health.rows_last_day == 3 and health.rows_median_day == 160
    alerts = rig.engine.monitor.evaluate(connection, now)
    assert [a.code for a in alerts] == ["connector.row_drop"]
    assert rig.engine.monitor.connection_health(connection, now).state == "degraded"
    assert rig.engine.monitor.ready_to_act("biz_1", now).can_act  # a warning, not a hold
    assert rig.engine.monitor.evaluate(connection, now + timedelta(minutes=30)) == []


def test_error_streaks_alert_once_and_hide_behind_staleness() -> None:
    rig = make_rig()
    connection = connect(rig, seed=False)
    cid = connection.connection_id
    ready(rig, cid, synced_at=T0)
    live = rig.store.position(cid, Stream.ORDERS, Lane.LIVE)
    rig.store.put_position(
        live.model_copy(update={"error_streak": 3, "last_error_code": "connector.transient"}), T0
    )
    alerts = rig.engine.monitor.evaluate(connection, T0 + timedelta(minutes=30))
    assert [a.code for a in alerts] == ["connector.errors"]
    stale = rig.engine.monitor.evaluate(connection, T0 + timedelta(hours=6))
    assert "connector.errors" not in [a.code for a in stale]


def test_the_send_gate_holds_queued_messages_while_data_is_stale() -> None:
    rig = make_rig()
    connection = connect(rig, seed=False)
    ready(rig, connection.connection_id, synced_at=T0)
    gate = rig.engine.monitor.send_gate("biz_1")
    decision = Decision(
        decision_id="dec_1",
        customer_id="c1",
        made_at=T0,
        kind=DecisionKind.ACT,
        action=Action(goal_id="g1", channel=Channel.EMAIL, content_id="ct_1", send_at=T0),
        probability=0.5,
        reason="test",
        policy_version="test",
    )
    assert gate(decision, T0 + timedelta(minutes=5)) is None
    reason = gate(decision, T0 + timedelta(hours=6))
    assert reason is not None and reason.startswith("data_not_fresh")


def test_a_business_without_saas_connections_is_not_held() -> None:
    rig = make_rig()
    verdict = rig.engine.monitor.ready_to_act("biz_without_connectors", T0)
    assert verdict.can_act and verdict.connections == ()


def test_a_backfill_that_keeps_failing_raises_one_alert() -> None:
    rig = make_rig()
    connection = connect(rig, seed=False)
    cid = connection.connection_id
    ready(rig, cid, synced_at=T0, reached_days=7)  # live fresh, history short of min_history_days
    history = rig.store.position(cid, Stream.ORDERS, Lane.HISTORY)
    rig.store.put_position(
        history.model_copy(update={"error_streak": 4, "last_error_code": "connector.remote_error"}), T0
    )
    health = rig.engine.monitor.connection_health(connection, T0 + timedelta(minutes=10))
    assert health.state == "catching_up" and not health.can_act
    alerts = rig.engine.monitor.evaluate(connection, T0 + timedelta(minutes=10))
    assert [a.code for a in alerts] == ["connector.errors"] and "history" in alerts[0].message
    assert rig.engine.monitor.evaluate(connection, T0 + timedelta(minutes=20)) == []  # once
