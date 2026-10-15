"""The sentences Data Doctor says for what is out of scope (docs/DATA_DOCTOR_SCENARIOS.md section 4)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine.doctor.out_of_scope import (
    load_out_of_scope,
    out_of_scope,
    out_of_scope_codes,
    out_of_scope_request,
)
from engine.pilot.data_request import load_request_messages
from engine.pilot.plain import jargon_in

CODES = (
    "PDF_OR_SCAN",
    "IDS_ROUNDED",
    "LIVE_SHEET",
    "CHAT_LOG",
    "AMOUNT_TARGET",
    "NEXT_BEST_PRODUCT",
    "UPLIFT_NOT_RANDOM",
    "CURRENCY_CONVERSION",
    "FUZZY_MATCHING",
    "FOREIGN_CONSENT",
    "LARGE_TABLE",
    "SHORT_SEASON",
    "TOO_FEW_POSITIVES",
    "NEXT_MONTH_RECHECK",
)


def test_there_is_a_sentence_for_each_row_of_section_four() -> None:
    assert out_of_scope_codes() == CODES


@pytest.mark.parametrize("code", CODES)
def test_each_code_resolves_to_a_plain_sentence_with_no_raw_blanks(code: str) -> None:
    sentence = out_of_scope(code)
    assert sentence.strip() and "{" not in sentence
    assert jargon_in(sentence) == ()


def test_blanks_are_filled_or_shown_for_the_caller_to_fill() -> None:
    assert "[amount]" in out_of_scope("AMOUNT_TARGET")
    assert "who will spend more than 5,000" in out_of_scope("AMOUNT_TARGET", amount="5,000")
    full = out_of_scope(
        "TOO_FEW_POSITIVES", count=40, outcome="left", needed=200, looser_count=90, date="1 Mar"
    )
    assert full.startswith("There are only 40 examples of customers who left. We need about 200.")


def test_an_unknown_code_is_refused() -> None:
    with pytest.raises(KeyError):
        out_of_scope("NO_SUCH_CODE")


def test_the_data_request_offered_with_a_sentence_exists(config_root: Path) -> None:
    messages = load_request_messages(config_root)
    offered = {code: out_of_scope_request(code) for code in CODES}
    assert offered["PDF_OR_SCAN"] == "PDF" and offered["IDS_ROUNDED"] == "IDS_ROUNDED"
    assert offered["CHAT_LOG"] is None
    assert all(selector in messages for selector in offered.values() if selector)


def test_the_sentences_are_the_ones_the_scenario_report_quotes(repo_root: Path) -> None:
    report = (repo_root / "docs" / "DATA_DOCTOR_SCENARIOS.md").read_text(encoding="utf-8")
    section = report.split("## 4.", 1)[1].split("## 5.", 1)[0]
    quoted = re.findall(r'"([^"]+)"', section)
    for code, entry in load_out_of_scope().items():
        if "{" in entry.say:
            continue
        assert entry.say in quoted, code
