"""Sensitive columns (G15) and long free text (G16): found by our code, kept out of the features and away from the AI.

The question the Data Doctor asks about them (Q15) and what each answer does are tested here too.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from engine.agent.egress import PERSONAL_DATA, Egress, shape_of
from engine.agent.tools import AgentContext
from engine.config import RunMode, load_use_case
from engine.doctor.case import CaseItem
from engine.doctor.protocol import approve_class_of, settle
from engine.doctor.sensitive_ask import sensitive_ask, sensitive_overrides
from engine.privacy.sensitive import FAIRNESS_KIND, find_long_text, find_sensitive
from engine.stages.ingest import profile_dataset
from engine.stages.prepare import prepare_rows
from engine.utils.time import utc_now

ROWS = 40


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": [f"C-{n:04d}" for n in range(ROWS)],
            "religion": ["Hindu", "Muslim", "Christian", "Sikh"] * (ROWS // 4),
            "diagnosis_code": ["E11", "I10", "J45", "None"] * (ROWS // 4),
            "buying_group": ["pharmacy", "grocery", "grocery", "fuel"] * (ROWS // 4),
            "gender": ["F", "M"] * (ROWS // 2),
            "city": ["Pune", "Delhi", "Goa", "Agra"] * (ROWS // 4),
            "agent_remarks": ["called twice, wants a callback"] * ROWS,
            "free_text": ["a long sentence that goes on and on about the customer and their day " * 2] * ROWS,
            "spend": list(range(ROWS)),
        }
    )


def test_sensitive_columns_are_found_by_their_name_and_by_their_values() -> None:
    found = {s.column: (s.kind, s.by) for s in find_sensitive(_frame())}
    assert found == {
        "religion": ("religion", "name"),
        "diagnosis_code": ("health", "name"),
        "buying_group": ("pharmacy", "values"),
    }


def test_ordinary_columns_are_left_alone_and_gender_is_not_listed() -> None:
    names = {s.column for s in find_sensitive(_frame())}
    assert not names & {"gender", "city", "spend", "customer_id"}
    assert find_sensitive(pd.DataFrame({"health_score_x": [1, 2]})) != ()  # a whole word "health" in a name
    assert find_sensitive(pd.DataFrame({"healthy_x": ["a", "b"]})) == ()  # not a whole word


def test_a_column_can_be_skipped_for_the_fairness_report_or_when_allowed() -> None:
    assert [s.column for s in find_sensitive(_frame(), skip=["religion", "diagnosis_code"])] == [
        "buying_group"
    ]


def test_long_text_is_found_by_its_name_or_by_how_long_it_is() -> None:
    found = {t.column: t.by for t in find_long_text(_frame())}
    assert found == {"agent_remarks": "name", "free_text": "length"}


def test_sensitive_columns_do_not_become_features_unless_allowed() -> None:
    config = load_use_case("telco-churn")
    frame = _frame().assign(churned=[0, 1] * (ROWS // 2))
    _, plan = prepare_rows(frame, config, primary_key="customer_id", target="churned")
    dropped = {c.name: c.reason for c in plan.dropped_columns}
    assert dropped["religion"] == dropped["diagnosis_code"] == dropped["buying_group"] == "sensitive"
    assert "gender" not in dropped

    allowed = config.model_copy(
        update={"prepare": config.prepare.model_copy(update={"allow_sensitive_columns": ("religion",)})}
    )
    _, plan = prepare_rows(frame, allowed, primary_key="customer_id", target="churned")
    assert "religion" not in {c.name for c in plan.dropped_columns}


def test_the_fairness_column_is_kept_for_the_fairness_report_and_not_dropped() -> None:
    config = load_use_case("telco-churn")
    evaluation = config.evaluation.model_copy(update={"fairness_column": "religion"})
    fair = config.model_copy(update={"evaluation": evaluation})
    frame = _frame().assign(churned=[0, 1] * (ROWS // 2))
    _, plan = prepare_rows(frame, fair, primary_key="customer_id", target="churned")
    assert "religion" not in {c.name for c in plan.dropped_columns}


def _context(frame: pd.DataFrame, allow: tuple[str, ...] = ()) -> AgentContext:
    config = load_use_case("telco-churn")
    if allow:
        config = config.model_copy(
            update={"prepare": config.prepare.model_copy(update={"allow_sensitive_columns": allow})}
        )
    profile = profile_dataset(
        frame,
        config,
        upload_id="u_test",
        file_name="f.csv",
        file_format="csv",
        file_size_bytes=1,
        delimiter=",",
        encoding="utf-8",
    )
    return AgentContext(
        use_case_id=config.id,
        config=config,
        config_root=None,
        upload_id="u_test",
        mode=RunMode.TRAIN,
        profile=profile,
        frame=frame,
    )


def _personal(ctx: AgentContext) -> set[str]:
    return {c.name for c in ctx.profile.columns if c.pii_kinds}


def test_the_ai_never_sees_a_sensitive_column() -> None:
    ctx = _context(_frame())
    assert {"religion", "diagnosis_code", "buying_group"} <= _personal(ctx)
    assert not _personal(ctx) & {"gender", "city", "spend"}

    gate = Egress.build([str(c) for c in ctx.frame.columns], personal_columns=sorted(_personal(ctx)))
    shown = gate.prepare({"column": "religion", "examples": ["Hindu", "Muslim"], "distinct": 4})
    assert "Hindu" not in str(shown) and "Muslim" not in str(shown)
    assert PERSONAL_DATA in str(shown)


def test_the_ai_is_shown_long_free_text_by_its_shape_only_even_in_masked_mode() -> None:
    ctx = _context(_frame())
    assert ctx.long_text == {"agent_remarks", "free_text"}
    gate = Egress.build([str(c) for c in ctx.frame.columns], shape_only=ctx.long_text)
    shown = gate.prepare({"column": "agent_remarks", "examples": ["called twice, wants a callback"]})
    assert "callback" not in str(shown) and "called" not in str(shown)
    assert shown["examples"] == [shape_of("called twice, wants a callback")]
    other = gate.prepare({"column": "city", "examples": ["Pune"]})  # an ordinary column is masked as before
    assert other["examples"] == ["Pune"]


def test_an_allowed_sensitive_column_is_shown_as_any_other_but_long_text_stays_shape_only() -> None:
    ctx = _context(_frame(), allow=("religion",))
    assert "religion" not in _personal(ctx)
    assert "agent_remarks" in ctx.long_text


# --- Q15 -------------------------------------------------------------------------------------------------------------
def test_the_question_offers_fairness_report_only_first_and_takes_it_when_unanswered() -> None:
    ask = sensitive_ask("u1", "religion", "'religion' looks like religion data.", ROWS)
    assert [o.label for o in ask.options] == ["Fairness report only (recommended)", "Remove it", "Use it"]
    assert ask.recommended == "c0" and ask.options[0].safe

    settled = settle(ask, now=utc_now(), over_budget=True)
    assert settled.how == "assume" and settled.item is not None
    assert (
        settled.item.kind == FAIRNESS_KIND
    )  # past the question budget the safe answer is taken, never "use"


def test_using_a_sensitive_column_needs_an_approvers_yes_and_removing_it_is_a_drop_step() -> None:
    ask = sensitive_ask("u1", "religion", "x", ROWS)
    by_label = {o.label: o.effect for o in ask.options}
    use = by_label["Use it"]
    assert use.kind == "pii_keep" and approve_class_of(use.kind or "", use.payload) is not None
    remove = by_label["Remove it"]
    step: Any = remove.payload["step"]
    assert remove.kind == "recipe_step" and step["kind"] == "drop_column"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Fairness report only (recommended)", {"evaluation.fairness_column": "religion"}),
        ("Use it", {"prepare.allow_sensitive_columns": ["religion"]}),
        ("Remove it", {}),
    ],
)
def test_each_answer_sets_the_run_settings_it_means(answer: str, expected: dict[str, object]) -> None:
    ask = sensitive_ask("u1", "religion", "x", ROWS)
    effect = {o.label: o.effect for o in ask.options}[answer]
    item = CaseItem(
        item_id="i1",
        kind=effect.kind or "x",
        title=effect.title,
        source="rules",  # type: ignore[arg-type]
        confidence="sure",  # type: ignore[arg-type]
        state="accepted",  # type: ignore[arg-type]
        upload_id="u1",
        payload=effect.payload,
        payload_digest="d",
        proposed_at=utc_now(),
    )
    case: Any = SimpleNamespace(items=(item,))
    assert sensitive_overrides(case) == expected
