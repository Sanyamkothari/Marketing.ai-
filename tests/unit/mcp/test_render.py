"""The text beside a campaign's result says what its figures are (Plan K M133, DEC-1404 (f)).

A report of several offers holds the first offer's figures at its top level. The text must state the campaign's
result in the Value Proof Pack's scope (`engine.pilot.proof._scope`): every offer together when the report adds
them (`offers_combined`), else the first offer alone, said so; and in the Pack's own words for each figure.
"""

from __future__ import annotations

from typing import Any

from api.mcp.render import render


def _lift(value: float, low: float, high: float) -> dict[str, float]:
    return {"value": value, "ci_low": low, "ci_high": high}


FIRST_OFFER: dict[str, Any] = {
    "treated_rows": 2_131,
    "control_rows": 2_131,
    "absolute_lift": _lift(0.0070, 0.0009, 0.0136),
}
ARMS: list[dict[str, Any]] = [{"arm": "a", "treated_rows": 2_131}, {"arm": "b", "treated_rows": 2_139}]


def _measured(report: dict[str, Any]) -> str:
    return render("measure", "Causal", "ev_1", {"measured": True, "report": report}, {})


def test_one_offer_is_the_campaign() -> None:
    text = _measured(dict(FIRST_OFFER))
    assert (
        "2,131 treated against 2,131 held back; difference +0.7 points (95% interval +0.1 points to +1.4 points)."
        in text
    )
    assert "every offer" not in text and "first offer" not in text


def test_several_offers_added_together_are_stated_as_every_offer_together() -> None:
    combined = {"treated_rows": 4_270, "control_rows": 2_131, "absolute_lift": _lift(0.0041, -0.0009, 0.0091)}
    text = _measured({**FIRST_OFFER, "arms": ARMS, "offers_combined": combined})
    assert (
        "4,270 treated against 2,131 held back, every offer together against the shared control group; "
        "difference +0.4 points (95% interval -0.1 points to +0.9 points)." in text
    )
    assert "+0.7 points" not in text, "the first offer's difference is not the campaign's"


def test_several_offers_not_added_together_are_said_to_be_the_first_offers() -> None:
    text = _audited({**FIRST_OFFER, "arms": ARMS})
    assert "2,131 treated against 2,131 held back, for the first offer alone" in text


def _audited(report: dict[str, Any]) -> str:
    out = {
        "audited": True,
        "audit": {"assignment_rows": 6_401, "randomness": {"auc": 0.51}},
        "report": report,
    }
    return render("audit_campaign", "Causal", "ev_1", out, {})


def test_an_amount_is_never_called_percentage_points() -> None:
    report = {
        "outcome_kind": "continuous",
        "treated_rows": 300,
        "control_rows": 300,
        "absolute_lift": None,
        "mean_difference_ci": _lift(1.25, -0.5, 3.0),
    }
    text = _measured(report)
    assert "difference per contacted customer +1.25 (95% interval -0.50 to +3.00)." in text
    assert "points" not in text
    combined = _measured(
        {**report, "arms": ARMS, "offers_combined": {"treated_rows": 600, "control_rows": 300}}
    )
    assert "600 treated against 300 held back, every offer together" in combined
    assert "+1.25" not in combined, "the first offer's difference in averages is not the campaign's"


def test_a_withheld_report_says_so() -> None:
    assert "No measurement is shown." in _measured(None)  # type: ignore[arg-type]
