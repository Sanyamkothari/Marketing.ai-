"""The egress scanners after the second review (findings 3-11 and 13-16): what is masked, what must stay.

`test_review_bypass_<n>.py` holds the reviewer's own repros and is not edited. This file adds, for each
fix, a few more shapes that must be masked and the ordinary text that must come through unchanged, a
timing fuzz over every scanner, and a precision check that masks the column names and cells of the
generated fixtures and asserts nothing legitimate changes.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from itertools import islice

import pandas as pd
import pytest

from engine.agent import egress
from engine.agent.config import DataAccess
from engine.agent.tools import MAX_SCANNED_CELL_CHARS, _masked
from engine.pii import REDACTION_MARKER_PATTERN
from tests.fixtures.agent_bench.cases import CASES
from tests.fixtures.make_data import GenerationSpec, generate, predictive_use_case_ids


def _left(text: str, pattern: str = r"\d{4}") -> bool:
    """True when `pattern` still matches after the markers are removed."""
    return re.search(pattern, REDACTION_MARKER_PATTERN.sub("", text)) is not None


def _kept(text: str) -> None:
    assert egress.mask_value(text, limit=None) == text
    assert egress.scrub(text) == text
    assert egress.assert_clean(text) == (text, 0)


# ---------------------------------------------------------------------------
# 3: typographic dashes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dash", ["‐", "‑", "‒", "–", "—", "―", "−", "﹣", "－"])
@pytest.mark.parametrize(
    "value",
    ["98765{d}43210", "+91 {d} 98765 {d} 43210", "2345{d}6789{d}0123", "4111 {d} 1111 {d} 1111 {d} 1111"],
)
def test_every_unicode_dash_is_read_as_a_hyphen(dash: str, value: str) -> None:
    assert not _left(egress.mask_value(value.format(d=dash)))
    assert not _left(egress.assert_clean(f'{{"value": "{value.format(d=dash)}"}}')[0])


@pytest.mark.parametrize(
    ("written", "shown"),
    [
        ("2020–01–31", "2020-01-31"),
        ("pages 12–15", "pages 12-15"),
        ("a well—known name", "a well-known name"),
        ("-12.5− 3", "-12.5- 3"),
    ],
)
def test_a_dash_between_short_numbers_is_folded_and_kept(written: str, shown: str) -> None:
    assert egress.mask_value(written, limit=None) == shown


# ---------------------------------------------------------------------------
# 4: bracketed phone numbers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "phone",
    [
        "+91 (98765) 43210",
        "+91 [98765] 43210",
        "+7 (495) 123-45-67",
        "(495) 123-45-67",
        "+1 (555) 123-4567",
        "+65 (6123) 4567",
    ],
)
def test_a_phone_with_a_bracketed_block_is_masked_in_a_sentence(phone: str) -> None:
    assert not _left(egress.mask_value(f"call {phone} tomorrow"), r"\d{3}")
    assert "[REDACTED:phone]" in egress.mask_value(f"call {phone} tomorrow")


@pytest.mark.parametrize(
    "text",
    [
        "Table (2019) 2020",
        "f(x) 12 34",
        "see (12) 3",
        "Smith (2019)",
        "(n=1200) 45%",
        "range [10, 20] 30",
        "a [1] b [2] c",
    ],
)
def test_brackets_around_a_short_number_are_not_a_phone(text: str) -> None:
    _kept(text)


# ---------------------------------------------------------------------------
# 5: invisible characters that are not category C*
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("char", ["͏", "︀", "️", "ᅟ", "ᅠ", "ㅤ", "ﾠ", "⠀", "᠋", "឵", "⃣", "́", "​"])
def test_an_invisible_character_inside_a_number_does_not_hide_it(char: str) -> None:
    assert not _left(egress.mask_value(f"98765{char}43210"))
    assert not _left(egress.assert_clean(f'{{"value": "4111{char}1111{char}1111{char}1111"}}')[0])


def test_the_backstop_reads_a_number_written_in_keycap_digits() -> None:
    keycaps = "".join(f"{digit}️⃣" for digit in "9876543210")
    assert not _left(egress.assert_clean(f'{{"value": "{keycaps}"}}')[0])


@pytest.mark.parametrize("text", ["café au lait", "café", "भारत नमस्ते", "❤️ loyal", "สวัสดี"])
def test_marks_on_letters_of_other_scripts_stay(text: str) -> None:
    shown = egress.mask_value(text, limit=None)
    assert shown == (text if text != "café" else "café")


def test_a_clean_prompt_is_returned_byte_for_byte_even_with_unusual_characters() -> None:
    prompt = 'line one\n  "note": "café → … ² – x",\n\ttab'
    assert egress.assert_clean(prompt) == (prompt, 0)


# ---------------------------------------------------------------------------
# 6: identifiers shaped like addresses
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "address",
    [
        "jane.roe@okhdfcbank",
        "ravi1985@oksbi",
        "x@intranet",
        "priya_s@ybl",
        "a.b-c+tag@corp-net",
        "алекс@почта",
        "taro@example.テスト",
        "ravi@example.भारत",
    ],
)
def test_an_address_without_a_dotted_ascii_domain_is_masked(address: str) -> None:
    local = re.split(r"[@]", address)[0]
    for text in (address, f"reach {address}, thanks", f"({address})", f"{address}."):
        assert local not in egress.mask_value(text), text
    assert local not in egress.assert_clean(f'{{"value": "{address}"}}')[0]


@pytest.mark.parametrize(
    "text",
    [
        "@jane said hi",
        "meet me @ 5pm",
        "call @ 10",
        "price 5@3",
        "a@b",
        "x@2024",
        "ping @channel",
        "user@ example",
        "50 @ 3.5 each",
        "e-mail:",
    ],
)
def test_an_at_sign_in_prose_is_not_an_address(text: str) -> None:
    _kept(text)


# ---------------------------------------------------------------------------
# 7: scan order
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "jane.roe@example.com/sales",
        "jane.roe@example.com/joe.bloggs@example.com",
        "see jane.roe@mail.example.co.uk/x/y",
    ],
)
def test_an_address_followed_by_a_path_keeps_no_local_part(text: str) -> None:
    for masked in (egress.mask_value(text), egress.assert_clean(f'{{"value": "{text}"}}')[0]):
        assert "jane.roe" not in masked and "joe.bloggs" not in masked, masked


@pytest.mark.parametrize(
    "text", ["https://example.com/path?q=1", "www.example.com", "docs.example.com/help/faq"]
)
def test_a_url_is_still_one_url(text: str) -> None:
    assert egress.mask_value(text) == "[REDACTED:url]"


def test_a_url_with_an_address_in_it_is_masked_whole() -> None:
    masked = egress.mask_value("https://example.com/u/jane.roe@example.com/view")
    assert "jane.roe" not in masked and "example.com" not in masked


# ---------------------------------------------------------------------------
# 8: cards
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "card",
    [
        "4111 - 1111 - 1111 - 1111",
        "4111_1111_1111_1111",
        "4111/1111/1111/1111",
        "4111,1111,1111,1111",
        "4111 , 1111 , 1111 , 1111",
        "5500 0000 0000 0004",
        "3782 822463 10005",
        "4111-1111-1111-1111",
        "4111111111111111",
    ],
)
def test_a_card_with_any_common_separator_is_masked_in_a_sentence(card: str) -> None:
    masked = egress.mask_value(f"paid with {card} today")
    assert not _left(masked, r"\d{4}") and "[REDACTED:card]" in masked, masked


def test_a_card_next_to_another_number_is_still_found() -> None:
    masked = egress.mask_value("Jane Roe, 4111 1111 1111 1111, 203.0.113.42")
    assert "[REDACTED:card]" in masked and "[REDACTED:ip]" in masked and not _left(masked)
    assert "[REDACTED:card]" in egress.mask_value("ref 12 4111 1111 1111 1111")
    assert "[REDACTED:card]" in egress.mask_value("4111 1111 1111 1111 12")


@pytest.mark.parametrize(
    "text",
    [
        "1,234,567",
        "12,345,678",
        "2024-01-31, 2024-02-01",
        "1/2/3",
        "2024/01/31 2024/02/28",
        "v1.2.3.4",
        "orders 1, 2, 3, 4, 5",
    ],
)
def test_short_number_lists_are_not_cards(text: str) -> None:
    _kept(text)


def test_a_long_number_that_fails_the_luhn_check_is_not_a_card() -> None:
    assert "[REDACTED:card]" not in egress.mask_value("4111,1111,1111,1112")


# ---------------------------------------------------------------------------
# 9: PAN with separators
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pan", ["ABCDE 1234 F", "ABCDE-1234-F", "ABCDE 1234F", "ABCDE1234 F", "abcde 1234 f", "ABCDE1234F"]
)
def test_a_pan_is_masked_however_it_is_typed(pan: str) -> None:
    assert egress.mask_value(f"PAN {pan}.") == "PAN [REDACTED:pan]."


@pytest.mark.parametrize(
    "text",
    [
        "Sales 2024 Q1",
        "Item 1234 boxes",
        "Room 1234 F",
        "ABCDE1234",
        "ABCDE 12345 F",
        "Total 2024 H2",
        "ABCDEF 1234 G",
    ],
)
def test_ordinary_words_and_numbers_are_not_a_pan(text: str) -> None:
    _kept(text)


# ---------------------------------------------------------------------------
# 10: dates and decimals inside a longer number
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "9876.543210",
        "98765 43210.0",
        "+91 98765 43210.0",
        "98765.43210",
        "98765-43210.0",
        "call 9876.543210 now",
    ],
)
def test_a_phone_written_like_a_decimal_is_masked(value: str) -> None:
    assert not _left(egress.mask_value(value))


@pytest.mark.parametrize(
    "text",
    [
        "12345.678",
        "3.14159265",
        "-122.4194155",
        "77.5945627",
        "1234.5678",
        "1234567.89",
        "12345678.90",
        "Order 12345 12.5 kg",
        "2024-01-31",
        "31/01/2024",
        "2024-01-31T10:30:00Z",
        "0.0001",
        "1500.75",
    ],
)
def test_ordinary_decimals_and_dates_are_kept(text: str) -> None:
    _kept(text)


# ---------------------------------------------------------------------------
# 11: ReDoS, and the cap on what is scanned
# ---------------------------------------------------------------------------
N = 8_000
BUDGET_SECONDS = 0.3
UNITS = [
    "a",
    "1",
    "-",
    ".",
    "a.",
    "1-",
    "a@",
    "@",
    "a@a.",
    "a.a@",
    " ",
    "a ",
    "1 ",
    "A ",
    "9-",
    "a-a-",
    "[",
    "[REDACTED:x]",
    "(12345)",
    "(1) ",
    "1,",
    "1/",
    "1_",
    "a/",
    "a.b/",
    "://",
    "a:",
    "::",
    "–",
    "1–",
    "͏",
    "1͏",
    "́",
    "AB",
    "ABCDE ",
    "ABCDE 1234 ",
    "x@y",
    '"',
    '\\"',
    '\\\\"',
    '"a\\\\',
    '": 1',
    ", 1",
    "1.",
    "1 2.5 ",
    "2024-01-31 ",
    "Aa ",
    "Aa9 ",
    "Aa!",
    "+91 ",
    "\n1",
    "{",
    "}",
    "4111 ",
    "9876.54",
]
SCANNERS: dict[str, Callable[[str], object]] = {
    "scrub": egress.scrub,
    "mask_value": lambda text: egress.mask_value(text, limit=None),
    "assert_clean": egress.assert_clean,
    "_masked": lambda text: _masked(text, 60),
}


@pytest.mark.parametrize("name", list(SCANNERS))
def test_no_scanner_is_slow_on_any_repeated_unit(name: str) -> None:
    slow = {}
    for unit in UNITS:
        text = (unit * (N // len(unit) + 1))[:N]
        # The best of three runs: a busy machine (pytest -n 4 beside other jobs) slows one run, rarely all
        # three, while a backtracking scanner is slow on every run, so the budget is unchanged.
        took = float("inf")
        for _ in range(3):
            start = time.perf_counter()
            SCANNERS[name](text)
            took = min(took, time.perf_counter() - start)
        if took > BUDGET_SECONDS:
            slow[unit] = round(took, 3)
    assert not slow, slow


def test_a_header_longer_than_the_name_limit_is_never_scanned_for_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int] = []
    real = egress.scrub

    def spy(text: str) -> str:
        seen.append(len(text))
        return real(text)

    gate = egress.Egress.build(["h" * N, "jane.roe@example.com", "amount"])
    monkeypatch.setattr(egress, "scrub", spy)
    reply = gate.person_text("column_1 and column_2 and amount")
    assert max(seen, default=0) <= 64, seen
    assert reply == "column_1 and column_2 and amount"  # neither name is clean enough to give back


def test_a_short_clean_header_is_given_back_in_the_reply() -> None:
    gate = egress.Egress.build(["customer id", "2020", "0.05"])
    assert gate.aliases == {"2020": "column_2", "0.05": "column_3"}
    assert gate.person_text("see column_2 and column_3") == "see 2020 and 0.05"


def test_a_long_cell_is_masked_on_its_head_and_the_rest_is_a_length() -> None:
    cell = "jane.roe@example.com " * 400
    shown = _masked(cell, 60)
    assert shown.startswith("[REDACTED:email]") and "jane.roe" not in shown and len(shown) <= 60
    blob = "Zm9v" * 2_500
    assert _masked(blob, 60) == "[REDACTED:token]"
    collapsed = ("4111 1111 1111 1111 " * 200) + "4111 11"
    shown_collapsed = _masked(collapsed, 40)
    assert (
        shown_collapsed.startswith("[REDACTED:") and "4111" not in shown_collapsed
    )  # a Luhn-valid run: card
    assert _masked("jane.roe@example.com," * 150 + "x", 300) == ("[REDACTED:email]," * 18)[:300]


def test_a_long_cell_never_shows_half_a_secret_at_the_cut() -> None:
    for offset in range(MAX_SCANNED_CELL_CHARS - 40, MAX_SCANNED_CELL_CHARS + 5):
        cell = "sk-" + "x" * 5 + " " + ("y" * (offset - 10)) + " jane.roe@example.com " + "z" * 500
        shown = _masked(cell, 60)
        assert "jane" not in shown[MAX_SCANNED_CELL_CHARS // 2 :]
    huge_local = "jane.roe" * 400 + "@example.com"
    assert "jane.roe" not in _masked(huge_local, 60)


def test_the_short_cell_path_is_unchanged() -> None:
    assert _masked("Basic plan", 60) == "Basic plan"
    assert _masked("jane.roe@example.com", 60) == "[REDACTED:email]"
    assert _masked("x" * 100, 60) == "x" * 59 + "…"  # cut, with the visible mark of a cut


# ---------------------------------------------------------------------------
# 13: identifier-shaped keys
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", [DataAccess.MASKED_DATA, DataAccess.SUMMARIES_ONLY])
def test_an_identifier_shaped_key_that_a_scanner_would_change_is_masked(mode: DataAccess) -> None:
    gate = egress.Egress.build(["customer_id"], mode=mode)
    shown = gate.prepare(
        {
            "values": {  # a registered field name: the dict under it is keyed by cells
                "user_9876543210": 3,
                "zk_live_51hxabcdef1234567890": 1,  # secret-scan: allow (a fake key the masking tests plant on purpose)
                "plan_basic": 2,
                "region_north_2": 4,
            }
        }
    )
    keys = list(shown["values"])
    assert not any("9876543210" in key or "51hxabcdef" in key for key in keys), keys
    if mode is DataAccess.MASKED_DATA:
        assert "plan_basic" in keys and "region_north_2" in keys


# ---------------------------------------------------------------------------
# 14: numeric headers and aliases
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("header", ["2020", "0.05", "500.0", "1,000", "2020-21", "12"])
def test_a_numeric_header_is_never_replaced_inside_a_sentence(header: str) -> None:
    gate = egress.Egress.build(["customer_id", header])
    sentence = "range 0.05 to 500.0 in 2020, 2020-01-31, 12020 rows, 1,000 and 12 items, 2020-21 too"
    assert gate.apply_aliases(sentence) == sentence
    assert gate.text(sentence) == sentence
    assert gate.label(header) == "column_2"  # but as a label or a whole cell it is still an alias
    assert gate.prepare({"column": header})["column"] == "column_2"


def test_a_word_header_is_replaced_only_as_a_whole_word() -> None:
    gate = egress.Egress.build(["customer_id", "jane.roe@example.com", "Q1 2024 (final)"])
    assert gate.apply_aliases("ask jane.roe@example.com now") == "ask column_2 now"
    assert gate.apply_aliases("xjane.roe@example.com") == "xjane.roe@example.com"
    assert gate.apply_aliases("the Q1 2024 (final) column") == "the column_3 column"
    tidy = egress.Egress.build(["customer_id", "Sales 2024 (x)"])
    assert tidy.apply_aliases("Sales 2024 (x) and MySales 2024 (x)") == "column_2 and MySales 2024 (x)"


# ---------------------------------------------------------------------------
# 15: JSON numbers in the backstop
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "prompt",
    [
        '{"rows": 1000000, "maximum": 10000000, "share": 0.25}',
        '{"counts": [1500000, 2000000, 12345678], "n": -1234567}',
        '{"a": {"rows": 7654321}, "b": [1234567.5, 2e7]}',
        '{"rows":1000000,"empty":2000000}',
    ],
)
def test_the_backstop_leaves_bare_json_numbers_alone(prompt: str) -> None:
    assert egress.assert_clean(prompt) == (prompt, 0)


@pytest.mark.parametrize("number", ["5551234567", "5551234567.0", "123456789", "-5551234567", "9876543210.5"])
def test_the_backstop_still_masks_a_bare_number_of_nine_digits_or_more(number: str) -> None:
    """A phone-like float statistic the gate missed: no file has nine-digit row counts, so it is a cell."""
    prompt = f'{{"rows": 1000000, "mean": {number}}}'
    cleaned, matches = egress.assert_clean(prompt)
    assert matches == 1 and number.lstrip("-").split(".")[0] not in cleaned and '"rows": 1000000' in cleaned


@pytest.mark.parametrize(
    "prompt",
    [
        '{"value": "9876543210"}',
        '{"value": "call me: 9876543210, ok", "rows": 1000000}',
        '{"value": "x\\": 9876543210, y"}',
        '{"value": "[1, 9876543210]"}',
    ],
)
def test_the_backstop_still_masks_a_number_inside_a_string(prompt: str) -> None:
    cleaned, count = egress.assert_clean(prompt)
    assert count >= 1 and "9876543210" not in cleaned, cleaned


# ---------------------------------------------------------------------------
# 16: feature names with a window
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "total_spend_last_90d",
        "orders_last_12m",
        "sessions_last_4wk",
        "avg_ticket_size_180d",
        "refund_rate_prev_30d_vs_90d",
        "revenue_1y_growth_flag",
    ],
)
def test_a_feature_name_with_a_window_suffix_is_plain(name: str) -> None:
    assert egress.is_plain_label(name)
    assert egress.mask_value(name) == name


@pytest.mark.parametrize(
    "key",
    [
        "zk_live_51HxAbCdEf1234567890",  # secret-scan: allow (a fake key the masking tests plant on purpose)
        "AKIAIOSFODNN7EXAMPLE12",
        "ghp_1234567890abcdefghijklmnopqrstuvwxyz",  # secret-scan: allow (a fake key the masking tests plant on purpose)
        "a1b2c3d4e5f6a7b8c9d0e1f2",
    ],
)
def test_a_key_is_still_a_token(key: str) -> None:
    assert egress.mask_value(key) == "[REDACTED:token]"


# ---------------------------------------------------------------------------
# Precision: the fixtures' own names and cells pass through unchanged
# ---------------------------------------------------------------------------
PERSONAL_COLUMN = re.compile(r"(?i)(^|_)(e?mail|phone|contact|billing|name|address)($|_)")
PHONE_LIKE_IDS = {"external_ref"}
"""The `id_like_column` variant's `REF-72304995`: an eight-digit run is a phone number to `engine.pii`, by
design, before and after this change. Every other column of every fixture must come through unchanged."""


def _fixture_frames() -> dict[str, pd.DataFrame]:
    frames = {name: build() for name, build in CASES.items()}
    for use_case in predictive_use_case_ids():
        frames[f"generated:{use_case}"] = generate(
            GenerationSpec(use_case_id=use_case, rows=400, variant="clean")
        )
    return frames


def _cells(frame: pd.DataFrame, column: str) -> list[str]:
    return [str(value) for value in islice(frame[column].dropna().drop_duplicates(), 150)]


def test_ordinary_column_names_and_cells_are_not_changed_by_any_scanner() -> None:
    changed: list[str] = []
    checked = 0
    for source, frame in _fixture_frames().items():
        for column in map(str, frame.columns):
            if PERSONAL_COLUMN.search(column) or column in PHONE_LIKE_IDS:
                continue
            checked += 1
            if not egress.is_plain_label(column):
                changed.append(f"{source}: header {column!r} is not a plain label")
            for cell in _cells(frame, column):
                shown = egress.mask_value(cell, limit=None)
                if shown != _normalised(cell):
                    changed.append(f"{source}.{column}: {cell!r} -> {shown!r}")
    assert checked > 200
    assert not changed, changed[:20]


def _normalised(cell: str) -> str:
    """What a clean cell looks like after the gate: whitespace collapsed, nothing else."""
    return " ".join(cell.split())


def test_a_whole_prompt_of_fixture_rows_is_not_masked_by_the_backstop() -> None:
    import json

    lines = 0
    for source, frame in _fixture_frames().items():
        keep = [
            c for c in map(str, frame.columns) if not PERSONAL_COLUMN.search(c) and c not in PHONE_LIKE_IDS
        ]
        rows = frame[keep].head(20).astype(object).where(frame[keep].head(20).notna(), None)
        payload = json.dumps(rows.to_dict(orient="records"), ensure_ascii=False, default=str)
        assert egress.assert_clean(payload) == (payload, 0), source
        lines += 1
    assert lines > 10
