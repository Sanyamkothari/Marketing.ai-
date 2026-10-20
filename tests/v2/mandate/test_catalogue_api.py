"""The Action Catalogue registry and the `/mandates` router, on a tiny app that mounts only it."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v2.engines.mandates import MandateRegistry, get_mandate_registry, router
from autonomy.contracts import Action, Channel, CustomerState, OfferOption
from autonomy.contracts.goals import OfferKind
from autonomy.mandate.catalogue import (
    ActionCatalogue,
    CatalogueContent,
    CatalogueError,
    CatalogueTemplate,
    ChannelRate,
    JsonDirCatalogueStore,
    catalogue_costs,
    check_action_against_catalogue,
)
from autonomy.mandate.models import SignatureAlgorithm, SignaturePolicy, SignerRole
from autonomy.mandate.signing import HmacSigner, KeyRing, signature_message

NOW = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)  # 11:30 in Kolkata
SECRETS = {
    "k_asha": b"asha-secret-0123456789",
    "k_ravi": b"ravi-secret-0123456789",
    "k_meera": b"meera-secret-012345678",
}


def _p10(amount: float = 10) -> OfferOption:
    return OfferOption(
        offer_id="p10", label="10% off", kind=OfferKind.PERCENT_OFF, amount=amount, cost_per_redemption=40
    )


def _content(**changes: Any) -> CatalogueContent:
    base: dict[str, Any] = {
        "offers": (_p10(),),
        "channels": (
            ChannelRate(channel=Channel.WHATSAPP, unit_cost=0.78, currency="INR", provider="gupshup"),
            ChannelRate(channel=Channel.EMAIL, unit_cost=0.02, currency="INR", provider="ses"),
        ),
        "templates": (
            CatalogueTemplate(
                template_id="tpl_diwali_wa",
                channel=Channel.WHATSAPP,
                provider_template_name="diwali_offer_v1",
                approved_at=NOW - timedelta(days=2),
                locale="en_IN",
            ),
        ),
    }
    base.update(changes)
    return CatalogueContent(**base)


# --- the catalogue registry -----------------------------------------------------------------------


def test_publish_versions_diff_and_history_chain() -> None:
    cat = ActionCatalogue()
    v1 = cat.publish(_content(), created_by="ops", now=NOW)
    assert v1.version == 1 and v1.diff_from_previous.from_version is None
    assert len(v1.diff_from_previous.changes) == 4  # everything is new

    v2 = cat.publish(_content(offers=(_p10(15),)), created_by="ops", now=NOW + timedelta(hours=1))
    assert v2.version == 2 and v2.previous_hash == v1.content_hash
    (change,) = v2.diff_from_previous.changes
    assert (change.kind, change.key, change.change, change.fields) == ("offer", "p10", "changed", ("amount",))
    assert cat.get(1) == v1 and cat.get("latest") == v2  # history is kept unchanged
    assert cat.diff(1, 2) == v2.diff_from_previous
    assert cat.verify_history() == []

    with pytest.raises(CatalogueError, match="nothing changed"):
        cat.publish(_content(offers=(_p10(15),)), created_by="ops", now=NOW + timedelta(hours=2))
    with pytest.raises(CatalogueError, match="does not exist"):
        cat.get(9)
    with pytest.raises(ValueError, match="duplicate offer"):
        _content(offers=(_p10(), _p10(20)))
    with pytest.raises(CatalogueError, match="time zone"):
        cat.publish(_content(), created_by="ops", now=datetime(2026, 11, 1))


def test_pricing_never_mixes_currencies() -> None:
    usd_wa = ChannelRate(channel=Channel.WHATSAPP, unit_cost=0.01, currency="USD", provider="twilio")
    content = _content()
    cat = ActionCatalogue()
    v1 = cat.publish(_content(channels=(*content.channels, usd_wa)), created_by="ops", now=NOW)
    action = Action(goal_id="g", channel=Channel.WHATSAPP, content_id="tpl_diwali_wa", send_at=NOW)
    # The USD rate is cheaper in number, but an INR mandate is priced (and checked) in INR only.
    assert catalogue_costs(v1, action, currency="INR") == (0.0, pytest.approx(0.78))
    assert check_action_against_catalogue(v1, action, currency="INR") == []
    assert check_action_against_catalogue(v1, action, currency="EUR") == ["catalogue_currency_mismatch"]
    with pytest.raises(ValueError):
        ChannelRate(channel=Channel.SMS, unit_cost=0.1, currency="inr", provider="x")


def test_json_dir_store_is_write_once_and_tamper_evident(tmp_path: Path) -> None:
    cat = ActionCatalogue(JsonDirCatalogueStore(tmp_path))
    v1 = cat.publish(_content(), created_by="ops", now=NOW)
    reopened = ActionCatalogue(JsonDirCatalogueStore(tmp_path))
    assert reopened.get("latest") == v1
    with pytest.raises(CatalogueError, match="already exists"):
        reopened.store.append(v1)

    path = tmp_path / "catalogue_v000001.json"
    raw = json.loads(path.read_text())
    raw["channels"][0]["unit_cost"] = 0.01  # someone quietly cheapens WhatsApp
    path.write_text(json.dumps(raw))
    assert reopened.verify_history() == ["v1 was changed after it was published"]


# --- the router -----------------------------------------------------------------------------------


@pytest.fixture
def reg() -> MandateRegistry:
    keys = KeyRing()
    keys.add_hmac("k_asha", SECRETS["k_asha"], person_id="asha", roles=[SignerRole.MARKETING_LEAD])
    keys.add_hmac("k_ravi", SECRETS["k_ravi"], person_id="ravi", roles=[SignerRole.CFO])
    keys.add_hmac("k_meera", SECRETS["k_meera"], person_id="meera", roles=[SignerRole.APPROVER])
    return MandateRegistry(
        policies={"INR": SignaturePolicy(finance_threshold=100_000)},
        keys=keys,
        custodial={k: HmacSigner(k, s) for k, s in SECRETS.items()},
        clock=lambda: NOW,
    )


@pytest.fixture
def client(reg: MandateRegistry) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_mandate_registry] = lambda: reg
    with TestClient(app) as c:
        yield c


def _publish(client: TestClient) -> dict[str, Any]:
    body = {"created_by": "ops", "now": NOW.isoformat(), **_content().model_dump(mode="json")}
    resp = client.post("/mandates/catalogue", json=body)
    assert resp.status_code == 201, resp.text
    out: dict[str, Any] = resp.json()
    return out


FOUNDER_EXAMPLE: dict[str, Any] = {
    "mandate_id": "mnd_diwali_2026",
    "goal_ids": ["goal_diwali"],
    "budget_ceiling_inr": 500_000,
    "margin_floor_pct": 18,
    "quiet_hours": {"start": "21:00", "end": "09:00", "timezone": "Asia/Kolkata"},
    "frequency_cap": {"max_per_customer": 2, "window_days": 7},
    "approved_templates": ["tpl_diwali_wa"],
    "approved_offers": ["p10"],
    "signers": [
        {"role": "marketing_lead", "signer_id": "asha"},
        {"role": "cfo", "signer_id": "ravi"},
    ],
    "created_by": "asha",
    "now": NOW.isoformat(),
}


def _draft_and_certify(client: TestClient) -> dict[str, Any]:
    _publish(client)
    resp = client.post("/mandates", json=FOUNDER_EXAMPLE)
    assert resp.status_code == 201, resp.text
    for role, who in (("marketing_lead", "asha"), ("cfo", "ravi")):
        assert client.post("/mandates/mnd_diwali_2026/sign", json={"role": role, "signer_id": who}).is_success
    resp = client.post("/mandates/mnd_diwali_2026/certify", json={"actor": "asha"})
    assert resp.status_code == 201, resp.text
    out: dict[str, Any] = resp.json()
    return out


def test_draft_sign_certify_needs_cfo_above_threshold(client: TestClient) -> None:
    _publish(client)
    resp = client.post("/mandates", json=FOUNDER_EXAMPLE)
    assert resp.status_code == 201, resp.text
    m = resp.json()["mandate"]
    assert (m["budget_ceiling"], m["currency"], m["margin_floor_pct"]) == (500_000, "INR", 0.18)
    assert m["quiet_hours"] == {"start": 21, "end": 9, "timezone": "Asia/Kolkata"}
    assert m["channels"] == ["whatsapp"]  # taken from the approved template in the catalogue
    assert resp.json()["catalogue_version"] == 1 and m["status"] == "draft"

    resp = client.post("/mandates/mnd_diwali_2026/sign", json={"role": "marketing_lead", "signer_id": "asha"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending_signatures"

    resp = client.post("/mandates/mnd_diwali_2026/certify", json={"actor": "asha"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "missing_role:finance" in detail["missing"]
    assert {"role": "cfo", "signer_id": "ravi", "for": "missing_role:finance"} in detail["awaiting"]

    resp = client.post("/mandates/mnd_diwali_2026/sign", json={"role": "cfo", "signer_id": "ravi"})
    assert resp.status_code == 200 and resp.json()["status"] == "pending_signatures"  # waits for certify
    resp = client.post("/mandates/mnd_diwali_2026/certify", json={"actor": "asha"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "active" and resp.json()["unmet"] == []

    got = client.get("/mandates/mnd_diwali_2026").json()
    assert got["status"] == "active" and len(got["mandate"]["signers"]) == 2
    ledger = client.get("/mandates/mnd_diwali_2026/ledger").json()
    assert [e["kind"] for e in ledger["entries"]] == ["created", "signed", "signed", "activated"]
    assert ledger["chain_ok"] is True


def test_carried_signature_is_verified_and_signers_are_checked(client: TestClient) -> None:
    _publish(client)
    view = client.post("/mandates", json=FOUNDER_EXAMPLE).json()
    signed_at = NOW.isoformat()
    message = signature_message(
        body_hash_hex=view["body_hash"],
        person_id="asha",
        role=SignerRole.MARKETING_LEAD,
        key_id="k_asha",
        algorithm=SignatureAlgorithm.HMAC_SHA256,
        signed_at=NOW,
    )
    good = HmacSigner("k_asha", SECRETS["k_asha"]).sign(message)
    carried = {
        "role": "marketing_lead",
        "signer_id": "asha",
        "key_id": "k_asha",
        "signed_at": signed_at,
        "algorithm": "hmac-sha256",
    }
    forged = client.post("/mandates/mnd_diwali_2026/sign", json={**carried, "signature": "00" * 32})
    assert forged.status_code == 403
    assert (
        client.post("/mandates/mnd_diwali_2026/sign", json={**carried, "signature": good}).status_code == 200
    )
    again = client.post("/mandates/mnd_diwali_2026/sign", json={**carried, "signature": good})
    assert again.status_code == 409  # the same person cannot sign twice in one role

    stranger = client.post("/mandates/mnd_diwali_2026/sign", json={"role": "approver", "signer_id": "meera"})
    assert stranger.status_code == 403 and "not listed" in stranger.json()["detail"]
    empty = client.post("/mandates/mnd_diwali_2026/sign", json={**carried, "signature": ""})
    assert empty.status_code == 422  # refused at the door, not a 500 from deeper down

    # Ravi's key may only sign as CFO, so he cannot pose as the marketing lead. The draft lists no
    # signers, so it is the key's role binding (not the signer list) that refuses him.
    open_draft = {**FOUNDER_EXAMPLE, "mandate_id": "mnd_open", "signers": []}
    assert client.post("/mandates", json=open_draft).status_code == 201
    wrong_role = client.post(
        "/mandates/mnd_open/sign",
        json={"role": "marketing_lead", "signer_id": "ravi", "key_id": "k_ravi"},
    )
    assert wrong_role.status_code == 403 and "does not verify" in wrong_role.json()["detail"]
    as_cfo = client.post("/mandates/mnd_open/sign", json={"role": "cfo", "signer_id": "ravi"})
    assert as_cfo.status_code == 200, as_cfo.text


def _state() -> dict[str, Any]:
    return CustomerState(customer_id="c1", as_of=NOW, timezone="Asia/Kolkata").model_dump(mode="json")


def _action(send_at: datetime) -> dict[str, Any]:
    return {
        "goal_id": "goal_diwali",
        "channel": "whatsapp",
        "content_id": "tpl_diwali_wa",
        "send_at": send_at.isoformat(),
    }


def test_check_action_catches_quiet_hours_and_revoke(client: TestClient) -> None:
    _draft_and_certify(client)
    ok = client.post(
        "/mandates/mnd_diwali_2026/check-action",
        json={"action": _action(NOW), "state": _state(), "now": NOW.isoformat()},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["allowed"] is True and ok.json()["violations"] == []
    assert ok.json()["costs"]["channel_cost"] == pytest.approx(0.78)  # priced from the catalogue

    late = NOW.replace(hour=17, minute=30)  # 23:00 in Kolkata
    resp = client.post(
        "/mandates/mnd_diwali_2026/check-action",
        json={
            "action": _action(late),
            "costs": {"channel_cost": 0.78},
            "state": _state(),
            "now": NOW.isoformat(),
        },
    )
    assert resp.json()["allowed"] is False
    assert resp.json()["violations"] == ["mandate_quiet_hours"]

    sms = {**_action(NOW), "channel": "sms"}
    resp = client.post(
        "/mandates/mnd_diwali_2026/check-action",
        json={"action": sms, "state": _state(), "now": NOW.isoformat()},
    )
    assert set(resp.json()["violations"]) == {
        "mandate_channel_not_allowed",
        "catalogue_template_channel_mismatch",
        "catalogue_channel_unpriced",
    }

    assert client.post("/mandates/mnd_diwali_2026/revoke", json={"actor": "ravi"}).status_code == 200
    resp = client.post(
        "/mandates/mnd_diwali_2026/check-action",
        json={"action": _action(NOW), "state": _state(), "now": NOW.isoformat()},
    )
    assert "mandate_not_active" in resp.json()["violations"]
    assert client.post("/mandates/mnd_diwali_2026/certify", json={}).status_code == 409


def test_catalogue_routes_publish_get_diff(client: TestClient) -> None:
    assert client.get("/mandates/catalogue").status_code == 404
    _publish(client)
    body = {
        "created_by": "ops",
        "now": (NOW + timedelta(days=1)).isoformat(),
        **_content(offers=(_p10(15),), templates=()).model_dump(mode="json"),
    }
    resp = client.post("/mandates/catalogue", json=body)
    assert resp.status_code == 201 and resp.json()["version"] == 2
    assert client.post("/mandates/catalogue", json=body).status_code == 409  # nothing changed

    assert client.get("/mandates/catalogue", params={"version": 1}).json()["version"] == 1
    assert client.get("/mandates/catalogue").json()["version"] == 2
    diff = client.get("/mandates/catalogue/diff", params={"a": 1, "b": 2}).json()
    assert diff["summary"] == "v1 -> v2: 1 offer changed, 1 template removed."
    history = client.get("/mandates/catalogue/history").json()
    assert [h["version"] for h in history] == [1, 2]

    dupes = {**body, "offers": [_p10().model_dump(mode="json")] * 2}
    assert client.post("/mandates/catalogue", json=dupes).status_code == 422


def test_draft_validation(client: TestClient) -> None:
    _publish(client)
    unknown = {**FOUNDER_EXAMPLE, "approved_templates": ["tpl_missing"]}
    assert client.post("/mandates", json=unknown).status_code == 422
    both = {**FOUNDER_EXAMPLE, "budget_ceiling": 10}
    assert client.post("/mandates", json=both).status_code == 422
    half_hour = {**FOUNDER_EXAMPLE, "quiet_hours": {"start": "21:30", "end": "09:00"}}
    resp = client.post("/mandates", json=half_hour)
    assert resp.status_code == 422 and "whole hour" in resp.json()["detail"]
    bare = {k: v for k, v in FOUNDER_EXAMPLE.items() if k != "budget_ceiling_inr"}
    resp = client.post("/mandates", json={**bare, "budget_ceiling": 500_000})
    assert resp.status_code == 422 and "needs a currency" in resp.json()["detail"]
    resp = client.post(
        "/mandates", json={**bare, "mandate_id": "m_usd", "budget_ceiling": 5_000, "currency": "USD"}
    )
    assert resp.status_code == 201 and resp.json()["mandate"]["currency"] == "USD"
    caps = {**FOUNDER_EXAMPLE, "frequency_cap": {"max_touches": 3, "max_per_customer": 2}}
    assert client.post("/mandates", json=caps).status_code == 422
    assert client.post("/mandates", json={**FOUNDER_EXAMPLE, "mandate_id": "catalogue"}).status_code == 400
    assert client.post("/mandates", json=FOUNDER_EXAMPLE).status_code == 201
    assert client.post("/mandates", json=FOUNDER_EXAMPLE).status_code == 409
    assert client.get("/mandates/nope").status_code == 404
