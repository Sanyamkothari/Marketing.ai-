"""The selection rule: which candidate runs, decided by held-out evidence and nothing else.

THE RULE (written down before any dataset was run; `SELECTION_RULE` is this text):

1. At most `MAX_CANDIDATES` (3) candidates are judged in a run; more is refused.
2. The SAFE DEFAULT is the best blanket policy by TRAIN evidence: the message arm with the highest
   inverse-propensity mean profit on the FIT part, provided the lower end of its 95% interval for the
   gain over sending nothing is above zero. If no arm has that, the default is SEND NOTHING.
3. Each candidate is fitted on the FIT part and scored on the HELD-OUT part (`autonomy.scientist.evaluate`).
   Its claim is "earns more per customer than the safe default"; the evidence is the paired DR
   difference on the held-out customers, one-sided, with p-value from its own standard error.
4. The m claims of the run (m = the number of candidates proposed, failed ones included) are corrected
   together by Holm's step-down procedure (valid whatever the dependence between them: they share
   customers). A candidate is ELIGIBLE when its Holm-adjusted p is below `ALPHA` (0.05), its point
   gain is above zero, it was scored without failure, and no BLOCKING flag is raised.
5. Among the eligible, the one with the best corrected lower bound wins: the lower end of its
   one-sided Bonferroni interval at level 1 - ALPHA / m for the gain over the default. Ties go to the
   candidate listed first.
6. No eligible candidate: KEEP THE SAFE DEFAULT. That is a valid, normal outcome, not a failure.
7. NOT ENOUGH DATA is its own outcome, taken before any ranking, when the campaign was not logged
   with probabilities (nothing can be proven), the held-out part has fewer than `MIN_HELD_OUT`
   customers, an arm has fewer than `MIN_ARM_ROWS` customers in a part, or fewer than
   `MIN_CONVERSIONS` customers converted in a part. The safe default is kept and the reason is said.
8. Flags (plain sentences, in the explanation): BLOCKING when a candidate's gain over the default is
   more than the whole mean margin per customer (too good to be true); warnings when few customers
   carry the estimate (effective n under `MIN_ESS_SHARE` of the held-out part), a weight above
   `MAX_WEIGHT_WARN`, or IPS and DR disagree by more than two standard errors.

The rule never reads a model's opinion. Everything is computed from the held-out numbers, so the same
inputs always give the same `Selection` (no randomness, no clock).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Estimate
from autonomy.proof.estimate import Z_95
from autonomy.scientist.campaign import Campaign
from autonomy.scientist.evaluate import (
    HELD_OUT_SHARE,
    SPLIT_SALT,
    Evaluation,
    Evaluations,
    evaluate_candidates,
    se_of,
    split_campaign,
)
from autonomy.scientist.menu import BlanketPolicy, Candidate, MenuError

__all__ = [
    "ALPHA",
    "MAX_CANDIDATES",
    "SELECTION_RULE",
    "SafeDefault",
    "Selection",
    "Verdict",
    "holm_adjust",
    "run_scientist",
    "safe_default",
    "select",
]

FloatArray = npt.NDArray[np.float64]

ALPHA = 0.05
MAX_CANDIDATES = 3
MIN_HELD_OUT = 400
MIN_ARM_ROWS = 30
MIN_CONVERSIONS = 10
MIN_ESS_SHARE = 0.10
MAX_WEIGHT_WARN = 50.0
DEFAULT_EVIDENCE_Z = Z_95

SELECTION_RULE = __doc__ or ""

Outcome = Literal["candidate", "default", "not_enough_data"]


def holm_adjust(p_values: Sequence[float]) -> list[float]:
    """Holm's step-down adjusted p-values, in the input order (sort ascending, multiply the i-th
    smallest by m - i, running maximum, cap at 1)."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p_values[i]))
        adjusted[i] = running
    return adjusted


def _one_sided_p(e: Estimate) -> float:
    """P(Z >= z) for z = point / se: the evidence that the true difference is above zero."""
    se = se_of(e)
    if se <= 0.0:
        return 0.0 if e.point > 0 else 1.0
    return 0.5 * math.erfc((e.point / se) / math.sqrt(2.0))


# --- the safe default -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SafeDefault:
    """What runs when no candidate has proved itself."""

    arm: int
    label: str
    reason: str
    gain: Estimate | None
    """Its gain over sending nothing on the FIT part (None for 'send nothing')."""

    def policy(self) -> BlanketPolicy:
        return BlanketPolicy(self.arm)


def _arm_value(r: FloatArray, p: FloatArray) -> tuple[float, float]:
    """Inverse-propensity mean of a reward within one arm and its standard error."""
    w = 1.0 / p
    total = float(w.sum())
    mean = float((w * r).sum()) / total
    se = math.sqrt(float((w * w * (r - mean) ** 2).sum())) / total
    return mean, se


def safe_default(train: Campaign, *, z: float = DEFAULT_EVIDENCE_Z) -> SafeDefault:
    """The best blanket policy by evidence on `train` (rule 2), or 'send nothing'."""
    if train.propensity is None:
        return SafeDefault(0, "send nothing", "no logged probabilities, so no message has evidence", None)
    r, p = train.reward(), train.propensity
    rows0 = train.arm == 0
    if rows0.sum() < 2:
        return SafeDefault(
            0, "send nothing", "no customer was left alone, so there is nothing to compare with", None
        )
    base, base_se = _arm_value(r[rows0], p[rows0])
    best: tuple[float, int, Estimate] | None = None
    for a in range(1, train.n_arms):
        rows = train.arm == a
        if rows.sum() < 2:
            continue
        v, se = _arm_value(r[rows], p[rows])
        gain = v - base
        gse = math.sqrt(se * se + base_se * base_se)
        est = Estimate(point=gain, low=gain - z * gse, high=gain + z * gse)
        if est.low > 0.0 and (best is None or gain > best[0]):
            best = (gain, a, est)
    if best is None:
        return SafeDefault(
            0,
            "send nothing",
            "no blanket arm has a positive gain over sending nothing on the train part (lower end of its 95% interval above 0)",
            None,
        )
    _, arm, est = best
    label = BlanketPolicy(arm).label(train.arm_names)
    return SafeDefault(
        arm,
        label,
        f"best blanket arm by train evidence: gain over sending nothing {est.point:+.4g} "
        f"(95% interval {est.low:+.4g} to {est.high:+.4g})",
        est,
    )


# --- the verdicts ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """What the held-out evidence says about one candidate."""

    label: str
    kind: str
    spec: dict[str, object]
    status: Literal["ok", "failed"]
    note: str
    value: Estimate | None
    vs_default: Estimate | None
    vs_none: Estimate | None
    contact_rate: float | None
    p_raw: float | None
    p_holm: float | None
    corrected: Estimate | None
    """Gain over the default with its one-sided Bonferroni lower bound (level 1 - alpha/m); `high` is
    the plain point plus the same half-width, shown for reference only."""
    eligible: bool
    blocking: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, eq=False)
class Selection:
    """The decision of one run, with everything needed to explain and to log it."""

    outcome: Outcome
    chosen: Candidate | None
    run: Candidate
    """What to run: the chosen candidate, else the safe default (a blanket policy)."""
    default: SafeDefault
    verdicts: tuple[Verdict, ...]
    reasons: tuple[str, ...]
    explanation: str
    alpha: float
    m: int
    evaluations: Evaluations | None
    forecast: Estimate | None
    """Held-out gain per customer over sending nothing for what will run (the monitor's forecast)."""

    @property
    def chosen_label(self) -> str | None:
        if self.chosen is None or self.evaluations is None:
            return None
        return self.chosen.label(self.evaluations.arm_names)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-able record of the run: the rule's inputs and outputs, no model text."""

        def est(e: Estimate | None) -> dict[str, float] | None:
            return None if e is None else {"point": e.point, "low": e.low, "high": e.high}

        ev = self.evaluations
        return {
            "outcome": self.outcome,
            "run": {"label": self.run.label(ev.arm_names) if ev else "send nothing", "spec": self.run.spec()},
            "chosen": self.chosen_label,
            "default": {
                "arm": self.default.arm,
                "label": self.default.label,
                "reason": self.default.reason,
                "train_gain_vs_none": est(self.default.gain),
            },
            "alpha": self.alpha,
            "candidates_proposed": self.m,
            "split": (
                None
                if ev is None
                else {
                    "method": ev.split.method,
                    "note": ev.split.note,
                    "salt": ev.split.salt,
                    "fit": ev.n_fit,
                    "held_out": ev.n_held,
                    "arms": list(ev.arm_names),
                    "arm_counts_fit": list(ev.arm_counts_fit),
                    "arm_counts_held": list(ev.arm_counts_held),
                }
            ),
            "verdicts": [
                {
                    "label": v.label,
                    "kind": v.kind,
                    "spec": v.spec,
                    "status": v.status,
                    "note": v.note,
                    "value": est(v.value),
                    "gain_vs_default": est(v.vs_default),
                    "gain_vs_none": est(v.vs_none),
                    "contact_rate": v.contact_rate,
                    "p_raw": v.p_raw,
                    "p_holm": v.p_holm,
                    "corrected_gain_vs_default": est(v.corrected),
                    "eligible": v.eligible,
                    "blocking": list(v.blocking),
                    "warnings": list(v.warnings),
                }
                for v in self.verdicts
            ],
            "default_value": None if ev is None else est(ev.default.value),
            "none_value": None if ev is None else est(ev.none.value),
            "forecast_gain_vs_none": est(self.forecast),
            "reasons": list(self.reasons),
            "explanation": self.explanation,
        }


def insufficiency(ev: Evaluations) -> list[str]:
    """Why the held-out evidence cannot be judged at all (rule 7); empty when it can."""
    why: list[str] = []
    if ev.n_held < MIN_HELD_OUT:
        why.append(f"only {ev.n_held} customers were held out (at least {MIN_HELD_OUT} are needed)")
    for part, counts in (("fit", ev.arm_counts_fit), ("held-out", ev.arm_counts_held)):
        thin = [ev.arm_names[a] for a, c in enumerate(counts) if c < MIN_ARM_ROWS]
        if thin:
            why.append(f"fewer than {MIN_ARM_ROWS} customers in the {part} part for: {', '.join(thin)}")
    for part, conv in (("fit", ev.conversions_fit), ("held-out", ev.conversions_held)):
        if conv < MIN_CONVERSIONS:
            why.append(
                f"only {conv} customers converted in the {part} part (at least {MIN_CONVERSIONS} are needed)"
            )
    return why


def _flags(e: Evaluation, ev: Evaluations) -> tuple[tuple[str, ...], tuple[str, ...]]:
    blocking: list[str] = []
    warnings: list[str] = []
    if e.vs_default is not None and e.vs_default.point > abs(ev.margin_held) > 0.0:
        blocking.append(
            f"too good to be true: its gain over the default ({e.vs_default.point:+.4g} per customer) is more "
            f"than the whole margin a customer brings ({ev.margin_held:.4g})"
        )
    if e.effective_n < MIN_ESS_SHARE * ev.n_held:
        warnings.append(f"few customers carry the estimate (effective n {e.effective_n:.0f} of {ev.n_held})")
    if e.max_weight > MAX_WEIGHT_WARN:
        warnings.append(f"one customer weighs {e.max_weight:.0f} times a typical one")
    if e.value is not None and e.ips is not None:
        se = se_of(e.value)
        if se > 0 and abs(e.ips - e.value.point) > 2.0 * se:
            warnings.append(
                "the two estimators (IPS and doubly robust) disagree by more than two standard errors"
            )
    return tuple(blocking), tuple(warnings)


def _failed(e: Evaluation) -> Verdict:
    return Verdict(
        e.label, e.kind, e.spec, "failed", e.note, None, None, None, None, None, None, None, False, (), ()
    )


def select(
    ev: Evaluations,
    default: SafeDefault,
    candidates: Sequence[Candidate],
    *,
    alpha: float = ALPHA,
    proposed: int | None = None,
) -> Selection:
    """Apply the rule to one evaluation run (see the module docstring). Pure and deterministic."""
    m = proposed if proposed is not None else len(candidates)
    if m < 1 or m > MAX_CANDIDATES or len(ev.candidates) != len(candidates):
        raise MenuError(f"between 1 and {MAX_CANDIDATES} candidates are judged in a run")
    default_policy = default.policy()
    ok = [(i, e) for i, e in enumerate(ev.candidates) if e.status == "ok" and e.vs_default is not None]
    raw_p = {i: _one_sided_p(e.vs_default) for i, e in ok if e.vs_default is not None}
    # Holm over all m claims; a failed candidate's claim counts as p = 1 (it cannot be proven)
    padded = [raw_p.get(i, 1.0) for i in range(len(ev.candidates))] + [1.0] * (m - len(ev.candidates))
    holm = holm_adjust(padded)
    z_c = NormalDist().inv_cdf(1.0 - alpha / m)
    verdicts: list[Verdict] = []
    eligible: list[tuple[float, int]] = []
    short = insufficiency(ev)
    for i, e in enumerate(ev.candidates):
        if e.status != "ok" or e.vs_default is None or e.value is None:
            verdicts.append(_failed(e))
            continue
        d = e.vs_default
        se = se_of(d)
        corrected = Estimate(point=d.point, low=d.point - z_c * se, high=d.point + z_c * se)
        blocking, warnings = _flags(e, ev)
        ok_here = holm[i] < alpha and d.point > 0.0 and not blocking and not short
        if ok_here:
            eligible.append((-corrected.low, i))
        verdicts.append(
            Verdict(
                e.label,
                e.kind,
                e.spec,
                "ok",
                e.note,
                e.value,
                d,
                e.vs_none,
                e.contact_rate,
                raw_p[i],
                holm[i],
                corrected,
                ok_here,
                blocking,
                warnings,
            )
        )
    eligible.sort()
    chosen_idx = eligible[0][1] if eligible else None
    reasons: list[str] = []
    if short:
        outcome: Outcome = "not_enough_data"
        reasons = short
    elif chosen_idx is not None:
        outcome = "candidate"
    else:
        outcome = "default"
    chosen = candidates[chosen_idx] if chosen_idx is not None and outcome == "candidate" else None
    run: Candidate = chosen if chosen is not None else default_policy
    forecast = (
        ev.candidates[chosen_idx].vs_none
        if outcome == "candidate" and chosen_idx is not None
        else ev.default.vs_none
    )
    return Selection(
        outcome=outcome,
        chosen=chosen,
        run=run,
        default=default,
        verdicts=tuple(verdicts),
        reasons=tuple(reasons),
        explanation=_explain(outcome, ev, default, verdicts, chosen_idx, short, alpha, m),
        alpha=alpha,
        m=m,
        evaluations=ev,
        forecast=forecast,
    )


def _explain(
    outcome: Outcome,
    ev: Evaluations,
    default: SafeDefault,
    verdicts: Sequence[Verdict],
    chosen_idx: int | None,
    short: Sequence[str],
    alpha: float,
    m: int,
) -> str:
    dv = ev.default.value
    default_line = f"The safe default is '{default.label}' ({default.reason})."
    if outcome == "not_enough_data":
        return f"Not enough data to judge: {'; '.join(short)}. Keeping the safe default. {default_line}"
    if outcome == "candidate" and chosen_idx is not None:
        v = verdicts[chosen_idx]
        assert (
            v.value is not None
            and v.vs_default is not None
            and v.corrected is not None
            and v.p_holm is not None
        )
        flags = f" Note: {'; '.join(v.warnings)}." if v.warnings else ""
        default_value = "" if dv is None else f" ({dv.point:.4g})"
        return (
            f"Chose '{v.label}'. On {ev.n_held} customers it was not fitted on it earns {v.value.point:.4g} per "
            f"customer, {v.vs_default.point:+.4g} more than '{default.label}'{default_value}. After correcting "
            f"for {m} candidate(s), the lower end of its gain is {v.corrected.low:+.4g} and its adjusted p is "
            f"{v.p_holm:.3g} (below {alpha}).{flags} {default_line}"
        )
    judged = [v for v in verdicts if v.status == "ok" and v.vs_default is not None]
    lines: list[str] = []
    for v in judged:
        assert v.vs_default is not None and v.p_holm is not None
        why = "; ".join(v.blocking) if v.blocking else f"adjusted p {v.p_holm:.3g}"
        lines.append(f"'{v.label}' {v.vs_default.point:+.4g} per customer ({why})")
    for v in verdicts:
        if v.status == "failed":
            lines.append(f"'{v.label}' could not be fitted ({v.note})")
    body = "; ".join(lines) if lines else "none could be scored"
    return (
        f"Kept the safe default: no candidate beat it with enough evidence (needs adjusted p below {alpha}). "
        f"Against '{default.label}' on {ev.n_held} held-out customers: {body}. {default_line}"
    )


# --- one call ---------------------------------------------------------------------------------------------


def _not_identified(m: int, alpha: float) -> Selection:
    nothing = BlanketPolicy(0)
    default = SafeDefault(
        0, "send nothing", "the campaign has no logged probabilities, so no message has evidence", None
    )
    why = (
        "no probabilities were logged for this campaign (it was neither randomised nor propensity-logged): "
        "nothing can be proven from it"
    )
    return Selection(
        outcome="not_enough_data",
        chosen=None,
        run=nothing,
        default=default,
        verdicts=(),
        reasons=(why,),
        explanation=f"Not enough data to judge: {why}. Keeping the safe default, which is to send nothing.",
        alpha=alpha,
        m=m,
        evaluations=None,
        forecast=None,
    )


def run_scientist(
    campaign: Campaign,
    candidates: Sequence[Candidate],
    *,
    alpha: float = ALPHA,
    held_share: float = HELD_OUT_SHARE,
    salt: str = SPLIT_SALT,
) -> Selection:
    """The whole loop on one campaign: split, safe default from the FIT part, fit and score every
    candidate on the HELD-OUT part, apply the rule.

    The campaign is whatever the caller is allowed to learn from; the caller refits the chosen
    candidate on all of it before using it on new customers."""
    if not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise MenuError(
            f"between 1 and {MAX_CANDIDATES} candidates are judged in a run, got {len(candidates)}"
        )
    if not campaign.identified:
        return _not_identified(len(candidates), alpha)
    sp = split_campaign(campaign, held_share=held_share, salt=salt)
    if len(sp.fit_rows) < 2 or len(sp.held_rows) < 2:
        return _not_identified(len(candidates), alpha)
    default = safe_default(campaign.take(sp.fit_rows))
    ev = evaluate_candidates(campaign, candidates, default=default.policy(), none=BlanketPolicy(0), split=sp)
    return select(ev, default, candidates, alpha=alpha)
