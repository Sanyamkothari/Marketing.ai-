"""Live checks against a real Klaviyo test account. Skipped unless credentials are in the environment.

* `AUTONOMY_LIVE_KLAVIYO_API_KEY`: a private key with read scopes. Reads one page of every stream
  and lists the audiences; changes nothing.
* `AUTONOMY_LIVE_KLAVIYO_WRITE_KEY` and `AUTONOMY_LIVE_KLAVIYO_TEST_EMAIL`: a private key with
  read and write scopes on a **test account** (Klaviyo's free secondary test account), and an inbox
  you own. Creates the `Autonomy Decision` metric, the template and the `Autonomy · email` flow if
  they are missing, then hands one decision to that inbox. Run it twice: the second run creates
  nothing new and Klaviyo records the hand-off once (same decision id).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from autonomy.channels.klaviyo import KlaviyoChannelAdapter, ensure_klaviyo_setup
from autonomy.connectors import (
    ConnectorEngine,
    ConnectorRegistry,
    ConnectorStore,
    Lane,
    MemoryTokenVault,
    SystemClock,
    create_connector_tables,
)
from autonomy.connectors.klaviyo import KlaviyoConnector
from autonomy.contracts import Channel, Message
from autonomy.store import SqlEventStore, create_all, memory_engine


def live_engine(connector: KlaviyoConnector) -> ConnectorEngine:
    db = memory_engine()
    create_all(db)
    create_connector_tables(db)
    return ConnectorEngine(
        registry=ConnectorRegistry([connector]),
        store=ConnectorStore(db),
        vault=MemoryTokenVault(),
        sink=SqlEventStore(db).append,
        clock=SystemClock(),
    )


@pytest.mark.live_connector
def test_live_klaviyo_reads_every_stream(live_env) -> None:
    env = live_env("AUTONOMY_LIVE_KLAVIYO_API_KEY")
    connector = KlaviyoConnector(auto_setup=False)
    engine = live_engine(connector)
    now = datetime.now(UTC)
    connection = engine.connect_api_key(
        "klaviyo", business_id="live", api_key=env["AUTONOMY_LIVE_KLAVIYO_API_KEY"], now=now
    )
    assert connection.external_id
    for spec in connector.capabilities.streams:
        report = engine.sync(connection.connection_id, spec.stream, Lane.LIVE, now, max_pages=1)
        assert report.status in ("ok", "more"), (spec.stream, report.error_code, report.error)
    ctx = engine.context(connection, now)
    try:
        connector.audiences(ctx)
    finally:
        ctx.http.close()


@pytest.mark.live_connector
def test_live_klaviyo_sets_up_its_flow_and_hands_off_one_decision(live_env) -> None:
    env = live_env("AUTONOMY_LIVE_KLAVIYO_WRITE_KEY", "AUTONOMY_LIVE_KLAVIYO_TEST_EMAIL")
    connector = KlaviyoConnector()
    engine = live_engine(connector)
    now = datetime.now(UTC)
    connection = engine.connect_api_key(
        "klaviyo", business_id="live", api_key=env["AUTONOMY_LIVE_KLAVIYO_WRITE_KEY"], now=now
    )
    status = ensure_klaviyo_setup(engine, connection.connection_id, datetime.now(UTC))
    assert status is not None
    if status.state == "pending":
        pytest.skip("Klaviyo is still creating the decision metric; run again in a few minutes")
    assert status.state == "ready", status.steps()
    message = Message(
        message_id="msg_live_check",
        decision_id="dec_live_check",
        customer_id=env["AUTONOMY_LIVE_KLAVIYO_TEST_EMAIL"].lower(),
        channel=Channel.EMAIL,
        to=env["AUTONOMY_LIVE_KLAVIYO_TEST_EMAIL"],
        subject="Autonomy live check",
        body="If you can read this, Klaviyo sent a decision Autonomy handed over.",
        send_at=now,
    )
    receipt = KlaviyoChannelAdapter(engine, connection.connection_id).send(message)
    assert receipt.accepted, receipt.error
