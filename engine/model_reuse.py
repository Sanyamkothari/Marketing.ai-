"""Next month's Start: "Use last month's model" or "Learn again on the new data", and which one fits.

The first month there is no current model, so only learning is offered. After that the current model (the
champion) can score the new prepared file when the file has the columns it needs; when it cannot, reusing is
disabled with the reason. The one-line hint is a tiny rule over what is already known (`ModelReuseRules` in
`configs/engine.yaml`): learn again when the model is old, the outcome rate moved a lot or the columns changed
since the last approved file; otherwise reuse. Only counts, shares and column names are used - no customer values.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Literal

from engine.config import ModelReuseRules, StrictBase
from engine.contracts import FeatureSchema, ModelVersion
from engine.doctor.fingerprint import Fingerprint
from engine.storage import Storage

__all__ = ["ModelOffer", "missing_columns", "model_offer"]


class ModelOffer(StrictBase):
    """What Start shows: the model that could be reused (none the first month), whether it can, and which fits."""

    model_version_id: str | None = None
    usable: bool = False
    reason: str | None = None  # why reusing is disabled
    suggest: Literal["reuse", "learn"] = "learn"
    hint: str = ""
    age_days: int | None = None


def missing_columns(storage: Storage, model: ModelVersion, offered: Collection[str]) -> list[str] | None:
    """The columns `model` needs that the file does not have; None when its schema cannot be read."""
    try:
        schema = storage.read_model(model.schema_key, FeatureSchema)
    except Exception:  # unreadable or gone: the caller decides what that means
        return None
    return [c.name for c in schema.columns if c.required and c.name not in set(offered)]


def _pct(rate: float) -> str:
    return f"{round(rate * 100)}%"


def _moved(a: float, b: float, ratio: float) -> bool:
    if a <= 0 or b <= 0:
        return (a > 0) != (b > 0)
    return max(a / b, b / a) > ratio


def model_offer(
    model: ModelVersion | None,
    missing: list[str] | None,
    *,
    now: datetime,
    current: Fingerprint | None,
    last: Fingerprint | None,
    rules: ModelReuseRules,
) -> ModelOffer:
    """The offer for one prepared file. `missing` is `missing_columns` of the current model (None: unreadable)."""
    if model is None:
        return ModelOffer()
    if missing is None:
        return ModelOffer(
            model_version_id=model.model_id, reason="Last month's model cannot be checked against this file."
        )
    if missing:
        names = ", ".join(f"'{n}'" for n in missing[:5]) + (" and others" if len(missing) > 5 else "")
        return ModelOffer(
            model_version_id=model.model_id,
            reason=f"Last month's model needs {names}, which this file does not have.",
            hint="Learn again on the new data.",
        )
    age = max(0, (now - model.created_at).days)
    why: list[str] = []
    if age > rules.max_age_days:
        why.append(f"the model is {age} days old")
    if current is not None and last is not None:
        if {c.name for c in current.columns} != {c.name for c in last.columns}:
            why.append("some columns changed")
        now_rate, was = current.outcome_rate, last.outcome_rate
        if now_rate is not None and was is not None and _moved(now_rate, was, rules.max_rate_ratio):
            why.append(f"the outcome rate moved from {_pct(was)} to {_pct(now_rate)}")
    hint = (
        f"Learning again fits better: {', '.join(why)}."
        if why
        else "This data looks like last month's, so the model you have should still fit."
    )
    return ModelOffer(
        model_version_id=model.model_id,
        usable=True,
        suggest="learn" if why else "reuse",
        hint=hint,
        age_days=age,
    )
