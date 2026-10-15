"""The forwardable data request messages (D1 to D12 of docs/DATA_DOCTOR_SCENARIOS.md)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine.pilot.data_request import load_request_messages, render_request_message
from engine.pilot.plain import jargon_in

SELECTORS = (
    "IDS_ROUNDED",
    "SUMMARY_NOT_ROWS",
    "PDF",
    "HISTORY_NEEDED",
    "CHURN_LIST_COMPLETE",
    "ID_MAPPING",
    "CONSENT_SOURCE",
    "PRODUCT_MASTER",
    "MORE_HISTORY",
    "RANDOM_GROUP",
    "EXCHANGE_RATES",
    "FEED_DELAY",
)


def test_there_is_one_message_for_each_of_d1_to_d12(config_root: Path) -> None:
    messages = load_request_messages(config_root)
    assert tuple(messages) == SELECTORS
    assert [m.id for m in messages.values()] == [f"D{n}" for n in range(1, 13)]
    assert all(messages[key].selector == key for key in messages)


@pytest.mark.parametrize("selector", SELECTORS)
def test_each_message_renders_as_plain_text_the_person_can_forward(selector: str, config_root: Path) -> None:
    text = render_request_message(selector, config_root)
    assert text.strip() and "{" not in text and "}" not in text
    assert len(text) < 700, "short enough to paste into an e-mail"
    assert jargon_in(text) == ()
    assert load_request_messages(config_root)[selector].title


def test_blanks_are_filled_from_the_values_or_shown_for_the_person_to_fill(config_root: Path) -> None:
    open_ = render_request_message("CHURN_LIST_COMPLETE", config_root)
    assert "[from date]" in open_ and "[to date]" in open_
    filled = render_request_message("CHURN_LIST_COMPLETE", config_root, from_date="1 Jan", to_date="30 Jun")
    assert "between 1 Jan and 30 Jun" in filled
    assert "5 to 10%" in render_request_message("RANDOM_GROUP", config_root)
    assert "8%" in render_request_message("RANDOM_GROUP", config_root, share="8%")


def test_an_unknown_selector_is_refused(config_root: Path) -> None:
    with pytest.raises(KeyError):
        render_request_message("NO_SUCH_MESSAGE", config_root)


def test_every_message_is_named_in_the_scenario_report(repo_root: Path, config_root: Path) -> None:
    report = (repo_root / "docs" / "DATA_DOCTOR_SCENARIOS.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"^\| (D\d+) \|", report, flags=re.MULTILINE))
    assert listed == {m.id for m in load_request_messages(config_root).values()}
