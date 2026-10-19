"""Two campaigns made the same day for one goal get different default names; stored names are not touched."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from engine.measurement.cycle import _unused_default_name

START = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
CONFIG: Any = SimpleNamespace(id="targeted_ad", name="Targeted Advertisement")


class _Store:
    def __init__(self, *names: str) -> None:
        self.names = names
        self.asked: dict[str, Any] = {}

    def list(self, **kwargs: Any) -> tuple[Any, ...]:
        self.asked = kwargs
        return tuple(SimpleNamespace(name=n) for n in self.names)


def _name(*taken: str) -> str:
    store = _Store(*taken)
    return _unused_default_name(store, CONFIG, START)  # type: ignore[arg-type]


def test_the_first_campaign_of_the_day_keeps_the_plain_name() -> None:
    assert _name() == "Targeted Advertisement, 16 Oct 2026"
    assert _name("Targeted Advertisement, 15 Oct 2026") == "Targeted Advertisement, 16 Oct 2026"


def test_the_second_and_third_are_numbered() -> None:
    base = "Targeted Advertisement, 16 Oct 2026"
    assert _name(base) == f"{base} (2)"
    assert _name(base, f"{base} (2)") == f"{base} (3)"


def test_it_asks_for_every_campaign_of_the_goal() -> None:
    store = _Store()
    _unused_default_name(store, CONFIG, START)  # type: ignore[arg-type]
    assert store.asked == {"use_case_id": "targeted_ad", "limit": None}
