"""Small-group suppression (Plan K M133, DEC-1404 (e)): Plan I's rule on every grouped output, and a walk that
fails when a grouped output of an exposed tool is not covered."""

from __future__ import annotations

import typing
from typing import Any

from pydantic import BaseModel

from api.mcp.suppress import MIN_GROUP_ROWS, RULES, CountMap, Groups, Whole, narrow, suppress, widen
from engine.agent.tools import MIN_GROUP_ROWS as PLAN_I_MINIMUM
from engine.agentic.tools import default_registry


def test_the_minimum_is_plan_is() -> None:
    assert MIN_GROUP_ROWS == PLAN_I_MINIMUM == 10


def _bands(*rows: tuple[str, int, int]) -> dict[str, Any]:
    bands = [
        {"band": name, "rows": n, "rows_valued": valued, "total_inr": 100.0 * n} for name, n, valued in rows
    ]
    return {"run_id": "r", "valued": True, "contactable_rows": sum(n for _, n, _ in rows), "bands": bands}


def test_a_small_band_is_withheld_with_the_next_smallest_while_the_rest_is_under_ten() -> None:
    out, withheld = suppress("value_segment", _bands(("Low", 40, 30), ("Medium", 14, 12), ("High", 9, 7)))
    assert [band["band"] for band in out["bands"]] == ["Low"] and withheld == {"bands": 2}
    out, withheld = suppress("value_segment", _bands(("Low", 40, 30), ("Medium", 14, 12), ("High", 0, 0)))
    assert len(out["bands"]) == 3 and withheld == {}, "a group of nobody describes nobody"
    out, _ = suppress("value_segment", _bands(("A", 40, 30), ("B", 5, 5), ("C", 6, 6), ("D", 30, 30)))
    assert [band["band"] for band in out["bands"]] == ["A", "D"], "5 + 6 withheld together reach 10"
    out, _ = suppress("value_segment", _bands(("A", 40, 3), ("B", 50, 50)))
    # 3 valued rows are a small group behind a total, and the total less B would give them back: B goes too.
    assert out["bands"] == []


def test_the_input_is_not_changed() -> None:
    given = _bands(("Low", 40, 30), ("High", 2, 2))
    suppress("value_segment", given)
    assert len(given["bands"]) == 2


def test_channels_reasons_and_whole_reports() -> None:
    contactability = {
        "channels": [
            {"channel": "sms", "contactable_rows": 50, "not_contactable_eligible_rows": 30,
             "reasons": {"not_contactable": 24, "inquiry_lapsed": 4, "legacy_unregistered": 2}},
            {"channel": "call", "contactable_rows": 3, "not_contactable_eligible_rows": 77, "reasons": {}},
        ]
    }  # fmt: skip
    out, withheld = suppress("check_contactable", contactability)
    assert [channel["channel"] for channel in out["channels"]] == ["sms"]
    # 4 + 2 is under 10, so the next smallest (24) joins them: 30 - 24 would give the six back.
    assert out["channels"][0]["reasons"] == {}
    assert withheld == {"channels": 1, "channels[0].reasons": 3}

    report = {"report": {"treated_rows": 500, "control_rows": 7, "arms": None}}
    out, withheld = suppress("measure", report)
    assert out["report"] is None and withheld == {"report": 1}
    arms = {
        "report": {
            "treated_rows": 120,
            "control_rows": 60,
            "arms": [
                {"arm": "A", "rows": 160, "treated_rows": 100, "control_rows": 60, "segments": None},
                {"arm": "B", "rows": 75, "treated_rows": 15, "control_rows": 60, "segments": None},
                {"arm": "C", "rows": 65, "treated_rows": 5, "control_rows": 60, "segments": None},
            ],
        }
    }
    out, withheld = suppress("audit_campaign", arms)
    assert [arm["arm"] for arm in out["report"]["arms"]] == ["A"] and withheld == {"report.arms": 2}
    out, withheld = suppress("evaluate_policy", {"ope": {"rows": 4}, "risk_comparison": {"rows": 400}})
    assert out["ope"] is None and out["risk_comparison"] == {"rows": 400}


def test_reasons_that_add_up_to_less_than_ten_take_the_next_smallest() -> None:
    out, _ = suppress(
        "check_contactable",
        {"channels": [{"contactable_rows": 50, "not_contactable_eligible_rows": 40,
                       "reasons": {"a": 30, "b": 4, "c": 2, "d": 12}}]},
    )  # fmt: skip
    assert out["channels"][0]["reasons"] == {"a": 30}, "b and c (6) withheld, then d joins them"


def test_narrowing_answers_from_the_suppressed_full_list() -> None:
    wide, wanted = widen("value_segment", {"run_id": "r", "band": "High"})
    assert wide == {"run_id": "r"} and wanted == "High"
    assert widen("value_segment", {"run_id": "r"}) == ({"run_id": "r"}, None)
    full, _ = suppress("value_segment", _bands(("Low", 40, 30), ("Medium", 14, 12), ("High", 9, 7)))
    high, medium, low = (narrow("value_segment", full, name) for name in ("High", "Medium", "Low"))
    assert high["bands"] == medium["bands"] == [] and high["reason"] == medium["reason"]
    assert low["bands"] == full["bands"] and low["total_inr"] is None and low.get("reason") is None


# --- every grouped output is covered ----------------------------------------------------------------------
def _models(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [model for arg in typing.get_args(annotation) for model in _models(arg)]


def _is_count_map(annotation: Any) -> bool:
    for candidate in (annotation, *typing.get_args(annotation)):
        if typing.get_origin(candidate) is dict and typing.get_args(candidate)[1:] == (int,):
            return True
    return False


def _grouped(model: type[BaseModel], path: tuple[str, ...]) -> list[tuple[tuple[str, ...], str]]:
    """Every list of models holding a row count, and every map of counts, under `model`."""
    found: list[tuple[tuple[str, ...], str]] = []
    for name, info in model.model_fields.items():
        annotation = info.annotation
        here = (*path, name)
        if _is_count_map(annotation):
            found.append((here, "map"))
        is_list = any(
            typing.get_origin(arg) in (tuple, list) for arg in (annotation, *typing.get_args(annotation))
        )
        for child in _models(annotation):
            counts = [
                field
                for field, sub in child.model_fields.items()
                if field.endswith("rows") and "int" in str(sub.annotation)
            ]
            if is_list and counts:
                found.append((here, "list"))
            found.extend(_grouped(child, (*here, "*") if is_list else here))
    return found


EXEMPT: dict[str, set[tuple[str, ...]]] = {
    # A pack's sections are parts of a page, not groups of customers: `table_rows` counts table lines.
    "get_proof_pack": {("sections",)},
}


def test_every_grouped_output_of_an_exposed_tool_has_a_rule() -> None:
    for tool in default_registry().list():
        rules = RULES.get(tool.name, ())
        covered = {rule.path for rule in rules if isinstance(rule, Groups | CountMap)}
        for path, kind in _grouped(tool.output_model, ()):
            if path in EXEMPT.get(tool.name, set()):
                continue
            assert path in covered, f"{tool.name}: the grouped output {'.'.join(path)} ({kind}) has no rule"
        for rule in rules:
            if isinstance(rule, Whole):
                assert rule.path[0] in tool.output_model.model_fields, (tool.name, rule.path)


def test_the_walk_finds_the_grouped_outputs_it_guards() -> None:
    tools = {tool.name: tool for tool in default_registry().list()}
    assert _grouped(tools["value_segment"].output_model, ()) == [(("bands",), "list")]
    assert (("channels", "*", "reasons"), "map") in _grouped(tools["check_contactable"].output_model, ())
    assert (("report", "arms", "*", "segments"), "list") in _grouped(tools["audit_campaign"].output_model, ())
