"""Compliance packs (India, US, EU/UK), the Critic with packs, and the studio's assemble endpoint."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time

import pytest

from api.v2.engines.studio import AssembleRequest, assemble, get_judge_llm, get_template_library, router
from autonomy.agents.base import ScriptedLLM
from autonomy.agents.critic import RUBRIC, BrandRules, Critic, check_rules
from autonomy.compliance_packs import (
    CombinedPack,
    CompliancePack,
    DltRegistration,
    EUPack,
    IndiaPack,
    PackCheck,
    PackSettings,
    Purpose,
    QuietWindow,
    USPack,
    pack_for,
    packs_for,
)
from autonomy.compliance_packs.base import covering_window
from autonomy.contracts import Channel, ContentItem, ContentStatus, OfferKind, OfferOption, Theme
from autonomy.fakes.samples import TEN_OFF

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)

DLT_TEMPLATE_ID = "1207161234567890123"
DLT = DltRegistration(
    template_id=DLT_TEMPLATE_ID,
    header="123456",
    entity_id="1201159876543210987",
    text="Hi {#var#}, new {#var#} just landed. Reply STOP to opt out.",
)
DLT_NO_OPT_OUT = DltRegistration(
    template_id="1207161234567890999",
    header="123456",
    entity_id="1201159876543210987",
    text="Hi {#var#}, new {#var#} just landed.",
)
SETTINGS = PackSettings(dlt_templates=(DLT, DLT_NO_OPT_OUT))


def _item(**overrides: object) -> ContentItem:
    base: dict[str, object] = {
        "content_id": "ci_sms",
        "channel": Channel.SMS,
        "theme": Theme.REMINDER,
        "offer_id": "none",
        "body": "Hi {{first_name}}, new {{top_category}} just landed. Reply STOP to opt out.",
        "slots": ("first_name", "top_category"),
        "created_at": NOW,
    }
    base.update(overrides)
    return ContentItem.model_validate(base)


def _email(body: str, subject: str = "New in for you") -> ContentItem:
    return _item(content_id="ci_email", channel=Channel.EMAIL, subject=subject, body=body)


def _codes(checks: list[PackCheck]) -> dict[str, bool]:
    return {c.code: c.passed for c in checks}


# --- packs ---------------------------------------------------------------------------------------


def test_every_shipped_pack_meets_the_protocol() -> None:
    for pack in (IndiaPack(), USPack(), EUPack(), pack_for(None)):
        assert isinstance(pack, CompliancePack)


def test_india_quiet_hours_bind_promotional_sms_only() -> None:
    india = IndiaPack()
    assert india.quiet_window(Channel.SMS) == QuietWindow(21, 9)
    assert india.quiet_hours(time(21, 0)) and india.quiet_hours(time(8, 59))
    assert not india.quiet_hours(time(9, 0)) and not india.quiet_hours(time(20, 59))
    assert india.quiet_window(Channel.SMS, Purpose.TRANSACTIONAL) is None
    assert india.quiet_window(Channel.EMAIL) is None
    assert india.quiet_hours(time(22, 0), "voice")


def test_india_ncpr_and_dpdp_consent() -> None:
    india = IndiaPack()
    assert india.needs_dnd_check(Channel.SMS, Purpose.PROMOTIONAL)
    assert india.needs_dnd_check("voice")
    assert not india.needs_dnd_check(Channel.SMS, Purpose.TRANSACTIONAL)
    assert not india.needs_dnd_check(Channel.EMAIL)
    assert "NCPR" in india.dnd_registry()
    whatsapp = india.consent_requirements(Channel.WHATSAPP)
    assert whatsapp.kind == "explicit" and whatsapp.channel_specific and not whatsapp.pre_ticked_allowed
    assert india.consent_requirements(Channel.SMS, Purpose.TRANSACTIONAL).kind == "none"
    assert [d.code for d in india.required_disclosures(Channel.WHATSAPP)] == ["opt_out"]
    assert india.required_disclosures(Channel.PUSH) == ()


def test_india_sms_must_be_a_registered_dlt_template_with_ids() -> None:
    ok = IndiaPack(SETTINGS).content_checks(_item())
    assert all(c.passed for c in ok), ok
    assert {"dlt:registered_template", "dlt:header", "dlt:entity_id", "dlt:template_match"} <= _codes(
        ok
    ).keys()

    # No registration at all, or one whose literal text differs.
    assert not _codes(IndiaPack().content_checks(_item()))["dlt:registered_template"]
    changed = _item(body="Hello {{first_name}}, new {{top_category}} just landed. Reply STOP to opt out.")
    assert not _codes(IndiaPack(SETTINGS).content_checks(changed))["dlt:registered_template"]

    # Named by id, with bad ids and a bad placeholder.
    broken = DltRegistration(
        template_id="42",
        header="SHOPCO",
        entity_id="",
        text="Hi {{first_name}}, {#name#}. Reply STOP to opt out.",
    )
    named = _item(template_name="42")
    codes = _codes(IndiaPack(PackSettings(dlt_templates=(broken,))).content_checks(named))
    assert not codes["dlt:header"]  # promotional headers are six digits
    assert not codes["dlt:entity_id"]
    assert not codes["dlt:template_id"]
    assert not codes["dlt:placeholder_format"]


def test_india_rendered_sms_keeps_each_variable_within_30_characters() -> None:
    india = IndiaPack(SETTINGS)
    good = india.rendered_checks("Hi Asha, new kurtas just landed. Reply STOP to opt out.", _item())
    assert [c.passed for c in good] == [True]
    long = "Hi Asha, new " + "x" * 31 + " just landed. Reply STOP to opt out."
    [bad] = india.rendered_checks(long, _item())
    assert not bad.passed and "30 characters" in bad.detail
    assert india.rendered_checks("anything", _item(channel=Channel.WHATSAPP, template_name="t")) == []


def test_us_tcpa_and_can_spam() -> None:
    us = USPack()
    assert us.quiet_window(Channel.SMS) == QuietWindow(21, 8)
    assert us.quiet_hours(time(7, 59)) and not us.quiet_hours(time(8, 0)) and us.quiet_hours(time(21, 0))
    assert us.needs_dnd_check(Channel.SMS) and not us.needs_dnd_check(Channel.EMAIL)
    sms = us.consent_requirements(Channel.SMS)
    assert sms.kind == "explicit" and sms.written
    assert us.consent_requirements(Channel.EMAIL).kind == "opt_out"
    assert us.content_problems(_item()) == []
    assert any("STOP" in p for p in us.content_problems(_item(body="New {{top_category}} for you.")))
    assert any("postal address" in p for p in us.content_problems(_email("New in. Unsubscribe any time.")))
    assert (
        us.content_problems(_email("New in. Unsubscribe any time. Shop Co, 1 Main St, Austin, TX 78701."))
        == []
    )

    exact = USPack(PackSettings(postal_address="1 Main St, Austin, TX 78701"))
    assert exact.content_problems(_email("New. Unsubscribe any time. 1 Main St,  Austin, TX 78701")) == []
    assert exact.content_problems(_email("New. Unsubscribe any time. 9 Elm St, Reno, NV 89501")) != []


def test_eu_gdpr_consent_unsubscribe_link_and_no_pre_ticked_box() -> None:
    eu = EUPack()
    assert pack_for("de").name == pack_for("GB").name == pack_for("uk").name == eu.name
    assert eu.quiet_window(Channel.SMS) is None
    email = eu.consent_requirements(Channel.EMAIL)
    assert email.kind == "explicit" and not email.pre_ticked_allowed
    assert not eu.needs_dnd_check(Channel.SMS)
    assert any("unsubscribe link" in p for p in eu.content_problems(_email("Hi. Unsubscribe any time.")))
    assert eu.content_problems(_email("Hi. Unsubscribe: https://shop.example.com/u")) == []
    ticked = _item(channel=Channel.WEB, body="[x] Yes, send me offers")
    assert any("ticked" in p for p in eu.content_problems(ticked))
    assert eu.content_problems(_item()) == []


def test_unknown_country_gets_the_strictest_of_all_packs() -> None:
    strict = pack_for("ZZ", SETTINGS)
    assert isinstance(strict, CombinedPack)
    assert pack_for(None).name == strict.name
    assert strict.quiet_window(Channel.SMS) == QuietWindow(21, 9)  # India's 9am and the US's 9pm
    assert strict.needs_dnd_check(Channel.SMS)
    consent = strict.consent_requirements(Channel.EMAIL)
    assert consent.kind == "explicit" and not consent.pre_ticked_allowed
    rules = strict.sms_template_rules
    assert rules is not None and rules.requires_registered_template and rules.opt_out_keyword == "STOP"
    codes = {d.code for d in strict.required_disclosures(Channel.EMAIL)}
    assert codes == {"opt_out", "unsubscribe", "postal_address", "unsubscribe_link"}
    assert pack_for("IN").name == "IN" and pack_for(" us ").name == "US"
    assert [p.name for p in packs_for(["IN", "in", "US"])] == ["IN", "US"]


def test_covering_window_widens_never_narrows() -> None:
    assert covering_window([]) is None
    assert covering_window(QuietWindow(21, 9).hours() | QuietWindow(22, 8).hours()) == QuietWindow(21, 9)
    assert covering_window({1, 5}) == QuietWindow(1, 6)
    with pytest.raises(ValueError, match="every hour"):
        covering_window(range(24))


# --- the Critic with packs -----------------------------------------------------------------------


def test_critic_with_india_pack_rejects_promotional_sms_without_opt_out() -> None:
    judge = ScriptedLLM(lambda _s, _p: "{}", model_id="other-family")
    no_opt_out = _item(body="Hi {{first_name}}, new {{top_category}} just landed.")
    india = BrandRules(countries=("IN",), pack_settings=SETTINGS)
    [reviewed] = Critic(judge).review([no_opt_out], india)
    assert reviewed.status is ContentStatus.REJECTED
    assert any("rule: IN:" in n and "opt out" in n for n in reviewed.critic_notes), reviewed.critic_notes
    assert judge.calls == []
    # The same item with no country named: unchanged behaviour, the brand rules alone.
    assert check_rules(no_opt_out, BrandRules()) == []
    assert check_rules(_item(), india) == []
    # A transactional SMS needs no opt-out line, but still its registered template.
    assert check_rules(no_opt_out, india.model_copy(update={"purpose": Purpose.TRANSACTIONAL})) != []


def test_critic_packs_can_be_passed_directly_and_from_brief_keeps_defaults() -> None:
    from autonomy.fakes.samples import sample_brief

    rules = BrandRules.from_brief(sample_brief(NOW), brand_voice="Plain.")
    assert rules.countries == () and rules.compliance_packs() == ()
    email = _email("New in {{top_category}}. Unsubscribe any time.")
    assert check_rules(email, rules) == []
    assert any(p.startswith("US:") for p in check_rules(email, rules, packs=[USPack()]))
    us_rules = BrandRules.from_brief(sample_brief(NOW), brand_voice="Plain.", countries=["US"])
    assert any("postal address" in p for p in check_rules(email, us_rules))


# --- the studio ----------------------------------------------------------------------------------


WHATSAPP = ContentItem(
    content_id="ci_wa_savings",
    channel=Channel.WHATSAPP,
    theme=Theme.SAVINGS,
    offer_id=TEN_OFF.offer_id,
    body="Hi {{first_name}}, {{offer_label}} on {{top_category}} this week. Reply STOP to opt out.",
    slots=("first_name", "offer_label", "top_category"),
    template_name="winback_savings_1",
    status=ContentStatus.APPROVED,
    created_at=NOW,
)
FACTS: dict[str, str | int | float | bool | None] = {
    "first_name": "Asha",
    "name": "Asha Rao",
    "email": "asha@example.com",
    "phone": "+91 98765 43210",
    "top_category": "kurtas",
    "timezone": "Asia/Kolkata",
}


def _request(**overrides: object) -> AssembleRequest:
    base: dict[str, object] = {
        "template": WHATSAPP,
        "customer_facts": FACTS,
        "channel": Channel.WHATSAPP,
        "creative_arm": Theme.SAVINGS,
        "country": "IN",
        "offer": TEN_OFF,
        "now": NOW,
    }
    base.update(overrides)
    return AssembleRequest.model_validate(base)


def test_assemble_certifies_a_good_whatsapp_payload() -> None:
    out = assemble(_request())
    assert out.status == "CERTIFIED", out.reasons
    payload = out.rendered_payload
    assert payload is not None
    assert payload.body == "Hi Asha, 10% off on kurtas this week. Reply STOP to opt out."
    assert payload.template_name == "winback_savings_1"
    assert payload.template_params == ["Asha", "10% off", "kurtas"]
    audit = out.critic_audit
    assert audit.pack == "IN" and audit.pack_checks and all(c.passed for c in audit.pack_checks)
    assert audit.slot_check.passed and audit.slot_check.fallbacks == []
    assert audit.pii_leaks == []
    assert audit.hallucination_check.passed and audit.hallucination_check.numbers == ["10"]
    assert audit.judge.requested is False


def test_assemble_rejects_an_invented_number() -> None:
    invented = WHATSAPP.model_copy(
        update={
            "body": "Hi {{first_name}}, {{offer_label}} on {{top_category}} for 48 hours. Reply STOP to opt out."
        }
    )
    out = assemble(_request(template=invented))
    assert out.status == "REJECTED"
    assert out.critic_audit.hallucination_check.unsupported == ["48"]
    assert any("48" in r for r in out.reasons)
    # Vouched for by the brand, the same number is fine.
    assert assemble(_request(template=invented, allowed_numbers=(48,))).status == "CERTIFIED"


def test_assemble_catches_pii_and_missing_opt_out() -> None:
    leaky = _request(customer_facts={**FACTS, "first_name": "asha@example.com"})
    out = assemble(leaky)
    assert out.status == "REJECTED"
    assert {leak.fact for leak in out.critic_audit.pii_leaks} >= {"email"}
    no_opt_out = WHATSAPP.model_copy(update={"body": "Hi {{first_name}}, {{offer_label}} this week."})
    out = assemble(_request(template=no_opt_out))
    assert out.status == "REJECTED"
    assert any("opt out" in r for r in out.reasons)
    # The US pack has no WhatsApp opt-out rule.
    assert assemble(_request(template=no_opt_out, country="US")).status == "CERTIFIED"


def test_assemble_india_sms_carries_the_dlt_header_and_falls_back_safely() -> None:
    sms = _item()
    out = assemble(
        _request(
            template=sms,
            channel=Channel.SMS,
            creative_arm=None,
            offer=None,
            pack_settings=SETTINGS,
            customer_facts={"name": "Ravi Kumar"},
        )
    )
    assert out.status == "CERTIFIED", out.reasons
    assert out.rendered_payload is not None and out.rendered_payload.header == "123456"
    assert out.rendered_payload.body.startswith("Hi Ravi, new our latest picks")
    assert out.critic_audit.slot_check.fallbacks == ["top_category"]


def test_assemble_picks_by_template_id_or_arm_and_refuses_unknown_slots() -> None:
    library = [WHATSAPP, WHATSAPP.model_copy(update={"content_id": "ci_wa_ship", "theme": Theme.CONVENIENCE})]
    assert (
        assemble(_request(template=None, template_id="ci_wa_savings"), library).content_id == "ci_wa_savings"
    )
    by_arm = assemble(_request(template=None, creative_arm=Theme.SAVINGS), library)
    assert by_arm.content_id == "ci_wa_savings"
    odd = WHATSAPP.model_copy(
        update={"body": "Hi {{store_name}}. Reply STOP to opt out.", "offer_id": "none"}
    )
    out = assemble(_request(template=odd, offer=None))
    assert (
        out.status == "REJECTED" and not out.critic_audit.slot_check.passed and out.rendered_payload is None
    )


def test_assemble_judge_flag_fails_closed_without_a_judge_and_runs_with_one() -> None:
    assert assemble(_request(judge=True)).status == "REJECTED"
    verdict = json.dumps({"scores": [{"criterion": c, "score": 5, "reason": "fine"} for c in RUBRIC]})
    llm = ScriptedLLM(lambda _s, _p: verdict, model_id="other-family")
    out = assemble(_request(judge=True), judge_llm=llm)
    assert out.status == "CERTIFIED" and out.critic_audit.judge.ran and len(llm.calls) == 2


def test_assemble_route() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router, prefix="/v2/engines")
    app.dependency_overrides[get_template_library] = lambda: [WHATSAPP]
    app.dependency_overrides[get_judge_llm] = lambda: None
    client = TestClient(app)
    body = {
        "template_id": "ci_wa_savings",
        "customer_facts": FACTS,
        "channel": "whatsapp",
        "creative_arm": "savings",
        "country": "IN",
        "offer": TEN_OFF.model_dump(mode="json"),
        "now": NOW.isoformat(),
    }
    ok = client.post("/v2/engines/studio/assemble", json=body)
    assert ok.status_code == 200 and ok.json()["status"] == "CERTIFIED", ok.json()
    assert client.post("/v2/engines/studio/assemble", json={**body, "template_id": "nope"}).status_code == 404
    other = {
        **body,
        "offer": OfferOption(offer_id="x", label="x", kind=OfferKind.GIFT).model_dump(mode="json"),
    }
    assert client.post("/v2/engines/studio/assemble", json=other).json()["status"] == "REJECTED"


# --- review regressions --------------------------------------------------------------------------


def test_us_address_guess_needs_a_real_state_and_zip_in_capitals() -> None:
    us = USPack()
    # 'to 10000' is not 'TO 10000': ordinary copy must not pass for a postal address.
    assert any(
        "postal address" in p for p in us.content_problems(_email("Unsubscribe. Win up to 10000 points."))
    )
    assert any("postal address" in p for p in us.content_problems(_email("Unsubscribe. Shop Co, XX 78701.")))
    assert us.content_problems(_email("Unsubscribe any time. Shop Co, PO Box 12, Reno.")) == []


def test_eu_check_mark_emoji_is_not_a_pre_ticked_box() -> None:
    eu = EUPack()
    assert eu.content_problems(_email("✅ Free delivery. Unsubscribe: https://shop.example.com/u")) == []
    assert eu.content_problems(_email("☑ I agree. Unsubscribe: https://shop.example.com/u")) != []


def test_assemble_never_certifies_a_rejected_or_retired_template() -> None:
    for status in (ContentStatus.REJECTED, ContentStatus.RETIRED):
        out = assemble(_request(template=WHATSAPP.model_copy(update={"status": status})))
        assert out.status == "REJECTED" and any(status.value in r for r in out.reasons)


def test_assemble_india_sms_must_use_the_registered_header() -> None:
    sms = _request(
        template=_item(),
        channel=Channel.SMS,
        creative_arm=None,
        offer=None,
        pack_settings=SETTINGS,
        customer_facts={"name": "Ravi Kumar"},
    )
    assert assemble(sms.model_copy(update={"sender_id": "123456"})).status == "CERTIFIED"
    out = assemble(sms.model_copy(update={"sender_id": "999999"}))
    assert out.status == "REJECTED"
    assert any(c.code == "dlt:sender_header" and not c.passed for c in out.critic_audit.pack_checks)


def test_assemble_identifier_digits_back_no_number() -> None:
    # The customer's phone number holds 43210; that must not vouch for '43210 sold'.
    claim = WHATSAPP.model_copy(
        update={
            "body": "Hi {{first_name}}, {{offer_label}} on {{top_category}}: 43210 sold. Reply STOP to opt out."
        }
    )
    out = assemble(_request(template=claim))
    assert out.status == "REJECTED" and out.critic_audit.hallucination_check.unsupported == ["43210"]


def test_assemble_render_failure_reports_the_slot_not_a_number() -> None:
    odd = WHATSAPP.model_copy(
        update={"body": "Hi {{store_name}}. Reply STOP to opt out.", "offer_id": "none"}
    )
    out = assemble(_request(template=odd, offer=None))
    assert out.status == "REJECTED" and not out.critic_audit.hallucination_check.passed
    assert all(r.startswith("slot: ") for r in out.reasons), out.reasons


def test_assemble_judge_is_not_reported_as_run_when_the_rules_reject_first() -> None:
    llm = ScriptedLLM(lambda _s, _p: "{}", model_id="other-family")
    out = assemble(_request(judge=True, allowed_domains=(), cta_url=None, offer=None), judge_llm=llm)
    assert out.status == "REJECTED" and not out.critic_audit.judge.ran and llm.calls == []
