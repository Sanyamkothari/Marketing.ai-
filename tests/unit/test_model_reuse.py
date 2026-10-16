"""Next month's Start: reuse last month's model or learn again, and the one-line hint on which fits."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from engine.config import ModelReuseRules
from engine.doctor.fingerprint import ColumnPrint, Fingerprint
from engine.model_reuse import model_offer
from engine.utils.time import utc_now

RULES = ModelReuseRules(max_age_days=45, max_rate_ratio=1.5)


def _model(age_days: int = 10) -> Any:
    return SimpleNamespace(model_id="m_1", created_at=utc_now() - timedelta(days=age_days))


def _print(rate: float, *names: str) -> Fingerprint:
    return Fingerprint(
        upload_id="u",
        taken_at=utc_now(),
        rows=100,
        outcome_rate=rate,
        columns=tuple(ColumnPrint(name=n, type="float", missing_share=0, distinct=3) for n in names),
    )


def _offer(
    model: Any, missing: list[str] | None, current: Fingerprint | None, last: Fingerprint | None
) -> Any:
    return model_offer(model, missing, now=utc_now(), current=current, last=last, rules=RULES)


def test_the_first_month_has_no_model_to_reuse() -> None:
    offer = _offer(None, None, None, None)
    assert offer.model_version_id is None and not offer.usable and offer.suggest == "learn"


def test_a_model_that_cannot_read_the_file_is_disabled_with_the_reason() -> None:
    offer = _offer(_model(), ["ad_ctr_90d"], _print(0.1, "a"), _print(0.1, "a"))
    assert not offer.usable and offer.suggest == "learn"
    assert offer.reason is not None and "'ad_ctr_90d'" in offer.reason
    unreadable = _offer(_model(), None, None, None)
    assert not unreadable.usable and unreadable.reason  # fails closed


def test_data_that_looks_like_last_months_suggests_reusing() -> None:
    offer = _offer(_model(), [], _print(0.12, "a", "b"), _print(0.11, "a", "b"))
    assert offer.usable and offer.suggest == "reuse" and "looks like last month's" in offer.hint


def test_an_old_model_a_moved_outcome_rate_or_changed_columns_suggest_learning_again() -> None:
    old = _offer(_model(60), [], _print(0.1, "a"), _print(0.1, "a"))
    moved = _offer(_model(), [], _print(0.23, "a"), _print(0.12, "a"))
    changed = _offer(_model(), [], _print(0.1, "a", "c"), _print(0.1, "a", "b"))
    for offer, words in ((old, "60 days old"), (moved, "from 12% to 23%"), (changed, "columns changed")):
        assert offer.usable and offer.suggest == "learn" and words in offer.hint
