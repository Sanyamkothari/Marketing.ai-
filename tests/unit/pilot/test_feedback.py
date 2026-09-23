"""The feedback store (Plan E M64): a route, a category and masked words; exported for the team."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from engine.pilot.feedback import export_csv, export_jsonl, list_feedback, new_feedback, save_feedback
from engine.storage import LocalStorage

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def entry(text: str = "The Output page is clear", screen: str = "#/uc/telco-churn/output/r_1"):
    return new_feedback(
        screen=screen, category="praise", text=text, actor_id="local-operator", demo=False, now=NOW
    )


def test_a_contact_typed_into_feedback_is_masked_before_it_is_stored(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    saved = entry("call me on 9876543210 or mail asha@example.invalid")
    save_feedback(storage, saved)
    raw = b"".join(p.read_bytes() for p in tmp_path.rglob("*.json"))
    assert b"9876543210" not in raw and b"asha@example.invalid" not in raw
    assert set(saved.redacted) >= {"email"}


def test_only_the_route_is_kept_never_a_query(tmp_path: Path) -> None:
    assert entry(screen="#/pilot?customer=42").screen == "#/pilot"
    with pytest.raises(ValueError):
        entry(screen="#/pilot/<script>")


def test_the_export_lists_every_entry_oldest_first(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    first, second = entry("one"), entry('=HYPERLINK("x")')
    save_feedback(storage, second)
    save_feedback(storage, first)
    entries = list_feedback(storage)
    assert len(entries) == 2
    rows = list(csv.DictReader(io.StringIO(export_csv(entries))))
    assert {row["text"] for row in rows} == {"one", '\'=HYPERLINK("x")'}, "a formula is never exported live"
    lines = export_jsonl(entries).splitlines()
    assert [json.loads(line)["feedback_id"] for line in lines] == [e.feedback_id for e in entries]


def test_a_long_text_is_cut_not_refused() -> None:
    assert len(entry("x" * 5000).text) == 1000
