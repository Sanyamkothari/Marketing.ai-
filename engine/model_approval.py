"""Whether a model version was approved, for the steps that run without a person (Plan M M184, DEC-1614 (b)).

A version is **approved** when it is the use case's champion now, or was made champion once: approved by an
Approver (`approved_at`) or promoted (`promoted_at`), by a person or - only with `governance.approval_required`
off when it was trained - by the champion rule. A `candidate`, a version still `pending_approval`, and a version
archived without ever being champion (a rejected challenger) are not.

Interactive scoring is unchanged: a person who names a version on `POST /runs` is asking for that model, and
`engine.stages.score.resolve_model_version` honours it whatever its status (DEC-050). The two steps that act with
nobody choosing at that moment refuse a version that is not approved while the use case's
`governance.approval_required` is on, with `MODEL_VERSION_NOT_APPROVED`:

* a **scheduled** scoring firing that names `parameters.model_version_id` (`engine.scheduling.firing`);
* the **campaign planner**, which skips a scoring run whose model is not approved when it looks for a list to
  plan, and refuses to carry out an approved plan whose run's model is not approved (`engine.agentic.planner`).

The champion, the default of every scoring run, is approved by definition, so nothing that scores with it changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from engine.contracts import ModelVersion
    from engine.registry import ModelRegistry

__all__ = ["MODEL_VERSION_NOT_APPROVED", "model_version_approved", "unapproved_model_message"]

MODEL_VERSION_NOT_APPROVED: Final[str] = "MODEL_VERSION_NOT_APPROVED"


def model_version_approved(version: ModelVersion) -> bool:
    """True for the champion, and for a version that was approved or made champion once (see the docstring)."""
    from engine.contracts import ModelStatus

    if version.status is ModelStatus.CHAMPION:
        return True
    return version.approved_at is not None or version.promoted_at is not None


def unapproved_model_message(registry: ModelRegistry, model_version_id: str | None) -> str | None:
    """None when `model_version_id` is an approved version; else the sentence saying why it is refused.

    A version the registry does not hold is not approved (fails closed).
    """
    from engine.registry import RegistryError

    if model_version_id is None:
        return "The scoring run records no model, so whether its model was approved cannot be checked."
    try:
        version = registry.get(model_version_id)
    except RegistryError:
        return f"The model {model_version_id} is not in the model registry, so it cannot have been approved."
    if model_version_approved(version):
        return None
    return (
        f"The model {model_version_id} is {version.status.value.replace('_', ' ')} and was never approved. While "
        "approval is required, a step that runs without a person uses only an approved model: approve it on the "
        "Approvals page, or use the model in use."
    )
