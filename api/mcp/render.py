"""The short text beside each structured result (Plan K M133, DEC-1404 (f)).

MCP clients show a tool result's text to their model, so the text is written here, by our code, from the
**numbers** of the structured result and from our own fixed words only - never a string the result carries
(an arm's name, a column, a headline that quotes a campaign's name), never third-party text. The structured
content is the answer; the text says what it is, its evidence label and id, and what was withheld.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from engine.pilot.proof import FigureFormat, format_value

__all__ = ["render", "render_error"]

_LABELS: Final[dict[str, str]] = {
    "Causal": "Causal: measured against a group held back at random",
    "Estimated": "Estimated: from a model or a plan, not a measurement",
    "Not proven": "Not proven: nobody verified that the groups were chosen at random, or it is an early look",
    "Descriptive": "Descriptive: a description, with no claim about cause",
}


def _int(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "not known"


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _difference(block: Any, fmt: FigureFormat) -> str:
    """`<value> (95% interval <low> to <high>)` in the Value Proof Pack's own words for `fmt`."""
    centre = _number(block.get("value")) if isinstance(block, Mapping) else None
    if centre is None:
        return "not measured"
    value = format_value(fmt, centre)
    low, high = _number(block.get("ci_low")), _number(block.get("ci_high"))
    if low is not None and high is not None:
        return f"{value} (95% interval {format_value(fmt, low)} to {format_value(fmt, high)})"
    return value


def _plan_test(out: Mapping[str, Any]) -> list[str]:
    lines = []
    for point in out.get("points") or ():
        share = point.get("holdout_share")
        mde = point.get("mde_pp")
        seen = (
            f"{mde:.2f} percentage points"
            if isinstance(mde, int | float)
            else "not computable from the inputs"
        )
        lines.append(
            f"Holding back {share:.1%}: {_int(point.get('n_control'))} held back, "
            f"{_int(point.get('n_treat'))} contacted; smallest change it can see: {seen}."
        )
    return lines


def _contactable(out: Mapping[str, Any]) -> list[str]:
    if not out.get("recorded"):
        return ["The run did not record who may be contacted on each channel."]
    channels = out.get("channels") or ()
    return [f"{_int(out.get('eligible_rows'))} eligible customers; {len(channels)} channels shown."]


def _value(out: Mapping[str, Any]) -> list[str]:
    if not out.get("valued"):
        return ["The run has no expected gross value."]
    total = out.get("total_inr")
    money = f"₹{total:,.0f}" if isinstance(total, int | float) else "shown by band"
    return [
        f"Expected gross value of {_int(out.get('contactable_rows'))} contactable customers: {money} "
        "(what the list is worth, not what a campaign would add).",
        f"{len(out.get('bands') or ())} bands shown.",
    ]


def _scope(report: Mapping[str, Any]) -> str:
    """What the report's figures describe, as the Value Proof Pack decides it (`engine.pilot.proof._scope`):
    one offer; every offer together (`offers_combined`); or, for several offers measured before
    `offers_combined` existed, the first offer alone, said so."""
    arms = report.get("arms")
    if not isinstance(arms, list) or len(arms) < 2:
        return "single"
    return "combined" if isinstance(report.get("offers_combined"), Mapping) else "first_offer"


def _report(report: Any) -> list[str]:
    """The campaign's comparison in the Pack's scope: never the first offer's figures as the campaign's."""
    if not isinstance(report, Mapping):
        return ["No measurement is shown."]
    scope = _scope(report)
    figures: Mapping[str, Any] = report["offers_combined"] if scope == "combined" else report
    whose = {
        "single": "",
        "combined": ", every offer together against the shared control group",
        "first_offer": ", for the first offer alone (the report does not add the offers together)",
    }[scope]
    if report.get("outcome_kind") == "continuous":
        if scope == "combined":
            difference = "the difference in averages is given for each offer in the structured result"
        else:
            interval = report.get("adjusted_interval")
            if not isinstance(interval, Mapping):
                interval = report.get("mean_difference_ci")
            difference = f"difference per contacted customer {_difference(interval, 'signed_amount')}"
    else:
        difference = f"difference {_difference(figures.get('absolute_lift'), 'points')}"
    return [
        f"{_int(figures.get('treated_rows'))} treated against {_int(figures.get('control_rows'))} held back"
        f"{whose}; {difference}.",
    ]


def _measure(out: Mapping[str, Any]) -> list[str]:
    return _report(out.get("report")) if out.get("measured") else ["The campaign has not been measured yet."]


def _audit(out: Mapping[str, Any]) -> list[str]:
    if not out.get("audited"):
        return ["The campaign is not an audited outside campaign."]
    audit = out.get("audit") or {}
    random = audit.get("randomness") or {}
    auc = random.get("auc")
    check = f"assignment predictable at AUC {auc:.3f}" if isinstance(auc, int | float) else "not run"
    return [
        f"{_int(audit.get('assignment_rows'))} customers in the assignment; randomness check: {check}.",
        *_report(out.get("report")),
    ]


def _proof(out: Mapping[str, Any]) -> list[str]:
    if not out.get("available"):
        return ["No Value Proof Pack can be drawn for this campaign now; the structured result says why."]
    return [f"Value Proof Pack with {len(out.get('sections') or ())} sections."]


def _policy(out: Mapping[str, Any]) -> list[str]:
    ope = out.get("ope")
    comparison = out.get("risk_comparison")
    return [
        "Off-policy estimate: " + ("stored." if isinstance(ope, Mapping) else "none stored."),
        "Equal-budget comparison: " + ("stored." if isinstance(comparison, Mapping) else "none stored."),
    ]


def _draft(out: Mapping[str, Any]) -> list[str]:
    if out.get("status") == "pending":
        return ["Proposal drafted and pending: nothing changes until an Approver approves it in the product."]
    return [f"Proposal refused by the policy ({len(out.get('codes') or ())} codes in the structured result)."]


_SPECIFIC: Final[dict[str, Callable[[Mapping[str, Any]], list[str]]]] = {
    "plan_test": _plan_test,
    "check_contactable": _contactable,
    "value_segment": _value,
    "measure": _measure,
    "audit_campaign": _audit,
    "get_proof_pack": _proof,
    "evaluate_policy": _policy,
    "draft_test_plan": _draft,
}


def render(
    tool: str, label: str, evidence_id: str, output: Mapping[str, Any], withheld: Mapping[str, int]
) -> str:
    """The text of one result: what it is, its label and evidence id, a few figures, and what was withheld."""
    lines = [f"Result of {tool}. {_LABELS.get(label, 'Unlabelled')}. Evidence id {evidence_id}."]
    specific = _SPECIFIC.get(tool)
    if specific is not None:
        lines.extend(specific(output))
    if withheld:
        lines.append(
            f"{sum(withheld.values())} groups of fewer than 10 customers, or text from outside the product, "
            "are not shown."
        )
    lines.append(
        "Every figure is in the structured result, computed by the product; no language model wrote it."
    )
    return "\n".join(lines)


def render_error(code: str, message: str) -> str:
    """The text of a refusal: the code and the (gated) message."""
    return f"Refused ({code}). {message}"
