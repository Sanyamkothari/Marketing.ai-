"""Memory (Plan L M159, DEC-1590 … DEC-1595): the next file of the same shape is offered the same treatment.

When a training file's steps are approved (by Guided setup's `apply` before, by the Data Doctor's approval now:
`remember_approved_case`), `remember` writes down the file's **shape** and what treated it: the recipe's id, the prepared upload that holds it and how intake read the
file (the sheet, intake on or off). When a later file of the same shape arrives - an upload, an import, a
hand-off from Data Doctor, training or scoring - `offer_for` says so ("This looks like billing_sept.csv,
which you prepared with Data Doctor on 3 Sep"), and **Use the same treatment**
(`use_same_treatment`) ticks the saved steps in the session. They are still shown, previewed and approved
like every other step: nothing is ever applied without Approve.

**Shape.** The column names in file order as ingest fingerprints them (`engine.stages.ingest.fingerprint_columns`,
the profile's `fingerprint.columns`), plus the sheet's name for a workbook, hashed (`shape_of`). Two files
with the same columns from the same sheet have the same shape whatever their rows say.

**Whose memory.** Per use case. An upload is not client-scoped today (`docs/CLIENT_ISOLATION.md` section
2.1: `POST /uploads` is the deployment's own path and a run of an upload records no client), and the
deployment is the client (single-tenant, section 1), so the memory is the deployment's, one per use case,
at `doctor_memory/<use case>/<shape>.json`. A Minfy laptop holding several clients' uploads shares it, as
it shares their uploads. Onboarding build specs are client-scoped and are not remembered here yet.

**What is stored.** Column names, the shape hash, ids (recipe, uploads), the sheet's name, intake on or
off, the number of steps and two dates. Never a cell value: the steps themselves stay in the prepared
upload's `data_recipe.json`, under that upload's retention and erasure, and an offer whose recipe is gone
is not made. Retention deletes an entry with its recipe's upload, or when it outlives the use case's
`governance.retention_days`; erasure deletes an entry that holds the person's id, as it deletes an LLM
cache entry (`engine.privacy`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import AwareDatetime, Field

from engine.config import StrictBase
from engine.storage import Storage, StorageError, upload_key
from engine.utils.logging import get_logger
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.agent.contracts import AgentSession, DataRecipe
    from engine.agent.tools import AgentContext
    from engine.doctor.case import Case
    from engine.doctor.diagnose import LoadedFile
    from engine.intake.contracts import IntakeReport

__all__ = [
    "MEMORY_PREFIX",
    "MEMORY_TOOL",
    "TREATMENT_RECIPE_FILENAME",
    "TreatmentMemory",
    "TreatmentOffer",
    "forget",
    "memory_key",
    "offer_for",
    "recall",
    "remember",
    "remember_approved_case",
    "remembered",
    "remembered_recipe",
    "shape_of",
    "use_same_treatment",
    "used_in",
]

_LOG = get_logger(__name__)

MEMORY_PREFIX: Final[str] = "doctor_memory/"
"""Where the memory lives in the artefact store; `engine.privacy.layout` knows it as its own store."""

MEMORY_TOOL: Final[str] = "doctor_memory"
"""The evidence a remembered step cites in a Guided-setup session."""

_SHAPE_HEADER: Final[bytes] = b"marketing-ai/doctor-shape/v1\n"
_RECIPE_FILENAME: Final[str] = "data_recipe.json"
"""`engine.agent.contracts.DATA_RECIPE_FILENAME`, spelled here so this module imports nothing heavy."""
TREATMENT_RECIPE_FILENAME: Final[str] = "treatment_recipe.json"
"""The recipe a Data Doctor approval keeps beside its source upload. Not `data_recipe.json`: that name marks an
upload as prepared, and the source upload is not."""


class TreatmentMemory(StrictBase):
    """`doctor_memory/<use case>/<shape>.json`: one shape, and what treated it last."""

    schema_version: Literal[1] = 1
    use_case_id: str = Field(description="The use case the treatment was approved for.")
    shape_hash: str = Field(description="`shape_of(columns, sheet)`.")
    columns: tuple[str, ...] = Field(description="The column names, in file order. Names only.")
    sheet: str | None = Field(
        default=None, description="The workbook sheet read; null for a CSV or Parquet file."
    )
    intake_enabled: bool = Field(default=True, description="False when the file was read as it arrived.")
    recipe_id: str = Field(description="The approved recipe.")
    recipe_upload_id: str = Field(description="The upload whose recipe file holds the steps.")
    recipe_file: str = Field(
        default=_RECIPE_FILENAME,
        description="The recipe's file name in that upload: `data_recipe.json` for a prepared upload (Guided "
        "setup), `treatment_recipe.json` for the Data Doctor's, which must not read as a prepared upload.",
    )
    source_upload_id: str = Field(description="The upload the treatment was approved on.")
    steps: int = Field(ge=1, description="How many steps the recipe has.")
    approved_at: AwareDatetime = Field(description="When the recipe was approved.")
    remembered_at: AwareDatetime = Field(
        description="When this entry was written; retention dates it by this."
    )


class TreatmentOffer(StrictBase):
    """What `POST /uploads` and Guided setup say about a file whose shape is remembered."""

    kind: Literal["same_shape", "same_sheet"] = Field(
        description=(
            "`same_shape`: the file has the columns (and sheet) of a file prepared before, and its steps can be "
            "ticked in Guided setup; `same_sheet`: a workbook that has the sheet read last time, which was "
            "not the one read now - read that sheet to be offered its treatment."
        )
    )
    use_case_id: str
    shape_hash: str
    recipe_id: str
    steps: int = Field(description="Steps the remembered recipe has.")
    source_upload_id: str = Field(description="The file the treatment was approved on.")
    source_file_name: str | None = Field(
        default=None,
        description="That file's name, while its upload is kept; null after retention removed it.",
    )
    approved_at: AwareDatetime
    sheet: str | None = Field(default=None, description="The sheet read last time.")
    intake_enabled: bool = Field(default=True, description="Whether intake read the file last time.")
    can_apply: bool = Field(
        description="True when Guided setup's Use the same treatment can tick the steps on this file (training only)."
    )
    sentence: str = Field(description="The offer in one plain sentence.")


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
def shape_of(columns: Sequence[str], sheet: str | None = None) -> str:
    """The shape of a file: its column names in order and its sheet, hashed. No value is read."""
    payload = json.dumps({"sheet": sheet or None, "columns": [str(c) for c in columns]}, ensure_ascii=False)
    return "shape:" + hashlib.sha256(_SHAPE_HEADER + payload.encode("utf-8")).hexdigest()


def memory_key(use_case_id: str, shape_hash: str) -> str:
    """`doctor_memory/<use case>/<hex>.json`."""
    if not use_case_id or "/" in use_case_id or use_case_id.startswith("."):
        raise ValueError(f"{use_case_id!r} is not a use case id.")
    return f"{MEMORY_PREFIX}{use_case_id}/{shape_hash.removeprefix('shape:')}.json"


def remember(
    storage: Storage,
    *,
    use_case_id: str,
    columns: Sequence[str],
    sheet: str | None,
    intake_enabled: bool,
    recipe: DataRecipe,
    recipe_upload_id: str,
    source_upload_id: str,
    recipe_file: str = _RECIPE_FILENAME,
    clock: Callable[[], datetime] = utc_now,
) -> TreatmentMemory:
    """Write down that files of this shape were treated with `recipe`; the latest approval wins."""
    shape = shape_of(columns, sheet)
    memory = TreatmentMemory(
        use_case_id=use_case_id,
        shape_hash=shape,
        columns=tuple(str(c) for c in columns),
        sheet=sheet,
        intake_enabled=intake_enabled,
        recipe_id=recipe.recipe_id,
        recipe_upload_id=recipe_upload_id,
        recipe_file=recipe_file,
        source_upload_id=source_upload_id,
        steps=len(recipe.steps),
        approved_at=recipe.created_at,
        remembered_at=clock(),
    )
    storage.write_model(memory_key(use_case_id, shape), memory)
    _LOG.info("doctor.memory.remembered use_case=%s steps=%d", use_case_id, memory.steps)
    return memory


def remember_approved_case(
    storage: Storage,
    *,
    case: Case,
    loaded: LoadedFile,
    clock: Callable[[], datetime] = utc_now,
) -> TreatmentMemory | None:
    """Data Doctor approval: write down the treatment of a one-file case for its goal, so `recall_treatment` offers
    it for the next file of the same shape. None when there is nothing to remember: no goal, several files, a
    build, no step (a join or stack is not a treatment of one file's shape), or a goal that does not train (a scoring
    case never writes over the memory a training case left, as Guided setup wrote it only for training). A workbook
    sheet the case read is part of the shape (`shape_of(columns, sheet)`), so the upload offers and the rename checks
    find the memory.

    The recipe is the case's stored prescription's, kept beside the source upload as `treatment_recipe.json`.
    """
    from engine.doctor.prescription import load_prescription
    from engine.doctor.visit import stored_brief

    goal = next((g for g in case.goals if g.active), None)
    brief = stored_brief(storage, case) if goal is not None else None
    if brief is not None and brief.mode != "train":
        return None
    if (
        goal is None
        or len(case.files) != 1
        or not case.prescriptions
        or loaded.upload_id != case.files[0].upload_id
    ):
        return None
    prescription = load_prescription(
        storage, case.client_id, case.case_id, case.prescriptions[-1].prescription_hash
    )
    recipes = prescription.recipes()
    if len(recipes) != 1:
        return None
    intake = prescription.files[0].intake if prescription.files else None
    storage.write_model(upload_key(loaded.upload_id, TREATMENT_RECIPE_FILENAME), recipes[0])
    return remember(
        storage,
        use_case_id=goal.goal_id,
        columns=[
            str(c) for c in loaded.frame.columns
        ],  # the columns `recall_treatment` reads the next file by
        sheet=intake.sheet if intake is not None else None,
        intake_enabled=not (intake is not None and intake.turned_off),
        recipe=recipes[0],
        recipe_upload_id=loaded.upload_id,
        source_upload_id=loaded.upload_id,
        recipe_file=TREATMENT_RECIPE_FILENAME,
        clock=clock,
    )


def recall(storage: Storage, use_case_id: str, shape_hash: str) -> TreatmentMemory | None:
    """The memory of this shape, while the recipe it names is still kept; else None."""
    try:
        key = memory_key(use_case_id, shape_hash)
        if not storage.exists(key):
            return None
        memory = storage.read_model(key, TreatmentMemory)
    except (StorageError, ValueError):
        _LOG.info("doctor.memory.unreadable use_case=%s", use_case_id)
        return None
    return memory if storage.exists(upload_key(memory.recipe_upload_id, memory.recipe_file)) else None


def remembered_recipe(storage: Storage, memory: TreatmentMemory) -> DataRecipe | None:
    """The steps a memory points at, read from the prepared upload that holds them; None when gone."""
    from engine.agent.contracts import DataRecipe

    key = upload_key(memory.recipe_upload_id, memory.recipe_file)
    try:
        recipe = storage.read_model(key, DataRecipe)
    except (StorageError, ValueError):
        return None
    return recipe if recipe.recipe_id == memory.recipe_id else None


def forget(storage: Storage, use_case_id: str, shape_hash: str) -> bool:
    """Delete one entry; whether there was one."""
    key = memory_key(use_case_id, shape_hash)
    if not storage.exists(key):
        return False
    storage.delete(key)
    return True


def _entries(storage: Storage, use_case_id: str) -> list[TreatmentMemory]:
    found: list[TreatmentMemory] = []
    for key in storage.list_keys(f"{MEMORY_PREFIX}{use_case_id}/"):
        try:
            found.append(storage.read_model(key, TreatmentMemory))
        except (StorageError, ValueError):
            continue
    return found


def remembered(storage: Storage, use_case_id: str) -> list[TreatmentMemory]:
    """Every shape remembered for the use case whose recipe is still kept (DEC-1597: a file of a shape close
    to one of these may be offered its renames, `engine.doctor.renames`)."""
    return [
        m for m in _entries(storage, use_case_id) if recall(storage, use_case_id, m.shape_hash) is not None
    ]


# ---------------------------------------------------------------------------
# The offer
# ---------------------------------------------------------------------------
def offer_for(
    storage: Storage,
    *,
    use_case_id: str,
    upload_id: str,
    columns: Sequence[str],
    intake: IntakeReport | None,
    mode: str,
) -> TreatmentOffer | None:
    """What to say about a new file: the treatment of a file of the same shape, or None.

    A workbook whose remembered sheet is there but was not the one read is told so (`same_sheet`), so
    the person can read that sheet as last time; nothing is read again here.
    """
    try:
        return _offer(storage, use_case_id, upload_id, columns, intake, mode)
    except Exception:  # an offer is a courtesy; a store it cannot read never fails an upload
        _LOG.warning("doctor.memory.offer_failed use_case=%s", use_case_id)
        return None


def _offer(
    storage: Storage,
    use_case_id: str,
    upload_id: str,
    columns: Sequence[str],
    intake: IntakeReport | None,
    mode: str,
) -> TreatmentOffer | None:
    sheet = intake.sheet if intake is not None else None
    memory = recall(storage, use_case_id, shape_of(columns, sheet))
    if memory is not None and upload_id not in {memory.source_upload_id, memory.recipe_upload_id}:
        return _as_offer(storage, memory, kind="same_shape", training=mode == "train")
    if intake is None or not intake.sheets:
        return None
    names = {s.name for s in intake.sheets if not s.used}
    candidates = [
        m
        for m in _entries(storage, use_case_id)
        if m.sheet is not None and m.sheet in names and recall(storage, use_case_id, m.shape_hash) is not None
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda m: m.remembered_at)
    return _as_offer(storage, latest, kind="same_sheet", training=False)


def _by(memory: TreatmentMemory) -> str:
    """Who prepared it, by the recipe the memory names: the Data Doctor's own, else the earlier guided steps."""
    return " with Data Doctor" if memory.recipe_file == TREATMENT_RECIPE_FILENAME else ""


def _as_offer(
    storage: Storage, memory: TreatmentMemory, *, kind: Literal["same_shape", "same_sheet"], training: bool
) -> TreatmentOffer:
    name = _file_name(storage, memory.source_upload_id)
    which = name if name else "a file"
    when = _day(memory.approved_at)
    steps = f"{memory.steps} step{'s' if memory.steps != 1 else ''}"
    if kind == "same_sheet":
        sentence = (
            f"Last time the sheet “{memory.sheet}” of {which} was prepared{_by(memory)} on {when} "
            f"({steps}). This workbook has that sheet: read it to be offered the same treatment."
        )
    elif training:
        sentence = (
            f"This looks like {which}, which you prepared{_by(memory)} on {when} ({steps}). "
            "Use the same treatment? The steps are ticked for you to check, and nothing changes until you approve."
        )
    else:
        sentence = (
            f"This looks like {which}, which you prepared{_by(memory)} on {when} ({steps}). A file to score "
            "is prepared by the steps saved with the model it is scored with."
        )
    return TreatmentOffer(
        kind=kind,
        use_case_id=memory.use_case_id,
        shape_hash=memory.shape_hash,
        recipe_id=memory.recipe_id,
        steps=memory.steps,
        source_upload_id=memory.source_upload_id,
        source_file_name=name,
        approved_at=memory.approved_at,
        sheet=memory.sheet,
        intake_enabled=memory.intake_enabled,
        can_apply=kind == "same_shape" and training,
        sentence=sentence,
    )


def _day(moment: datetime) -> str:
    """`3 Sep 2026`."""
    return f"{moment.day} {moment:%b %Y}"


def _file_name(storage: Storage, upload_id: str) -> str | None:
    try:
        record: Any = json.loads(storage.read_bytes(upload_key(upload_id, "upload.json")))
    except (StorageError, ValueError):
        return None
    name = record.get("file_name") if isinstance(record, dict) else None
    return str(name) if isinstance(name, str) and name else None


# ---------------------------------------------------------------------------
# Use the same treatment, in a Guided-setup session
# ---------------------------------------------------------------------------
def used_in(session: AgentSession, recipe_id: str) -> bool:
    """Whether the session already took this remembered treatment."""
    return any(r.tool == MEMORY_TOOL and r.args.get("recipe_id") == recipe_id for r in session.tool_results)


def use_same_treatment(
    session: AgentSession,
    ctx: AgentContext,
    recipe: DataRecipe,
    memory: TreatmentMemory,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> AgentSession:
    """The session with the remembered steps ticked, as the person's own decision; `SessionError` if not.

    A step the helper suggested again is ticked where it is; one it did not is added, labelled as the
    person's (they approved it last time) and ticked. The roles the recipe was approved with are ticked
    when the helper proposes them. Nothing else is decided: what the helper found this time is still
    there to decide, and Approve still runs the preview's checks on every row.
    """
    from engine.agent.advisor import ROLE_PRIMARY_KEY, ROLE_TARGET, recipe_steps
    from engine.agent.contracts import (
        AgentConfidence,
        DecidedBy,
        Proposal,
        ProposalKind,
        ProposalSource,
        ProposalState,
        RecipeStepKind,
        SessionStatus,
        ToolResult,
    )
    from engine.agent.proposals import SessionError, identity, roles_of, with_summary
    from engine.agent.recipe import RecipeError, check_recipe
    from engine.agent.untrusted import display_name

    # Steps that change the rows come back only through their own blocking question (decision O3): memory
    # never ticks one. `unpivot` keeps its Plan L behaviour (it is re-added ticked) until Plan L's owners decide.
    asked_first: dict[RecipeStepKind, str] = {
        RecipeStepKind.COMBINE_ROWS: (
            "Last time the rows of each customer were combined. Answer the helper's question about "
            "combining rows first, then use the same treatment."
        ),
        RecipeStepKind.KEEP_LATEST_PER_ID: (
            "Last time only the latest row of each customer was kept. Answer the helper's question about "
            "keeping the latest row first, then use the same treatment."
        ),
    }

    if session.status is SessionStatus.APPLIED:
        raise SessionError(
            "AGENT_SESSION_APPLIED", "This setup was already approved. Start again to change it."
        )
    if session.explore or session.mode != "train":
        raise SessionError(
            "DOCTOR_MEMORY_NOT_TRAINING",
            "The same treatment is used while setting up a file to learn from; a file to score is prepared by "
            "the steps saved with its model.",
        )
    if session.stop_reason is not None:
        raise SessionError("AGENT_SESSION_STOPPED", session.stop_reason)
    if used_in(session, recipe.recipe_id):
        raise SessionError("DOCTOR_MEMORY_USED", "The same treatment is already ticked in this setup.")

    decided = {"state": ProposalState.ACCEPTED, "decided_by": DecidedBy.USER}
    proposals = list(session.proposals)
    for path, wanted in ((ROLE_PRIMARY_KEY, recipe.primary_key), (ROLE_TARGET, recipe.target)):
        for index, proposal in enumerate(proposals):
            if (
                wanted is not None
                and proposal.kind is ProposalKind.ROLE
                and proposal.path == path
                and proposal.value == wanted
                and proposal.state is ProposalState.PENDING
            ):
                proposals[index] = proposal.model_copy(update=decided)

    already = {
        identity(p)
        for p in proposals
        if p.kind is ProposalKind.RECIPE_STEP and p.step is not None and p.state is ProposalState.ACCEPTED
    }
    for step in recipe.steps:
        if step.kind in asked_first and not any(
            p.step is not None and p.step.kind is step.kind and p.state is ProposalState.ACCEPTED
            for p in proposals
        ):
            # A change to the rows comes back only through its own blocking question, never ticked by memory
            # (decision O3, Plan O M238).
            raise SessionError("DOCTOR_MEMORY_COMBINE", asked_first[step.kind])

    evidence_id = f"m{len(session.tool_results) + 1}"
    while any(r.evidence_id == evidence_id for r in session.tool_results):
        evidence_id += "x"
    when = _day(memory.approved_at)
    added = 0
    for step in recipe.steps:
        if step.kind in asked_first:
            continue  # accepted above, through the helper's own question
        candidate = Proposal(
            proposal_id="m0",
            kind=ProposalKind.RECIPE_STEP,
            title="-",
            reason="-",
            step=step,
            evidence_ids=(evidence_id,),
            confidence=AgentConfidence.CHECK,
        )
        wanted_identity = identity(candidate)
        if wanted_identity in already:
            continue
        match = next((i for i, p in enumerate(proposals) if identity(p) == wanted_identity), None)
        if match is not None:
            proposals[match] = proposals[match].model_copy(update=decided)
            already.add(wanted_identity)
            continue
        proposal_id = f"m{len(proposals) + 1}"
        while any(p.proposal_id == proposal_id for p in proposals):
            proposal_id += "x"
        reason = (
            f"You approved this on {when} for a file with the same columns. It is ticked because you chose the "
            "same treatment; untick it to leave it out."
        )
        proposals.append(
            candidate.model_copy(
                update={
                    "proposal_id": proposal_id,
                    "title": f"As last time: {step.kind.value.replace('_', ' ')} in '{display_name(step.column)}'",
                    "reason": reason,
                    "step": step.model_copy(update={"decided_by": DecidedBy.USER}),
                    "source": ProposalSource.PERSON,
                    **decided,
                }
            )
        )
        already.add(wanted_identity)
        added += 1

    updated = session.model_copy(update={"proposals": tuple(proposals)})
    key, target = roles_of(updated)
    try:
        check_recipe(
            recipe_steps(p for p in proposals if p.state is ProposalState.ACCEPTED),
            columns=[str(c) for c in ctx.frame.columns],
            primary_key=key,
            target=target,
            levels=ctx.config.agent.levels,
        )
    except RecipeError as exc:
        raise SessionError(exc.code, exc.message) from exc
    result = ToolResult(
        evidence_id=evidence_id,
        tool=MEMORY_TOOL,
        args={"recipe_id": recipe.recipe_id},
        result={
            "recipe_id": recipe.recipe_id,
            "steps": len(recipe.steps),
            "added": added,
            "approved_at": memory.approved_at.isoformat(),
            "shape_hash": memory.shape_hash,
        },
        created_at=clock(),
    )
    updated = updated.model_copy(update={"tool_results": (*session.tool_results, result)})
    return with_summary(updated, ctx, clock=clock)
