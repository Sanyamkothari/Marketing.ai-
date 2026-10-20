"""Run the fixed agent eval cases several times and report pass rate, pass^k and consistency.

plan-v2 section 7: each agent has a fixed test set, each case runs k times (5 by default), and we
report three numbers per agent:

- **pass rate**: passed runs / all runs.
- **pass^k**: the share of cases that passed on *every* one of the k runs. This is the number that
  matters for an agent that runs unattended: a case that passes 4 times in 5 fails one goal in 5.
- **consistency**: the share of cases whose k answers were all the same on the expected keys, right
  or wrong. A stable wrong answer is a prompt problem; an unstable one is a model or temperature one.

An agent under eval is a function from a case's `input` dict to an output dict (`EvalAgent`). An
`AgentFactory` makes one for run number `run` (0..k-1), so a factory can vary what should vary
between runs, such as the Critic's seeded rubric order ("judges are order-shuffled"). The adapters
below wrap the real agents; swap in another factory to evaluate a different model or agent.

    python -m autonomy.agents.evals.runner --fake      # ScriptedLLM fakes: checks the plumbing, in CI
    python -m autonomy.agents.evals.runner             # a real model, from engine settings

`--fake` measures the harness and the agents' code paths, not model quality: its scripted models are
keyword rules written to answer these cases. Only a real model's report says anything about the
model. Time is fixed (`EVAL_NOW`), so runs repeat exactly given the same model answers.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from autonomy.agents.base import ScriptedLLM, extract_json
from autonomy.contracts import NO_OFFER, Channel, OfferKind, OfferOption, stable_id
from autonomy.interfaces import TextSignals
from autonomy.signals.llm_signals import LLMTextSignals
from engine.llm import LLMClient, LLMCompletion

EvalOutput = dict[str, Any]
EvalAgent = Callable[[Mapping[str, Any]], EvalOutput]
AgentFactory = Callable[[int], EvalAgent]

CASES_PATH = Path(__file__).with_name("cases.yaml")
EVAL_NOW = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
EVAL_BUSINESS = "eval-shop"
EVAL_CHANNELS: tuple[Channel, ...] = (Channel.EMAIL, Channel.WHATSAPP, Channel.SMS)
EVAL_OFFERS: tuple[OfferOption, ...] = (
    OfferOption(offer_id="pct10", label="10% off", kind=OfferKind.PERCENT_OFF, amount=10),
    OfferOption(offer_id="freeship", label="Free shipping", kind=OfferKind.FREE_SHIPPING),
    OfferOption(
        offer_id="vip30", label="30% off", kind=OfferKind.PERCENT_OFF, amount=30, needs_approval=True
    ),
)
EVAL_BRAND_VOICE = "Warm, plain and never pushy. Short sentences. No slang, no hype, no false urgency."
EVAL_DOMAINS: tuple[str, ...] = ("demo-shop.com",)
_CONTAINS = "_contains"


# --- Cases ----------------------------------------------------------------------------------------


class EvalCase(BaseModel):
    """One fixed case: what the agent gets, and what its output must hold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    input: dict[str, Any]
    expect: dict[str, Any] = Field(min_length=1)
    note: str = ""


def load_cases(path: Path | None = None, *, agents: Collection[str] | None = None) -> list[EvalCase]:
    """The cases in `path` (default: the shipped `cases.yaml`), optionally only for some agents.

    The file maps an agent name to a list of `{id, input, expect, note?}`; ids must be unique.
    """
    raw = yaml.safe_load((path or CASES_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("the cases file must map agent names to lists of cases")
    cases: list[EvalCase] = []
    for agent, items in raw.items():
        if not isinstance(items, list):
            raise ValueError(f"cases for {agent!r} must be a list")
        if agents is not None and agent not in agents:
            continue
        cases.extend(EvalCase.model_validate({**item, "agent": agent}) for item in items)
    ids = [c.id for c in cases]
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        raise ValueError(f"case ids repeat: {repeated}")
    return cases


# --- Checking -------------------------------------------------------------------------------------


def _canon(value: Any) -> Any:
    """A comparable form: enums by value, lists and tuples as sorted unique values (set semantics)."""
    if isinstance(value, list | tuple | set | frozenset):
        return sorted({json.dumps(_canon(v), sort_keys=True) for v in value})
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in value.items()}
    if hasattr(value, "value") and isinstance(value.value, str | int | float):
        return value.value
    return value


def _flat_text(value: Any) -> str:
    if isinstance(value, list | tuple):
        return "\n".join(_flat_text(v) for v in value)
    return str(value)


def check_output(expect: Mapping[str, Any], output: Mapping[str, Any]) -> list[str]:
    """What in `output` misses `expect`, in plain words; empty means the run passed."""
    problems: list[str] = []
    for key, want in expect.items():
        if key.endswith(_CONTAINS):
            field = key[: -len(_CONTAINS)]
            text = _flat_text(output.get(field, "")).lower()
            phrases = want if isinstance(want, list) else [want]
            problems.extend(f"{field} does not mention {p!r}" for p in phrases if str(p).lower() not in text)
        elif key not in output:
            problems.append(f"no {key} in the output")
        elif _canon(output[key]) != _canon(want):
            problems.append(f"{key}: expected {_canon(want)!r}, got {_canon(output[key])!r}")
    return problems


def answer_key(expect: Mapping[str, Any], output: Mapping[str, Any] | None) -> str:
    """The run's answer on what the case checks, as a string, for the consistency comparison.

    For a `_contains` key the answer is which of its phrases the field mentions, not the field's
    whole text: free text such as the Critic's reasons carries judge scores and a seeded rubric
    order that differ between runs even when the verdict and its reason do not.
    """
    if output is None:
        return "<error>"
    key: dict[str, Any] = {}
    for k, want in expect.items():
        if k.endswith(_CONTAINS):
            text = _flat_text(output.get(k[: -len(_CONTAINS)], "")).lower()
            phrases = want if isinstance(want, list) else [want]
            key[k] = sorted(str(p) for p in phrases if str(p).lower() in text)
        else:
            key[k] = _canon(output.get(k))
    return json.dumps(key, sort_keys=True, default=str)


# --- Report ---------------------------------------------------------------------------------------


class CaseResult(BaseModel):
    case_id: str
    agent: str
    runs: int
    passes: int
    pass_all: bool = Field(description="pass^k: every run passed")
    consistent: bool = Field(description="every run gave the same answer on the expected keys")
    answers: list[str] = Field(description="The distinct answers, in the order first seen")
    failures: list[str] = Field(description="Distinct reasons runs failed, in the order first seen")


class AgentSummary(BaseModel):
    agent: str
    cases: int
    pass_rate: float
    pass_k: float
    consistency: float


class EvalReport(BaseModel):
    repeats: int
    pass_rate: float
    pass_k: float
    consistency: float
    agents: list[AgentSummary]
    cases: list[CaseResult]
    skipped: list[str] = Field(description="Case ids whose agent had no factory")

    def text(self) -> str:
        """The report for a terminal: one line per agent, then every case that was not perfect."""
        lines = [f"Agent evals, {self.repeats} runs per case"]
        for a in self.agents:
            lines.append(
                f"  {a.agent:<14} {a.cases:>3} cases  pass rate {a.pass_rate:6.1%}  "
                f"pass^{self.repeats} {a.pass_k:6.1%}  consistent {a.consistency:6.1%}"
            )
        lines.append(
            f"  {'all':<14} {len(self.cases):>3} cases  pass rate {self.pass_rate:6.1%}  "
            f"pass^{self.repeats} {self.pass_k:6.1%}  consistent {self.consistency:6.1%}"
        )
        for c in self.cases:
            if c.pass_all and c.consistent:
                continue
            lines.append(f"- {c.case_id} ({c.agent}): {c.passes}/{c.runs} passed")
            lines.extend(f"    {f}" for f in c.failures[:5])
            if not c.consistent:
                lines.append(f"    answers varied: {c.answers}")
        if self.skipped:
            lines.append(f"Skipped (no agent): {', '.join(self.skipped)}")
        return "\n".join(lines)


def _share(flags: Sequence[bool]) -> float:
    return sum(flags) / len(flags) if flags else 0.0


def _summary(name: str, results: Sequence[CaseResult]) -> AgentSummary:
    runs = sum(r.runs for r in results)
    return AgentSummary(
        agent=name,
        cases=len(results),
        pass_rate=sum(r.passes for r in results) / runs if runs else 0.0,
        pass_k=_share([r.pass_all for r in results]),
        consistency=_share([r.consistent for r in results]),
    )


def run_evals(
    agent_factories: Mapping[str, AgentFactory], cases: Sequence[EvalCase], repeats: int = 5
) -> EvalReport:
    """Run every case whose agent has a factory `repeats` times. An exception fails that run only."""
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    runnable = [c for c in cases if c.agent in agent_factories]
    skipped = [c.id for c in cases if c.agent not in agent_factories]
    passes = {c.id: 0 for c in runnable}
    answers: dict[str, list[str]] = {c.id: [] for c in runnable}
    failures: dict[str, list[str]] = {c.id: [] for c in runnable}
    for run in range(repeats):
        agents = {name: agent_factories[name](run) for name in sorted({c.agent for c in runnable})}
        for case in runnable:
            output: EvalOutput | None
            try:
                output = agents[case.agent](case.input)
                problems = check_output(case.expect, output)
            except Exception as err:  # one broken run must not hide the rest of the report
                output, problems = None, [f"error: {type(err).__name__}: {err}"]
            answers[case.id].append(answer_key(case.expect, output))
            if problems:
                failures[case.id].extend(p for p in problems if p not in failures[case.id])
            else:
                passes[case.id] += 1
    results = [
        CaseResult(
            case_id=c.id,
            agent=c.agent,
            runs=repeats,
            passes=passes[c.id],
            pass_all=passes[c.id] == repeats,
            consistent=len(set(answers[c.id])) == 1,
            answers=list(dict.fromkeys(answers[c.id])),
            failures=failures[c.id],
        )
        for c in runnable
    ]
    names = list(dict.fromkeys(r.agent for r in results))
    overall = _summary("all", results)
    return EvalReport(
        repeats=repeats,
        pass_rate=overall.pass_rate,
        pass_k=overall.pass_k,
        consistency=overall.consistency,
        agents=[_summary(n, [r for r in results if r.agent == n]) for n in names],
        cases=results,
        skipped=skipped,
    )


# --- Adapters: the real agents as EvalAgents -------------------------------------------------------


class _RecordingLLM:
    """Passes calls through and keeps every completion's text, so an adapter can read the model's
    own answer (the Strategist's `BriefDraft.kind` is not stored on the `Brief`)."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.texts: list[str] = []

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        out = self._inner.complete(
            prompt, system=system, model_id=model_id, max_tokens=max_tokens, temperature=temperature
        )
        self.texts.append(out.text)
        return out

    def embed(self, texts: Sequence[str], *, model_id: str | None = None) -> tuple[tuple[float, ...], ...]:
        return self._inner.embed(texts, model_id=model_id)

    def count_tokens(self, text: str, *, model_id: str | None = None) -> int:
        return self._inner.count_tokens(text, model_id=model_id)


_TEMPLATE_KIND = re.compile(r"Standard (\w+) plan")


def strategist_eval(
    make_llm: Callable[[Mapping[str, Any]], LLMClient], *, model_id: str | None = None
) -> AgentFactory:
    """Goal text -> {kind, objective, channels, used_template}.

    Input: `text`, `budget`, optional `kind` / `objective` (as the marketer set them) and `channels`
    (the business's; default `EVAL_CHANNELS`). `make_llm(input)` gives the model for one case, so a
    fake can answer per case; a real run returns the same client every time. `kind` is the model's
    draft kind, or the template's when the Strategist fell back to one.
    """
    from autonomy.agents.strategist import BriefDraft, BusinessFacts, Strategist
    from autonomy.contracts import Goal, GoalKind, Objective

    def factory(run: int) -> EvalAgent:  # noqa: ARG001 - the Strategist has no per-run seed
        def agent(case: Mapping[str, Any]) -> EvalOutput:
            text = str(case["text"])
            goal = Goal(
                goal_id=stable_id("goal", text),
                business_id=EVAL_BUSINESS,
                text=text,
                kind=GoalKind(case.get("kind", GoalKind.CUSTOM.value)),
                objective=Objective(case.get("objective", Objective.GROSS_PROFIT.value)),
                budget=float(case.get("budget", 1000)),
                starts_at=EVAL_NOW,
                created_at=EVAL_NOW,
            )
            channels = tuple(Channel(c) for c in case.get("channels", [c.value for c in EVAL_CHANNELS]))
            facts = BusinessFacts(
                business_id=EVAL_BUSINESS, name="Demo Shop", channels=channels, offers=EVAL_OFFERS
            )
            llm = _RecordingLLM(make_llm(case))
            brief = Strategist(llm, model_id).draft_brief(goal, business_facts=facts, now=EVAL_NOW)
            used_template = brief.created_by.endswith(":template")
            if used_template:
                found = _TEMPLATE_KIND.search(brief.rationale)
                kind = found.group(1) if found else "unknown"
            else:
                kind = BriefDraft.model_validate_json(extract_json(llm.texts[-1])).kind.value
            return {
                "kind": kind,
                "objective": brief.objective.value,
                "channels": [c.value for c in brief.envelope.channels],
                "used_template": used_template,
            }

        return agent

    return factory


def critic_eval(judge_llm: LLMClient, *, model_id: str | None = None) -> AgentFactory:
    """Content item -> {verdict, reasons, notes}.

    Input: `item` (ContentItem fields; `content_id` and `created_at` are filled in), and optional
    `brand_voice` and `allowed_domains`. The approved offers are "none" plus `EVAL_OFFERS` that need
    no approval. Run `run` seeds the Critic, so each run shuffles the judge's rubric differently.
    `reasons` holds only the notes that explain a rejection (rule breaks and the judge's low scores).
    """
    from autonomy.agents.critic import BrandRules, Critic
    from autonomy.contracts import ContentItem

    rules_offers = (NO_OFFER, *(o for o in EVAL_OFFERS if not o.needs_approval))

    def factory(run: int) -> EvalAgent:
        critic = Critic(judge_llm, model_id, seed=run)

        def agent(case: Mapping[str, Any]) -> EvalOutput:
            fields = dict(case["item"])
            fields.setdefault("content_id", stable_id("content", json.dumps(fields, sort_keys=True)))
            fields.setdefault("created_at", EVAL_NOW)
            item = ContentItem.model_validate(fields)
            rules = BrandRules(
                brand_voice=str(case.get("brand_voice", EVAL_BRAND_VOICE)),
                offers=rules_offers,
                allowed_domains=tuple(case.get("allowed_domains", EVAL_DOMAINS)),
            )
            reviewed = critic.review([item], rules)[0]
            notes = list(reviewed.critic_notes)
            return {
                "verdict": reviewed.status.value,
                "reasons": [n for n in notes if n.startswith(("rule:", "rejected:"))],
                "notes": notes,
            }

        return agent

    return factory


def reply_intent_eval(signals: TextSignals) -> AgentFactory:
    """Reply text -> {intent, confidence} through the Conversation agent (keyword nets, then `signals`)."""
    from autonomy.agents.conversation import Conversation

    def factory(run: int) -> EvalAgent:  # noqa: ARG001
        conversation = Conversation(signals)

        def agent(case: Mapping[str, Any]) -> EvalOutput:
            intent, confidence, _, _ = conversation.classify(str(case["text"]))
            return {"intent": intent, "confidence": confidence}

        return agent

    return factory


def analyst_claims_eval() -> AgentFactory:
    """Text and code's numbers -> {problems}: the numbers the text states that code never computed."""
    from autonomy.agents.claims import check_numbers

    def factory(run: int) -> EvalAgent:  # noqa: ARG001
        def agent(case: Mapping[str, Any]) -> EvalOutput:
            numbers = {str(k): float(v) for k, v in dict(case["numbers"]).items()}
            return {"problems": check_numbers(str(case["text"]), numbers)}

        return agent

    return factory


# --- Fakes: ScriptedLLM answers for --fake --------------------------------------------------------

_FAKE_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cart_recovery", ("cart", "checkout")),
    ("win_back", ("win back", "lapsed", "haven't ordered", "come back")),
    ("repeat_purchase", ("second purchase", "repeat", "buy again")),
    ("onboarding", ("onboard", "sign-up", "signup", "new customer")),
    ("retention", ("churn", "retain", "retention")),
)
_FAKE_CHANNELS = ("email", "whatsapp", "sms", "push")


def fake_strategist_llm(case: Mapping[str, Any]) -> LLMClient:
    """A model that drafts by keyword: kind and channels from the goal's words, no filters, no offers."""
    text = str(case["text"]).lower()
    kind = next((k for k, words in _FAKE_KINDS if any(w in text for w in words)), "custom")
    objective = case.get("objective") or ("revenue" if "revenue" in text else "gross_profit")
    business = [str(c) for c in case.get("channels", [c.value for c in EVAL_CHANNELS])]
    named = [c for c in _FAKE_CHANNELS if c in text and c in business]
    draft = {
        "kind": kind,
        "objective": objective,
        "audience_description": "Customers this goal is about.",
        "channels": named or business,
        "offer_ids": [],
        "horizon_days": 14,
        "max_contacts_per_week": 1,
        "rationale": "A narrow plan on the channels the marketer named.",
    }
    return ScriptedLLM(lambda _system, _prompt: json.dumps(draft))


_INVENTED_CLAIMS = re.compile(
    r"\b\d[\d,]* (?:customers|people|shoppers)\b|\brated\b|\bthe best in\b|\bhours? left\b|\bforever\b",
    re.IGNORECASE,
)


def _fake_judge(_system: str, prompt: str) -> str:
    """Honesty 2 when the message makes a claim nothing given supports; otherwise 4 for both."""
    start, end = prompt.find("The message:"), prompt.find("Score these criteria")
    message = prompt[start:end] if 0 <= start < end else prompt
    honest = _INVENTED_CLAIMS.search(message) is None
    scores = [
        {"criterion": "brand_voice", "score": 4, "reason": "plain and warm"},
        {
            "criterion": "honesty",
            "score": 4 if honest else 2,
            "reason": "claims are supportable" if honest else "states a result or deadline nothing supports",
        },
    ]
    return json.dumps({"scores": scores})


_FAKE_INTENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("other", ("ignore your instructions", "out of the office", "out of office")),
    ("complaint", ("broken", "charged twice", "rude", "terrible")),
    ("not_now", ("not right now", "not now", "maybe next", "later")),
    ("interested", ("yes please", "sounds good", "how do i order")),
    ("question", ("?",)),
)


def _fake_reply_classifier(_system: str, prompt: str) -> str:
    """Answers `LLMTextSignals` prompts by keyword; reads the texts from the prompt's JSON list."""
    labels: list[str] = []
    texts: list[dict[str, Any]] = []
    for line in prompt.splitlines():
        if line.startswith("Allowed labels"):
            labels = json.loads(line.split(":", 1)[1])
        elif line.startswith('[{"index"'):
            texts = json.loads(line)
    answers = []
    for item in texts:
        low = str(item["text"]).lower()
        label = next((lab for lab, words in _FAKE_INTENTS if any(w in low for w in words)), labels[-1])
        answers.append({"index": item["index"], "label": label, "probability": 0.85})
    return json.dumps({"answers": answers})


def fake_factories() -> dict[str, AgentFactory]:
    """Every agent on ScriptedLLM fakes. See the module docstring for what this does and does not test."""
    return {
        "strategist": strategist_eval(fake_strategist_llm),
        "critic": critic_eval(ScriptedLLM(_fake_judge, model_id="fake-judge")),
        "reply_intent": reply_intent_eval(LLMTextSignals(ScriptedLLM(_fake_reply_classifier))),
        "analyst": analyst_claims_eval(),
    }


def real_factories(
    llm: LLMClient,
    *,
    model_id: str | None = None,
    judge_llm: LLMClient | None = None,
    judge_model_id: str | None = None,
    signals_model_id: str | None = None,
) -> dict[str, AgentFactory]:
    """Every agent on a real model. Give the Critic a judge from a different model family if you can."""
    return {
        "strategist": strategist_eval(lambda _case: llm, model_id=model_id),
        "critic": critic_eval(judge_llm or llm, model_id=judge_model_id),
        "reply_intent": reply_intent_eval(LLMTextSignals(llm, signals_model_id)),
        "analyst": analyst_claims_eval(),
    }


def llm_from_settings() -> LLMClient:
    """The real client engine settings name (Bedrock today). Refuses the fake: use --fake for that."""
    from engine.llm import BedrockLLMClient
    from engine.settings import load_settings

    settings = load_settings()
    if settings.llm_backend != "bedrock":
        raise SystemExit(
            "The engine's LLM backend is the fake. Set MARKETING_AI_LLM_BACKEND=bedrock (with a model id "
            "and region), or run with --fake."
        )
    return BedrockLLMClient(region=settings.aws_region or "", model_id=settings.bedrock_model_id or "")


# --- CLI ------------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m autonomy.agents.evals.runner", description=__doc__)
    parser.add_argument("--fake", action="store_true", help="use ScriptedLLM fakes (no model calls)")
    parser.add_argument("--repeats", type=int, default=5, help="runs per case (k)")
    parser.add_argument("--cases", type=Path, default=None, help="a cases file (default: the shipped one)")
    parser.add_argument("--agent", action="append", default=None, help="only this agent; repeatable")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--fail-under", type=float, default=None, help="exit 1 if overall pass^k is below")
    parser.add_argument("--model-id", default=None, help="model for the Strategist")
    parser.add_argument("--judge-model-id", default=None, help="model for the Critic's judge")
    parser.add_argument("--signals-model-id", default=None, help="model for reply intent")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases, agents=args.agent)
    if args.fake:
        factories = fake_factories()
    else:
        llm = llm_from_settings()
        factories = real_factories(
            llm,
            model_id=args.model_id,
            judge_model_id=args.judge_model_id,
            signals_model_id=args.signals_model_id,
        )
    report = run_evals(factories, cases, repeats=args.repeats)
    sys.stdout.write((report.model_dump_json(indent=2) if args.json else report.text()) + "\n")
    if args.fail_under is not None and report.pass_k < args.fail_under:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
