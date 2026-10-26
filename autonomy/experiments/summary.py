"""One plain sentence per experiment, for the Analyst's weekly briefing.

Every number in the sentence is computed here from the reading and appears in `numbers`, so the
Analyst can quote it without doing arithmetic of its own (principle 7 of plan-v3: LLMs think, maths
decides). Money is per customer, in the shop's currency; ranges are the always-valid intervals the
decisions used.
"""

from __future__ import annotations

from autonomy.contracts import BriefingSection
from autonomy.experiments.model import EndKind, Experiment
from autonomy.experiments.reading import ArmReading, Reading

__all__ = ["briefing_section", "summary_line"]


def _money(x: float, currency: str) -> str:
    return f"-{currency}{abs(x):,.2f}" if x < 0 else f"{currency}{x:,.2f}"


def _measured(reading: Reading) -> int:
    return sum(a.n for a in reading.arms)


def _numbers(reading: Reading, arm: ArmReading | None) -> dict[str, float]:
    out: dict[str, float] = {
        "customers_measured": float(_measured(reading)),
        "holdout_customers": float(reading.holdout_n),
        "days": round(reading.days_running, 1),
    }
    if arm is not None and arm.vs_holdout_seq is not None:
        out["gain_vs_nothing"] = arm.vs_holdout_seq.point
    if arm is not None and arm.vs_reference_seq is not None:
        out["gain_vs_default"] = arm.vs_reference_seq.point
        out["gain_vs_default_low"] = arm.vs_reference_seq.low
        out["gain_vs_default_high"] = arm.vs_reference_seq.high
    return out


def _parts(exp: Experiment, reading: Reading, currency: str, promoted: bool) -> tuple[str, dict[str, float]]:
    spec = exp.spec
    audience = spec.audience.description or "the goal's customers"
    ref = spec.label_of(spec.reference_arm)
    days = f"{reading.days_running:.0f} days"
    who = f"{_measured(reading):,} customers"
    if exp.running:
        return (
            f"Testing {', '.join(a.label for a in spec.arms)} for {audience}: day {reading.days_running:.0f} of "
            f"{spec.stopping_rule.max_days}, {who} measured, nothing has proved itself yet.",
            _numbers(reading, None),
        )
    if exp.end_kind is EndKind.WINNER and exp.winner is not None:
        arm = reading.arm(exp.winner)
        assert arm.vs_reference_seq is not None and arm.vs_holdout_seq is not None  # a winner has both
        gain, ref_gain = arm.vs_holdout_seq, arm.vs_reference_seq
        now_default = f" It is now the default {spec.kind.value}." if promoted else ""
        return (
            f"{arm.label} beat {ref} for {audience}: {_money(ref_gain.point, currency)} more profit per customer "
            f"(range {_money(ref_gain.low, currency)} to {_money(ref_gain.high, currency)}) and "
            f"{_money(gain.point, currency)} more than doing nothing, across {who} in {days}.{now_default}",
            _numbers(reading, arm),
        )
    if exp.end_kind is EndKind.STOPPED_LOSING:
        names = ", ".join(spec.label_of(a) for a in exp.losing)
        after = (
            f"{ref}, today's default, loses money against doing nothing: it needs a person's review."
            if spec.reference_arm in exp.losing
            else f"The default stays {ref}."
        )
        return (
            f"The test of {', '.join(a.label for a in spec.arms)} for {audience} stopped after {days}: {names} "
            f"was clearly losing. {after}",
            _numbers(reading, reading.arm(exp.losing[0]) if exp.losing else None),
        )
    if exp.end_kind is EndKind.STOPPED_MANUALLY:
        return (
            f"The test of {', '.join(a.label for a in spec.arms)} for {audience} was stopped by a person after "
            f"{days}. The default stays {ref}.",
            _numbers(reading, None),
        )
    return (
        f"No clear winner between {', '.join(a.label for a in spec.arms)} for {audience} after {days} "
        f"({who} measured). The default stays {ref}.",
        _numbers(reading, None),
    )


def summary_line(
    exp: Experiment, reading: Reading, *, currency: str = "$", promoted: bool | None = None
) -> str:
    """The experiment in one sentence. `promoted` defaults to what the experiment record says."""
    return _parts(exp, reading, currency, exp.promoted if promoted is None else promoted)[0]


def briefing_section(
    exp: Experiment, reading: Reading, *, currency: str = "$", promoted: bool | None = None
) -> BriefingSection:
    """The same sentence as a briefing section, with every number it quotes."""
    body, numbers = _parts(exp, reading, currency, exp.promoted if promoted is None else promoted)
    return BriefingSection(title=f"Test: {exp.spec.question}", body=body, numbers=numbers)
