"""Connections (Plan H M80): read-only links to where a client's data lives.

`registry` is the catalogue (S3 and S3-compatible stores, PostgreSQL and Redshift, MySQL / MariaDB
built in; Snowflake, BigQuery and Azure Blob Storage as optional add-ons), `store` keeps saved
connections with their secrets encrypted, and each connector module knows one kind of service.
Nothing here imports an optional SDK at module scope.
"""

from engine.connections.base import ConnectorError, Selection, TestReport
from engine.connections.registry import AI_SERVICE, CONNECTORS, catalogue, check_form, connector
from engine.connections.store import ConnectionRecord, ConnectionStore, connections_key

__all__ = [
    "AI_SERVICE",
    "CONNECTORS",
    "ConnectionRecord",
    "ConnectionStore",
    "ConnectorError",
    "Selection",
    "TestReport",
    "catalogue",
    "check_form",
    "connections_key",
    "connector",
]
