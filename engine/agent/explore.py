"""Ask your data (Plan I, DEC-1250 … DEC-1259): the Guided-setup helper's chat on an upload, outside setup.

It is the same helper - the same `chat_turn`, read tools, default-deny egress gate, `numbers_grounded`
check and guardrails - in a read-only *explore* session (`AgentSession.explore`): the session holds no
suggestion and no question, the prompt lists no setting, and a `propose_setting` action is refused
(`AGENT_EXPLORE_READ_ONLY`). It is stored apart from the Guided-setup session of the same upload
(`uploads/<upload_id>/agent/explore_session.json`), so asking questions never touches a setup in progress,
and it is deleted with the upload like the setup session is (DEC-1007).

A reply whose turn called `rate_by` comes back with that tool result's groups as a `RateChart`: the
numbers the screen draws are the tool's, read from the stored `ToolResult`, never from what the model
wrote (DEC-1254).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import Field

from engine.agent.contracts import AgentSession, ChatRole, SessionStatus, ToolResult
from engine.agent.tools import AgentContext
from engine.config import StrictBase
from engine.utils.time import utc_now

__all__ = [
    "CHART_TOOL",
    "EXPLORE_NOTE",
    "EXPLORE_SESSION_FILENAME",
    "MAX_CHARTS_PER_REPLY",
    "ChartBar",
    "RateChart",
    "charts_of",
    "start_explore",
]

EXPLORE_SESSION_FILENAME: Final[str] = "explore_session.json"
"""Under `uploads/<upload_id>/agent/`, beside (never instead of) the Guided-setup session."""
EXPLORE_NOTE: Final[str] = (
    "This is an Ask-your-data chat, outside Guided setup. There are no suggestions or questions here and no "
    "setting can be suggested: answer what the person asks by looking at the file with the tools, then reply."
)
"""What the state block of an explore turn says instead of listing suggestions."""
CHART_TOOL: Final[str] = "rate_by"
MAX_CHARTS_PER_REPLY: Final[int] = 3
ChartFunction = Literal["rows", "positive_rate", "mean"]
CHART_FUNCTIONS: Final[dict[str, ChartFunction]] = {
    "rows": "rows",
    "positive_rate": "positive_rate",
    "mean": "mean",
}
"""What a `rate_by` result measures, by its `function`."""


def start_explore(
    ctx: AgentContext, *, session_id: str, clock: Callable[[], datetime] = utc_now
) -> AgentSession:
    """An empty explore session: no advisor run, nothing to decide, only the chat."""
    now = clock()
    return AgentSession(
        session_id=session_id,
        upload_id=ctx.upload_id,
        use_case_id=ctx.use_case_id,
        mode=ctx.mode.value,
        agent_name=ctx.config.agent.name_for(ctx.config.name),
        status=SessionStatus.READY,
        explore=True,
        created_at=now,
        updated_at=now,
    )


class ChartBar(StrictBase):
    """One group of a `rate_by` result, as a bar."""

    label: str = Field(
        description="The group: a value of the column (masked), a range, `(other)` or `(empty)`."
    )
    rows: int | None = Field(description="Rows in the group; null when it is suppressed.")
    share: float | None = Field(description="The group's share of all rows, 0-1; null when suppressed.")
    value: float | None = Field(
        description="The measure (the positive rate, or the mean); null for `rows` and when suppressed."
    )
    suppressed: bool = Field(description="True for a group too small to show any figure for.")


class RateChart(StrictBase):
    """A bar chart of one `rate_by` result, drawn under the reply whose turn called it (DEC-1254)."""

    message_index: Annotated[int, Field(ge=0, description="The reply's position in the transcript.")]
    evidence_id: str = Field(description="The tool result the bars are read from.")
    column: str = Field(description="The column the rows were grouped by.")
    function: ChartFunction = Field(
        description="What each bar measures: its rows, its rate of the outcome's positive value, or a mean."
    )
    outcome_column: str | None = Field(default=None, description="The column measured; null for `rows`.")
    positive_label: str | None = Field(
        default=None, description="For `positive_rate`: the value counted as yes."
    )
    min_group_rows: int = Field(description="A group of fewer rows shows no figure.")
    bars: tuple[ChartBar, ...] = Field(description="The groups, in the tool's order.")


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _chart(index: int, result: ToolResult) -> RateChart | None:
    body = result.result
    function = CHART_FUNCTIONS.get(str(body.get("function")))
    groups = body.get("groups")
    if function is None or not isinstance(groups, list) or not groups:
        return None  # a refusal (a personal-data column) has no groups
    raw_outcome = body.get("outcome")
    outcome: dict[str, Any] | None = raw_outcome if isinstance(raw_outcome, dict) else None
    bars = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        rows = group.get("rows")
        bars.append(
            ChartBar(
                label=str(group.get("group", "")),
                rows=int(rows) if isinstance(rows, int) and not isinstance(rows, bool) else None,
                share=_number(group.get("share")),
                value=None if function == "rows" else _number(group.get(function)),
                suppressed=bool(group.get("suppressed")),
            )
        )
    minimum = body.get("min_group_rows")
    return RateChart(
        message_index=index,
        evidence_id=result.evidence_id,
        column=str(body.get("column", "")),
        function=function,
        outcome_column=str(outcome["column"]) if outcome and isinstance(outcome.get("column"), str) else None,
        positive_label=(
            str(outcome["positive_label"])
            if outcome and isinstance(outcome.get("positive_label"), str)
            else None
        ),
        min_group_rows=int(minimum) if isinstance(minimum, int) else 0,
        bars=tuple(bars),
    )


def charts_of(session: AgentSession) -> tuple[RateChart, ...]:
    """Every chart of the session: each reply's own turn's `rate_by` results, at most three per reply.

    A turn names its evidence `c<n>-e<k>`, `n` counting the turns from one (`loop.chat_turn`), so the reply
    at transcript position `i` owns the results whose id starts with `c<i // 2 + 1>-`.
    """
    charts: list[RateChart] = []
    for index, message in enumerate(session.transcript):
        if message.role is not ChatRole.AGENT:
            continue
        prefix = f"c{index // 2 + 1}-"
        own = [r for r in session.tool_results if r.tool == CHART_TOOL and r.evidence_id.startswith(prefix)]
        drawn = [chart for chart in (_chart(index, r) for r in own) if chart is not None]
        charts.extend(drawn[-MAX_CHARTS_PER_REPLY:])
    return tuple(charts)
