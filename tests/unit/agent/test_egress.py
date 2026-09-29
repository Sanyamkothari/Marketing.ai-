"""The egress gate (Plan G §7.6): scanners, shapes, aliases, default-deny, hidden columns, the cap."""

from __future__ import annotations

import json
import random
import string
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine.agent.config import DataAccess
from engine.agent.contracts import ChatMessage, ChatRole, SentItem
from engine.agent.egress import (
    HIDDEN_BY_SETTINGS,
    MAX_SENT_ITEMS,
    MAX_SENT_PREVIEW_CHARS,
    MAX_SENT_SESSION_CHARS,
    MAX_SENT_SESSION_ITEMS,
    PERSONAL_DATA,
    SAFE_KEYS,
    SENT_DROPPED,
    SHAPE_ALPHABET,
    Egress,
    alias_table,
    assert_clean,
    cap_sent,
    is_plain_label,
    is_third_party,
    mask_value,
    prepare,
    safe_column_label,
    scrub,
    shape_of,
    unalias,
)
from engine.utils.time import utc_now

FAKE_KEY = (
    "zk_live_51HxAbCdEf123456789012345"  # secret-scan: allow (a fake key the masking tests plant on purpose)
)


MASKED = DataAccess.MASKED_DATA
SUMMARIES = DataAccess.SUMMARIES_ONLY


# ---------------------------------------------------------------------------
# Scanners: each kind, and what it must leave alone
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("jane.roe@example.test", "[REDACTED:email]"),
        ("+91 98765 43210", "[REDACTED:phone]"),
        ("ABCDE1234F", "[REDACTED:pan]"),
        ("4111 1111 1111 1111", "[REDACTED:card]"),
        ("4111-1111-1111-1111", "[REDACTED:card]"),
        ("4111111111111111", "[REDACTED:card]"),
        ("378282246310005", "[REDACTED:card]"),  # 15 digits (Amex test number)
        ("IN12 3456 7890", "[REDACTED:iban]"),
        ("GB82 WEST 1234 5698 7654 32", "[REDACTED:iban]"),
        ("DE89370400440532013000", "[REDACTED:iban]"),
        ("192.0.2.44", "[REDACTED:ip]"),
        ("2001:db8::8a2e:370:7334", "[REDACTED:ip]"),
        ("::1", "[REDACTED:ip]"),
        ("https://example.test/u?token=abc123def456ghi789", "[REDACTED:url]"),
        ("www.example.test/path", "[REDACTED:url]"),
        ("example.test/account/42", "[REDACTED:url]"),
        (FAKE_KEY, "[REDACTED:token]"),
        ("abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),  # long, but no digit: a word, not a key
        ("a1B2c3D4e5F6g7H8i9J0k1L2", "[REDACTED:token]"),
        ("123456789", "[REDACTED:phone]"),  # a plain run is a phone shape first (engine.pii)
        ("12 34 56 78 9", "[REDACTED:number]"),  # ...and the number scanner takes what that leaves
        ("12-3456-78-90", "[REDACTED:number]"),
        ("1 2 3 4 5 6 7 8 9 0", "[REDACTED:number]"),
    ],
)
def test_each_scanner_masks_its_kind(text: str, marker: str) -> None:
    assert mask_value(text) == marker


def test_the_card_scanner_needs_a_luhn_valid_number() -> None:
    assert mask_value("4111 1111 1111 1111") == "[REDACTED:card]"
    # The same shape with a wrong check digit is not a card - but sixteen digits are still a number.
    assert "[REDACTED:" in mask_value("4111 1111 1111 1112") and "card" not in mask_value(
        "4111 1111 1111 1112"
    )
    assert mask_value("1234 56") == "1234 56"  # six digits: not a card, not a number


def test_a_card_is_labelled_a_card_not_a_phone() -> None:
    """`engine.pii`'s phone shape would take a card first; the structured scanners run before it."""
    assert "phone" not in mask_value("card 4111 1111 1111 1111 on file")
    assert mask_value("card 4111 1111 1111 1111 on file") == "card [REDACTED:card] on file"


def test_numbers_of_nine_or_more_digits_are_masked_and_short_ones_kept() -> None:
    assert mask_value("order 123456") == "order 123456"  # six digits (seven or eight read as a phone number)
    assert "123456789" not in mask_value("order 123456789")
    assert mask_value("id 1 2 3 4 5 6 7 8 9") == "id [REDACTED:number]"
    assert mask_value("3,000 of 2,998 rows") == "3,000 of 2,998 rows"  # thousands separators


@pytest.mark.parametrize(
    "text",
    [
        "2024-01-31",
        "2024-01-31 12:30:00",
        "2024-01-31T12:30:00.123456+05:30",
        "31/01/2024",
        "31.01.2024",
        "2024/01/31",
        "12345.678",
        "0.123456",
        "1234567.89",
        "550e8400-e29b-41d4-a716-446655440000",
        "12:30:45",
        "campaign_summer_promo_2024_q3",
    ],
)
def test_dates_short_decimals_uuids_and_readable_names_are_kept(text: str) -> None:
    assert mask_value(text) == text


def test_a_date_of_birth_is_not_guessed() -> None:
    assert mask_value("born 1985-03-12") == "born 1985-03-12"


@pytest.mark.parametrize(
    "text",
    [
        "98765.43210",  # two five-digit groups with a dot is a phone written oddly
        "1234567890.5",  # ten digits before the point
        "555.555.0001",
    ],
)
def test_a_phone_or_long_number_is_not_saved_by_its_decimal_point(text: str) -> None:
    assert "[REDACTED:" in mask_value(text)


def test_an_address_that_is_not_an_ip() -> None:
    assert mask_value("300.1.2.3") == "300.1.2.3"  # an octet above 255
    assert mask_value("time 12:30:45") == "time 12:30:45"  # not an IPv6 address
    assert mask_value("version 1.2") == "version 1.2"


def test_invisible_characters_and_full_width_digits_do_not_hide_a_value() -> None:
    assert mask_value("jane​.roe@example.test") == "[REDACTED:email]"
    assert mask_value("４１１１ 1111 1111 1111") == "[REDACTED:card]"
    assert mask_value("jane.roe＠example.test") == "[REDACTED:email]"


def test_a_long_free_text_cell_is_masked_whole_then_cut() -> None:
    cell = "Thanks for the help. " * 3 + "Mail jane.roe@example.test or call +91 98765 43210 " + "x " * 60
    shown = mask_value(cell)
    assert len(shown) <= 81
    assert "jane" not in shown and "98765" not in shown
    # The cut never leaves half a marker.
    padded = "a" * 70 + " jane.roe@example.test"
    assert "[REDACTED" not in mask_value(padded) or mask_value(padded).count("[REDACTED:") == mask_value(
        padded
    ).count("]")
    assert not mask_value(padded).endswith("[REDACTED:em…")


def test_a_name_in_a_sentence_is_not_caught_and_that_is_stated() -> None:
    """Masking by pattern cannot know a name (docs/AGENTS.md §7.7); `summaries_only` is the answer."""
    sentence = "Jane Roe asked about 221B Baker Street"
    assert mask_value(sentence) == sentence
    assert "Jane" not in mask_value(sentence, SUMMARIES)


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------
def test_a_shape_keeps_the_format_and_none_of_the_value() -> None:
    assert shape_of("Jane 2024") == "Aaaa 9999"
    assert shape_of("Aaaaaaaa 9999999") == "Aaaaa 9999"  # runs collapse to at most four
    assert shape_of("jane.roe@example.test") == "aaaa.aaa@aaaa.aaaa"
    assert shape_of("+91 98765 43210") == "+99 9999 9999"
    assert shape_of("x" * 500) == "aaaa"
    assert shape_of("é ü 日本") == "a a aa"
    assert len(shape_of("Aa9 " * 100)) <= 33


def test_a_shape_only_uses_its_own_alphabet() -> None:
    for text in ["Jane Roe", "３１ 日本語", "a\x00b", "‮evil", "tab\there", "emoji \U0001f600"]:
        assert SHAPE_ALPHABET.fullmatch(shape_of(text)), text


def test_summaries_only_returns_the_shape_of_every_cell() -> None:
    assert mask_value("Jane Roe", SUMMARIES) == "Aaaa Aaa"
    assert mask_value("jane.roe@example.test", SUMMARIES) == "aaaa.aaa@aaaa.aaaa"


# ---------------------------------------------------------------------------
# Property tests: nothing built from a scanner's alphabet survives, and masking is stable
# ---------------------------------------------------------------------------
def _luhn_number(rng: random.Random, length: int) -> str:
    body = [rng.randrange(10) for _ in range(length - 1)]
    total = 0
    for index, digit in enumerate(reversed(body)):
        value = digit * 2 if index % 2 == 0 else digit
        total += value - 9 if value > 9 else value
    return "".join(map(str, body)) + str((10 - total % 10) % 10)


def _joined(rng: random.Random, digits: str, groups: int) -> str:
    size = max(1, len(digits) // groups)
    pieces = [digits[i : i + size] for i in range(0, len(digits), size)]
    return rng.choice([" ", "-", ""]).join(pieces)


@pytest.mark.parametrize("seed", range(40))
def test_random_secrets_never_survive_mask_value(seed: int) -> None:
    rng = random.Random(seed)
    alnum = string.ascii_letters + string.digits
    card = _luhn_number(rng, rng.randrange(13, 20))
    number = "".join(rng.choice(string.digits) for _ in range(rng.randrange(9, 22)))
    token = "".join(rng.choice(alnum) for _ in range(rng.randrange(20, 60)))
    if not (any(c.isdigit() for c in token) and any(c.isalpha() for c in token)):
        token = "aB3" + token
    ip4 = ".".join(str(rng.randrange(256)) for _ in range(4))
    ip6 = ":".join(f"{rng.randrange(0x10000):x}" for _ in range(8))
    local = "".join(
        rng.choice(string.ascii_lowercase + string.digits + "._-") for _ in range(rng.randrange(3, 20))
    )
    email = f"{local}@{rng.choice(['example.test', 'mail.example.org'])}"
    iban = (
        rng.choice(["GB", "DE", "IN", "FR"])
        + f"{rng.randrange(100):02d}"
        + "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(rng.randrange(8, 25)))
    )
    if sum(c.isdigit() for c in iban) < 6:
        iban += "123456"
    url = f"https://{rng.choice(['a.example.test', 'b.example.org'])}/{token[:8]}?q={rng.randrange(10**6)}"
    secrets = {
        "card": _joined(rng, card, rng.choice([1, 2, 4])),
        "number": _joined(rng, number, rng.choice([1, 3])),
        "token": token,
        "ipv4": ip4,
        "ipv6": ip6,
        "email": email,
        "iban": iban,
        "url": url,
    }
    for kind, secret in secrets.items():
        for frame in ("{}", "before {} after", "x={};", "({})"):
            text = frame.format(secret)
            masked = mask_value(text, limit=None)
            assert secret not in masked, (kind, text, masked)
            assert assert_clean(masked)[1] == 0, (kind, masked)
            assert mask_value(masked, limit=None) == masked, (kind, masked)


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.text(alphabet=string.ascii_letters + string.digits + " .@:/_-+=,;()[]", max_size=120))
def test_masking_is_stable_and_leaves_nothing_for_the_late_check(text: str) -> None:
    masked = mask_value(text, limit=None)
    assert mask_value(masked, limit=None) == masked  # a masked value masks to itself
    assert assert_clean(masked) == (masked, 0)  # so the last check finds nothing to do


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=120))
def test_a_shape_is_always_inside_its_alphabet(text: str) -> None:
    assert SHAPE_ALPHABET.fullmatch(shape_of(text))


# ---------------------------------------------------------------------------
# assert_clean
# ---------------------------------------------------------------------------
def test_assert_clean_masks_what_slipped_through_and_counts_it() -> None:
    clean, count = assert_clean('{"value": "call +91 98765 43210 or jane.roe@example.test"}')
    assert count == 2
    assert "98765" not in clean and "jane" not in clean
    assert assert_clean(clean) == (clean, 0)


def test_assert_clean_leaves_markers_dates_and_numbers_alone() -> None:
    text = '{"rows": 3000, "rate": 0.042, "when": "2024-01-31", "x": "[REDACTED:email]", "id": "550e8400-e29b-41d4-a716-446655440000"}'
    assert assert_clean(text) == (text, 0)


# ---------------------------------------------------------------------------
# Column labels and aliases
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    ["monthly_spend", "signupDate", "Customer Name", "ad-ctr", "Q3 spend", "spend_30d", "région"],
)
def test_ordinary_words_are_shown_as_they_are(name: str) -> None:
    assert is_plain_label(name)
    assert safe_column_label(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "jane.roe@example.test",
        "123456789012",
        "order 987654",
        "https://example.test/a",
        "/home/user/file",
        "x" * 65,
        "+91 98765 43210",
        "IN12 3456 7890",
        "a​b@c.de",
        "",
    ],
)
def test_anything_else_gets_a_stable_alias(name: str) -> None:
    assert not is_plain_label(name)
    columns = ["id", name, "spend"]
    table = alias_table(columns)
    assert table == {name: "column_2"}
    assert alias_table(columns) == table  # stable
    assert safe_column_label(name, table) == "column_2"


def test_an_alias_never_equals_a_real_column() -> None:
    table = alias_table(["column_2", "jane.roe@example.test"])
    assert table == {"jane.roe@example.test": "column_2x"}


def test_unalias_restores_clean_names_and_keeps_the_alias_for_personal_ones() -> None:
    table = {"Q3​ spend?": "column_2", "jane.roe@example.test": "column_3", "x" * 70: "column_4"}
    text = "See column_2, column_3 and column_4 but not column_9."
    out = unalias(text, table)
    assert "column_2" not in out  # a clean name comes back
    assert "column_3" in out and "jane" not in out  # an e-mail header stays an alias in what is stored
    assert "column_9" in out


def test_unalias_does_not_touch_a_longer_word() -> None:
    assert unalias("mycolumn_2 and column_22", {"real name!": "column_2"}) == "mycolumn_2 and column_22"


def test_apply_aliases_replaces_every_form_of_a_name() -> None:
    gate = Egress.build(["id", "jane.roe@example.test", "€"])
    assert gate.aliases == {"jane.roe@example.test": "column_2", "€": "column_3"}
    text = gate.apply_aliases("Hide 'jane.roe@example.test' and '€', keep € apart")
    assert text == "Hide 'column_2' and 'column_3', keep € apart"  # a short name only when quoted


# ---------------------------------------------------------------------------
# prepare: default-deny
# ---------------------------------------------------------------------------
COLUMNS = ["customer_id", "notes", "contact_email", "region", "hdr.owner@example.test", "name"]


def _prep(result: dict[str, Any], mode: DataAccess = MASKED, **kwargs: Any) -> dict[str, Any]:
    return prepare(
        result,
        ctx_columns=COLUMNS,
        personal_columns=kwargs.pop("personal", ["contact_email"]),
        always_hide=kwargs.pop("always_hide", []),
        mode=mode,
    )


def test_the_allow_list_is_short_and_documented() -> None:
    required = {"column", "columns", "type", "kind", "code", "name", "function", "dtype", "label", "message"}
    assert required <= SAFE_KEYS
    assert len(SAFE_KEYS) <= 16


def test_a_string_under_an_unknown_key_is_a_cell_and_masked() -> None:
    out = _prep({"weird_key": "call +91 98765 43210", "nested": [{"other": "joe.b@example.test"}]})
    assert out == {"weird_key": "call [REDACTED:phone]", "nested": [{"other": "[REDACTED:email]"}]}


def test_a_string_under_an_unknown_key_is_a_shape_in_summaries_only() -> None:
    out = _prep({"value": "Jane Roe", "buckets": {"values": ["Baker Street 221B"]}}, SUMMARIES)
    assert out == {"value": "Aaaa Aaa", "buckets": {"values": ["Aaaaa Aaaaa 999A"]}}


def test_labels_under_safe_keys_pass_but_are_scanned() -> None:
    result = {
        "column": "notes",
        "type": "string",
        "kind": "date",
        "code": "TARGET_CONSTANT",
        "function": "mean",
        "dtype": "datetime64[ns]",
        "message": "'notes' is empty in 3 rows.",
        "label": "Search strategy",
    }
    assert _prep(result) == result
    assert _prep(result, SUMMARIES) == result


def test_a_cell_hiding_under_a_label_key_is_still_a_cell() -> None:
    out = _prep({"type": "joe.b@example.test", "kind": "Jane Roe", "code": "4111 1111 1111 1111"}, SUMMARIES)
    assert out == {"type": "aaa.a@aaaa.aaaa", "kind": "Aaaa Aaa", "code": "9999 9999 9999 9999"}
    masked = _prep({"type": "joe.b@example.test", "code": "4111 1111 1111 1111"})
    assert masked == {"type": "[REDACTED:email]", "code": "[REDACTED:card]"}


def test_a_message_is_scanned_even_though_its_key_is_safe() -> None:
    out = _prep({"message": "Rows like joe.b@example.test are odd."})
    assert out["message"] == "Rows like [REDACTED:email] are odd."


def test_a_column_that_is_not_a_known_column_is_a_cell() -> None:
    assert _prep({"column": "not_in_the_file"}) == {"column": "not_in_the_file"}  # words: no scanner hit
    assert _prep({"column": "joe.b@example.test extra"}) == {"column": "[REDACTED:email] extra"}


def test_a_column_name_that_is_not_plain_becomes_its_alias_everywhere() -> None:
    out = _prep(
        {
            "column": "hdr.owner@example.test",
            "columns": ["notes", "hdr.owner@example.test"],
            "examples": ["hdr.owner@example.test"],
            "message": "'hdr.owner@example.test' is empty.",
        }
    )
    assert out["column"] == "column_5"
    assert out["columns"] == ["notes", "column_5"]
    assert out["examples"] == ["column_5"]
    assert out["message"] == "'column_5' is empty."


def test_a_dict_key_that_is_a_cell_is_masked_too() -> None:
    out = _prep({"counts": {"hdr.owner@example.test": 3, "north": 4}})
    assert out == {"counts": {"column_5": 3, "north": 4}}
    out = _prep({"values": {"Jane Roe": 3}}, SUMMARIES)
    assert out == {"values": {"Aaaa Aaa": "9"}}  # a number under a cell-shaped key is not known to be a count


def test_numbers_stay_evidence_but_a_long_number_is_a_value() -> None:
    result = {
        "rows": 3000,
        "null_rate": 0.0123,
        "flag": True,
        "gone": None,
        "minimum": 234567890123,
        "ids": 123456789012,
    }
    out = _prep(result)
    assert out["rows"] == 3000 and out["null_rate"] == 0.0123 and out["flag"] is True and out["gone"] is None
    assert out["minimum"] == "[REDACTED:number]"
    assert out["ids"] == 123456789012  # a count keeps every digit


def test_summaries_only_turns_cell_numbers_into_shapes_and_keeps_counts_and_means() -> None:
    out = _prep(
        {"rows": 3000, "distinct": 12, "mean": 41.5, "minimum": 18, "maximum": 99.25, "values": [1, 2]},
        SUMMARIES,
    )
    assert out == {
        "rows": 3000,
        "distinct": 12,
        "mean": 41.5,
        "minimum": "99",
        "maximum": "99.99",
        "values": ["9", "9"],
    }


def test_a_non_finite_number_becomes_null() -> None:
    assert _prep({"mean": float("nan"), "rows": float("inf")}) == {"mean": None, "rows": None}


def test_a_row_keyed_by_column_names_holds_cells_even_under_a_label_key() -> None:
    out = _prep({"rows": [{"customer_id": "1", "name": "Jane Roe", "region": "North"}]}, SUMMARIES)
    assert out["rows"] == [{"customer_id": "9", "name": "Aaaa Aaa", "region": "Aaaaa"}]


def test_a_tool_that_writes_a_column_value_under_the_column_name_is_hidden_for_a_hidden_column() -> None:
    out = _prep({"rows": [{"customer_id": "c1", "contact_email": "joe.b@example.test", "region": "North"}]})
    assert out["rows"] == [{"customer_id": "c1", "contact_email": PERSONAL_DATA, "region": "North"}]


def test_personal_and_always_hide_columns_never_yield_a_value() -> None:
    result = {
        "column": "contact_email",
        "rows": 3000,
        "distinct": 2999,
        "type": "string",
        "minimum": 4,
        "mean": 3.5,
        "examples": ["a", "b"],
        "top_values": [{"value": "joe.b@example.test", "rows": 2}],
        "message": "Looks fine.",
    }
    for mode in (MASKED, SUMMARIES):
        out = _prep(result, mode)
        assert out["examples"] == [PERSONAL_DATA, PERSONAL_DATA]
        assert out["top_values"] == [{"value": PERSONAL_DATA, "rows": 2}]
        assert out["minimum"] is None and out["mean"] is None
        assert (out["rows"], out["distinct"], out["type"], out["message"]) == (
            3000,
            2999,
            "string",
            "Looks fine.",
        )
    hidden = _prep({**result, "column": "Notes"}, personal=[], always_hide=["notes"])
    assert hidden["examples"] == [HIDDEN_BY_SETTINGS] * 2
    assert hidden["top_values"] == [{"value": HIDDEN_BY_SETTINGS, "rows": 2}]


def test_a_result_that_lists_a_hidden_column_among_its_columns_is_hidden() -> None:
    out = _prep({"columns": ["region", "notes"], "share": 0.4, "pairs": [["x", "y"]]}, always_hide=["notes"])
    assert out["pairs"] == [[HIDDEN_BY_SETTINGS, HIDDEN_BY_SETTINGS]]
    assert (
        out["share"] == 0.4
    )  # a share of all rows the engine measured, not a cell (review: personal key counts)


def test_a_profile_entry_of_a_hidden_column_keeps_its_counts_and_kinds() -> None:
    entry = {"name": "notes", "type": "string", "null_rate": 0.1, "distinct": 5, "personal_data": ["email"]}
    out = _prep({"columns": [entry, {**entry, "name": "region"}]}, always_hide=["NOTES"])
    assert out["columns"][0] == entry  # names, types, counts and detector kinds are not values
    assert out["columns"][1] == {**entry, "name": "region"}


def test_a_new_tool_is_masked_by_default() -> None:
    out = _prep({"brand_new_field": ["Jane Roe", "4111 1111 1111 1111"], "sample": {"x": "192.0.2.44"}})
    assert out == {"brand_new_field": ["Jane Roe", "[REDACTED:card]"], "sample": {"x": "[REDACTED:ip]"}}
    # A field name nobody registered (`egress.RESULT_KEYS`) may be a cell, so `summaries_only` shapes it too.
    assert _prep({"brand_new_field": ["Jane Roe"]}, SUMMARIES) == {"aaaa_aaa_aaaa": ["Aaaa Aaa"]}


def test_literals_and_markers_are_not_cells() -> None:
    result = {"value": "[REDACTED:email]", "values": "(empty)", "examples": "phone", "bucket": PERSONAL_DATA}
    assert _prep(result, SUMMARIES) == result


def test_a_result_nested_too_deep_is_cut_and_a_long_list_is_bounded() -> None:
    deep: dict[str, Any] = {"x": "ok"}
    for _ in range(30):
        deep = {"n": deep}
    assert "ok" not in json.dumps(_prep(deep))
    assert len(_prep({"items": list(range(1000))})["items"]) == 200


def test_extra_text_keys_are_sentences_not_cells() -> None:
    gate = Egress.build(COLUMNS, mode=SUMMARIES, known_text=["Search strategy"])
    out = gate.prepare(
        {"title": "Turn 'notes' into numbers; 'Search strategy'; '12/03/2024'"}, extra_text_keys={"title"}
    )
    assert out["title"] == "Turn 'notes' into numbers; 'Search strategy'; '99/99/9999'"
    assert (
        gate.prepare({"title": "Hide 'hdr.owner@example.test'"}, extra_text_keys={"title"})["title"]
        == "Hide 'column_5'"
    )


# ---------------------------------------------------------------------------
# The transparency record and its cap
# ---------------------------------------------------------------------------
def _message(previews: list[str]) -> ChatMessage:
    items = tuple(
        SentItem(tool="inspect_column", args={}, preview=text, chars=len(text), mode=MASKED)
        for text in previews
    )
    return ChatMessage(role=ChatRole.AGENT, text="x", sent=items, created_at=utc_now())


def test_an_old_message_without_sent_still_loads() -> None:
    message = ChatMessage.model_validate({"role": "agent", "text": "hi", "created_at": utc_now().isoformat()})
    assert message.sent == ()


def test_cap_sent_keeps_its_item_limit_and_twenty_kb_newest_first() -> None:
    big = "x" * MAX_SENT_PREVIEW_CHARS
    transcript = [_message([big] * (MAX_SENT_ITEMS + 3)) for _ in range(3)]
    capped = cap_sent(transcript)
    assert [len(m.sent) for m in capped] == [MAX_SENT_ITEMS] * 3
    kept = sum(len(i.preview) for m in capped for i in m.sent if not i.preview.startswith("[not kept"))
    assert kept <= MAX_SENT_SESSION_CHARS
    assert capped[-1].sent[0].preview == big  # the newest message keeps its previews
    assert capped[0].sent[-1].preview.startswith("[not kept")  # the oldest lose theirs first
    assert capped[0].sent[-1].chars == MAX_SENT_PREVIEW_CHARS  # the size is still recorded


# ---------------------------------------------------------------------------
# Third party
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("backend", "expected"),
    [("fake", False), ("bedrock", False), ("openrouter", True), ("huggingface", True), ("OpenRouter", True)],
)
def test_only_fake_and_bedrock_are_not_third_parties(backend: str, expected: bool) -> None:
    assert is_third_party(SimpleNamespace(backend=backend)) is expected
    assert is_third_party(SimpleNamespace(backend=SimpleNamespace(value=backend))) is expected


def test_scrub_masks_a_persons_message_without_cutting_it() -> None:
    text = "My card is 4111 1111 1111 1111. " + "word " * 100
    out = scrub(text)
    assert "4111" not in out and out.endswith("word")


def test_an_advisor_sentence_that_names_a_hidden_column_shows_none_of_its_examples() -> None:
    gate = Egress.build(COLUMNS, personal_columns=["contact_email"], always_hide=["notes"])
    sentence = {"title": "In 'notes' ('12/03/2024', 'Jane Roe'), which comes first: the day or the month?"}
    out = gate.prepare(sentence, extra_text_keys={"title"})["title"]
    assert (
        out
        == f"In 'notes' ('{HIDDEN_BY_SETTINGS}', '{HIDDEN_BY_SETTINGS}'), which comes first: the day or the month?"
    )
    personal = gate.prepare(
        {"title": "Hide 'contact_email' ('joe.b@example.test')"}, extra_text_keys={"title"}
    )
    assert personal["title"] == f"Hide 'contact_email' ('{PERSONAL_DATA}')"
    # A sentence about another column keeps its examples (masked), as before.
    other = gate.prepare({"title": "In 'region' ('North', 'South'), pick one"}, extra_text_keys={"title"})
    assert other["title"] == "In 'region' ('North', 'South'), pick one"


def test_text_for_the_person_keeps_a_personal_header_as_its_alias_and_restores_a_clean_one() -> None:
    gate = Egress.build(["id", "hdr.owner@example.test", "Spend (INR)\u200b?", "notes"])
    fallback = "Still to decide: Hide 'hdr.owner@example.test' and 'Spend (INR)\u200b?' and 'notes'."
    assert gate.person_text(fallback) == "Still to decide: Hide 'column_2' and 'Spend (INR)?' and 'notes'."
    assert gate.unalias_args({"column": "column_2", "columns": ["column_3", "notes"]}) == {
        "column": "hdr.owner@example.test",
        "columns": ["Spend (INR)\u200b?", "notes"],
    }


def test_a_very_long_session_keeps_a_bounded_number_of_sent_items() -> None:
    """A session of hundreds of turns must not keep a placeholder for every old item (review of PR 6)."""
    big = "x" * 900
    transcript = [_message([big] * MAX_SENT_ITEMS) for _ in range(500)]
    capped = cap_sent(transcript)
    kept = [item for message in capped for item in message.sent]
    assert len(kept) <= MAX_SENT_SESSION_ITEMS
    assert sum(len(item.preview) for item in kept) <= MAX_SENT_SESSION_CHARS + len(kept) * len(SENT_DROPPED)
    assert len(capped) == 500  # every message stays; only the oldest lose their items
    assert not capped[0].sent and capped[-1].sent
    assert len(json.dumps([m.model_dump(mode="json") for m in capped])) < 400_000
