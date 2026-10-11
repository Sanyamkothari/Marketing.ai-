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
    request: ToolCallRequest | ProposalRequest | PlannerScenarioRequest
    deployment: str = Field(default="local", description="Which of the world's deployments answers it.")
    principal: str = Field(default="analyst", description="Who asks: one of the world's principals.")
    approve_as: str | None = Field(default=None, description="Then try to approve it as this principal.")
    expect_status: OutcomeStatus
    expect_codes: tuple[str, ...] = ()
    expect_digest: str | None = Field(
        default=None, description="The expected payload digest; None when it depends on ids the world mints."
    )
    guards: tuple[str, ...] = Field(default=(), description="The policy rules this scenario exists to catch.")


class PlannerScenarioRequest(StrictBase):
    """Run the campaign planner (M139) on this request (`engine.agentic.planner.PlanRequest`)."""

    plan: dict[str, Any]
    llm: str | None = Field(
        default=None, description="Which of the test's language models answers; None: no AI service."
    )


class Outcome(StrictBase):
    status: OutcomeStatus
    codes: tuple[str, ...] = ()
    payload_digest: str | None = None
    # Plan K M139: what the planner's model did, kept out of `key` (wording may vary; decisions may not).
    claim_failures: tuple[str, ...] = ()
    llm_calls: int = 0

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
        description="Model calls the planner's scenarios made (M139); 0 for tool and proposal scenarios."
    )
    cost_usd: float = Field(description="What those calls cost, from the Meter; 0.0 with none.")
    claim_failures: dict[str, int] = Field(
        default_factory=dict,
        description="Narrative claim failures over every run, by code (design §4; M139). Tracked, not gated.",
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
PlannerFactory = Callable[[Scenario, ToolRegistry], Any]
"""M139: the `engine.agentic.planner.PlannerDeps` a planner scenario runs with (its cards and its model)."""


def _run_planner(
    scenario: Scenario,
    request: PlannerScenarioRequest,
    ctx: ToolContext,
    registry: ToolRegistry,
    planner: PlannerFactory | None,
) -> tuple[Outcome, str | None]:
    """Run the planner to its interrupt; the outcome is its proposal's, or the code it stopped with."""
    from engine.agentic.planner import PlannerDeps, PlanRequest, outcome_of, start_plan

    deps = planner(scenario, registry) if planner is not None else PlannerDeps(registry=registry)
    state = start_plan(PlanRequest.model_validate(request.plan), ctx, deps)
    status, codes, payload_digest = outcome_of(state)
    failures = () if state.narrative is None else tuple(f.code for f in state.narrative.failures)
    outcome = Outcome.model_validate(
        {
            "status": status,
            "codes": codes,
            "payload_digest": payload_digest,
            "claim_failures": failures,
            "llm_calls": state.llm_calls,
        }
    )
    return outcome, state.proposal_id


def run_once(
    scenario: Scenario,
    make_ctx: ContextFactory,
    registry: ToolRegistry,
    *,
    principals: Approvers,
    rules: Mapping[str, Rule] | None = None,
    planner: PlannerFactory | None = None,
) -> Outcome:
    """One run of `scenario` through the registry, `submit_proposal` or the planner (M139), then an
    approval when asked."""
    from engine.agentic.proposals import submit_proposal
    from engine.approvals import decide_proposal

    ctx = dataclasses.replace(make_ctx(scenario), policy_rules=rules)
    request = scenario.request
    proposal_id: str | None = None
    try:
        if isinstance(request, PlannerScenarioRequest):
            outcome, proposal_id = _run_planner(scenario, request, ctx, registry, planner)
        elif isinstance(request, ToolCallRequest):
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
    planner: PlannerFactory | None = None,
) -> EvalReport:
    """Run every scenario `k` times; each passes only when all k outcomes agree and match the expectation.

    Plan K M139: a planner scenario's model calls and claim failures (by code) are totalled into the report;
    they are not part of an outcome's key, so wording that varies between runs passes, decisions that vary
    fail.
    """
    from engine.agentic.tools import default_registry

    if k < 1:
        raise ValueError("k must be at least 1.")
    tools = registry if registry is not None else default_registry()
    results: list[ScenarioResult] = []
    calls = 0
    claim_failures: dict[str, int] = {}
    for scenario in scenarios:
        outcomes = [
            run_once(scenario, make_ctx, tools, principals=principals, rules=rules, planner=planner)
            for _ in range(k)
        ]
        for outcome in outcomes:
            calls += outcome.llm_calls
            for code in outcome.claim_failures:
                claim_failures[code] = claim_failures.get(code, 0) + 1
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
        llm_calls=calls,
        cost_usd=0.0,
        claim_failures=dict(sorted(claim_failures.items())),
    )


def mutation_test(
    scenarios: Sequence[Scenario],
    make_ctx: ContextFactory,
    *,
    principals: Approvers,
    registry: ToolRegistry | None = None,
    planner: PlannerFactory | None = None,
) -> dict[str, tuple[str, ...]]:
    """Per policy rule: the scenarios that fail once it is removed. A rule with none is untested."""
    caught: dict[str, tuple[str, ...]] = {}
    for name in RULES:
        mutant = {rule: function for rule, function in RULES.items() if rule != name}
        report = pass_k(
            scenarios, make_ctx, principals=principals, registry=registry, k=1, rules=mutant, planner=planner
        )
        caught[name] = tuple(result.name for result in report.scenarios if not result.passed)
    return caught


def write_report(report: EvalReport, path: Path = REPORT_PATH) -> Path:
    """Write the report as canonical JSON (stable across machines, so a change shows as a diff)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(report) + "\n", encoding="utf-8")
    return path
