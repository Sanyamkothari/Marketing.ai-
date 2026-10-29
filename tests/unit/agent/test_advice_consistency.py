"""What Guided setup suggests must survive Approve: every repro here once reached 'ready' and then
failed `apply` (422 or 409), or lost or kept a decision the person had changed."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd
import pytest

from engine.agent.advisor import advise, recipe_steps, run_overrides
from engine.agent.checks import check_plan
from engine.agent.contracts import (
    AgentConfidence,
    AgentSession,
    Proposal,
    ProposalKind,
    ProposalState,
)
from engine.agent.proposals import roles_of, rules_session
from engine.agent.recipe import run_recipe
from engine.agent.tools import AgentContext
from engine.config import resolve_config
from tests.fixtures.make_data import GenerationSpec, generate
from tests.unit.agent.helpers import context_for


def _apply_errors(ctx: AgentContext, session: AgentSession) -> list[str]:
    """What `POST /agent-session/apply` would refuse with: a ConfigError code (422), else the
    unacknowledged error checks (409), exactly as the route runs them."""
    key, target = roles_of(session)
    accepted = [p for p in session.proposals if p.state is ProposalState.ACCEPTED]
    steps = recipe_steps(accepted)
    frame = ctx.frame
    if steps:
        frame = run_recipe(
            ctx.frame,
            steps,
            upload_id=ctx.upload_id,
            primary_key=key,
            target=target,
            levels=ctx.config.agent.levels,
            max_failure_pct=ctx.config.agent.max_conversion_failure_pct,
        ).frame
    try:
        resolved = resolve_config(ctx.use_case_id, run_overrides(session.proposals), root=ctx.config_root)
    except Exception as exc:  # ConfigError -> 422
        return [str(getattr(exc, "code", exc))]
    report = check_plan(
        frame,
        resolved.config,
        mode=ctx.mode,
        primary_key=key,
        target=target,
        upload_id=ctx.upload_id,
        row_count=len(frame),
        schema=ctx.schema,
    )
    return [c.code for c in report.checks if c.severity.value == "error" and not c.acknowledged]


def _settings(proposals: Sequence[Proposal]) -> dict[str, Any]:
    return {str(p.path): p.value for p in proposals if p.kind is ProposalKind.SETTING}


# ---------------------------------------------------------------------------
# A split by date is suggested only on a column the use case can split by
# ---------------------------------------------------------------------------
def _with_other_date(use_case_id: str) -> pd.DataFrame:
    frame = generate(GenerationSpec(use_case_id=use_case_id, rows=3_000))
    if "snapshot_date" in frame.columns:
        return frame.rename(columns={"snapshot_date": "reading_date"})
    days = np.random.default_rng(1).integers(0, 400, len(frame))
    dates = pd.Timestamp("2025-01-01") + pd.to_timedelta(days, unit="D")
    return frame.assign(signup_date=dates.strftime("%Y-%m-%d"))


# ---------------------------------------------------------------------------
# An error no suggestion fixes is never left for Approve to find
# ---------------------------------------------------------------------------
def _fault(variant: str) -> pd.DataFrame:
    frame = generate(GenerationSpec(use_case_id="fault-prediction", rows=3_000))
    if variant == "renamed":
        return frame.rename(columns={"snapshot_date": "reading_date"})
    broken = frame.copy()
    rows = np.random.default_rng(0).choice(len(frame), len(frame) // 3, replace=False)
    broken["snapshot_date"] = broken["snapshot_date"].astype(str)
    broken.loc[rows, "snapshot_date"] = "not a date"
    return broken


def test_an_error_no_suggestion_can_fix_stops_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """The safety net: an error check the advisor neither stops on, asks about nor fixes stops it."""
    from engine.agent import advisor

    monkeypatch.setattr(advisor, "recommend_settings", lambda *args, **kwargs: ())
    ctx = context_for(_fault("renamed"), "fault-prediction")
    advice = advise(ctx)
    assert advice.status.value == "stopped"
    assert advice.stop_reason is not None and "snapshot_date" in advice.stop_reason


# ---------------------------------------------------------------------------
# A column a leak question may hide is never the column a setting depends on
# ---------------------------------------------------------------------------
def _drifting() -> pd.DataFrame:
    frame = generate(GenerationSpec(use_case_id="targeted-advertisement", rows=3_000))
    dates = pd.to_datetime(frame["snapshot_date"])
    late = dates >= sorted(dates.unique())[-max(1, dates.nunique() // 6)]
    return frame.assign(converted_30d=late.astype(int))


# ---------------------------------------------------------------------------
# Re-advice keeps what the person accepted, and drops only what can no longer apply
# ---------------------------------------------------------------------------
def _two_outcomes() -> AgentContext:
    frame = generate(GenerationSpec(use_case_id="targeted-advertisement", rows=3_000))
    frame = frame.drop(columns=["converted_30d"]).assign(
        bought=frame["converted_30d"], purchased=frame["converted_30d"]
    )
    return context_for(frame)


def _chat_setting(proposal_id: str, path: str, value: Any, evidence: str) -> Proposal:
    return Proposal(
        proposal_id=proposal_id,
        kind=ProposalKind.SETTING,
        title=f"Set {path} to {value}",
        reason="You asked for it.",
        path=path,
        value=value,
        suggested_value=value,
        evidence_ids=(evidence,),
        confidence=AgentConfidence.CHECK,
    )


def _helper_and_chat_minutes() -> tuple[AgentContext, AgentSession]:
    """The helper suggests a time limit worked out for the file; the person asks in chat for 30."""
    ctx = context_for(generate(GenerationSpec(use_case_id="targeted-advertisement", rows=3_000)))
    session = rules_session(ctx, session_id="s1")
    helper = next(p for p in session.proposals if p.path == "model_search.time_limit_minutes")
    assert helper.value != 30
    evidence = session.tool_results[0].evidence_id
    chat = _chat_setting("c1-p1", "model_search.time_limit_minutes", 30, evidence)
    return ctx, session.model_copy(update={"proposals": (*session.proposals, chat)})


# ---------------------------------------------------------------------------
# A split by date is decided with its date column
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The random-split reason says what is wrong with the date column
# ---------------------------------------------------------------------------


def test_an_unreadable_date_column_is_named_as_unreadable_and_cleared() -> None:
    ctx = context_for(_fault("unparseable"), "fault-prediction")
    session = rules_session(ctx, session_id="s1")
    split = next(p for p in session.proposals if p.path == "split.type")
    assert "cannot be read as dates" in split.reason
    cleared = next(p for p in session.proposals if p.path == "split.time_column")
    assert cleared.value is None
    check = next(r for r in session.tool_results if r.tool == "check_data")
    assert check.evidence_id in cleared.evidence_ids  # the check that could not read it is cited
