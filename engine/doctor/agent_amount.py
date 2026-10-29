"""The chat's AI-first meaning actions: an amount outcome and a campaign test's groups, proposed with a reason.

* `propose_amount_outcome`: the AI read `describe_order_file` (#90) and defines the spend outcome - which amount (or
  quantity times price), the window, whether a negative row is a refund - with its reason. The registry's tool builds
  the point-in-time query; the answer is kept with the model's other queries and becomes a pending, never-ticked item
  whose sentence says every choice, for the person to approve.
* `propose_test_column`: the AI read `find_test_columns` (#91) and names the column that holds a campaign test and its
  control group by level id. Code checks both against the file and makes a pending, never-ticked `treatment_column`
  item; once the person accepts it, the goal's balance check runs on what they confirmed
  (`engine.doctor.tools.confirmed_tests`). The group's own name is cell text: the model and the item carry its id.

What goes back to the model is counts and names only, through the egress gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.agent.grounding import grounded_numbers
from engine.agent.loop import whole_reason
from engine.agent.tools import AgentToolError
from engine.agent.untrusted import display_name
from engine.doctor.actions import ProposeAmountOutcomeArgs
from engine.doctor.case import CaseItem, payload_digest

if TYPE_CHECKING:
    from engine.doctor.agent_turn import DoctorTurn

__all__ = ["AMOUNT_ARGS", "run_amount_outcome", "run_test_column"]

AMOUNT_ARGS: Final[frozenset[str]] = frozenset(ProposeAmountOutcomeArgs.model_fields) - {"replaces"}


def run_amount_outcome(chat: DoctorTurn, index: int, args: dict[str, Any]) -> None:
    from engine.doctor.agent_turn import _Sql
    from engine.doctor.tools_features import parse_query_output

    chat._file_only(index)
    ctx = chat.ctxs[index]
    given: dict[str, Any] = {k: v for k, v in args.items() if k in AMOUNT_ARGS and v is not None}
    given["upload_id"] = ctx.upload_id
    reason = whole_reason(given)
    if not reason:
        raise AgentToolError(
            "AGENT_REASON_MISSING", "Say why this is the amount and the window, in a sentence."
        )
    given["reason"] = reason
    from engine.config import ProblemType

    if ctx.config.problem_type is ProblemType.REGRESSION and ctx.config.target.column:
        given.setdefault("outcome_name", ctx.config.target.column)  # the amount goal's own outcome column
    name = "outcome_spend"
    if name in chat.turn.closed:
        raise AgentToolError(
            "AGENT_RETRIES_USED", "The spend outcome has failed too often; it is left for the person."
        )
    result = chat.registry.call(
        "propose_amount_outcome", given, chat._context(index, primary_key=None, replacing=chat.replacing)
    )
    proposal = parse_query_output(result.output)
    chat.turn.queries[name] = _Sql(result, tuple(proposal.findings))
    shown = {
        "evidence_id": result.evidence_id,
        "file": chat.labels[index],
        "tool": "propose_amount_outcome",
        "result": chat.gates[index].prepare(
            {
                "rows_out": proposal.rows_out,
                "findings": [f.model_dump(mode="json") for f in proposal.findings],
            },
            extra_text_keys=frozenset({"message", "code", "level", "key", "column", "kind"}),
        ),
    }
    chat.checker.grounded = chat.checker.grounded | grounded_numbers([shown["result"]])
    chat.turn.feedback.append(shown)


def run_test_column(chat: DoctorTurn, index: int, args: dict[str, Any]) -> None:
    from engine.doctor.tools import TEST_COLUMN_KIND
    from engine.doctor.tools_facts import level_labels

    chat._file_only(index)
    ctx = chat.ctxs[index]
    column = ctx.resolve(str(args.get("column") or ""))
    if column not in ctx.frame.columns:
        raise AgentToolError("AGENT_COLUMN_UNKNOWN", "Name a column of the file, from `find_test_columns`.")
    labels = level_labels(ctx.frame[column])
    if not 2 <= len(labels) <= 10:
        raise AgentToolError(
            "AGENT_TEST_COLUMN_INVALID", "A campaign test's column has 2 to 10 groups; this one has not."
        )
    control = str(args.get("control") or "").strip().upper()
    number = int(control[1:]) if control[:1] == "L" and control[1:].isdigit() else 0
    if not 1 <= number <= len(labels):
        raise AgentToolError(
            "AGENT_TEST_COLUMN_INVALID", f"Name the control group by its id, L1 to L{len(labels)}."
        )
    reason = whole_reason(args)
    if not reason:
        raise AgentToolError(
            "AGENT_REASON_MISSING", "Say why this column is the test and this group the control."
        )
    from engine.doctor.uplift_scope import _text

    rows = int(ctx.frame[column].dropna().map(_text).eq(labels[number - 1]).sum())
    payload: dict[str, Any] = {"upload_id": ctx.upload_id, "column": column, "control_level": f"L{number}"}
    item = CaseItem(
        item_id=f"treatment_column.{ctx.upload_id}.{payload_digest(payload)[:12]}"[:128],
        kind=TEST_COLUMN_KIND,
        title=(
            f"'{display_name(column)}' says who got the campaign; its group {number} of {len(labels)} "
            f"({rows:,} customers) got none: the control group"
        ),
        source=ProposalSource.AI,
        confidence=AgentConfidence.CHECK,
        state=ProposalState.PENDING,
        upload_id=ctx.upload_id,
        payload=payload,
        payload_digest=payload_digest(payload),
        proposed_at=chat.clock(),
        assumption=reason,
    )
    chat.chosen.combined.append(item)
    chat.checker.grounded = chat.checker.grounded | grounded_numbers([item.title])
    chat.turn.feedback.append(
        {"tool": "propose_test_column", "proposed": "waits for the person", "rows": rows}
    )
