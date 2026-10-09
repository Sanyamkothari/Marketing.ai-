"""Choose the offer inside a scoring run of several offers (Plan J M100 part B, DEC-1310 (q) on).

Part A (`engine.decide.offer_choice`) is the rule as a pure function of arrays. This module runs it
**during** a scoring run of a model of several offers, after the actions stage - once suppression, the
control group, per-channel contactability (M99, `channel_contactability.parquet`) and the catalogue
stamp (`catalogue_stamp.json`) exist - and writes what it chose where the treat list reads it:

* `offer_choice.parquet` - one row per scored row, joined to the scores on every key column: the offer
  given (`offer_arm`, 1..K, 0 for none; its level, catalogue action, label and channel), its net value
  (`offer_net_value`) and its total expected cost (`offer_total_cost`: contact cost + offer cost x
  `p_treated`, in rupees - not the catalogue's offer cost alone), the runner-up's offer, channel and net
  value, `offer_reason` (one of `engine.decide.offer_choice.OFFER_REASONS`), and the best offer the
  customer could be given at all (`explore_arm`, its label, channel, net value and total cost: the
  preferred offer before the budget, else the best eligible offer that is not a sleeping dog for them,
  whatever its value), which the treat list gives a customer M92 explores. Row-level: registered in
  `configs/privacy.yaml`, `engine/privacy/layout.py` and `ROW_LEVEL_ARTEFACTS`.
* `offer_choice.json` (:class:`OfferChoiceSummary`) - counts and money only: per offer its costs and
  where they came from, how many customers could get it, were sleeping dogs for it and got it; the
  budget and what was spent. Served with the uplift reports (`UPLIFT_ARTEFACTS`).

`scores.csv` and `scores.parquet` are not touched; a run of one offer never reaches this module (the
seam returns its stage table untouched), so every binary artefact is byte for byte what it was.

**Per offer k** (the model's own `treatment_levels`, control first):

* **costs** - the catalogue action the offer is mapped to (`uplift.policy.arm_action_ids`), read from
  the run's `catalogue_stamp.json`, which holds the catalogue the use case was checked against: its
  offer cost (paid by the customers who take it: offer cost x `p_treated_arm_k`) and contact cost. An
  offer with no catalogue action is priced as M97 prices the first offer's list: `configs/pilot/value.yaml`
  on the value path, else `uplift.policy.cost_per_contact`;
* **net value** - M97's, per offer (`arm_net_values`): predicted extra conversions x value - cost;
* **eligibility** - not suppressed, not held back as control (the persistent holdout included), and
  contactable on at least one of the offer's planned channels (the catalogue action's, else the
  configured channels), from `channel_contactability.parquet` joined on every key column; a customer
  the file does not cover, and every customer of a run that configures no channels, is contactable as
  before. A planned channel the use case does not configure has no flag: it is open only to a customer
  whose consent covers every channel (the file's `all_channel_consent`, written when the consent ledger
  let someone through on a channel grant alone), else to everyone, as in M99;
* **sleeping dog** - the offer's predicted effect at or below the model's sleeping-dog cut: never that
  offer, whatever its value.

Then `choose_offers` with `uplift.policy.min_roi`, the total budget `uplift.policy.total_budget`
(rupees) and `uplift.policy.budget_contacts` (a count), greedy by net value per rupee. A customer's
channel is the first planned channel of their offer they are contactable on.

**When it cannot choose.** With neither `value_per_conversion` nor a value column there is no money to
choose by: `offer_choice.json` says so (`chosen: false`, `code` `OFFER_CHOICE_NOT_MADE`, a plain note),
no row file is written, and the treat list stays part A's (the first offer's list).

Everything is linear in the customers except the budget walk's one sort.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import Field

from engine.contracts import Artefact
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import numpy as np
    import numpy.typing as npt
    import pandas as pd

    from engine.decide.catalogue import CatalogueStamp
    from engine.decide.offer_choice import ArmMoney, OfferChoice
    from engine.pilot.roi import ValueCosts
    from engine.uplift.config import UpliftPolicyConfig

    BoolArray = npt.NDArray[np.bool_]
    ObjectArray = npt.NDArray[np.object_]

__all__ = [
    "OFFER_CHOICE_CODES",
    "OFFER_CHOICE_FILENAME",
    "OFFER_CHOICE_NOT_MADE",
    "OFFER_CHOICE_SUMMARY_FILENAME",
    "OFFER_REASON_TEXT",
    "ArmPlan",
    "OfferArmSummary",
    "OfferChoiceSummary",
    "OfferRows",
    "decide_offers",
    "install_offer_choice",
    "plan_arms",
]

_LOGGER = get_logger(__name__)

OFFER_CHOICE_FILENAME: Final[str] = "offer_choice.parquet"
OFFER_CHOICE_SUMMARY_FILENAME: Final[str] = "offer_choice.json"

OFFER_CHOICE_NOT_MADE: Final[str] = "OFFER_CHOICE_NOT_MADE"
"""`offer_choice.json`'s code when a run of several offers had no value to choose an offer by."""
OFFER_CHOICE_CODES: Final[frozenset[str]] = frozenset({OFFER_CHOICE_NOT_MADE})
"""The code this module defines, for `engine.decide.codes.PLAN_J_CODES` (joined at integration)."""

OFFER_REASON_TEXT: Final[dict[str, str]] = {
    "no_eligible_offer": "No offer can be sent: the customer cannot be reached on a channel of any offer.",
    "sleeping_dog": (
        "Every offer this customer could get is predicted to make them less likely to respond, so none is sent."
    ),
    "below_cost": "No offer is expected to earn more than it costs for this customer.",
    "over_budget": "The campaign budget went to customers worth more per rupee before this one.",
}
"""Why a customer who could be treated got no offer, in plain words (the treat list's `offer_reason`)."""

_NOT_MADE_NOTE: Final[str] = (
    "No offer was chosen per customer: set a value per response (uplift.policy.value_per_conversion) or a "
    "value column, so each offer's net value can be worked out. The treat list is the first offer's."
)
_INSTALLED: Final[str] = "_offer_choice_installed"

CostSource = Literal["catalogue", "value_settings", "run_settings"]


class OfferArmSummary(Artefact):
    """One offer of the choice: what it costs and how many customers it went to."""

    position: int = Field(description="1..K, in the model's treatment_levels order (0 is no offer).")
    level: str = Field(description="The offer's value in the treatment column.")
    action_id: str | None = Field(description="Its catalogue action, or null when it has none.")
    label: str = Field(description="What the treat list calls it: the action's label, else the level.")
    channels: tuple[str, ...] = Field(description="Its planned channels, in order of preference.")
    offer_cost: float = Field(description="Offer cost in rupees, paid by a customer who takes it.")
    contact_cost: float | None = Field(
        description="Cost of one contact in rupees; null when no cost per contact is set."
    )
    cost_source: CostSource = Field(
        description=(
            "Where the costs came from: catalogue (the run's catalogue_stamp.json), value_settings "
            "(configs/pilot/value.yaml) or run_settings (uplift.policy.cost_per_contact)."
        )
    )
    eligible_rows: int = Field(
        description="Customers who could get it: not suppressed, not held back, reachable on one of its channels."
    )
    sleeping_dog_rows: int = Field(
        description="Of the customers neither suppressed nor held back, those it is predicted to put off."
    )
    offered_rows: int = Field(description="Customers it was chosen for.")
    net_value_total: float = Field(description="Predicted net value of the customers it went to, in rupees.")
    cost_total: float = Field(description="Their cost, in rupees.")


class OfferChoiceSummary(Artefact):
    """`offer_choice.json`: the offer chosen per customer, in counts and rupees (Plan J M100 part B)."""

    run_id: str = Field(description="Scoring run the choice belongs to.")
    use_case_id: str = Field(description="Use case of the run.")
    chosen: bool = Field(description="False when no value was set, so no offer could be chosen by money.")
    code: str | None = Field(default=None, description="OFFER_CHOICE_NOT_MADE when chosen is false.")
    note: str | None = Field(default=None, description="Why nothing was chosen, or what was ignored.")
    rows: int = Field(description="Rows scored.")
    levels: tuple[str, ...] = Field(description="The model's treatment levels, control (no offer) first.")
    arms: tuple[OfferArmSummary, ...] = Field(description="Each offer, in levels order.")
    no_offer_rows: int = Field(description="Rows given no offer (suppressed and held-back rows included).")
    reasons: dict[str, int] = Field(
        description="Of the customers neither suppressed nor held back, how many got each outcome."
    )
    budget: float | None = Field(description="uplift.policy.total_budget, in rupees, or null.")
    budget_contacts: int | None = Field(description="uplift.policy.budget_contacts, or null.")
    spent: float = Field(description="Total cost of the offers given, in rupees.")
    net_value_total: float = Field(description="Total predicted net value of the offers given, in rupees.")
    value_per_conversion: float | None = Field(description="The value per response the choice used, if any.")
    value_column: str | None = Field(description="The value column the choice used, if any.")
    catalogue_sha256: str | None = Field(description="The catalogue the costs came from, or null.")
    created_at: datetime = Field(description="UTC time the choice was made.")


@dataclass(frozen=True)
class ArmPlan:
    """One offer as the choice sees it: its level, catalogue action, label, channels and costs."""

    position: int
    level: str
    action_id: str | None
    label: str
    channels: tuple[str, ...]
    costs: ValueCosts
    cost_source: CostSource
    contact_cost: float | None
    """The contact cost as configured; None when nothing sets one (then contacts cost nothing)."""


@dataclass(frozen=True)
class OfferRows:
    """What `decide_offers` worked out per row (every array aligned with the scored rows)."""

    choice: OfferChoice
    money: ArmMoney
    eligible: BoolArray
    """`rows x K`."""
    sleeping_dog: BoolArray
    """`rows x K`."""
    channel: ObjectArray
    """The channel of the offer given; None for no offer."""
    runner_up_channel: ObjectArray
    """The first channel of the runner-up the customer is reachable on; None when there is none."""
    explore_arm: npt.NDArray[np.int_]
    """The best offer the customer could be given at all: the preferred offer before the budget, else
    the runner-up (the best eligible offer that is not a sleeping dog, whatever its value); 0 for none."""
    explore_channel: ObjectArray
    """Its channel; None when there is none."""
    explore_net_value: npt.NDArray[np.float64]
    """Its net value in rupees; NaN when there is none."""
    explore_cost: npt.NDArray[np.float64]
    """Its total expected cost in rupees (contact + offer x p_treated); 0 when there is none."""


def plan_arms(
    levels: Sequence[str],
    policy: UpliftPolicyConfig,
    *,
    stamp: CatalogueStamp | None,
    configured_channels: tuple[str, ...],
    value_costs: ValueCosts | None,
) -> tuple[tuple[ArmPlan, ...], tuple[str, ...]]:
    """Each offer's plan, and the notes on what was ignored (a level or an action the run cannot use)."""
    from engine.pilot.roi import ValueCosts
    from engine.uplift.config import level_text

    keys = [level_text(level) for level in levels]
    mapped = dict(policy.arm_action_ids)
    notes: list[str] = []
    for key in mapped:
        if key not in keys[1:]:
            notes.append(
                f"uplift.policy.arm_action_ids names {key!r}, which is not one of this model's offers."
            )
    stamped = {} if stamp is None else stamp.actions
    plans: list[ArmPlan] = []
    for position, (level, key) in enumerate(zip(levels[1:], keys[1:], strict=True), start=1):
        action_id = mapped.get(key)
        action = None if action_id is None else stamped.get(action_id)
        if action_id is not None and action is None:
            notes.append(
                f"Offer {level!r} names catalogue action {action_id!r}, which the run's catalogue does not "
                "hold; it is priced from the value settings instead."
            )
        if action is not None and action_id is not None:
            plans.append(
                ArmPlan(
                    position=position,
                    level=level,
                    action_id=action_id,
                    label=action.label,
                    channels=tuple(action.channels),
                    costs=ValueCosts(offer_cost=action.offer_cost, contact_cost=action.contact_cost),
                    cost_source="catalogue",
                    contact_cost=action.contact_cost,
                )
            )
            continue
        source: CostSource
        contact: float | None
        if value_costs is not None and policy.cost_per_contact is None:
            costs, source, contact = value_costs, "value_settings", value_costs.contact_cost
        else:
            # M97: the run's own cost per contact (none: contacts cost nothing); an offer cost only on the
            # value path, from the value settings.
            contact = policy.cost_per_contact
            costs = ValueCosts(
                offer_cost=0.0 if value_costs is None else value_costs.offer_cost,
                contact_cost=0.0 if contact is None else contact,
            )
            source = "run_settings"
        plans.append(
            ArmPlan(
                position=position,
                level=level,
                action_id=None,
                label=level,
                channels=configured_channels,
                costs=costs,
                cost_source=source,
                contact_cost=contact,
            )
        )
    return tuple(plans), tuple(notes)


def _first_open(
    channels: tuple[str, ...],
    contactable: Mapping[str, BoolArray] | None,
    rows: int,
    unlisted: BoolArray | None = None,
) -> tuple[BoolArray, ObjectArray]:
    """Per row: reachable on one of `channels`, and the first of them it is reachable on (None if none).

    A channel the run has no flag for (none configured, or not among the configured) is open to the
    rows `unlisted` marks (those whose consent covers every channel), and to everyone without it."""
    import numpy as np

    if not channels:
        return np.ones(rows, dtype=np.bool_), np.full(rows, None, dtype=object)
    first = np.full(rows, None, dtype=object)
    any_open = np.zeros(rows, dtype=np.bool_)
    everyone = np.ones(rows, dtype=np.bool_) if unlisted is None else np.asarray(unlisted, dtype=np.bool_)
    for channel in reversed(channels):
        open_ = everyone if contactable is None else contactable.get(channel, everyone)
        first = np.where(open_, channel, first)
        any_open |= open_
    return any_open, first


def decide_offers(
    uplift: npt.ArrayLike,
    p_treated: npt.ArrayLike,
    *,
    arms: Sequence[ArmPlan],
    policy: UpliftPolicyConfig,
    suppressed: npt.ArrayLike,
    control: npt.ArrayLike,
    sleeping_dog_max: float,
    contactable: Mapping[str, BoolArray] | None = None,
    values: npt.ArrayLike | None = None,
    unlisted: BoolArray | None = None,
) -> OfferRows:
    """The offer per customer from the run's arrays (see the module docstring). Pure; linear in rows.

    `unlisted` marks the rows that may be contacted on a planned channel `contactable` has no flag for
    (None: every row may). `ValueError` when no value is configured (no value per response and no value
    column with values)."""
    import numpy as np

    from engine.decide.offer_choice import NO_OFFER, ArmMoney, arm_net_values, choose_offers

    lift = np.asarray(uplift, dtype=np.float64)
    taken = np.asarray(p_treated, dtype=np.float64)
    rows, count = lift.shape
    if count != len(arms) or taken.shape != lift.shape:
        raise ValueError(f"{len(arms)} offers planned for a prediction of {count}.")
    held = np.asarray(suppressed, dtype=np.bool_) | np.asarray(control, dtype=np.bool_)
    net = np.empty((rows, count), dtype=np.float64)
    cost = np.empty((rows, count), dtype=np.float64)
    eligible = np.empty((rows, count), dtype=np.bool_)
    firsts: list[ObjectArray] = []
    for index, arm in enumerate(arms):
        # A catalogue offer's contact cost is the catalogue's; the run's cost per contact prices the rest.
        own = (
            policy.model_copy(update={"cost_per_contact": None}) if arm.cost_source == "catalogue" else policy
        )
        money = arm_net_values(
            lift[:, [index]], own, arm_costs=[arm.costs], p_treated=taken[:, [index]], values=values
        )
        net[:, index] = money.net_value[:, 0]
        cost[:, index] = money.cost[:, 0]
        reachable, first = _first_open(arm.channels, contactable, rows, unlisted)
        eligible[:, index] = ~held & reachable
        firsts.append(first)
    dogs = np.asarray(lift <= sleeping_dog_max, dtype=np.bool_)
    choice = choose_offers(
        net,
        cost,
        sleeping_dog=dogs,
        eligible=eligible,
        budget=policy.total_budget,
        min_roi=0.0 if policy.min_roi is None else float(policy.min_roi),
        max_offers=policy.budget_contacts,
    )
    # The best offer the customer could be given at all: the preferred one before the budget, else the
    # runner-up (with no preferred offer it is the best eligible offer that is not a sleeping dog).
    explore = np.where(choice.preferred_arm != NO_OFFER, choice.preferred_arm, choice.runner_up_arm)
    rows_index = np.arange(rows)
    column = np.maximum(explore - 1, 0)
    has_explore = explore != NO_OFFER
    explore_value = np.where(has_explore, net[rows_index, column], np.nan)
    explore_cost = np.where(has_explore, cost[rows_index, column], 0.0)
    channel = np.full(rows, None, dtype=object)
    runner_channel = np.full(rows, None, dtype=object)
    explore_channel = np.full(rows, None, dtype=object)
    for index, first in enumerate(firsts):
        mine = choice.arm == index + 1
        channel[mine] = first[mine]
        runner = choice.runner_up_arm == index + 1
        runner_channel[runner] = first[runner]
        best = explore == index + 1
        explore_channel[best] = first[best]
    channel[choice.arm == NO_OFFER] = None
    return OfferRows(
        choice=choice,
        money=ArmMoney(net_value=net, cost=cost),
        eligible=eligible,
        sleeping_dog=dogs,
        channel=channel,
        runner_up_channel=runner_channel,
        explore_arm=np.asarray(explore, dtype=np.int_),
        explore_channel=explore_channel,
        explore_net_value=np.asarray(explore_value, dtype=np.float64),
        explore_cost=np.asarray(explore_cost, dtype=np.float64),
    )


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------
def install_offer_choice(score_flow: type[Any]) -> None:
    """Wrap `score_flow._bodies` so a scoring run of several offers chooses the offer after actions.

    Idempotent. Installed after `engine.decide.contactability`'s seam, so its actions body (and the
    holdout service's inside it) has run first. A flow that is not an uplift score flow, and an uplift
    run of one offer, are untouched: the wrapper checks `_arm_prediction` after the body and returns.
    """
    if getattr(score_flow, _INSTALLED, False):
        return
    original: Callable[[Any], tuple[tuple[Any, Callable[[], Any]], ...]] = score_flow._bodies

    def _bodies(self: Any) -> tuple[tuple[Any, Callable[[], Any]], ...]:
        bodies = original(self)
        if not hasattr(self, "_arm_prediction"):
            return bodies
        return tuple((key, _after_actions(self, key, body)) for key, body in bodies)

    score_flow._bodies = _bodies
    setattr(score_flow, _INSTALLED, True)


def _after_actions(flow: Any, key: Any, body: Callable[[], Any]) -> Callable[[], Any]:
    from functools import wraps

    from engine.contracts import StageKey

    if key is not StageKey.ACTIONS:
        return body

    @wraps(body)
    def acted() -> Any:
        outcome = body()
        if getattr(flow, "_arm_prediction", None) is not None:
            _choose(flow)
        return outcome

    return acted


def _row_text(frame: pd.DataFrame, primary_key: Any) -> pd.Series:
    """One text per row naming it from every key column (`engine.keys.key_text`, joined)."""
    from engine.keys import KEY_SEPARATOR, key_columns, key_text

    names = key_columns(primary_key)
    joined = key_text(frame[names[0]]).astype("object")
    for name in names[1:]:
        joined = joined + KEY_SEPARATOR + key_text(frame[name]).astype("object")
    return joined.reset_index(drop=True)


def _contactable(flow: Any, keys: pd.DataFrame) -> tuple[dict[str, BoolArray] | None, BoolArray | None]:
    """Per configured channel, the run's flag per scored row, joined on every key column; and the rows
    that may be contacted on a channel the use case does not configure (the file's `all_channel_consent`,
    None when it has none: every row may).

    `(None, None)` when the run wrote no per-channel contactability (no channels configured). A row the
    file does not cover is contactable, as before M99."""
    import numpy as np
    import pandas as pd

    from engine.decide.contactability import (
        ALL_CHANNEL_CONSENT_COLUMN,
        CHANNEL_CONTACTABILITY_FILENAME,
        contactable_column,
    )
    from engine.storage import StorageError, run_key

    ctx = flow._ctx
    channels = tuple(ctx.config.actions.suppression.channels)
    try:
        data = flow._storage.read_bytes(run_key(ctx.run_id, CHANNEL_CONTACTABILITY_FILENAME))
    except StorageError:
        return None, None
    table = pd.read_parquet(io.BytesIO(data))
    index = pd.Index(_row_text(table, ctx.key))
    if not index.is_unique:  # pragma: no cover - the run writes one row per scored row
        raise RuntimeError(f"{CHANNEL_CONTACTABILITY_FILENAME} repeats a customer key")
    positions = index.get_indexer(pd.Index(_row_text(keys, ctx.key)))
    covered = positions >= 0
    flags: dict[str, BoolArray] = {}
    for channel in channels:
        name = contactable_column(channel)
        if name not in table.columns:  # pragma: no cover - the run writes every configured channel
            continue
        values = table[name].to_numpy(dtype=bool)[np.where(covered, positions, 0)]
        flags[channel] = np.where(covered, values, True)
    unlisted: BoolArray | None = None
    if ALL_CHANNEL_CONSENT_COLUMN in table.columns:
        every = table[ALL_CHANNEL_CONSENT_COLUMN].to_numpy(dtype=bool)[np.where(covered, positions, 0)]
        unlisted = np.where(covered, every, True)
    return flags, unlisted


def _stamp(flow: Any) -> CatalogueStamp | None:
    from engine.decide.catalogue import CATALOGUE_STAMP_FILENAME, CatalogueStamp
    from engine.storage import StorageError, run_key

    key = run_key(flow._ctx.run_id, CATALOGUE_STAMP_FILENAME)
    if not flow._storage.exists(key):
        return None
    try:
        stamp: CatalogueStamp = flow._storage.read_model(key, CatalogueStamp)
    except (StorageError, ValueError):
        _LOGGER.warning("offer choice: %s could not be read", CATALOGUE_STAMP_FILENAME)
        return None
    return stamp


def _choose(flow: Any) -> None:
    """Choose, then write `offer_choice.parquet` and `offer_choice.json` (see the module docstring)."""
    import numpy as np
    import pandas as pd

    from engine.decide.offer_choice import NO_OFFER, OFFER_REASONS
    from engine.stages.actions import CONTROL_GROUP_COLUMN, SUPPRESSED_REASON_COLUMN
    from engine.stages.export import _key_output
    from engine.storage import run_key
    from engine.utils.time import utc_now

    ctx = flow._ctx
    scored: pd.DataFrame = flow._scored
    card = flow._card
    predicted = flow._arm_prediction
    levels = tuple(card.treatment_levels or ())
    policy = ctx.config.uplift.policy
    stamp = _stamp(flow)
    # The costs the run's actions stage read from `configs/pilot/value.yaml` (read ONCE per run, M97), so
    # an edit during the run cannot price the choice differently from the first offer's list.
    value_costs = flow._run_value_costs if hasattr(flow, "_run_value_costs") else flow._value_costs(scored)
    arms, notes = plan_arms(
        levels,
        policy,
        stamp=stamp,
        configured_channels=tuple(ctx.config.actions.suppression.channels),
        value_costs=value_costs,
    )
    rows = len(scored.index)
    suppressed = scored[SUPPRESSED_REASON_COLUMN].notna().to_numpy(dtype=bool)
    control = scored[CONTROL_GROUP_COLUMN].astype(bool).to_numpy(dtype=bool)
    values = (
        pd.to_numeric(scored[policy.value_column], errors="coerce").to_numpy(dtype=np.float64)
        if policy.value_column is not None and policy.value_column in scored.columns
        else None
    )
    keys = pd.DataFrame(_key_output(scored, ctx.key, key_source=flow._frame)).reset_index(drop=True)
    common: dict[str, Any] = {
        "run_id": ctx.run_id,
        "use_case_id": ctx.config.id,
        "rows": rows,
        "levels": levels,
        "budget": policy.total_budget,
        "budget_contacts": policy.budget_contacts,
        "value_per_conversion": policy.value_per_conversion,
        "value_column": policy.value_column if values is not None else None,
        "catalogue_sha256": None if stamp is None else stamp.catalogue_sha256,
        "created_at": utc_now(),
    }
    contactable, unlisted = _contactable(flow, keys)
    try:
        decided = decide_offers(
            predicted.uplift,
            predicted.p_treated,
            arms=arms,
            policy=policy,
            suppressed=suppressed,
            control=control,
            sleeping_dog_max=card.segment_thresholds.sleeping_dog_max_uplift,
            contactable=contactable,
            values=values,
            unlisted=unlisted,
        )
    except ValueError as exc:
        _LOGGER.warning("offer choice: not made: %s", exc)
        flow._write(
            OFFER_CHOICE_SUMMARY_FILENAME,
            OfferChoiceSummary(
                **common,
                chosen=False,
                code=OFFER_CHOICE_NOT_MADE,
                note=_NOT_MADE_NOTE,
                arms=tuple(_arm_summary(arm, None, None) for arm in arms),
                no_offer_rows=rows,
                reasons={},
                spent=0.0,
                net_value_total=0.0,
            ),
        )
        return

    choice = decided.choice
    by_position = {arm.position: arm for arm in arms}

    def labelled(codes: np.ndarray, field: str) -> np.ndarray:
        lookup = np.array(
            [None, *(getattr(by_position[k], field) for k in range(1, len(arms) + 1))], dtype=object
        )
        named: np.ndarray = lookup[codes]
        return named

    table = keys.copy()
    table["offer_arm"] = choice.arm.astype(np.int64)
    table["offer_level"] = labelled(choice.arm, "level")
    table["offer_action_id"] = labelled(choice.arm, "action_id")
    table["offer_label"] = labelled(choice.arm, "label")
    table["offer_channel"] = decided.channel
    table["offer_net_value"] = choice.net_value
    table["offer_total_cost"] = choice.cost
    table["runner_up_arm"] = choice.runner_up_arm.astype(np.int64)
    table["runner_up_label"] = labelled(choice.runner_up_arm, "label")
    table["runner_up_channel"] = decided.runner_up_channel
    table["runner_up_net_value"] = choice.runner_up_net_value
    table["offer_reason"] = np.asarray(OFFER_REASONS, dtype=object)[choice.reason_code]
    table["explore_arm"] = decided.explore_arm.astype(np.int64)
    table["explore_label"] = labelled(decided.explore_arm, "label")
    table["explore_channel"] = decided.explore_channel
    table["explore_net_value"] = decided.explore_net_value
    table["explore_total_cost"] = decided.explore_cost
    buffer = io.BytesIO()
    table.to_parquet(buffer, engine="pyarrow", index=False)
    parquet_key = run_key(ctx.run_id, OFFER_CHOICE_FILENAME)
    flow._storage.write_bytes(parquet_key, buffer.getvalue())
    flow._artefacts[OFFER_CHOICE_FILENAME] = parquet_key

    open_rows = ~(suppressed | control)
    reason_words = np.asarray(OFFER_REASONS, dtype=object)[choice.reason_code[open_rows]]
    names, counts = np.unique(reason_words.astype(str), return_counts=True)
    summary = OfferChoiceSummary(
        **common,
        chosen=True,
        note=" ".join(notes) or None,
        arms=tuple(_arm_summary(arm, decided, open_rows) for arm in arms),
        no_offer_rows=int((choice.arm == NO_OFFER).sum()),
        reasons={str(name): int(count) for name, count in zip(names, counts, strict=True)},
        spent=round(float(choice.spent), 2),
        net_value_total=round(float(np.nansum(choice.net_value)), 2),
    )
    flow._write(OFFER_CHOICE_SUMMARY_FILENAME, summary)
    _LOGGER.info(
        "offer choice: rows=%d offers=%s no_offer=%d spent=%.2f budget=%s",
        rows,
        ",".join(f"{arm.level}:{item.offered_rows}" for arm, item in zip(arms, summary.arms, strict=True)),
        summary.no_offer_rows,
        summary.spent,
        policy.total_budget,
    )


def _arm_summary(arm: ArmPlan, decided: OfferRows | None, open_rows: BoolArray | None) -> OfferArmSummary:

    contact = arm.contact_cost
    base: dict[str, Any] = {
        "position": arm.position,
        "level": arm.level,
        "action_id": arm.action_id,
        "label": arm.label,
        "channels": arm.channels,
        "offer_cost": arm.costs.offer_cost,
        "cost_source": arm.cost_source,
    }
    if decided is None or open_rows is None:
        return OfferArmSummary(
            **base,
            contact_cost=contact,
            eligible_rows=0,
            sleeping_dog_rows=0,
            offered_rows=0,
            net_value_total=0.0,
            cost_total=0.0,
        )
    k = arm.position - 1
    mine = decided.choice.arm == arm.position
    return OfferArmSummary(
        **base,
        contact_cost=contact,
        eligible_rows=int(decided.eligible[:, k].sum()),
        sleeping_dog_rows=int((decided.sleeping_dog[:, k] & open_rows).sum()),
        offered_rows=int(mine.sum()),
        net_value_total=round(float(decided.choice.net_value[mine].sum()), 2),
        cost_total=round(float(decided.choice.cost[mine].sum()), 2),
    )
