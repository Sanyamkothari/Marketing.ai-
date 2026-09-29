"""The catalogue of connectors, and the checking of what a person typed into a connection's form."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from engine.connections.azure_blob import AzureBlobConnector
from engine.connections.base import ConfigValue, Connector, ConnectorError, FieldSpec, KindInfo
from engine.connections.bigquery import BigQueryConnector
from engine.connections.mysql import MySqlConnector
from engine.connections.postgres import PostgresConnector
from engine.connections.s3 import S3Connector
from engine.connections.snowflake import SnowflakeConnector

__all__ = ["AI_SERVICE", "CONNECTORS", "catalogue", "check_form", "connector"]

CONNECTORS: Final[Mapping[str, Connector]] = {
    c.kind: c
    for c in (
        S3Connector(),
        S3Connector(compatible=True),
        PostgresConnector(),
        PostgresConnector(redshift=True),
        MySqlConnector(),
        SnowflakeConnector(),
        BigQueryConnector(),
        AzureBlobConnector(),
    )
}
"""Every kind a connection can be, in the order the catalogue shows them (built in first)."""

AI_SERVICE: Final[KindInfo] = KindInfo(
    kind="ai_service",
    label="AI service",
    description="The AI services that write summaries, campaign copy and chat answers: Product AI for your "
    "team's Guided setup chat, Deliverable AI for what your customer receives. Each is set up and tested "
    "on its own screen.",
    group="ai",
    tier="built_in",
    available=True,
    creatable=False,
    screen="#/connections/ai/product",
)
"""The AI service's card. Its backend is `/ai-service` (two slots, DEC-1140); the page draws one card per slot."""

MAX_TEXT: Final[int] = 1000
MAX_SECRET: Final[int] = 20_000
"""A BigQuery service-account key is about 2,400 characters; nothing else comes close."""
MAX_NAME: Final[int] = 80


def catalogue() -> list[KindInfo]:
    """Every card of the Connections page: the data connectors, then the AI service."""
    return [c.info() for c in CONNECTORS.values()] + [AI_SERVICE]


def connector(kind: str) -> Connector:
    try:
        return CONNECTORS[kind]
    except KeyError:
        raise ConnectorError(
            "CONNECTION_KIND_UNKNOWN",
            "Marketing AI cannot connect to that kind of service.",
            f"Choose one of: {', '.join(CONNECTORS)}.",
            status=422,
        ) from None


def check_form(
    info: KindInfo,
    config: Mapping[str, Any],
    secrets: Mapping[str, Any],
    *,
    kept_secrets: frozenset[str] = frozenset(),
) -> tuple[dict[str, ConfigValue], dict[str, str]]:
    """The form's values checked against the kind's fields: `(config, secrets)` ready to store.

    Errors name fields, never values. A blank secret means "not given" (on an update: keep the saved
    one, listed in `kept_secrets`). A secret must not arrive in `config`, nor a setting in `secrets`.
    """
    by_name = {f.name: f for f in info.fields}
    unknown = sorted(set(config) - {f.name for f in info.fields if not f.secret})
    unknown += sorted(set(secrets) - {f.name for f in info.fields if f.secret})
    if unknown:
        known = [f.name for f in info.fields]
        named = [name for name in unknown if name in by_name]
        raise ConnectorError(
            "CONNECTION_FIELD_UNKNOWN",
            (f"These go in the other section: {', '.join(named)}. " if named else "")
            + f"A {info.label} connection takes only: {', '.join(known)}.",
            status=422,
        )
    clean: dict[str, ConfigValue] = {}
    for field in info.fields:
        if field.secret:
            continue
        value = _coerce(field, config.get(field.name, field.default))
        if field.required and (value is None or value == ""):
            raise _missing(field)
        if value is not None and value != "":
            clean[field.name] = value
    clean_secrets: dict[str, str] = {}
    for field in info.fields:
        if not field.secret:
            continue
        raw = secrets.get(field.name)
        if raw is not None and not isinstance(raw, str):
            raise _invalid(field, "must be text")
        value = (raw or "").strip()
        if len(value) > MAX_SECRET:
            raise _invalid(field, "is too long")
        if value:
            clean_secrets[field.name] = value
        elif field.required and field.name not in kept_secrets:
            raise _missing(field)
    return clean, clean_secrets


def check_name(name: Any, info: KindInfo) -> str:
    if name is None or (isinstance(name, str) and not name.strip()):
        return info.label
    if not isinstance(name, str) or len(name.strip()) > MAX_NAME:
        raise ConnectorError(
            "CONNECTION_NAME_INVALID", f"The name must be text of at most {MAX_NAME} characters.", status=422
        )
    return name.strip()


def _coerce(field: FieldSpec, value: Any) -> ConfigValue:
    if value is None or value == "":
        return None
    if field.type == "checkbox":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false", "1", "0", "on", "off", "yes", "no"}:
            return value.lower() in {"true", "1", "on", "yes"}
        raise _invalid(field, "must be ticked or not")
    if field.type == "number":
        if isinstance(value, bool):
            raise _invalid(field, "must be a whole number")
        try:
            number = int(str(value).strip())
        except ValueError:
            raise _invalid(field, "must be a whole number") from None
        if not 1 <= number <= 65535:
            raise _invalid(field, "must be between 1 and 65535")
        return number
    if not isinstance(value, str | int) or isinstance(value, bool):
        raise _invalid(field, "must be text")
    text = str(value).strip()
    if len(text) > MAX_TEXT or "\x00" in text:
        raise _invalid(field, "is too long")
    if field.type == "select" and text not in field.options:
        raise _invalid(field, f"must be one of: {', '.join(field.options)}")
    return text


def _missing(field: FieldSpec) -> ConnectorError:
    return ConnectorError("CONNECTION_FIELD_REQUIRED", f"{field.label} is needed.", status=422)


def _invalid(field: FieldSpec, why: str) -> ConnectorError:
    return ConnectorError("CONNECTION_FIELD_INVALID", f"{field.label} {why}.", status=422)
