"""Plan O M239 acceptance: the outcome tools (DEC-1809; the design §5.5 D #45-#47, §6.6, §9.2).

* A file with Active / Churned / Suspended trains after an approved map; Suspended rows are left out and counted.
* "A bill paid more than 7 days late" builds as a value threshold, counted by the builder's `build_labels`, through
  the customer-ID rules M157's offer uses.
* A changed sentence records nothing (`APPROVAL_SENTENCE_CHANGED`).
* The recipe still refuses any step on the outcome; `map_outcome_values` writes no step.
* A text regression target is `not_ready` with `X_REGRESSION_TARGET_NUMERIC`.

And what the binding rules add: the outcome is never picked silently (whatever decides the meaning and is not given
is a blocking question, and no definition is proposed), every definition is APPROVE-class with its exact sentence
(M233's rule sets, default deny), and the builder runs only the approved definition.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.access.roles import Principal, Role
from engine.agent.checks import check_plan
from engine.agent.config import AgentLevel
from engine.agent.contracts import RecipeStep, RecipeStepKind
from engine.agent.recipe import RecipeError, check_recipe
from engine.config import LabelType, ProblemType, RunMode, get_roles, load_use_case
from engine.doctor import policy as pol
from engine.doctor.case import CaseStore
from engine.doctor.diagnose import LoadedFile
from engine.doctor.outcome_definition import (
    OutcomeDefinition,
    OutcomeError,
    apply_outcome_values,
    approval_for,
    check_outcome_approval,
    label_spec_of,
    propose_event_outcome,
    record_outcome_approval,
)
from engine.doctor.outcomes import load_outcome_catalogue
from engine.doctor.registry import DataToolContext, DataToolError, DataToolResult, data_registry
from engine.stages.ingest import profile_dataset
from engine.storage import LocalStorage
from tests.unit.agent.helpers import context_for
from tests.unit.doctor.readiness_fixtures import CONFIGS, config, customers, report
from tests.unit.doctor.test_outcomes import as_file, recharges
from tests.unit.doctor.test_outcomes import customers as rca_customers

ANALYST = Principal(user_id="u-asha", username="asha", roles=frozenset({Role.ANALYST}), kind="user")
OTHER = Principal(user_id="u-ravi", username="ravi", roles=frozenset({Role.ANALYST}), kind="user")
VIEWER = Principal(user_id="u-vik", username="vik", roles=frozenset({Role.VIEWER}), kind="user")
AT = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
USE_CASE = "targeted-advertisement"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def status_file(rows: int = 2000, *, seed: int = 0) -> pd.DataFrame:
    """A Targeted Advertisement file whose outcome is a status column: Churned, Active, or Suspended."""
    frame = customers(rows, seed=seed)
    converted = frame.pop("converted_30d")
    frame["status"] = [
        "Churned" if value == 1 else ("Suspended" if index % 9 == 0 else "Active")
        for index, value in enumerate(converted)
    ]
    return frame


def tool_context(frame: pd.DataFrame, *, target: str | None = "status", **extra: Any) -> DataToolContext:
    agent = context_for(frame, USE_CASE, config_root=CONFIGS, target=target)
    return DataToolContext(
        agent=agent,
        principal=ANALYST,
        caller="person",
        config_root=CONFIGS,
        primary_key="customer_id",
        **extra,
    )


def call(name: str, given: dict[str, Any], ctx: DataToolContext) -> DataToolResult:
    return data_registry().call(name, given, ctx)


def approved_map(frame: pd.DataFrame) -> tuple[OutcomeDefinition, str, DataToolResult]:
    result = call(
        "map_outcome_values", {"column": "status", "map": {"Suspended": "exclude"}}, tool_context(frame)
    )
    definition = OutcomeDefinition.model_validate(result.output["definition"])
    return definition, str(result.output["sentence"]), result


def plan_of(frame: pd.DataFrame, target: str = "status") -> Any:
    return check_plan(
        frame,
        load_use_case(USE_CASE, CONFIGS),
        mode=RunMode.TRAIN,
        primary_key="customer_id",
        target=target,
        upload_id="u-fixture",
        row_count=len(frame),
    )


def codes_of(validation: Any) -> set[str]:
    return {check.code for check in validation.checks if check.severity.value == "error"}


# ---------------------------------------------------------------------------
# 1. Active / Churned / Suspended trains after an approved map; Suspended rows are left out and counted
# ---------------------------------------------------------------------------
def test_active_churned_suspended_trains_after_an_approved_map_with_suspended_left_out_and_counted(
    tmp_path: Path,
) -> None:
    frame = status_file()
    before = plan_of(frame)
    assert not before.passed and "TARGET_NOT_BINARY" in codes_of(before), "three values cannot train today"

    asked = call("map_outcome_values", {"column": "status"}, tool_context(frame))
    out = asked.output
    assert out["definition"] is None and out["blocking"] == ["OUTCOME_UNDECIDED"]
    by_value = {item["value"]: item for item in out["values"]}
    assert by_value["Churned"]["meaning"] == "yes" and by_value["Active"]["meaning"] == "no"
    assert by_value["Suspended"]["meaning"] is None, "the rules never guess what Suspended means"
    (question,) = out["questions"]
    assert question["blocking"] and "Suspended" in question["text"]
    assert {o["option_id"] for o in question["options"]} == {"yes", "no", "exclude", "dont-know"}

    definition, sentence, answered = approved_map(frame)
    suspended = int((frame["status"] == "Suspended").sum())
    churned = int((frame["status"] == "Churned").sum())
    assert definition.kind == "column_values" and definition.exclude == ("Suspended",)
    assert f"the {suspended:,} rows with 'Suspended' are left out" in sentence
    assert answered.output["counts"]["excluded"] == suspended and answered.output["source"] == "person"

    store = CaseStore(LocalStorage(tmp_path))
    case = store.create(client_id="default", created_by=ANALYST.user_id)
    approval = approval_for(definition, sentence, user_id=ANALYST.user_id, at=AT)
    recorded = record_outcome_approval(
        store,
        client_id="default",
        case_id=case.case_id,
        expected_version=case.version,
        item_id=answered.evidence_id,
        kind="data.map_outcome_values",
        definition=definition,
        current_sentence=sentence,
        approval=approval,
        approver=ANALYST,
        config_root=CONFIGS,
    )
    (kept,) = recorded.approvals
    assert kept.approve_class and kept.text_shown == sentence and kept.approved_by == ANALYST.user_id

    prepared, receipt = apply_outcome_values(frame, definition, approval)
    assert receipt.excluded == suspended and receipt.rows_out == len(frame) - suspended == len(prepared)
    assert receipt.yes == churned and receipt.no == len(frame) - suspended - churned
    assert set(prepared["status"]) == {0, 1} and int(prepared["status"].sum()) == churned
    assert list(prepared.columns) == list(frame.columns), "only the outcome column changes"
    after = plan_of(prepared)
    assert after.passed, sorted(codes_of(after))
    readiness = report(prepared, config(USE_CASE), target="status")
    assert readiness.verdict != "not_ready", readiness.verdict_reason


class _NoJobs:
    def submit(self, job_id: str, fn: object) -> object:  # pragma: no cover - never called
        raise AssertionError("run_train must not submit jobs")

    def status(self, job_id: str) -> object:  # pragma: no cover - never called
        raise AssertionError("run_train must not read job status")

    def cancel(self, job_id: str) -> bool:  # pragma: no cover - never called
        return False

    def shutdown(self, *, wait: bool = True) -> None:  # pragma: no cover - never called
        return None


@pytest.mark.slow
def test_the_approved_map_really_trains(tmp_path: Path) -> None:
    """A real training run on the mapped file, on a temporary store (marked slow: it starts AutoGluon)."""
    from engine.config import resolve_config
    from engine.contracts import RunRecord, RunState
    from engine.jobs import CancelToken
    from engine.pipeline import Pipeline, StageContext
    from engine.registry import LocalModelRegistry
    from engine.storage import run_key, upload_key

    frame = status_file()
    definition, sentence, _result = approved_map(frame)
    prepared, _receipt = apply_outcome_values(
        frame, definition, approval_for(definition, sentence, user_id=ANALYST.user_id, at=AT)
    )
    storage = LocalStorage(tmp_path / "data")
    registry = LocalModelRegistry(tmp_path / "registry.db")
    source = upload_key("u1", "source.csv")
    storage.write_text(source, prepared.to_csv(index=False))
    overrides = {
        "model_search.time_limit_minutes": 1,
        "model_search.strategy": "fast",
        "model_search.tuning_trials": 5,
    }
    resolved = resolve_config(USE_CASE, overrides, root=CONFIGS)
    storage.write_model(run_key("r-m239", "run_config.json"), resolved)
    context = StageContext(
        run_id="r-m239",
        mode=RunMode.TRAIN,
        config=resolved.config,
        resolved=resolved,
        storage=storage,
        registry=registry,
        cancel=CancelToken(),
        primary_key=["customer_id"],
        target="status",
        upload_key=source,
        model_version_id=None,
    )
    Pipeline(storage, registry, _NoJobs()).run_train(context)  # type: ignore[arg-type]
    record = storage.read_model(run_key("r-m239", "run.json"), RunRecord)
    assert record.state is RunState.DONE, record.error


# ---------------------------------------------------------------------------
# 2. "A bill paid more than 7 days late" builds as a value threshold
# ---------------------------------------------------------------------------
def bill_payments() -> pd.DataFrame:
    """One payment per recharge of the M157 fixture, each paid some days after its due date."""
    paid = recharges()
    return pd.DataFrame(
        {
            "customer_id": paid["customer_id"],
            "payment_date": paid["recharge_date"],
            "days_late": [(index * 7) % 15 for index in range(len(paid))],
        }
    )


def plan_moves() -> pd.DataFrame:
    """Every fifth customer moves to another plan in the middle of each month (they are left out then)."""
    rows = [
        (f"C{index:04d}", f"2025-{month:02d}-15", "family")
        for index in range(0, 200, 5)
        for month in range(1, 7)
    ]
    return pd.DataFrame(rows, columns=["customer_id", "change_date", "new_plan"])


def _prefixed(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(customer_id="CUST-" + frame["customer_id"].str.slice(1))


def _bare(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(customer_id=frame["customer_id"].str.slice(1).astype(int).astype(str))


def threshold_proposal(files: list[Any], **change: Any) -> Any:
    rca = load_use_case("rca", CONFIGS)
    given: dict[str, Any] = {
        "kind": "value_threshold",
        "event_upload_id": "u_pay",
        "days": 30,
        "column": "days_late",
        "operator": ">",
        "threshold": 7,
        **change,
    }
    return propose_event_outcome(files, rca, get_roles(CONFIGS), load_outcome_catalogue(CONFIGS), **given)


def recount(
    payments: pd.DataFrame, moves: pd.DataFrame | None, snapshot: date, days: int
) -> tuple[int, int, int]:
    """(customers counted, yes, left out) at `snapshot`, counted directly with pandas: (T, T + days]."""
    low, high = pd.Timestamp(snapshot), pd.Timestamp(snapshot) + pd.Timedelta(days=days)
    times = pd.to_datetime(payments["payment_date"])
    late = payments.loc[(times > low) & (times <= high) & (payments["days_late"] > 7), "customer_id"]
    left: set[str] = set()
    if moves is not None:
        moved = pd.to_datetime(moves["change_date"])
        left = set(moves.loc[(moved > low) & (moved <= high), "customer_id"])
    everyone = {f"C{index:04d}" for index in range(200)} - left
    return len(everyone), len(set(late) & everyone), len(left)


def test_a_bill_paid_more_than_seven_days_late_builds_as_a_value_threshold() -> None:
    files = [
        as_file(rca_customers(), "customers.csv", CONFIGS, "u_list"),
        as_file(bill_payments(), "bill_payments.csv", CONFIGS, "u_pay"),
    ]
    found = threshold_proposal(files)
    definition = found.definition
    assert definition is not None and definition.kind == "value_threshold"
    assert (definition.column, definition.operator, definition.threshold) == ("days_late", ">", 7.0)
    spec = label_spec_of(definition)
    assert spec.type is LabelType.VALUE_THRESHOLD and spec.expression == "days_late > 7"
    assert spec.horizon_days == 30 and spec.role == definition.event_role == "payments"
    assert "a bill in bill_payments.csv within 30 days after the snapshot has 'days_late' more than 7" in (
        found.sentence or ""
    )
    counts = found.counts
    assert counts is not None and counts.measured_on is not None
    customers_, yes, _left = recount(bill_payments(), None, counts.measured_on, 30)
    assert (counts.rows, counts.yes, counts.no) == (customers_, yes, customers_ - yes)
    assert counts.not_over >= 1, "month-ends whose window is not over are left out and counted"


def test_a_threshold_with_grace_and_a_file_that_leaves_customers_out_is_counted_by_the_builder() -> None:
    files = [
        as_file(rca_customers(), "customers.csv", CONFIGS, "u_list"),
        as_file(bill_payments(), "bill_payments.csv", CONFIGS, "u_pay"),
        as_file(plan_moves(), "plan_changes.csv", CONFIGS, "u_moves"),
    ]
    found = threshold_proposal(files, grace_days=5, exclude_upload_ids=("u_moves",))
    definition, counts = found.definition, found.counts
    assert definition is not None and counts is not None and counts.measured_on is not None
    assert definition.grace_days == 5 and definition.exclude == ("plan_changes",)
    assert definition.censoring == "leave_out"
    assert label_spec_of(definition).exclude_roles == ("plan_changes",)
    customers_, yes, left = recount(bill_payments(), plan_moves(), counts.measured_on, 35)
    assert (counts.rows, counts.yes, counts.excluded) == (customers_, yes, left) and left == 40
    sentence = found.sentence or ""
    assert "with 5 days' grace (35 days in all)" in sentence
    assert "A customer with a plan change in plan_changes.csv in that window is left out." in sentence
    assert f"{left:,} are left out" in sentence


def test_the_threshold_is_measured_through_the_key_rules_as_the_m157_offer_is() -> None:
    alike = threshold_proposal(
        [
            as_file(rca_customers(), "customers.csv", CONFIGS, "u_list"),
            as_file(bill_payments(), "bill_payments.csv", CONFIGS, "u_pay"),
        ]
    )
    ruled = threshold_proposal(
        [
            as_file(_prefixed(rca_customers()), "customers.csv", CONFIGS, "u_list"),
            as_file(_bare(bill_payments()), "bill_payments.csv", CONFIGS, "u_pay"),
        ]
    )
    assert alike.definition is not None and ruled.definition is not None
    assert (
        ruled.counts == alike.counts
    ), "the same customers, counted the same, once their IDs are written one way"
    assert alike.definition.entity_key_rule is None and ruled.definition.event_key_rule is not None
    assert ruled.definition.entity_key_rule is not None
    assert "nothing is matched on a near match" in (ruled.sentence or "")
    strangers = _bare(bill_payments()).assign(
        customer_id=lambda f: (f["customer_id"].astype(int) + 5000).astype(str),
        days_late=lambda f: f["days_late"] + 0.5,
    )
    with pytest.raises(OutcomeError) as refused:
        threshold_proposal(
            [
                as_file(_prefixed(rca_customers()), "customers.csv", CONFIGS, "u_list"),
                as_file(strangers, "bill_payments.csv", CONFIGS, "u_pay"),
            ]
        )
    assert refused.value.code == "OUTCOME_CANNOT_MEASURE"


def test_presence_absence_and_grace_count_what_the_m157_offer_counts() -> None:
    from tests.unit.doctor.test_outcomes import rca_offer

    files = [
        as_file(rca_customers(), "customers.csv", CONFIGS, "u_list"),
        as_file(recharges(), "recharges_2025.csv", CONFIGS, "u_events"),
    ]
    offer = rca_offer(files, CONFIGS)
    assert offer is not None
    rca = load_use_case("rca", CONFIGS)
    roles, catalogue = get_roles(CONFIGS), load_outcome_catalogue(CONFIGS)
    for choice in offer.choices:
        found = propose_event_outcome(files, rca, roles, catalogue, kind="event_absence", days=choice.days)
        assert found.counts is not None and found.definition is not None
        assert (found.counts.measured_on, found.counts.rows, found.counts.yes) == (
            choice.measured_on,
            choice.customers,
            choice.yes,
        )
        assert label_spec_of(found.definition).model_dump(
            exclude={"description"}
        ) == choice.label_spec.model_dump(exclude={"description"})
    graced = propose_event_outcome(files, rca, roles, catalogue, kind="event_absence", days=30, grace_days=30)
    sixty = next(choice for choice in offer.choices if choice.days == 60)
    assert graced.counts is not None and graced.counts.yes == sixty.yes, "30 days and 30 days' grace read 60"
    present = propose_event_outcome(files, rca, roles, catalogue, kind="event_presence", days=60)
    assert present.counts is not None and present.counts.yes == sixty.no, "presence is absence turned over"


# ---------------------------------------------------------------------------
# 3. A changed sentence records nothing
# ---------------------------------------------------------------------------
def test_a_changed_sentence_records_nothing(tmp_path: Path) -> None:
    frame = status_file()
    definition, sentence, answered = approved_map(frame)
    store = CaseStore(LocalStorage(tmp_path))
    case = store.create(client_id="default", created_by=ANALYST.user_id)
    before = (store.get("default", case.case_id), store.timeline("default", case.case_id))

    def attempt(
        approval: Any, *, now: str | None = sentence, approver: Principal = ANALYST, **extra: Any
    ) -> str:
        with pytest.raises(OutcomeError) as refused:
            record_outcome_approval(
                store,
                client_id="default",
                case_id=case.case_id,
                expected_version=case.version,
                item_id=answered.evidence_id,
                kind="data.map_outcome_values",
                definition=definition,
                current_sentence=now,
                approval=approval,
                approver=approver,
                config_root=CONFIGS,
                **extra,
            )
        assert (store.get("default", case.case_id), store.timeline("default", case.case_id)) == before
        return refused.value.code

    edited = approval_for(definition, sentence + " Also.", user_id=ANALYST.user_id, at=AT)
    assert attempt(edited) == "APPROVAL_SENTENCE_CHANGED"
    # The file changed since the person read the sentence: the server now shows another one.
    moved = frame.copy()
    moved.loc[moved.index[frame["status"].eq("Active")][0], "status"] = "Churned"
    _definition, new_sentence, _ = approved_map(moved)
    assert new_sentence != sentence and _definition == definition, "same meaning, new counts"
    read = approval_for(definition, sentence, user_id=ANALYST.user_id, at=AT)
    assert attempt(read, now=new_sentence) == "APPROVAL_SENTENCE_CHANGED"
    # Who approves: an Analyst, the person named in the approval, and another one when separation is on.
    viewer = approval_for(definition, sentence, user_id=VIEWER.user_id, at=AT)
    assert attempt(viewer, approver=VIEWER) == "TOOL_ROLE_REQUIRED"
    assert attempt(read, approver=OTHER) == "OUTCOME_NOT_APPROVED"
    assert attempt(read, approval_required=True, proposed_by=ANALYST.user_id) == "POLICY_ROLE_SEPARATION"
    other_definition = definition.model_copy(update={"exclude": ()})
    assert attempt(approval_for(other_definition, sentence, user_id=ANALYST.user_id, at=AT)) == (
        "OUTCOME_NOT_APPROVED"
    )
    # The sentence as shown, by the person who read it: recorded, once, with one approval event.
    kept = record_outcome_approval(
        store,
        client_id="default",
        case_id=case.case_id,
        expected_version=case.version,
        item_id=answered.evidence_id,
        kind="data.map_outcome_values",
        definition=definition,
        current_sentence=sentence,
        approval=read,
        approver=ANALYST,
        config_root=CONFIGS,
    )
    assert kept.version == case.version + 1 and kept.approvals[0].text_shown == sentence
    events = store.timeline("default", case.case_id)[len(before[1]) :]
    assert [event.kind.value for event in events] == ["approval"]
    assert events[0].details["definition_hash"] == definition.definition_hash
    assert sentence not in str(events[0].model_dump()), "the timeline holds ids and hashes, never the values"


def test_an_event_outcome_needs_its_exact_sentence_too() -> None:
    files = [
        as_file(rca_customers(), "customers.csv", CONFIGS, "u_list"),
        as_file(bill_payments(), "bill_payments.csv", CONFIGS, "u_pay"),
    ]
    found = threshold_proposal(files)
    assert found.definition is not None and found.sentence
    approval = approval_for(found.definition, found.sentence, user_id=ANALYST.user_id, at=AT)
    check_outcome_approval(
        kind="data.define_outcome",
        definition=found.definition,
        current_sentence=found.sentence,
        approval=approval,
        approver=ANALYST,
        config_root=CONFIGS,
    )
    other = threshold_proposal(files, threshold=10)
    with pytest.raises(OutcomeError) as changed:
        check_outcome_approval(
            kind="data.define_outcome",
            definition=found.definition,
            current_sentence=other.sentence,
            approval=approval,
            approver=ANALYST,
            config_root=CONFIGS,
        )
    assert changed.value.code == "APPROVAL_SENTENCE_CHANGED"


# ---------------------------------------------------------------------------
# 4. The recipe still refuses any step on the outcome
# ---------------------------------------------------------------------------
def test_the_recipe_still_refuses_any_step_on_the_outcome() -> None:
    frame = status_file()
    cases: tuple[tuple[RecipeStepKind, dict[str, Any]], ...] = (
        (RecipeStepKind.MAP_VALUES, {"mapping": {"Suspended": "Active"}}),
        (RecipeStepKind.MAP_BOOLEAN, {"true_values": ["Churned"], "false_values": ["Active"]}),
        (RecipeStepKind.DROP_COLUMN, {}),
    )
    for kind, params in cases:
        step = RecipeStep(order=1, kind=kind, column="status", params=params)
        with pytest.raises(RecipeError) as refused:
            check_recipe(
                [step],
                columns=list(frame.columns),
                primary_key="customer_id",
                target="status",
                levels=(AgentLevel.CLEAN, AgentLevel.DERIVE),
            )
        assert refused.value.code == "RECIPE_STEP_INVALID", kind
    # The chat's step on the outcome is refused by M233's policy too, with the same code.
    chat = pol.DataProposal(
        kind="data.recipe_step",
        source="ai",
        steps=(pol.StepFacts(RecipeStepKind.MAP_VALUES, "status", {"mapping": {"Suspended": "Active"}}),),
        looked_at=True,
        effect=pol.StepEffect(changed=10, failed=0, out_of=100, rows_tried=100),
    )
    ctx = pol.DataPolicyContext(
        policy=pol.load_data_policy(CONFIGS),
        primary_key="customer_id",
        target="status",
        levels=(AgentLevel.CLEAN, AgentLevel.DERIVE),
    )
    refused_by = pol.first_refusal(pol.evaluate(chat, ctx))
    assert refused_by is not None and refused_by.code == "RECIPE_STEP_INVALID"
    # map_outcome_values writes the outcome definition, never a recipe step.
    _definition, _sentence, answered = approved_map(frame)
    (proposal,) = pol.output_proposals("data.map_outcome_values", answered.output)
    assert proposal.steps == () and "step" not in answered.output and "steps" not in answered.output


# ---------------------------------------------------------------------------
# 5. A text regression target is not_ready with X_REGRESSION_TARGET_NUMERIC
# ---------------------------------------------------------------------------
def _regression(frame: pd.DataFrame, target: str) -> DataToolContext:
    ctx = tool_context(frame, target=target)
    assert ctx.agent is not None
    regression = config(USE_CASE, top={"problem_type": ProblemType.REGRESSION})
    return dataclasses.replace(ctx, agent=dataclasses.replace(ctx.agent, config=regression))


def test_a_text_regression_target_is_not_ready_with_x_regression_target_numeric() -> None:
    frame = customers()
    frame["converted_30d"] = np.random.default_rng(7).choice(["low", "medium", "high"], len(frame))
    checked = call("check_target_type", {}, _regression(frame, "converted_30d")).output
    assert checked["verdict"] == "not_ready" and checked["code"] == "X_REGRESSION_TARGET_NUMERIC"
    assert checked["not_numbers"] == len(frame) and checked["next_step"]
    regression = config(USE_CASE, top={"problem_type": ProblemType.REGRESSION})
    readiness = report(frame, regression)
    assert (
        readiness.verdict == "not_ready" and readiness.probe("X_REGRESSION_TARGET_NUMERIC").status == "fail"
    )
    amounts = customers().assign(converted_30d=np.random.default_rng(7).random(2000).round(2) * 900)
    fine = call("check_target_type", {}, _regression(amounts, "converted_30d")).output
    assert fine["verdict"] == "ok" and fine["code"] is None


def test_check_target_type_says_what_a_yes_no_outcome_needs() -> None:
    frame = status_file()
    three = call("check_target_type", {"column": "status"}, tool_context(frame)).output
    assert three["verdict"] == "needs_outcome_definition" and three["code"] == "TARGET_NOT_BINARY"
    assert three["next_tool"] == "map_outcome_values" and three["distinct"] == 3
    two = call("check_target_type", {}, tool_context(customers(), target="converted_30d")).output
    assert two["verdict"] == "ok"
    amounts = customers().assign(converted_30d=np.arange(2000) * 3)
    many = call("check_target_type", {}, tool_context(amounts, target="converted_30d")).output
    assert many["switch_to"] == "regression" and many["code"] == "TARGET_NOT_BINARY"
    one = call(
        "check_target_type", {}, tool_context(customers().assign(converted_30d=1), target="converted_30d")
    )
    assert one.output["verdict"] == "not_ready" and one.output["code"] == "TARGET_CONSTANT"


# ---------------------------------------------------------------------------
# Never picked silently
# ---------------------------------------------------------------------------
def test_a_value_the_rules_cannot_place_is_asked_and_one_sided_maps_propose_nothing() -> None:
    frame = status_file().assign(tier=lambda f: np.where(np.arange(len(f)) % 2 == 0, "Gold", "Silver"))
    unknown = call("map_outcome_values", {"column": "tier"}, tool_context(frame)).output
    assert unknown["definition"] is None and unknown["sentence"] is None
    assert len(unknown["questions"]) == 2 and unknown["questions_total"] == 2
    one_sided = call(
        "map_outcome_values", {"column": "tier", "map": {"Gold": "yes", "Silver": "yes"}}, tool_context(frame)
    )
    assert one_sided.output["definition"] is None and one_sided.output["blocking"] == ["OUTCOME_UNDECIDED"]
    assert one_sided.output["questions"] == [], "nothing left to ask, and still nothing proposed"
    assert pol.output_proposals("data.map_outcome_values", unknown) == [], "no definition, nothing to decide"
    with pytest.raises(DataToolError) as named:
        call("map_outcome_values", {"column": "tier", "map": {"Bronze": "no"}}, tool_context(frame))
    assert named.value.code == "OUTCOME_UNDECIDED"


def test_the_window_a_second_event_file_and_a_second_date_are_asked_never_picked() -> None:
    rca = load_use_case("rca", CONFIGS)
    roles, catalogue = get_roles(CONFIGS), load_outcome_catalogue(CONFIGS)
    files = [
        as_file(rca_customers(), "customers.csv", CONFIGS, "u_list"),
        as_file(recharges(), "recharges_2025.csv", CONFIGS, "u_events"),
    ]
    window = propose_event_outcome(files, rca, roles, catalogue, kind="event_absence")
    assert window.definition is None and window.sentence is None and window.blocking == ("OUTCOME_UNDECIDED",)
    (asked,) = window.questions
    assert [o.option_id for o in asked.options] == ["days-30", "days-60", "days-90"]
    assert [choice.days for choice in window.choices] == [30, 60, 90]
    assert all(choice.counts is not None for choice in window.choices), "each window is measured"
    two = [*files, as_file(bill_payments(), "bill_payments.csv", CONFIGS, "u_pay")]
    which = propose_event_outcome(two, rca, roles, catalogue, kind="event_absence", days=60)
    assert which.definition is None and which.questions[0].question_id == "outcome-event-file"
    dated = recharges().assign(entered_on=lambda f: f["recharge_date"])
    dates = [files[0], as_file(dated, "recharges_2025.csv", CONFIGS, "u_events")]
    when = propose_event_outcome(dates, rca, roles, catalogue, kind="event_absence", days=60)
    assert len(dates[1].diagnosis.date_columns) == 2
    assert when.definition is None and when.questions[0].question_id == "outcome-date-column"
    named = propose_event_outcome(
        dates, rca, roles, catalogue, kind="event_absence", days=60, date_column="recharge_date"
    )
    assert named.definition is not None and named.definition.date_column == "recharge_date"


def test_an_outcome_proposal_is_approve_class_never_ticked_and_default_deny() -> None:
    frame = status_file()
    _definition, sentence, answered = approved_map(frame)
    policy = pol.load_data_policy(CONFIGS)
    ctx = pol.DataPolicyContext(policy=policy)
    assert pol.check_tool_output("data.map_outcome_values", answered.output, ctx) is None
    for kind in ("data.map_outcome_values", "data.define_outcome"):
        rules = policy.rule_sets[kind]
        assert rules.approve == "always" and rules.approve_role is Role.ANALYST
        ticked = pol.check_tool_output(kind, {**answered.output, "source": "rules", "ticked": True}, ctx)
        assert ticked is not None and ticked.code == "POLICY_NEVER_TICKED"
        ai = pol.check_tool_output(kind, {**answered.output, "source": "ai", "ticked": True}, ctx)
        assert ai is not None and ai.code == "POLICY_NEVER_TICKED"
        unsentenced = pol.check_tool_output(kind, {**answered.output, "sentence": None}, ctx)
        assert unsentenced is not None and unsentenced.code == "POLICY_PAYLOAD_INVALID"
        bare = policy.model_copy(
            update={"rule_sets": {k: v for k, v in policy.rule_sets.items() if k != kind}}
        )
        denied = pol.check_tool_output(kind, answered.output, pol.DataPolicyContext(policy=bare))
        assert denied is not None and denied.code == "POLICY_NO_RULES"
    # request_approval (M236) asks for the person's own yes, with this exact sentence.
    asked = call(
        "request_approval", {"evidence_id": answered.evidence_id}, tool_context(frame, evidence=(answered,))
    ).output
    assert asked["approve_class"] is True and asked["sentence"] == sentence and asked["role"] == "analyst"
    assert answered.output["ticked"] is False and answered.output["confidence"] == "check"


def test_only_an_analyst_may_ask_for_an_outcome() -> None:
    frame = status_file()
    for name in ("map_outcome_values", "check_target_type"):
        with pytest.raises(DataToolError) as refused:
            call(name, {"column": "status"}, dataclasses.replace(tool_context(frame), principal=VIEWER))
        assert refused.value.code == "TOOL_ROLE_REQUIRED"


# ---------------------------------------------------------------------------
# The outcome builder runs only the approved definition
# ---------------------------------------------------------------------------
def test_the_builder_runs_only_the_approved_definition_and_never_guesses_a_new_value() -> None:
    frame = status_file()
    definition, sentence, _answered = approved_map(frame)
    approval = approval_for(definition, sentence, user_id=ANALYST.user_id, at=AT)
    other = definition.model_copy(update={"exclude": ()})
    with pytest.raises(OutcomeError) as wrong:
        apply_outcome_values(frame, other, approval)
    assert wrong.value.code == "OUTCOME_NOT_APPROVED"
    # Next month: other counts, the same meaning - the same approval applies (replay).
    later = status_file(1500, seed=3)
    later.loc[later.index[:7], "status"] = None
    prepared, receipt = apply_outcome_values(later, definition, approval)
    assert receipt.empty == 7 and receipt.rows_out == len(later) - 7 - receipt.excluded
    assert receipt.rows_in == receipt.rows_out + receipt.excluded + receipt.empty == len(later)
    assert len(prepared) == receipt.rows_out and set(prepared["status"]) == {0, 1}
    # A value the approved map never placed stops the builder; the file is untouched.
    paused = status_file().assign(status=lambda f: f["status"].where(f.index % 50 != 0, "Paused"))
    snapshot = paused.copy()
    with pytest.raises(OutcomeError) as unmapped:
        apply_outcome_values(paused, definition, approval)
    assert unmapped.value.code == "OUTCOME_VALUE_NOT_MAPPED" and "Paused" not in unmapped.value.message
    pd.testing.assert_frame_equal(paused, snapshot)


def test_columns_that_are_never_an_outcome_are_refused() -> None:
    frame = status_file().assign(contact_email=lambda f: [f"user{i}@example.test" for i in range(len(f))])
    ctx = tool_context(frame)
    for given, code in (
        ({"column": "tenure_months"}, "OUTCOME_TOO_MANY_VALUES"),
        ({"column": "customer_id"}, "TOOL_INPUT_INVALID"),
        ({"column": "contact_email"}, "TOOL_INPUT_INVALID"),
        ({"column": "nowhere"}, "AGENT_COLUMN_UNKNOWN"),
    ):
        with pytest.raises(Exception) as refused:
            call("map_outcome_values", given, ctx)
        assert getattr(refused.value, "code", None) == code, given
    undecided = tool_context(frame, target=None)
    assert undecided.agent is not None
    nothing = dataclasses.replace(
        undecided,
        agent=dataclasses.replace(
            undecided.agent,
            config=undecided.agent.config.model_copy(
                update={"target": undecided.agent.config.target.model_copy(update={"column": None})}
            ),
        ),
    )
    with pytest.raises(DataToolError) as unnamed:
        call("map_outcome_values", {}, nothing)
    assert unnamed.value.code == "TOOL_INPUT_INVALID", "the outcome column is never guessed"


# ---------------------------------------------------------------------------
# define_outcome through the registry
# ---------------------------------------------------------------------------
def _loaded(frame: pd.DataFrame, name: str, upload_id: str) -> LoadedFile:
    profile = profile_dataset(
        frame,
        load_use_case("rca", CONFIGS),
        upload_id=upload_id,
        file_name=name,
        file_format="csv",
        file_size_bytes=0,
        delimiter=",",
        encoding="utf-8",
        row_count=len(frame),
    )
    return LoadedFile(upload_id=upload_id, file_name=name, frame=frame, profile=profile)


def test_define_outcome_through_the_registry_is_pending_with_its_sentence_and_fits_its_budget() -> None:
    files = (
        _loaded(rca_customers(), "customers.csv", "u_list"),
        _loaded(bill_payments(), "bill_payments.csv", "u_pay"),
    )
    agent = context_for(rca_customers(), "rca", config_root=CONFIGS)
    ctx = DataToolContext(agent=agent, principal=ANALYST, caller="person", files=files, config_root=CONFIGS)
    given = {"kind": "value_threshold", "days": 30, "column": "days_late", "operator": ">", "threshold": 7}
    first = call("define_outcome", given, ctx)
    again = call("define_outcome", given, ctx)
    assert first.evidence_id == again.evidence_id, "the same question on the same files: the same evidence"
    out = first.output
    assert out["state"] == "pending" and out["ticked"] is False and out["confidence"] == "check"
    assert out["definition"]["kind"] == "value_threshold" and out["sentence"]
    assert out["definition_hash"] == OutcomeDefinition.model_validate(out["definition"]).definition_hash
    (proposal,) = pol.output_proposals("data.define_outcome", out)
    assert proposal.sentence == out["sentence"]
    asked = call("define_outcome", {"kind": "event_presence"}, ctx).output
    assert asked["definition"] is None and asked["questions"][0]["question_id"] == "outcome-window"
    with pytest.raises(DataToolError) as invalid:
        call("define_outcome", {"kind": "event_presence", "column": "days_late"}, ctx)
    assert invalid.value.code == "TOOL_INPUT_INVALID"
    with pytest.raises(DataToolError) as unknown:
        call("define_outcome", {**given, "event_upload_id": "u_nowhere"}, ctx)
    assert unknown.value.code == "OUTCOME_CANNOT_MEASURE"
    with pytest.raises(DataToolError) as words:
        call("define_outcome", {**given, "column": "customer_id"}, ctx)
    assert words.value.code == "OUTCOME_CANNOT_MEASURE"
    later = call("define_outcome", {**given, "days": 45}, ctx).output
    assert later["sentence"] != out["sentence"] and later["definition_hash"] != out["definition_hash"]
    assert timedelta(days=45).days == later["definition"]["days"]
