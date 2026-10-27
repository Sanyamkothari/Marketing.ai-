"""What the proof may claim when the shop's margin or a channel's price is not known (docs/v4/ONBOARDING_MARGIN.md).

Profit is margin less cost. Two inputs are the shop's own and have no honest default:

* **the margin**: purchases that carry no margin of their own are counted at the shop's contribution
  margin (`RuntimeSettings.margin_rate`). Until the shop has answered the onboarding question, those
  purchases would be counted at their full value, which overstates profit, so the profit figures are
  withheld and the page shows revenue only;
* **the price of a message**: a channel the shop sends on but has no price for would be counted as free,
  which also overstates profit. The proof names the channel and withholds profit instead of using a
  dollar-sized number as the shop's currency (docs/v3/CURRENCY.md).

Revenue, the groups, the spend that can be counted and the conversion comparison stay as they were.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from api.v2.engines.measurement import FinancialImpact, MeasurementSummary
from autonomy.contracts import Channel, Decision, Event, ProofSummary
from autonomy.onboarding.limits import MARGIN_QUESTION
from autonomy.proof.rewards import sent_decisions
from autonomy.runtime.prices import PAID_CHANNELS

__all__ = [
    "MARGIN_EXPLANATION",
    "MARGIN_QUESTION",
    "margin_missing_note",
    "unpriced_channels",
    "unpriced_note",
    "withhold_measurement_profit",
    "withhold_proof_profit",
]

MARGIN_EXPLANATION = (
    "This is your contribution margin. Say the shop sells 100 in a week and 60 goes on what the products "
    "cost you, shipping and payment fees: the answer is 40. Count refused cash-on-delivery parcels and "
    "returns in it too. We use it to turn extra sales into extra profit, so the proof never shows a profit "
    "we made up. Until you answer, the proof shows revenue only."
)


def margin_missing_note() -> str:
    """Why the proof shows revenue only: the margin question has not been answered."""
    return (
        "Profit needs your margin, and it is not set, so this page shows revenue only. "
        f"{MARGIN_QUESTION} Answer it in onboarding (or set AUTONOMY_MARGIN_RATE) and profit appears."
    )


def unpriced_channels(
    decisions: Iterable[Decision], events: Iterable[Event], prices: Mapping[Channel, float]
) -> tuple[Channel, ...]:
    """The paid channels these decisions were really sent on (`sent_decisions` of `events`) that have no price
    in `prices`, in channel order: the sends whose cost would otherwise be counted as nothing."""
    sent = sent_decisions(events)
    used = {
        d.action.channel
        for d in decisions
        if d.action is not None and d.decision_id in sent and d.action.channel in PAID_CHANNELS
    }
    return tuple(c for c in sorted(used, key=lambda c: c.value) if c not in prices)


def unpriced_note(channels: Iterable[Channel]) -> str:
    """Names the channels with no price: their messages are not counted, so profit is not shown."""
    names = ", ".join(c.value for c in channels)
    return (
        f"No price is set for {names}, so what those messages cost is not counted and profit is not shown. "
        "Set the price of one message per channel (AUTONOMY_CHANNEL_COSTS, in the shop's currency)."
    )


def _reason(margin_rate: float | None, margin_assumed: bool, unpriced: tuple[Channel, ...]) -> str | None:
    parts: list[str] = []
    if margin_rate is None and margin_assumed:
        parts.append(margin_missing_note())
    if unpriced:
        parts.append(unpriced_note(unpriced))
    return " ".join(parts) if parts else None


def withhold_proof_profit(
    summary: ProofSummary, *, margin_rate: float | None, unpriced: tuple[Channel, ...] = ()
) -> ProofSummary:
    """The summary with every profit figure removed when profit would rest on an unknown margin or price.

    The margin matters only when some of the margin counted was assumed (`margin_note` is set): a shop
    whose orders all carry their own margin needs no answer. Revenue, spend and the groups are kept.
    """
    reason = _reason(margin_rate, summary.margin_note is not None, unpriced)
    if reason is None:
        return summary
    return summary.model_copy(
        update={
            "incremental_profit": None,
            "lift_vs_baseline": None,
            "roi": None,
            "detectable": None,
            "margin_note": reason,
        }
    )


def withhold_measurement_profit(
    summary: MeasurementSummary, *, margin_rate: float | None, unpriced: tuple[Channel, ...] = ()
) -> MeasurementSummary:
    """`withhold_proof_profit` for the measurement ledger's summary."""
    reason = _reason(margin_rate, summary.financial_impact.margin_note is not None, unpriced)
    if reason is None:
        return summary
    money: FinancialImpact = summary.financial_impact.model_copy(
        update={
            "net_incremental_profit": None,
            "net_incremental_profit_p_value": None,
            "roi_ratio": None,
            "minimum_detectable_profit": None,
            "margin_note": reason,
        }
    )
    return summary.model_copy(update={"financial_impact": money})
