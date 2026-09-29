"""The screens' files are revalidated on every load, so an upgrade shows up without a hard refresh."""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.main import create_app

SCRIPT = "/ui/modules/connections/page.js"


def test_ui_files_are_sent_with_no_cache_and_answer_a_matching_etag_with_304() -> None:
    client = TestClient(create_app())
    first = client.get(SCRIPT)
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-cache"
    again = client.get(SCRIPT, headers={"if-none-match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.headers["cache-control"] == "no-cache"


def test_the_page_itself_is_revalidated_too() -> None:
    assert TestClient(create_app()).get("/ui/").headers["cache-control"] == "no-cache"
