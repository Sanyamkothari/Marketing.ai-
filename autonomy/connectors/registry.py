"""The catalogue of SaaS connectors and what each can do, for the API and the Connect screen.

Connectors register themselves by kind. The built-in ones are found lazily by module path
(`BUILTIN`), so this module never imports a connector it does not need, and a connector whose
module is not in the build (another stream has not merged yet) is simply not listed. Adding a
connector is: write `autonomy/connectors/<kind>.py` with a `SaasConnector` subclass, then add one
line to `BUILTIN` (or call `registry.register(...)` at wiring time).

`catalogue()` reports, per connector, whether our app's OAuth credentials are configured
(`AUTONOMY_<KIND>_CLIENT_ID` / `_CLIENT_SECRET`) so the screen can say "not set up on this server"
instead of failing at the consent step.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable, Mapping
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from autonomy.connectors.base import AuthMethod, ConnectorCapabilities, NotSupported, SaasConnector
from autonomy.connectors.oauth import AppCredentials

__all__ = ["BUILTIN", "CatalogueEntry", "ConnectorRegistry", "UnknownConnector", "default_registry"]

log = logging.getLogger(__name__)

BUILTIN: Final[Mapping[str, str]] = {
    "shopify": "autonomy.connectors.shopify:ShopifyConnector",
    "klaviyo": "autonomy.connectors.klaviyo:KlaviyoConnector",
}
"""kind -> "module:Class" of the connectors that ship with Autonomy, loaded on first use."""


class UnknownConnector(NotSupported):
    code = "connector.unknown_kind"


class CatalogueEntry(BaseModel):
    """One card of the Connect screen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capabilities: ConnectorCapabilities
    configured: bool = Field(description="Our app's credentials for this platform are set on the server")
    missing_settings: tuple[str, ...] = Field(default=(), description="Environment variables still to set")


class ConnectorRegistry:
    def __init__(self, connectors: Iterable[SaasConnector] = ()) -> None:
        self._connectors: dict[str, SaasConnector] = {}
        for connector in connectors:
            self.register(connector)

    def register(self, connector: SaasConnector) -> None:
        self._connectors[connector.kind] = connector

    def find(self, kind: str) -> SaasConnector | None:
        return self._connectors.get(kind)

    def get(self, kind: str) -> SaasConnector:
        connector = self._connectors.get(kind)
        if connector is None:
            raise UnknownConnector(f"Autonomy has no connector called {kind!r}")
        return connector

    def kinds(self) -> tuple[str, ...]:
        return tuple(self._connectors)

    def __contains__(self, kind: object) -> bool:
        return kind in self._connectors

    def capabilities(self) -> list[ConnectorCapabilities]:
        return [c.capabilities for c in self._connectors.values()]

    def catalogue(self, environ: Mapping[str, str]) -> list[CatalogueEntry]:
        entries = []
        for connector in self._connectors.values():
            missing: tuple[str, ...] = ()
            if connector.capabilities.auth is AuthMethod.OAUTH2:
                id_name, secret_name = AppCredentials.env_names(connector.kind)
                missing = tuple(n for n in (id_name, secret_name) if not environ.get(n, "").strip())
            entries.append(
                CatalogueEntry(
                    capabilities=connector.capabilities, configured=not missing, missing_settings=missing
                )
            )
        return entries


def load_builtin(path: str) -> SaasConnector | None:
    """Import "module:Class" and instantiate it; None when the module is not part of this build."""
    module_name, _, class_name = path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name is not None and module_name.startswith(exc.name):
            return None  # the connector itself is absent
        raise
    factory = getattr(module, class_name, None)
    if factory is None:
        log.warning("connectors: %s has no %s; not listed", module_name, class_name)
        return None
    connector = factory()
    if not isinstance(connector, SaasConnector):
        raise TypeError(f"{path} is not a SaasConnector")
    return connector


def default_registry(extra: Iterable[SaasConnector] = (), *, builtin: bool = True) -> ConnectorRegistry:
    """Every built-in connector present in this build, plus `extra`."""
    registry = ConnectorRegistry()
    if builtin:
        for path in BUILTIN.values():
            connector = load_builtin(path)
            if connector is not None:
                registry.register(connector)
    for connector in extra:
        registry.register(connector)
    return registry
