"""The scenario harness: pass^k and the policy mutation test (Plan K M132, design §5).

**A scenario** is ``(fixture world, request, expected outcome)``. The world is a small deterministic set of
artefacts built by the product's own code (a trained and scored run, campaigns, a consent ledger, a
configuration root: `tests/agentic/scenarios/world.py`); the request is either a tool call through the
registry (`draft_test_plan`, a read tool) or a proposal as M139's planner will draft it; the expected outcome
is a status (`pending`, `refused`, `approval_refused`, `approval_allowed`, `read`, `error`), the refusal codes,
and - when the payload does not depend on ids the world mints - the expected `payload_digest`.

**pass^k** (tau-bench's pass^k, design §5.2). Each scenario is run `k` times (k = 10). It passes only when all
`k` outcomes are identical (status, codes and payload digest) and match the expectation. M132 has no language
model step yet, so pass^k checks the determinism of the tool and policy path itself; the harness takes an
`LLMClient` all the same (the `FakeLLMClient` in CI), records its model id in the report and, today, never
calls it - so the cost recorded is zero, and says so.

**The mutation test.** :func:`mutation_test` removes each policy rule in turn and runs every scenario; a rule
is *caught* when at least one scenario then fails. Every rule must be caught (policy-violation rate 0,
design §5.2): a rule no scenario notices missing is a rule nothing tests.

The report (:class:`EvalReport`) is written to `reports/agentic_eval.json` by :func:`write_report`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import Field

from engine.agentic.canonical import canonical_json, digest
from engine.agentic.errors import AgenticError
from engine.agentic.policy import RULES, Rule
from engine.agentic.registry import ToolContext, ToolKind, ToolRegistry
from engine.config import StrictBase

if TYPE_CHECKING:
    from engine.llm import LLMClient

__all__ = [
    "DEFAULT_K",
    "REPORT_PATH",
    "EvalReport",
    "Outcome",
    "ProposalRequest",
    "Scenario",
    "ScenarioResult",
    "ToolCallRequest",
    "mutation_test",
    "pass_k",
    "run_once",
    "write_report",
]

DEFAULT_K: Final[int] = 10
REPORT_PATH: Final[Path] = Path("reports") / "agentic_eval.json"

OutcomeStatus = Literal["pending", "refused", "approval_refused", "approval_allowed", "read", "error"]


class ToolCallRequest(StrictBase):
    """Call one registry tool with this input."""

    tool: str
    input: dict[str, Any]


class ProposalRequest(StrictBase):
    """Draft one proposal, as a planner would (M139), with this payload."""

    kind: str
    payload: dict[str, Any]
    evidence_ids: tuple[str, ...] = ()


class Scenario(StrictBase):
    name: str = Field(pattern=r"^[a-z0-9_]+$")
    description: str
    request: ToolCallRequest | ProposalRequest
    deployment: str = Field(default="local", description="Which of the world's deployments answers it.")
    principal: str = Field(default="analyst", description="Who asks: one of the world's principals.")
    approve_as: str | None = Field(default=None, description="Then try to approve it as this principal.")
    expect_status: OutcomeStatus
    expect_codes: tuple[str, ...] = ()
    expect_digest: str | None = Field(
        default=None, description="The expected payload digest; None when it depends on ids the world mints."
    )
    guards: tuple[str, ...] = Field(default=(), description="The policy rules this scenario exists to catch.")


class Outcome(StrictBase):
    status: OutcomeStatus
    codes: tuple[str, ...] = ()
    payload_digest: str | None = None

    @property
    def key(self) -> str:
        """What must be identical across the k runs."""
        return digest(
            {"status": self.status, "codes": list(self.codes), "payload_digest": self.payload_digest}
        )


class ScenarioResult(StrictBase):
    name: str
    k: int
    passed: bool
    outcome: Outcome = Field(description="The first run's outcome.")
    distinct_outcomes: int = Field(description="1 when all k runs agreed.")
    failure: str | None = None


class EvalReport(StrictBase):
    """`reports/agentic_eval.json`."""

    k: int
    scenarios: tuple[ScenarioResult, ...]
    passed: int
    failed: int
    pass_k: float = Field(description="Share of scenarios that passed all k runs.")
    llm_model_id: str | None = Field(description="The model the harness was given; M132 never calls it.")
    llm_calls: int = Field(
        description="Model calls made: 0 until a scenario has a language-model step (M139)."
    )
    cost_usd: float = Field(description="What those calls cost, from the Meter; 0.0 with none.")
    claim_failures: dict[str, int] = Field(
        default_factory=dict, description="By claim code (design §4; M139)."
    )
    mutations: dict[str, tuple[str, ...]] = Field(
        default_factory=dict, description="Per policy rule removed, the scenarios that caught it."
    )


class _ExecutionReachedError(Exception):
    """The harness never executes an approved proposal: reaching execution is the outcome."""


def _never_execute(_proposal: Any) -> str | None:
    raise _ExecutionReachedError


ContextFactory = Callable[[Scenario], ToolContext]
Approvers = Callable[[str], Any]


def run_once(
    scenario: Scenario,
    make_ctx: ContextFactory,
    registry: ToolRegistry,
    *,
    principals: Approvers,
    rules: Mapping[str, Rule] | None = None,
) -> Outcome:
    """One run of `scenario` through the registry or `submit_proposal`, then an approval when asked."""
    from engine.agentic.proposals import submit_proposal
    from engine.approvals import decide_proposal

    ctx = dataclasses.replace(make_ctx(scenario), policy_rules=rules)
    request = scenario.request
    proposal_id: str | None = None
    try:
        if isinstance(request, ToolCallRequest):
            result = registry.call(request.tool, request.input, ctx)
            if result.kind is ToolKind.READ:
                return Outcome(status="read", payload_digest=digest(result.output))
            output = result.output
            proposal_id = str(output["proposal_id"])
            outcome = Outcome(
                status=output["status"], codes=tuple(output["codes"]), payload_digest=output["payload_digest"]
            )
        else:
            proposal = submit_proposal(
                request.kind, request.payload, tool_ctx=ctx, evidence_ids=request.evidence_ids
            )
            proposal_id = proposal.proposal_id
            outcome = Outcome.model_validate(
                {
                    "status": proposal.status,
                    "codes": proposal.codes,
                    "payload_digest": proposal.payload_digest,
                }
            )
    except AgenticError as exc:
        return Outcome(status="error", codes=(exc.code,))
    if scenario.approve_as is None or outcome.status != "pending" or proposal_id is None:
        return outcome
    try:
        decide_proposal(
            proposal_id,
            "approved",
            principal=principals(scenario.approve_as),
            reason=None,
            tool_ctx=ctx,
            execute=_never_execute,
        )
    except _ExecutionReachedError:
        return outcome.model_copy(update={"status": "approval_allowed"})
    except AgenticError as exc:
        status: OutcomeStatus = "approval_refused"
        return outcome.model_copy(update={"status": status, "codes": (exc.code,)})
    return outcome  # pragma: no cover - `_never_execute` always raises


def _judge(scenario: Scenario, outcomes: Sequence[Outcome]) -> tuple[bool, str | None]:
    keys = {outcome.key for outcome in outcomes}
    first = outcomes[0]
    if len(keys) != 1:
        return False, f"{len(keys)} different outcomes in {len(outcomes)} runs"
    if first.status != scenario.expect_status:
        return False, f"status {first.status}, expected {scenario.expect_status} (codes {list(first.codes)})"
    if tuple(sorted(first.codes)) != tuple(sorted(scenario.expect_codes)):
        return False, f"codes {list(first.codes)}, expected {list(scenario.expect_codes)}"
    if scenario.expect_digest is not None and first.payload_digest != scenario.expect_digest:
        return False, f"payload digest {first.payload_digest}, expected {scenario.expect_digest}"
    return True, None


def pass_k(
    scenarios: Sequence[Scenario],
    make_ctx: ContextFactory,
    *,
    principals: Approvers,
    registry: ToolRegistry | None = None,
    k: int = DEFAULT_K,
    llm: LLMClient | None = None,
    rules: Mapping[str, Rule] | None = None,
) -> EvalReport:
    """Run every scenario `k` times; each passes only when all k outcomes agree and match the expectation."""
    from engine.agentic.tools import default_registry

    if k < 1:
        raise ValueError("k must be at least 1.")
    tools = registry if registry is not None else default_registry()
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        outcomes = [run_once(scenario, make_ctx, tools, principals=principals, rules=rules) for _ in range(k)]
        passed, failure = _judge(scenario, outcomes)
        results.append(
            ScenarioResult(
                name=scenario.name,
                k=k,
                passed=passed,
                outcome=outcomes[0],
                distinct_outcomes=len({outcome.key for outcome in outcomes}),
                failure=failure,
            )
        )
    passed_count = sum(result.passed for result in results)
    return EvalReport(
        k=k,
        scenarios=tuple(results),
        passed=passed_count,
        failed=len(results) - passed_count,
        pass_k=passed_count / len(results) if results else 0.0,
        llm_model_id=None if llm is None else str(getattr(llm, "model_id", None) or type(llm).__name__),
        llm_calls=0,
        cost_usd=0.0,
    )


def mutation_test(
    scenarios: Sequence[Scenario],
    make_ctx: ContextFactory,
    *,
    principals: Approvers,
    registry: ToolRegistry | None = None,
) -> dict[str, tuple[str, ...]]:
    """Per policy rule: the scenarios that fail once it is removed. A rule with none is untested."""
    caught: dict[str, tuple[str, ...]] = {}
    for name in RULES:
        mutant = {rule: function for rule, function in RULES.items() if rule != name}
        report = pass_k(scenarios, make_ctx, principals=principals, registry=registry, k=1, rules=mutant)
        caught[name] = tuple(result.name for result in report.scenarios if not result.passed)
    return caught


def write_report(report: EvalReport, path: Path = REPORT_PATH) -> Path:
    """Write the report as canonical JSON (stable across machines, so a change shows as a diff)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(report) + "\n", encoding="utf-8")
    return path
