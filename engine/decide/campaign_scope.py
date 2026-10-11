"""An approved campaign's limits on its run's treat list (Plan M M184, DEC-1614 (a)).

An approved campaign plan (`engine.agentic.planner.execute_plan`) was checked by the policy on a payload that
names **one channel** and **a number of contacts** (`planned_contacts`). The run's treat list, built on its own,
contacts everyone it may on whichever planned channel each customer has. So that what is sent is what was
checked, EXECUTE writes the approved limits beside the run's artefacts as `campaign_scope.json`, and every build
of the run's treat list (`engine.decide.treat_list.build_treat_list`) applies them, after the compliance gate and
before the message arms are drawn:

1. **Channel.** A customer the list would contact on any other channel (or on none) is not contacted:
   `treat = 0`, no channel and no offer, `offer_reason` says why.
2. **Number.** When more customers are left than `max_contacts`, the first `max_contacts` are kept in the list's
   own priority order, from the highest value of the run's **ranking column** down (a row missing it last, ties
   broken by the customer key's text in ascending order, so the cut is the same every time it is made). The rest
   are not contacted, as in 1. The ranking column is chosen from the run itself
   (`engine.decide.treat_list._scope_ranking`): on an uplift run, the list's `net_value` when it holds values,
   else the predicted `uplift`; on a propensity run, the run's score (`actions.score_field` of its own settings).
   An uplift run whose list was ranked by a propensity model (`ranking_choice.json` says `propensity_model`) does
   not keep that model's score, so it has no ranking column. **When a cut is needed and there is no ranking
   column (missing, or null on every row), nothing is cut by key order**: `CampaignScopeError`, which the build
   turns into `CAMPAIGN_LIST_NOT_AS_APPROVED`, and EXECUTE refuses. The column used is written to the summary
   (`campaign_scope_ranked_by`) and named in each over-limit row's `offer_reason`.

**What is never touched.** The held-back rows (`holdout`, `control_group`) keep their flags, and the campaign's
comparison is built from the run's scores exactly as before (`engine.measurement.campaign.build_assignment`): the
customers the limits leave uncontacted stay in the intended population, so the measured effect is intent to
treat, as it already is for a customer no channel could reach. The explore flag is kept; an explored customer the
limits leave out is not contacted.

The file holds ids and two limits, never a customer row, and is written only by EXECUTE. A run without it -
every run before M184 and every run not carried out by the planner - builds its treat list exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import pandas as pd
from pydantic import AwareDatetime, Field

from engine.contracts import Artefact
from engine.storage import Storage, StorageError, run_key

__all__ = [
    "CAMPAIGN_SCOPE_FILENAME",
    "OTHER_CHANNEL",
    "OVER_LIMIT",
    "CampaignScope",
    "CampaignScopeError",
    "ScopeCut",
    "apply_campaign_scope",
    "priority_order",
    "ranking_label",
    "read_campaign_scope",
    "scope_note",
    "write_campaign_scope",
]

CAMPAIGN_SCOPE_FILENAME: Final[str] = "campaign_scope.json"

OTHER_CHANNEL: Final[str] = "other_channel"
"""`TreatListSummary.campaign_scope_withheld` key: customers the list would have contacted on another channel."""
OVER_LIMIT: Final[str] = "over_limit"
"""`TreatListSummary.campaign_scope_withheld` key: customers beyond the approved number of contacts."""

OTHER_CHANNEL_REASON: Final[str] = (
    "Not contacted: the approved campaign sends on {channel} only, and this customer would be contacted on "
    "another channel."
)
OVER_LIMIT_REASON: Final[str] = (
    "Not contacted: the approved campaign contacts at most {limit} customers, taken from the highest {ranked_by} "
    "down."
)

_RANKING_LABELS: Final[dict[str, str]] = {"net_value": "net value", "uplift": "predicted uplift"}


class CampaignScopeError(ValueError):
    """The approved number of contacts needs a cut, and the run has no column to rank its customers by."""


def ranking_label(column: str | None) -> str:
    """The words for the ranking column in `offer_reason`: `net value`, `predicted uplift` or `score (<column>)`."""
    if column is None:
        return "score"
    return _RANKING_LABELS.get(column, f"score ({column})")


class CampaignScope(Artefact):
    """`campaign_scope.json`: the approved campaign's limits on the run's treat list. No customer row."""

    run_id: str = Field(description="The scoring run whose treat list is limited.")
    channel: str = Field(description="The only channel the approved campaign sends on.")
    max_contacts: int | None = Field(
        default=None,
        ge=0,
        description="The most customers it contacts; null for no limit beyond the channel.",
    )
    proposal_id: str | None = Field(default=None, description="The approved proposal that set these limits.")
    set_at: AwareDatetime = Field(description="UTC time the limits were written (at the approval).")


@dataclass(frozen=True)
class ScopeCut:
    """The treat flags and channels after the limits, and how many customers each limit took off."""

    treat: np.ndarray
    channel: np.ndarray
    other_channel: np.ndarray
    """Per row: contacted before the limits, but on another channel (or none)."""
    over_limit: np.ndarray
    """Per row: on the approved channel, but beyond the approved number."""

    def withheld(self) -> dict[str, int]:
        return {OTHER_CHANNEL: int(self.other_channel.sum()), OVER_LIMIT: int(self.over_limit.sum())}


def read_campaign_scope(storage: Storage, run_id: str) -> CampaignScope | None:
    """The run's limits, or None when no approved campaign set any (the default)."""
    key = run_key(run_id, CAMPAIGN_SCOPE_FILENAME)
    if not storage.exists(key):
        return None
    try:
        return storage.read_model(key, CampaignScope)
    except (StorageError, ValueError) as exc:
        # Fail closed: limits that cannot be read never let the whole list through.
        raise ValueError(f"{CAMPAIGN_SCOPE_FILENAME} of run {run_id} could not be read: {exc}") from exc


def write_campaign_scope(storage: Storage, scope: CampaignScope) -> None:
    storage.write_model(run_key(scope.run_id, CAMPAIGN_SCOPE_FILENAME), scope)


def apply_campaign_scope(
    scope: CampaignScope,
    *,
    treat: Any,
    channel: Any,
    priority: Any | None,
    keys: Any,
    no_ranking: str | None = None,
) -> ScopeCut:
    """The limits applied to one treat list (see the module docstring).

    `treat`, `channel` (each row's channel, None for none), `priority` (the run's ranking column; NaN for a row
    missing it; None when the run has none) and `keys` (each row's key text) are aligned to the treat list's rows.
    Raises `CampaignScopeError` (with `no_ranking` as the reason, when given) when a cut is needed and `priority`
    is None or null on every row: the cut is never made by key order alone.
    """
    treat_in = np.asarray(treat, dtype=bool)
    channels = np.asarray(pd.Series(channel, dtype="object").to_numpy(dtype=object), dtype=object)
    on_channel = np.asarray(channels == scope.channel, dtype=bool)
    other = treat_in & ~on_channel
    kept = treat_in & on_channel
    over = np.zeros(len(treat_in), dtype=bool)
    if scope.max_contacts is not None and int(kept.sum()) > scope.max_contacts:
        if priority is None or not pd.to_numeric(pd.Series(priority), errors="coerce").notna().any():
            raise CampaignScopeError(
                f"The approved campaign contacts at most {scope.max_contacts:,} customers and the list holds "
                f"{int(kept.sum()):,} on {scope.channel}, but "
                + (no_ranking or "the run has no score to rank its customers by")
                + ", so which customers to keep cannot be decided. The list is not cut by customer key: nothing "
                "is sent. Plan again on a run whose list is ranked by its own scores."
            )
        order = priority_order(priority, keys)
        ranked = order[kept[order]]
        over[ranked[scope.max_contacts :]] = True
        kept &= ~over
    out_channel = channels.copy()
    out_channel[other | over] = None
    return ScopeCut(treat=kept, channel=out_channel, other_channel=other, over_limit=over)


def priority_order(priority: Any, keys: Any) -> np.ndarray:
    """Row positions from the highest score down; a missing score last; ties by key text, ascending."""
    score = pd.to_numeric(pd.Series(priority), errors="coerce").to_numpy(dtype=float)
    texts = pd.Series(keys, dtype="object").astype(str).to_numpy(dtype=object)
    key_rank = np.empty(len(texts), dtype=np.int64)
    key_rank[np.argsort(texts, kind="stable")] = np.arange(len(texts), dtype=np.int64)
    missing = np.isnan(score)
    filled = np.where(missing, 0.0, -score)
    # np.lexsort sorts by the last key first: missing scores last, then the highest score, then the key.
    return np.asarray(np.lexsort((key_rank, filled, missing)), dtype=np.int64)


def scope_note(scope: CampaignScope, ranked_by: str | None = None) -> dict[str, str]:
    """The `offer_reason` text for each limit, filled in; `ranked_by` is the ranking column the cut used."""
    limit = "no" if scope.max_contacts is None else f"{scope.max_contacts:,}"
    return {
        OTHER_CHANNEL: OTHER_CHANNEL_REASON.format(channel=scope.channel),
        OVER_LIMIT: OVER_LIMIT_REASON.format(limit=limit, ranked_by=ranking_label(ranked_by)),
    }
