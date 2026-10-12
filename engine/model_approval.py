"""Whether a model version may be used, the one rule behind `MODEL_NOT_APPROVED` (Plan M M184, DEC-1614 (b)).

One rule, two callers, one code. While a use case requires approval (`governance.approval_required`):

* **Held everywhere** - a version still `pending_approval`, and one `archived` without ever having been the
  champion (`promoted_at` unset: a rejected challenger). A demoted former champion keeps `promoted_at` and stays
  usable. The contact-list routes hold such a list (`api.routes.runs.unapproved_list_model`, Plan M final
  integration, DEC-1611 (p)), and a cross-goal arbitrated list when any run it was built from used such a model
  (`api.routes.runs.refuse_unapproved_sources`, DEC-1620), and so does every step below.
* **Held only where nobody chooses the model** (`unattended=True`) - also a plain `candidate` (a version the
  champion rule kept back that no person approved or promoted). A person who names a candidate on `POST /runs`
  is asking for that model, and `engine.stages.score.resolve_model_version` honours it whatever its status
  (DEC-050), so a list they scored with it stays downloadable. The steps that act with nobody choosing at that
  moment refuse it:

  * a **scheduled** scoring firing that names `parameters.model_version_id` (`engine.scheduling.firing`);
  * the **campaign planner**, which skips a scoring run whose model may not be used when it looks for a list to
    plan, and EXECUTE, which refuses to carry out an approved plan whose run's model may not be used
    (`engine.agentic.planner`, `api.routes.agentic`).

The champion, the default of every scoring run, is always usable, so nothing that scores with it changes. Every
refusal answers `MODEL_NOT_APPROVED`, the code the contact-list routes already raise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from engine.contracts import ModelVersion
    from engine.registry import ModelRegistry

__all__ = ["MODEL_NOT_APPROVED", "model_not_approved", "unapproved_model_message"]

MODEL_NOT_APPROVED: Final[str] = "MODEL_NOT_APPROVED"


def model_not_approved(version: ModelVersion, *, unattended: bool) -> bool:
    """True when `version` may not be used while approval is required (see the module docstring).

    `unattended=False` is the contact-list rule (pending, or rejected); `unattended=True` also holds a candidate.
    """
    from engine.contracts import ModelStatus

    if version.status is ModelStatus.CHAMPION:
        return False
    if version.status is ModelStatus.PENDING_APPROVAL:
        return True
    if version.status is ModelStatus.ARCHIVED:
        return version.promoted_at is None
    return unattended


def unapproved_model_message(registry: ModelRegistry, model_version_id: str | None) -> str | None:
    """For a step nobody chooses the model of: None when `model_version_id` may be used, else why it may not.

    A run with no model, and a version the registry does not hold, may not (fails closed).
    """
    from engine.contracts import ModelStatus
    from engine.registry import RegistryError

    if model_version_id is None:
        return "The scoring run records no model, so whether its model was approved cannot be checked."
    try:
        version = registry.get(model_version_id)
    except RegistryError:
        return f"The model {model_version_id} is not in the model registry, so it cannot have been approved."
    if not model_not_approved(version, unattended=True):
        return None
    if version.status is ModelStatus.PENDING_APPROVAL:
        where = "is still waiting for an Approver"
    elif version.status is ModelStatus.ARCHIVED:
        where = "was turned down by an Approver"
    else:
        where = "was kept back by the champion rule and never approved"
    return (
        f"The model {model_version_id} {where}. While approval is required, a step that runs without a person "
        "uses only an approved model: approve it on the Approvals page, or use the model in use."
    )
