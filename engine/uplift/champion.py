"""The champion rule for uplift models (plan B §5), as a pure function.

Phase 1's rule (`engine.registry.should_promote`) promotes a challenger that beats the champion by
`min_improvement_pct` on the same hold-out. Uplift keeps that shape and adds two gates in front of
it, because a better AUUC is only worth deploying when it means something:

1. **Causal.** When `TREATMENT_NOT_RANDOM` was acknowledged, the "uplift" is a difference between
   the people who were contacted and the people who were not - not what contacting changed. Such a
   model may be shown, never promoted: it would target whoever looks like past targets.
2. **Measurable.** The challenger's AUUC interval must lie above zero (`ci_low > 0`). A model whose
   interval includes zero cannot be shown to target better than random, and random targeting needs
   no model. A missing lower bound is treated the same way: an interval nobody measured is not
   evidence.

Only then is the challenger compared with the champion, on AUUC. That comparison is meaningful
only when both were measured on the SAME hold-out - the Phase 1 precondition of DEC-044, and the
caller's duty. This function cannot see the rows, but it refuses the comparison outright
(`ValueError`) when the two evaluations disagree on the row, treated or control counts, which is the
cheapest proof that they were not, or when both carry a `holdout_fingerprint` (a hash of the
hold-out's primary keys) and the fingerprints differ: equal counts on different customers are
caught too (DEC-670).

A relative improvement over a champion AUUC that is zero or negative is undefined, so there any
strictly greater AUUC wins (the challenger already cleared `ci_low > 0`) and `improvement_pct` is
`None` rather than a number divided by nothing. The classification champion rule in
`engine/registry.py` is not touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.uplift.contracts import UpliftEvaluation

__all__ = ["UpliftChampionDecision", "decide_uplift_champion"]


@dataclass(frozen=True)
class UpliftChampionDecision:
    """Whether the challenger becomes the uplift champion, and a sentence saying why."""

    promote: bool
    reason: str
    improvement_pct: float | None
    """AUUC gain over the champion as a percentage of |champion AUUC|; `None` when not computed."""


def _auuc(value: float) -> str:
    text = f"{value:.4f}"
    return "0.0000" if text == "-0.0000" else text


def decide_uplift_champion(
    challenger: UpliftEvaluation,
    champion: UpliftEvaluation | None,
    *,
    min_improvement_pct: float,
) -> UpliftChampionDecision:
    """Apply the uplift champion rule; see the module docstring for the order of the gates.

    PRECONDITION: when `champion` is given it must have been re-scored on the challenger's hold-out.
    Different row, treated or control counts raise `ValueError`, and so do two different hold-out
    fingerprints when both evaluations carry one.
    """
    if not challenger.causal:
        return UpliftChampionDecision(
            promote=False,
            reason=(
                "Not promoted: the treatment was not randomly assigned, so this model's uplift is not "
                "causal and it cannot become the champion."
            ),
            improvement_pct=None,
        )
    ci_low = challenger.auuc.ci_low
    if ci_low is None or ci_low <= 0.0:
        interval = "has no interval" if ci_low is None else "has an interval that includes zero or less"
        return UpliftChampionDecision(
            promote=False,
            reason=(
                f"Not promoted: no measurable uplift - the AUUC {interval}, so this model cannot be "
                f"shown to target better than random."
            ),
            improvement_pct=None,
        )
    if champion is None:
        return UpliftChampionDecision(
            promote=True,
            reason=(
                f"Promoted: there is no uplift champion yet and this model's AUUC "
                f"({_auuc(challenger.auuc.value)}) is measurably above random targeting."
            ),
            improvement_pct=None,
        )

    mismatched = [
        name
        for name in ("rows_evaluated", "treated_rows", "control_rows")
        if getattr(challenger, name) != getattr(champion, name)
    ]
    fingerprints = (challenger.holdout_fingerprint, champion.holdout_fingerprint)
    if None not in fingerprints and fingerprints[0] != fingerprints[1]:
        mismatched.append("holdout_fingerprint")
    if mismatched:
        raise ValueError(
            f"The challenger and the champion were not evaluated on the same hold-out ({', '.join(mismatched)} "
            f"differ); re-score the champion on the challenger's hold-out before comparing them."
        )

    new, old = challenger.auuc.value, champion.auuc.value
    if old <= 0.0:
        promote = new > old
        verdict = "beats" if promote else "does not beat"
        return UpliftChampionDecision(
            promote=promote,
            reason=(
                f"{'Promoted' if promote else 'Not promoted'}: the challenger's AUUC ({_auuc(new)}) "
                f"{verdict} the champion's ({_auuc(old)}), which is not above zero, on the same hold-out."
            ),
            improvement_pct=None,
        )
    improvement_pct = (new - old) / abs(old) * 100.0
    promote = improvement_pct >= min_improvement_pct
    return UpliftChampionDecision(
        promote=promote,
        reason=(
            f"{'Promoted' if promote else 'Not promoted'}: the challenger's AUUC ({_auuc(new)}) is "
            f"{improvement_pct:+.1f}% against the champion's ({_auuc(old)}) on the same hold-out; the rule "
            f"needs at least {min_improvement_pct:+.1f}%."
        ),
        improvement_pct=improvement_pct,
    )
