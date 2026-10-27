"""Opt-outs in Indian languages (item 4): "band karo" and "बंद करो" unsubscribe at once."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autonomy.agents.conversation import Conversation, stop_keyword
from autonomy.channels.consent import ConsentProof, InMemoryConsentRegistry, evidence_hash, normalize_address
from autonomy.channels.gateway import DispatchGateway, Provider
from autonomy.channels.optout import (
    OptOutLexicon,
    default_lexicon,
    load_lexicon,
    normalize_words,
    opt_out_phrase,
)
from autonomy.channels.whatsapp import OPT_OUT_REASON, WhatsAppCloudAdapter, sign_body
from autonomy.contracts import Channel, EventType
from autonomy.fakes import InMemoryEventStore, KeywordTextSignals

PNID = "106540352242922"
WA_ID = "15550001111"
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)

OPT_OUTS = [
    # the spec's list, as typed
    "STOP",
    "unsubscribe",
    "band karo",
    "band kar do",
    "mat bhejo",
    "nahi chahiye",
    "बंद करो",
    "मत भेजो",
    "नहीं चाहिए",
    "रोको",
    # case, spacing and punctuation do not matter
    "Stop!",
    "  UNSUBSCRIBE.  ",
    "BAND KARO!!!",
    "Band   Kar   Do",
    "mat-bhejo",
    "Nahi Chahiye...",
    "बंद करो!",
    "मत भेजो।",
    "बंद  करो",
    "bandkaro",
    "s t o p",
    # a clear phrase inside a longer message
    "please band karo ab",
    "mujhe ye messages nahi chahiye",
    "कृपया बंद करो",
    "Hi, mat bhejo yaar",
]

NOT_OPT_OUTS = [
    "thanks, send me the link",
    "mujhe offer chahiye",  # "I want the offer": chahiye alone is not an opt-out
    "yes bhejo",
    "band hai kya?",  # "is it closed?"
    "stopover in Delhi",  # a word that merely starts with stop
    "रोकोगे",  # a different word
    "",
    "   ",
    "👍",
]


@pytest.mark.parametrize("text", OPT_OUTS)
def test_opt_out_phrases_are_recognised(text: str) -> None:
    assert opt_out_phrase(text) is not None, text


@pytest.mark.parametrize("text", NOT_OPT_OUTS)
def test_other_replies_are_not_opt_outs(text: str) -> None:
    assert opt_out_phrase(text) is None, text


def test_words_are_cut_on_letters_not_on_vowel_signs() -> None:
    # A regular expression's \b would split "बंद" at its anusvara; the lexicon must not.
    assert normalize_words("बंद,  करो!") == "बंद करो"
    assert opt_out_phrase("xबंद करो") is None  # glued to another letter: not the word


def test_a_language_is_added_in_config_without_code(tmp_path: Path) -> None:
    path = tmp_path / "phrases.yaml"
    path.write_text("languages:\n  english: [stop]\n  tamil: ['நிறுத்து', 'vendam']\n", encoding="utf-8")
    lexicon = load_lexicon(path)
    assert opt_out_phrase("நிறுத்து", lexicon) == "நிறுத்து" and lexicon.language_of("vendam") == "tamil"
    assert opt_out_phrase("band karo", lexicon) is None  # only what this file lists
    assert opt_out_phrase("band karo") is not None
    bad = tmp_path / "bad.yaml"
    bad.write_text("languages:\n  hindi: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_lexicon(bad)


def test_the_shipped_lexicon_names_each_language() -> None:
    langs = {lang for _p, lang in default_lexicon().phrases}
    assert {"english", "hinglish", "hindi"} <= langs
    assert isinstance(default_lexicon(), OptOutLexicon)


# --- the conversation agent's keyword net --------------------------------------------------------


@pytest.mark.parametrize("text", ["band karo", "बंद करो", "nahi chahiye", "मत भेजो"])
def test_the_conversation_agent_reads_them_as_stop(text: str) -> None:
    assert stop_keyword(text) is not None
    intent, confidence, matched, _why = Conversation(KeywordTextSignals()).classify(text)
    assert (intent, confidence) == ("stop", 1.0) and matched


# --- the adapter: an UNSUBSCRIBE next to the REPLY -----------------------------------------------


def inbound(text: str, *, kind: str = "text", wamid: str = "wamid.IN1") -> bytes:
    message: dict[str, object] = {
        "from": WA_ID,
        "id": wamid,
        "timestamp": str(int(NOW.timestamp())),
        "type": kind,
    }
    if kind == "text":
        message["text"] = {"body": text}
    else:
        message["button"] = {"text": text, "payload": "stop_promotions"}
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": PNID},
                                "messages": [message],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


def test_an_opt_out_reply_yields_an_unsubscribe_and_a_consent_revocation() -> None:
    events = WhatsAppCloudAdapter(PNID, "t", "secret").parse_webhook({}, inbound("Band karo"))
    assert [e.type for e in events] == [EventType.REPLY, EventType.UNSUBSCRIBE, EventType.CONSENT_REVOKED]
    unsub = events[1]
    assert unsub.channel is Channel.WHATSAPP and unsub.customer_id == events[0].customer_id
    assert unsub.attrs["reason"] == OPT_OUT_REASON and unsub.attrs["address"] == WA_ID
    assert unsub.attrs["matched"] == "band karo"
    assert unsub.ts == events[0].ts  # stamped with the reply's time, not the processing time


def test_a_quick_reply_button_saying_stop_counts_too() -> None:
    events = WhatsAppCloudAdapter(PNID, "t", "s").parse_webhook({}, inbound("बंद करो", kind="button"))
    assert EventType.UNSUBSCRIBE in {e.type for e in events}


def test_an_ordinary_reply_is_only_a_reply() -> None:
    events = WhatsAppCloudAdapter(PNID, "t", "s").parse_webhook({}, inbound("what is the price?"))
    assert [e.type for e in events] == [EventType.REPLY]


def test_the_same_webhook_twice_makes_the_same_events() -> None:
    wa = WhatsAppCloudAdapter(PNID, "t", "s")
    first, second = wa.parse_webhook({}, inbound("STOP")), wa.parse_webhook({}, inbound("STOP"))
    assert [e.event_id for e in first] == [e.event_id for e in second]
    store = InMemoryEventStore()
    assert store.append(first) == 3 and store.append(second) == 0


def test_the_gateway_suppresses_the_number_and_revokes_its_consent_while_handling_the_webhook() -> None:
    proof = ConsentProof(
        consent_id="cons_wa",
        purpose="promotional",
        channel=Channel.WHATSAPP,
        captured_at=NOW - timedelta(days=30),
        source="checkout",
        evidence_hash=evidence_hash("opt-in at checkout"),
        recipient="+15550001111",
    )
    registry = InMemoryConsentRegistry([proof])
    wa = WhatsAppCloudAdapter(PNID, "t", "secret")
    events = InMemoryEventStore()
    gw = DispatchGateway({Channel.WHATSAPP: [Provider("whatsapp", wa)]}, consent=registry, events=events)
    assert registry.find(
        Channel.WHATSAPP, normalize_address(Channel.WHATSAPP, "+15550001111"), "promotional", NOW
    )

    body = inbound("band kar do")
    outcome = gw.handle_webhook("whatsapp", {"X-Hub-Signature-256": sign_body("secret", body)}, body, NOW)

    assert outcome.suppressed == 1 and outcome.stored == 3
    assert (Channel.WHATSAPP, WA_ID) in gw.suppressed
    assert registry.find(Channel.WHATSAPP, WA_ID, "promotional", NOW + timedelta(seconds=1)) is None
    stored = registry.get("cons_wa")
    assert stored is not None and stored.revoked_at == NOW  # the opt-out is on the consent record
    kinds = {e.type for e in events.for_customer(WA_ID)}
    assert {EventType.UNSUBSCRIBE, EventType.CONSENT_REVOKED, EventType.REPLY} <= kinds
