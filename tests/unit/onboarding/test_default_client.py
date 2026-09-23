"""`LocalClientStore.ensure_client`: the client every installation starts with (Plan A M35).

The header's picker stands on "Demo" before anyone has created a client, so the API creates it the
first time it is asked for - and must never create a second one, however many screens ask.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from engine.clients import DEFAULT_CLIENT_ID, DEFAULT_CLIENT_NAME, LocalClientStore


def test_the_default_client_is_created_the_first_time_and_returned_after(tmp_path: Path) -> None:
    store = LocalClientStore(tmp_path / "clients.db")
    first = store.ensure_client(DEFAULT_CLIENT_ID, DEFAULT_CLIENT_NAME, "telecom")
    again = store.ensure_client(DEFAULT_CLIENT_ID, "A different name", "utilities")
    assert first == again
    assert (first.client_id, first.name, first.industry) == ("c_demo", "Demo", "telecom")
    assert [client.client_id for client in store.list_clients()] == ["c_demo"]


def test_racing_first_requests_leave_exactly_one_default_client(tmp_path: Path) -> None:
    store = LocalClientStore(tmp_path / "clients.db")
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(
            pool.map(
                lambda _: store.ensure_client(DEFAULT_CLIENT_ID, DEFAULT_CLIENT_NAME, "telecom"), range(16)
            )
        )
    assert len({record.created_at for record in records}) == 1
    assert len(store.list_clients()) == 1


def test_the_default_id_never_collides_with_a_minted_one(tmp_path: Path) -> None:
    """A client a person names "Demo" gets a minted `c_demo_<n>`, never the fixed default id."""
    store = LocalClientStore(tmp_path / "clients.db")
    store.ensure_client(DEFAULT_CLIENT_ID, DEFAULT_CLIENT_NAME, "telecom")
    named = store.create_client("Demo", "telecom")
    assert named.client_id == "c_demo_1"
    assert {client.client_id for client in store.list_clients()} == {"c_demo", "c_demo_1"}
