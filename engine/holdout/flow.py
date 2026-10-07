"""The holdout service's seam into the score flow (Plan J M92).

`engine.pipeline` calls :func:`install_holdout_service` once, at import, with its `_ScoreFlow`. The
seam is the flow's stage table (`_bodies`), which both the Phase 1 flow and the uplift flow use, so
one rebinding covers propensity and uplift scoring alike and wraps whichever actions body the class
has at call time (Phase 4b's consent-gated one, the uplift one).

When the run's use case does not engage the service - `actions.holdout.scope: run` and
`actions.explore_fraction: 0`, the defaults - the stage table is returned untouched: no setting is
read, no database is opened, no file is written, and the run is today's byte for byte
(`SCORE_ARTEFACTS`, which `tests/unit/production/test_consent_scoring.py` pins for a default run,
does not change). **`holdout_assignment.parquet` is therefore written only by an engaged run**
(DEC-1302 (e)). A reader that needs the same table for a default run - the hand-off builder (M94) -
derives it from `scores.*` with :func:`engine.holdout.assign.assignment_frame` (`active=None`,
`explore_fraction=0`): `holdout_member` is the run's `control_group`, nobody is explored.

When it does:

* **before ingest** the holdout is checked without writing anything (`resolve_holdout(record=False)`),
  so a missing or changed salt, or a lowered fraction, fails the run at its first stage with its code
  rather than after the model has scored every row;
* **the actions stage** resolves it again, runs inside `holdout_context` so `_control_mask` /
  `_entity_control_mask` draw the persistent holdout, writes `holdout_assignment.parquet` (every row,
  suppressed rows included) and `holdout_assignment.json` (the run's `HoldoutSpec` and counts), and
  only then records a first use or a raised fraction in the ledger - so a run that fails never moves
  an epoch's fraction.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final

from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from engine.config import UseCaseConfig
    from engine.holdout.assign import ActiveHoldout, Assignment
    from engine.holdout.salt import ResolvedHoldout

__all__ = ["install_holdout_service"]

_LOGGER = get_logger(__name__)

_INSTALLED: Final[str] = "_holdout_service_installed"


def install_holdout_service(score_flow: type[Any]) -> None:
    """Wrap `score_flow._bodies` so the ingest and actions bodies go through the holdout service.

    Sets `score_flow._holdout_service_installed`, which a guard test checks after `import
    engine.pipeline`: a dropped install would otherwise silently score persistent holdouts with
    today's per-run draw.
    """
    if getattr(score_flow, _INSTALLED, False):
        return
    original: Callable[[Any], tuple[tuple[Any, Callable[[], Any]], ...]] = score_flow._bodies

    def _bodies(self: Any) -> tuple[tuple[Any, Callable[[], Any]], ...]:
        bodies = original(self)
        config = getattr(getattr(self, "_ctx", None), "config", None)
        if config is None or not _engaged(config):
            return bodies
        return tuple((key, _wrapped(self, key, body)) for key, body in bodies)

    score_flow._bodies = _bodies
    setattr(score_flow, _INSTALLED, True)


def _engaged(config: UseCaseConfig) -> bool:
    from engine.holdout.spec import service_on

    actions = getattr(config, "actions", None)
    if actions is None:
        return False
    return service_on(actions, float(getattr(actions, "explore_fraction", 0.0)))


def _wrapped(flow: Any, key: Any, body: Callable[[], Any]) -> Callable[[], Any]:
    """`body`, or a wrapper that keeps its name (the stage table reads the same either way)."""
    from functools import wraps

    from engine.contracts import StageKey

    if key is StageKey.INGEST:

        @wraps(body)
        def checked() -> Any:
            _resolve(flow, record=False)
            return body()

        return checked
    if key is StageKey.ACTIONS:

        @wraps(body)
        def acted() -> Any:
            return _actions(flow, body)

        return acted
    return body


def _resolve(flow: Any, *, record: bool) -> ResolvedHoldout:
    from engine.holdout.salt import holdout_engine_for, resolve_holdout
    from engine.holdout.spec import is_persistent
    from engine.settings import load_settings
    from engine.utils.time import utc_now

    config = flow._ctx.config
    if not is_persistent(config.actions):
        return resolve_holdout(config, None, None, at=utc_now(), record=record)
    settings = load_settings()
    engine = holdout_engine_for(flow._storage, settings)
    return resolve_holdout(config, settings, engine, at=utc_now(), record=record)


def _scored_rows(flow: Any) -> Any:
    banded = flow._scored
    if banded is None:  # the body always sets it; a stage that did not is the body's own failure
        raise RuntimeError("the actions stage produced no scored rows")
    return banded


def _write_assignment(
    flow: Any, banded: Any, *, active: ActiveHoldout | None, explore_fraction: float
) -> Assignment:
    """`holdout_assignment.parquet` for every row of the actions stage's output."""
    import io

    from engine.holdout.assign import HOLDOUT_ASSIGNMENT_FILENAME, assignment_frame
    from engine.storage import run_key

    ctx = flow._ctx
    assignment = assignment_frame(
        banded,
        ctx.config,
        primary_key=ctx.key,
        row_key=ctx.row_key,
        entity_key=ctx.entity_key,
        run_id=ctx.run_id,
        active=active,
        explore_fraction=explore_fraction,
        key_source=flow._frame,
    )
    buffer = io.BytesIO()
    assignment.table.to_parquet(buffer, engine="pyarrow", index=False)
    key = run_key(ctx.run_id, HOLDOUT_ASSIGNMENT_FILENAME)
    flow._storage.write_bytes(key, buffer.getvalue())
    flow._artefacts[HOLDOUT_ASSIGNMENT_FILENAME] = key
    return assignment


def _actions(flow: Any, body: Callable[[], Any]) -> Any:
    """The actions body under the resolved holdout, then the two holdout files."""
    from engine.holdout.assign import holdout_context
    from engine.holdout.spec import HOLDOUT_REPORT_FILENAME, HoldoutAssignmentReport
    from engine.utils.text import humanise_count
    from engine.utils.time import utc_now

    resolved = _resolve(flow, record=False)
    with holdout_context(resolved.active):
        outcome = body()
    ctx = flow._ctx
    banded = _scored_rows(flow)
    assignment = _write_assignment(
        flow, banded, active=resolved.active, explore_fraction=resolved.spec.explore_fraction
    )
    control_rows = int(banded["control_group"].astype(bool).sum())
    flow._write(
        HOLDOUT_REPORT_FILENAME,
        HoldoutAssignmentReport(
            run_id=ctx.run_id,
            use_case_id=ctx.config.id,
            spec=resolved.spec,
            rows=len(assignment.table.index),
            holdout_members=assignment.members,
            control_rows=control_rows,
            explore_candidates=assignment.candidates,
            explore_rows=assignment.explored,
            created_at=utc_now(),
        ),
    )
    _resolve(flow, record=True)  # the same answer, now recorded: the stage has succeeded
    _LOGGER.info(
        "holdout scope=%s epoch=%s members=%d explore_rows=%d",
        resolved.spec.scope,
        resolved.spec.epoch,
        assignment.members,
        assignment.explored,
    )
    words = [outcome.detail]
    if resolved.spec.scope != "run":
        words.append(f"{resolved.spec.scope.replace('_', ' ')} holdout, epoch {resolved.spec.epoch}")
    if resolved.spec.explore_fraction > 0.0:
        words.append(f"{humanise_count(assignment.explored)} explored")
    return replace(outcome, detail=" · ".join(words))
