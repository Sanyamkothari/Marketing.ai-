"""The registry and catalogue, and the mapping helper connectors use to make stable events."""

from __future__ import annotations

import pytest

from autonomy.connectors import ConnectorRegistry, UnknownConnector, default_registry, map_with
from autonomy.connectors.registry import load_builtin
from autonomy.contracts import EventType

from .base_support import ORDER_MAPPING, T0, DemoConnector, iso


def test_register_get_and_catalogue_say_what_is_configured() -> None:
    registry = ConnectorRegistry([DemoConnector()])
    assert registry.kinds() == ("demo",) and "demo" in registry
    assert registry.get("demo").capabilities.label == "Demo Shop"
    with pytest.raises(UnknownConnector):
        registry.get("myspace")
    entry = registry.catalogue({})[0]
    assert not entry.configured
    assert entry.missing_settings == ("AUTONOMY_DEMO_CLIENT_ID", "AUTONOMY_DEMO_CLIENT_SECRET")
    ready = registry.catalogue({"AUTONOMY_DEMO_CLIENT_ID": "i", "AUTONOMY_DEMO_CLIENT_SECRET": "s"})[0]
    assert ready.configured and ready.missing_settings == ()
    caps = ready.capabilities
    assert caps.api_version.value == "2026-07" and caps.api_version.header == "X-Demo-Version"
    assert [t.topic for t in caps.webhook_topics if t.mandatory] == ["customers/redact"]


def test_default_registry_skips_connectors_not_in_this_build() -> None:
    assert load_builtin("autonomy.connectors.not_built_yet:Nothing") is None
    registry = default_registry([DemoConnector()])
    assert "demo" in registry  # shopify / klaviyo join when their modules exist


def test_map_with_gives_stable_per_account_ids_and_collects_bad_rows() -> None:
    records = [
        {"id": "1001", "customer_id": "c1", "total": "19.90", "created_at": iso(T0), "updated_at": iso(T0)},
        {"id": "1002", "customer_id": None, "total": 5, "created_at": iso(T0), "updated_at": iso(T0)},
        {"id": "1003", "customer_id": "c2", "created_at": "not a date", "updated_at": iso(T0)},
    ]
    shop_a = map_with(ORDER_MAPPING, records, source="demo", account="a.example")
    again = map_with(ORDER_MAPPING, records, source="demo", account="a.example")
    shop_b = map_with(ORDER_MAPPING, records[:1], source="demo", account="b.example")
    assert [e.event_id for e in shop_a.events] == [e.event_id for e in again.events]
    assert shop_a.events[0].event_id != shop_b.events[0].event_id  # two shops' order #1001
    assert shop_a.events[0].type is EventType.PURCHASE and shop_a.events[0].value == pytest.approx(19.9)
    assert sorted(p.code for p in shop_a.problems) == ["bad_timestamp", "missing_customer"]
    # a later page with the same record at a different position keeps its id
    moved = map_with(ORDER_MAPPING, [{"x": 1, **records[1]}, records[0]], source="demo", account="a.example")
    assert moved.events[0].event_id == shop_a.events[0].event_id
    assert map_with(ORDER_MAPPING, [], source="demo", account="a").events == ()
    with pytest.raises(ValueError):
        map_with(ORDER_MAPPING.model_copy(update={"row_key": ()}), records, source="demo", account="a")
