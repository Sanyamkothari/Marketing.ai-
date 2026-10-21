"""The honest evaluator: fit on one part of the shop's campaign, score on customers it never saw.

For a candidate (`autonomy.scientist.menu`) and a `Campaign`:

1. **Split.** A randomised campaign is split by customer: a fixed hash of the customer id puts each in
   the FIT part or the HELD-OUT part (the same split for every candidate and in every run). A campaign
   that was not randomised (propensity-logged, so its mix of arms drifted with the shop's own rules)
   is split by time instead: the earlier customers fit, the later ones score, so a policy is never
   graded on the past it learned from. A campaign with no timestamps (or one reference time for every
   row, which orders nobody) falls back to the customer split and says so.
2. **Fit** on the FIT part only. Nothing of the HELD-OUT part (features, arms, outcomes) is read
   until the fitted policy has produced its probabilities.
3. **Score** on the HELD-OUT part with off-policy evaluation from the logged probabilities
   (`autonomy.proof.ope`: IPS, SNIPS and DR; the DR reward model is a per-arm mean cross-fitted on
   the held-out halves, `autonomy.proof.replay.per_action_mean_model`). The headline is DR profit per
   customer with a 95% interval, and the same value's paired difference to the safe default and to
   sending nothing, taken row by row (the same customers), each with its interval.

A campaign with no logged probabilities cannot be evaluated: there is no way to say what a policy
that chooses differently would have earned, so the evaluator refuses (`identified` False) instead of
guessing. Candidates that cannot be fitted come back with `status="failed"` and the reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt

from autonomy.contracts import Estimate
from autonomy.proof import ope
from autonomy.proof.estimate import Z_95
from autonomy.proof.replay import per_action_mean_model
from autonomy.proof.replay.datasets import LoggedBandit
from autonomy.scientist.campaign import Campaign, unit_hash
from autonomy.scientist.menu import Candidate, CandidateFailed

__all__ = [
    "HELD_OUT_SHARE",
    "SPLIT_SALT",
    "Evaluation",
    "Evaluations",
    "Split",
    "dr_terms",
    "evaluate_candidates",
    "se_of",
    "split_campaign",
]

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

SPLIT_SALT = "autonomy-scientist-v1:split"
HELD_OUT_SHARE = 0.5
"""Share of customers held out for scoring."""
_SUM_TOLERANCE = 1e-6


def se_of(e: Estimate) -> float:
    """The standard error a normal 95% interval implies."""
    return (e.high - e.low) / (2.0 * Z_95)


@dataclass(frozen=True, eq=False)
class Split:
    """Which rows fit and which score."""

    method: Literal["customer", "time"]
    fit_rows: IntArray
    held_rows: IntArray
    salt: str
    note: str


def split_campaign(
    campaign: Campaign, *, held_share: float = HELD_OUT_SHARE, salt: str = SPLIT_SALT
) -> Split:
    """The fit / held-out split (see the module docstring): by customer hash, or by time when the
    campaign was not randomised and has timestamps."""
    if not 0.0 < held_share < 1.0:
        raise ValueError("held_share must be between 0 and 1")
    n = campaign.n
    has_clock = campaign.times is not None and n > 0 and float(np.ptp(campaign.times)) > 0.0
    if not campaign.randomised and campaign.times is not None and has_clock:
        order = np.lexsort((np.arange(n), campaign.times))
        cut = round(n * (1.0 - held_share))
        return Split(
            "time",
            np.sort(order[:cut]).astype(np.int64),
            np.sort(order[cut:]).astype(np.int64),
            salt,
            "not randomised: the earlier customers fit, the later ones score",
        )
    h = unit_hash(campaign.customer_ids, salt)
    held = h < held_share
    note = "randomised: split by customer id hash"
    if not campaign.randomised:
        note = "not randomised and no usable timestamps: split by customer id hash (drift is not guarded)"
    return Split(
        "customer",
        np.flatnonzero(~held).astype(np.int64),
        np.flatnonzero(held).astype(np.int64),
        salt,
        note,
    )


def dr_terms(logged: LoggedBandit, pi: FloatArray, q: FloatArray) -> FloatArray:
    """Per-row doubly robust terms of the policy `pi` (rows, arms) on a log, reward model `q`."""
    rows = np.arange(logged.n_rows)
    w = pi[rows, logged.actions] / logged.propensity
    out: FloatArray = (pi * q).sum(axis=1) + w * (logged.reward - q[rows, logged.actions])
    return out


@dataclass(frozen=True, eq=False)
class Evaluation:
    """One candidate scored on the held-out part."""

    label: str
    kind: str
    spec: dict[str, object]
    status: Literal["ok", "failed"]
    note: str
    value: Estimate | None = None
    """DR profit per customer on the held-out part."""
    ips: float | None = None
    snips: float | None = None
    vs_default: Estimate | None = None
    """Paired DR difference to the safe default, same customers."""
    vs_none: Estimate | None = None
    """Paired DR difference to sending nothing."""
    contact_rate: float | None = None
    arm_shares: tuple[float, ...] = ()
    effective_n: float = 0.0
    max_weight: float = 0.0
    terms: FloatArray | None = None


@dataclass(frozen=True, eq=False)
class Evaluations:
    """Everything one evaluation run measured, on one split."""

    split: Split
    n_fit: int
    n_held: int
    arm_names: tuple[str, ...]
    arm_counts_fit: tuple[int, ...]
    arm_counts_held: tuple[int, ...]
    conversions_fit: int
    conversions_held: int
    margin_held: float
    """Mean gross margin per customer on the held-out part (the scale a gain is judged against)."""
    none: Evaluation
    default: Evaluation
    candidates: tuple[Evaluation, ...]


def _check_pi(pi: FloatArray, n: int, k: int) -> FloatArray:
    out = np.asarray(pi, dtype=np.float64)
    if out.shape != (n, k):
        raise CandidateFailed(f"probabilities have shape {out.shape}, expected {(n, k)}")
    if not np.all(np.isfinite(out)) or np.any(out < -_SUM_TOLERANCE):
        raise CandidateFailed("probabilities are missing, infinite or negative")
    if not np.allclose(out.sum(axis=1), 1.0, atol=_SUM_TOLERANCE):
        raise CandidateFailed("probabilities do not sum to 1")
    return np.clip(out, 0.0, 1.0)


def _interval(terms: FloatArray) -> Estimate:
    return ope.mean_interval(terms)


def _score(
    cand: Candidate,
    fit: Campaign,
    held: Campaign,
    logged: LoggedBandit,
    q: FloatArray,
) -> Evaluation:
    label = cand.label(held.arm_names)
    spec = cand.spec()
    try:
        pi = _check_pi(cand.held_out_probabilities(fit, held), held.n, held.n_arms)
    except CandidateFailed as err:
        return Evaluation(label, cand.kind, spec, "failed", str(err))
    rows = np.arange(held.n)
    terms = dr_terms(logged, pi, q)
    pi_logged = pi[rows, logged.actions]
    ips = ope.ips(logged.reward, logged.propensity, pi_logged)
    snips = ope.snips(logged.reward, logged.propensity, pi_logged)
    return Evaluation(
        label=label,
        kind=cand.kind,
        spec=spec,
        status="ok",
        note="",
        value=_interval(terms),
        ips=float(ips.value.point),
        snips=None if snips is None else float(snips.value.point),
        contact_rate=float(1.0 - pi[:, 0].mean()),
        arm_shares=tuple(float(x) for x in pi.mean(axis=0)),
        effective_n=float(ips.effective_n),
        max_weight=float(ips.max_weight),
        terms=terms,
    )


def _paired(a: Evaluation, b: Evaluation) -> Estimate | None:
    if a.terms is None or b.terms is None:
        return None
    return _interval(a.terms - b.terms)


def evaluate_candidates(
    campaign: Campaign,
    candidates: Sequence[Candidate],
    *,
    default: Candidate,
    none: Candidate,
    split: Split | None = None,
) -> Evaluations:
    """Fit every candidate (and the safe default, and 'send nothing') on the FIT part of `campaign`,
    score them on the HELD-OUT part, and pair each with the default and with sending nothing.

    Raises ValueError when the campaign has no logged propensities (nothing can be proven) or a part
    of the split is empty."""
    if not campaign.identified:
        raise ValueError("the campaign has no logged propensities: it cannot be evaluated")
    sp = split if split is not None else split_campaign(campaign)
    if len(sp.fit_rows) < 2 or len(sp.held_rows) < 2:
        raise ValueError("the split leaves fewer than two customers in a part")
    fit = campaign.take(sp.fit_rows)
    held = campaign.take(sp.held_rows)
    logged = held.logged()
    q = per_action_mean_model(logged)
    ev_none = _score(none, fit, held, logged, q)
    ev_default = _score(default, fit, held, logged, q)
    if ev_none.status != "ok" or ev_default.status != "ok":
        raise ValueError(f"the reference policies could not be scored: {ev_none.note or ev_default.note}")
    scored: list[Evaluation] = []
    for cand in candidates:
        ev = _score(cand, fit, held, logged, q)
        if ev.status == "ok":
            ev = Evaluation(
                label=ev.label,
                kind=ev.kind,
                spec=ev.spec,
                status=ev.status,
                note=ev.note,
                value=ev.value,
                ips=ev.ips,
                snips=ev.snips,
                vs_default=_paired(ev, ev_default),
                vs_none=_paired(ev, ev_none),
                contact_rate=ev.contact_rate,
                arm_shares=ev.arm_shares,
                effective_n=ev.effective_n,
                max_weight=ev.max_weight,
                terms=ev.terms,
            )
        scored.append(ev)
    # the references, each paired with sending nothing (the default also with itself: zero)
    ev_default = Evaluation(
        label=ev_default.label,
        kind=ev_default.kind,
        spec=ev_default.spec,
        status="ok",
        note=ev_default.note,
        value=ev_default.value,
        ips=ev_default.ips,
        snips=ev_default.snips,
        vs_default=_paired(ev_default, ev_default),
        vs_none=_paired(ev_default, ev_none),
        contact_rate=ev_default.contact_rate,
        arm_shares=ev_default.arm_shares,
        effective_n=ev_default.effective_n,
        max_weight=ev_default.max_weight,
        terms=ev_default.terms,
    )
    return Evaluations(
        split=sp,
        n_fit=fit.n,
        n_held=held.n,
        arm_names=campaign.arm_names,
        arm_counts_fit=tuple(int(c) for c in fit.arm_counts()),
        arm_counts_held=tuple(int(c) for c in held.arm_counts()),
        conversions_fit=int((fit.converted > 0).sum()),
        conversions_held=int((held.converted > 0).sum()),
        margin_held=float(held.margin.mean()),
        none=ev_none,
        default=ev_default,
        candidates=tuple(scored),
    )
