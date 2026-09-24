"""`/` sends a person to the screens (DEC-953).

uvicorn prints `http://127.0.0.1:8000` and a person types `localhost:8000`; both used to answer a JSON
404. The redirect is a plain route beside the `/ui` mount, not an API route: it has no access policy,
is not in the OpenAPI document, and needs no sign-in, exactly like the static screens it points at.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api.main import create_app


def test_the_root_redirects_to_the_screens(tmp_path: Path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui/"
    assert client.get("/").status_code == 200


def test_the_redirect_is_not_part_of_the_api(tmp_path: Path) -> None:
    client = TestClient(create_app(data_dir=tmp_path))
    assert "/" not in client.get("/openapi.json").json()["paths"]
