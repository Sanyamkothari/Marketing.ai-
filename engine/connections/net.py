"""Reaching a host: the first step of every database test, and the one address rule (Plan H M80).

A connection points the server at a host a person typed. That is the feature - a client's database
is wherever it is - but one kind of address is never a client's system: the link-local range, where
cloud instance metadata lives (169.254.169.254 hands out the server's own credentials). A connection
to it is refused before any byte is sent, whatever name resolved to it.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Final
from urllib.parse import urlsplit

from engine.connections.base import CONNECT_TIMEOUT_S, ConnectorError

__all__ = ["check_host", "check_url", "reach"]

_HOST_FIX: Final[str] = "Check the server name. It is the part before the port, such as db.example.com."


def _blocked(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_link_local or ip.is_multicast or ip.is_unspecified


def check_host(host: str) -> None:
    """Refuse an empty host, or one that is (or resolves to) a link-local, multicast or unspecified address."""
    if not host or any(ch.isspace() for ch in host) or "/" in host:
        raise ConnectorError("CONNECTION_HOST_INVALID", "The server name is not valid.", _HOST_FIX)
    if _blocked(host):
        raise _not_allowed()
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return  # unresolvable here: the reach step reports it with its own fix
    if any(_blocked(str(info[4][0])) for info in infos):
        raise _not_allowed()


def check_url(url: str) -> None:
    """`check_host` for an endpoint URL, which must be http or https."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConnectorError(
            "CONNECTION_ENDPOINT_INVALID",
            "The endpoint must be a web address starting with https:// (or http:// on a private network).",
            "Copy the endpoint from your storage provider's console, e.g. https://storage.googleapis.com.",
        )
    check_host(parts.hostname)


def reach(host: str, port: int, timeout: float = CONNECT_TIMEOUT_S) -> None:
    """Open and close a TCP connection to `host:port`, or raise a `ConnectorError` saying what to fix."""
    check_host(host)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except socket.gaierror:
        raise ConnectorError(
            "CONNECTION_UNREACHABLE",
            f"There is no server called {host} that this machine can find.",
            _HOST_FIX,
        ) from None
    except TimeoutError:
        raise ConnectorError(
            "CONNECTION_UNREACHABLE",
            f"{host} did not answer on port {port} within {int(timeout)} seconds.",
            "Check the port, and ask whoever runs the database to let this machine's address through "
            "its firewall.",
        ) from None
    except OSError:
        raise ConnectorError(
            "CONNECTION_UNREACHABLE",
            f"{host} refused a connection on port {port}.",
            "Check the port number, and that the database is running and accepts outside connections.",
        ) from None


def _not_allowed() -> ConnectorError:
    return ConnectorError(
        "CONNECTION_HOST_NOT_ALLOWED",
        "That address belongs to the server itself, not to a data source, so it cannot be used.",
        "Use the address of your database or storage service.",
    )
