"""Same-treatment memory in the Data Doctor: an approval that reaches the hand-off is remembered, and
`recall_treatment` offers it for the next file of the same shape (it used to be written only by Guided setup).
"""

from __future__ import annotations

from pathlib import Path

from engine.access.roles import LOCAL_OPERATOR
from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState, RecipeStep, RecipeStepKind
from engine.agent.tools import AgentContext
from engine.config import RunMode, load_use_case
from engine.doctor.case import Case, CaseStore
from engine.doctor.memory import recall, remembered_recipe, shape_of
from engine.doctor.prescription import CasePrescriber
from engine.doctor.registry import DataToolContext, data_registry
from engine.storage import LocalStorage, upload_key
from tests.unit.doctor import visit_support as vs

STEP = RecipeStep(
    order=1, kind=RecipeStepKind.PARSE_NUMBER, column="monthly_spend", reason="approved fix"
).model_dump(mode="json")


def _with_a_step(storage: LocalStorage, case: Case) -> Case:
    """A sure fix the person has ticked, as the rules would have proposed it."""
    items = list(case.items)
    base = next(i for i in items if i.goal_id is None)
    step = base.model_copy(
        update={
            "item_id": "recipe_step.base.spend",
            "kind": "recipe_step",
            "title": "Turn 'monthly_spend' into numbers.",
            "upload_id": vs.FIRST,
            "payload": {"upload_id": vs.FIRST, "step": STEP},
            "payload_digest": "e" * 64,
            "source": ProposalSource.RULES,
            "confidence": AgentConfidence.SURE,
            "state": ProposalState.ACCEPTED,
            "decided_by": vs.ANALYST,
            "goal_id": None,
        }
    )
    return CaseStore(storage).update(
        vs.CLIENT,
        case.case_id,
        expected_version=case.version,
        actor=vs.ANALYST,
        change=lambda current: (current.model_copy(update={"items": (*items, step)}), []),
    )


def _approved(tmp_path: Path) -> tuple[LocalStorage, Case, vs.World]:
    storage = LocalStorage(tmp_path)
    w = vs.world()
    prescriber = CasePrescriber(storage, load_files=w.load)
    case = vs.open_case(storage, w)
    for name, _op, act in vs.SCRIPT:
        if name == "approve":
            case = _with_a_step(storage, CaseStore(storage).get(vs.CLIENT, case.case_id))
        case = act(
            vs.deps_for(storage, w, prescribe=prescriber), CaseStore(storage).get(vs.CLIENT, case.case_id)
        )
        if name == "approve":
            break
    return storage, CaseStore(storage).get(vs.CLIENT, case.case_id), w


def test_the_data_doctors_approval_is_remembered_and_recall_treatment_offers_it(tmp_path: Path) -> None:
    storage, case, w = _approved(tmp_path)
    assert case.visit is not None and case.visit.hand_off is not None, "the approval reached the hand-off"
    first = w.files[vs.FIRST]
    columns = [str(c) for c in first.frame.columns]
    memory = recall(storage, vs.GOAL_A, shape_of(columns))
    assert memory is not None and memory.steps == 1 and memory.source_upload_id == vs.FIRST
    recipe = remembered_recipe(storage, memory)
    assert recipe is not None and recipe.steps[0].column == "monthly_spend"
    assert not storage.exists(
        upload_key(vs.FIRST, "data_recipe.json")
    ), "the source upload is not made 'prepared'"

    # next month: another upload of the same shape is offered the same treatment through the tool
    config = load_use_case(vs.GOAL_A)
    second = w.files[vs.SECOND]
    agent = AgentContext(
        use_case_id=config.id,
        config=config,
        config_root=None,
        upload_id=second.upload_id,
        mode=RunMode.TRAIN,
        profile=second.profile,
        frame=second.frame,
    )
    ctx = DataToolContext(agent=agent, principal=LOCAL_OPERATOR, caller="person", storage=storage)
    offer = data_registry().call("recall_treatment", {}, ctx).output["offer"]
    assert offer is not None and offer["kind"] == "same_shape" and offer["recipe_id"] == recipe.recipe_id
    assert offer["steps"] == 1 and offer["can_apply"] is True


def test_a_plan_with_no_steps_remembers_nothing(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    w = vs.world()
    prescriber = CasePrescriber(storage, load_files=w.load)
    case = vs.open_case(storage, w)
    for name, _op, act in vs.SCRIPT:
        case = act(
            vs.deps_for(storage, w, prescribe=prescriber), CaseStore(storage).get(vs.CLIENT, case.case_id)
        )
        if name == "approve":
            break
    columns = [str(c) for c in w.files[vs.FIRST].frame.columns]
    assert recall(storage, vs.GOAL_A, shape_of(columns)) is None
