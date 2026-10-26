"""The batch-approval routes over HTTP, on a bare app (no demo services needed)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v2.images import ImageServices, attach_image_services, router
from autonomy.assets.generate import Flaw
from autonomy.assets.library import InMemoryAssetLibrary
from tests.v3.images.support import desk, make_image


def build() -> tuple[TestClient, InMemoryAssetLibrary, Any]:
    library = InMemoryAssetLibrary()
    d = desk(library=library)
    app = FastAPI()
    attach_image_services(app, ImageServices(desk=d))
    app.include_router(router)
    return TestClient(app), library, d


def test_the_routes_answer_503_when_images_are_not_turned_on() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/v2/images/batches").status_code == 503
    assert client.post("/v2/images/batches/imgb_0001/approve", json={"approved_by": "ana"}).status_code == 503


def test_list_a_batch_then_approve_all() -> None:
    client, library, d = build()
    for i in range(3):
        d.submit(make_image(f"shoe-{i}"))
    listing = client.get("/v2/images/batches", params={"open": "true"}).json()
    assert [(b["batch_id"], b["size"], b["pending"]) for b in listing] == [("imgb_0001", 3, 3)]
    body = client.get("/v2/images/batches/imgb_0001").json()
    assert len(body["images"]) == 3
    first = body["images"][0]
    assert first["status"] == "pending"
    assert first["texts"] == ["Runner 0", "20% off", "₹1,299.50", "₹1,039.60"]
    assert library.find(approved_only=False) == []

    done = client.post("/v2/images/batches/imgb_0001/approve", json={"approved_by": "Asha"})
    assert done.status_code == 200
    assert len(done.json()["approved_asset_ids"]) == 3
    assert done.json()["approved_by"] == "Asha"
    assert len(library.find()) == 3
    assert all(a.approved_by == "Asha" for a in library.find())
    assert client.get("/v2/images/batches", params={"open": "true"}).json() == []


def test_reject_one_then_approve_the_rest() -> None:
    client, library, d = build()
    ids = [d.submit(make_image(f"shoe-{i}")).image.image_id for i in range(3)]
    rejected = client.post(f"/v2/images/{ids[0]}/reject", json={"rejected_by": "ben", "reason": "off colour"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected" and rejected.json()["reason"] == "off colour"
    client.post("/v2/images/batches/imgb_0001/approve", json={"approved_by": "ana"})
    assert {a.item_id for a in library.find()} == {"shoe-1", "shoe-2"}
    again = client.post(f"/v2/images/{ids[0]}/reject", json={"rejected_by": "ben"})
    assert again.status_code == 409


def test_errors_are_plain() -> None:
    client, _, d = build()
    d.submit(make_image())
    assert client.get("/v2/images/batches/imgb_9999").status_code == 404
    assert client.post("/v2/images/batches/imgb_9999/approve", json={"approved_by": "a"}).status_code == 404
    assert client.post("/v2/images/img_nope/reject", json={"rejected_by": "a"}).status_code == 404
    assert client.get("/v2/images/img_nope/preview.svg").status_code == 404
    assert client.post("/v2/images/batches/imgb_0001/approve", json={}).status_code == 422
    assert client.post("/v2/images/batches/imgb_0001/approve", json={"approved_by": "   "}).status_code == 422
    assert (
        client.post("/v2/images/batches/imgb_0001/approve", json={"approved_by": "ana", "x": 1}).status_code
        == 422
    )
    assert client.post("/v2/images/batches/imgb_0001/approve", json={"approved_by": "ana"}).status_code == 200
    assert client.post("/v2/images/batches/imgb_0001/approve", json={"approved_by": "ana"}).status_code == 409


def test_refused_images_are_listed_with_reasons_and_cannot_be_approved() -> None:
    client, library, d = build()
    d.submit(make_image("shoe-0", flaws={"shoe-0": [Flaw.WRONG_PRODUCT]}))
    refused = client.get("/v2/images/refused").json()
    assert len(refused) == 1
    assert refused[0]["status"] == "refused"
    assert [r["code"] for r in refused[0]["reasons"]] == ["wrong_product_seen"]
    assert client.get("/v2/images/batches").json() == []
    assert client.post("/v2/images/batches/imgb_0001/approve", json={"approved_by": "ana"}).status_code == 404
    assert library.find(approved_only=False) == []


def test_preview_is_the_svg_with_the_exact_text() -> None:
    client, _, d = build()
    image_id = d.submit(make_image()).image.image_id
    response = client.get(f"/v2/images/{image_id}/preview.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    root = ET.fromstring(response.text)
    texts = [t.text for t in root.iter("{http://www.w3.org/2000/svg}text")]
    assert texts == ["Runner 0", "20% off", "₹1,299.50", "₹1,039.60"]


def test_create_app_wires_the_routes_and_leaves_them_off_by_default() -> None:
    from datetime import UTC, datetime

    from api.v2.app import create_app
    from api.v2.deps import build_demo_services

    services = build_demo_services(datetime(2026, 10, 20, 12, tzinfo=UTC))
    d = desk()
    d.submit(make_image())
    on = TestClient(create_app(services, images=ImageServices(desk=d)))
    assert [b["batch_id"] for b in on.get("/v2/images/batches").json()] == ["imgb_0001"]
    assert TestClient(create_app(services)).get("/v2/images/batches").status_code == 503
