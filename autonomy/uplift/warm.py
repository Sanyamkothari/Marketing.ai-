"""One call for the runtime: fit an uplift model on the history's latest past campaign and attach it to
the response model, so every bidder that shares that model reads it.

    result = warm_start_uplift(policy.bundle.response, states, history, content_by_id, now,
                               horizon_days=policy.learn_horizon_days, decisions=past_decisions)

Where it belongs: the agent's warm start (`autonomy.runtime.agent.AgentPolicy._warm_start`, which has
the history and the states together before the first decision), and again nightly if a newer
campaign has closed. It refits from scratch each time and replaces what was attached; when the
history supports no model (no logged probabilities, no control group, the balance check fails) it
detaches any old one and says why. Pure apart from the attachment: time is passed in.
"""

from __future__ import annotations

from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from autonomy.contracts import ContentItem, CustomerState, Decision
from autonomy.uplift.campaign import campaign_table
from autonomy.uplift.features import EventLike
from autonomy.uplift.learner import UpliftLearner
from autonomy.uplift.model import FittedUplift, fit_uplift
from autonomy.uplift.score import attach_uplift, detach_uplift

__all__ = ["UpliftWarmStart", "warm_start_uplift"]


@dataclass(frozen=True)
class UpliftWarmStart:
    """What `warm_start_uplift` did: the model (None when none could be fitted) and why."""

    model: FittedUplift | None
    attached: bool
    notes: tuple[str, ...]


def warm_start_uplift(
    response_model: object,
    states: Iterable[CustomerState] | Mapping[str, CustomerState],
    events_by_customer: Mapping[str, Sequence[EventLike]],
    content_by_id: Mapping[str, ContentItem],
    now: datetime,
    *,
    horizon_days: float = 7.0,
    decisions: Iterable[Decision] = (),
    exclude: Container[str] = frozenset(),
    learner: UpliftLearner | None = None,
) -> UpliftWarmStart:
    """Fit on the latest closed past campaign and attach the model to `response_model` (see the module
    docstring). `learner` swaps the default numpy learner, e.g. for the v1 AutoML bridge."""
    by_id = dict(states) if isinstance(states, Mapping) else {s.customer_id: s for s in states}
    table, notes = campaign_table(
        events_by_customer,
        by_id,
        content_by_id,
        now,
        horizon_days=horizon_days,
        decisions=decisions,
        exclude=exclude,
    )
    if table is None:
        detach_uplift(response_model)
        return UpliftWarmStart(model=None, attached=False, notes=tuple(notes))
    model, fit_notes = fit_uplift(table, learner=learner)
    if model is None:
        detach_uplift(response_model)
        return UpliftWarmStart(model=None, attached=False, notes=tuple(fit_notes))
    attached = attach_uplift(response_model, model)
    return UpliftWarmStart(model=model, attached=attached, notes=tuple(fit_notes))
