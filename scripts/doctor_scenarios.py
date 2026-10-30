"""Harness to run Data Doctor scenarios with a replay AI client or default author (Stage 0).

Executes the real app in-process using FastAPI TestClient with MARKETING_AI_ALLOW_FAKE_AI=true.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from unittest import mock

import pandas as pd
from fastapi.testclient import TestClient

import api.routes.case_combine as case_combine
import api.routes.uploads as upload_routes
import engine.ai_service as ai_service
from api.main import create_app
from engine.llm import APPROX_CHARS_PER_TOKEN, FakeLLMClient, FakeLLMMode, LLMCall, LLMCompletion
from scripts.doctor_scenario_checks import (
    INVARIANTS,
    check_invariants,
    evaluate,
    plan_kind,
)
from scripts.doctor_scenario_data import (
    generate_bank_marketing,
    generate_churn_list,
    generate_customers_and_orders,
    generate_hillstrom_campaign,
    generate_order_log,
    generate_two_currencies,
    generate_wide_monthly,
)

__all__ = [
    "OutOfRepliesError",
    "ReplayLLMClient",
    "ScenarioResult",
    "UsageTracker",
    "run_scenario",
]

SCENARIOS_DIR: Final[Path] = Path("tests/fixtures/doctor_scenarios")


class OutOfRepliesError(Exception):
    """Raised when replies.jsonl has no more recorded replies for the current prompt."""


@dataclass
class UsageTracker:
    """Usage counts across model calls."""

    calls: int = 0
    input_chars: int = 0
    input_tokens: int = 0
    output_chars: int = 0
    output_tokens: int = 0

    def record_call(self, input_chars: int, input_tokens: int) -> None:
        self.calls += 1
        self.input_chars += input_chars
        self.input_tokens += input_tokens

    def record_output(self, output_chars: int, output_tokens: int) -> None:
        self.output_chars += output_chars
        self.output_tokens += output_tokens

    def cost_line(self) -> str:
        return (
            f"COST: {self.calls} calls, {self.input_chars:,} input chars, "
            f"{self.input_tokens:,} input tokens, {self.output_tokens:,} output tokens"
        )


class ReplayLLMClient(FakeLLMClient):
    """Replays recorded replies from replies.jsonl; writes next_prompt.txt when out of replies."""

    def __init__(
        self,
        replies: Sequence[str],
        prompt_log_path: Path,
        usage_tracker: UsageTracker,
    ) -> None:
        super().__init__(mode=FakeLLMMode.GROUNDED)
        self.replies = list(replies)
        self.prompt_log_path = prompt_log_path
        self.usage_tracker = usage_tracker
        self.reply_index = 0
        self.prompts: list[str] = []
        """Every prompt the AI was sent, for the invariant that reads the refusals it received."""

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model_id: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMCompletion:
        input_chars = len(system) + len(prompt)
        in_tokens = -(-len(system) // APPROX_CHARS_PER_TOKEN) + -(-len(prompt) // APPROX_CHARS_PER_TOKEN)
        self.usage_tracker.record_call(input_chars=input_chars, input_tokens=in_tokens)
        self.prompts.append(prompt)

        if self.reply_index < len(self.replies):
            text = self.replies[self.reply_index].strip()
            self.reply_index += 1
            out_tokens = -(-len(text) // APPROX_CHARS_PER_TOKEN)
            self.usage_tracker.record_output(output_chars=len(text), output_tokens=out_tokens)
            chosen_model = model_id or "doctor-replay"
            self._calls.append(
                LLMCall(
                    kind="complete",
                    model_id=chosen_model,
                    prompt=prompt,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                )
            )
            return LLMCompletion(
                text=text,
                model_id=chosen_model,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cost_estimate_usd=None,
                stop_reason="end_turn",
            )

        # Out of replies: log prompt to next_prompt.txt and stop run cleanly!
        prompt_content = f"=== SYSTEM ===\n{system}\n\n=== USER ===\n{prompt}\n"
        self.prompt_log_path.write_text(prompt_content, encoding="utf-8")
        print(f"\n[REPLAY] Out of replies! Next prompt written to {self.prompt_log_path}")
        print(self.usage_tracker.cost_line())
        raise OutOfRepliesError(f"Scenario out of recorded replies. Prompt written to {self.prompt_log_path}")


@dataclass
class ScenarioResult:
    """Outcome of running a single scenario."""

    name: str
    status: str  # "PASS" or "FAIL"
    cost_line: str
    invariants: dict[str, list[str]] = field(default_factory=dict)
    """Invariants 1 to 6: what broke, by invariant, over every step."""
    checkpoints: dict[str, list[str]] = field(default_factory=dict)
    """What differs from `expect.json`, by checkpoint: the names of its `after` steps, and `final`."""
    chat_results: dict[str, str] = field(default_factory=dict)
    plan_kind: tuple[Any, ...] = ()
    """The kind of plan before the first chat message (or at the end), for invariant 7."""
    replies_from: str = ""
    details: str = ""
    notes: list[str] = field(default_factory=list)
    """What the person's script could not do, by step: no such question, no such change, an approval the app refused."""

    @property
    def invariant_violations(self) -> list[str]:
        return [f"{name}: {v}" for name, found in self.invariants.items() for v in found]

    @property
    def expectation_failures(self) -> list[str]:
        return [f"{name}: {v}" for name, found in self.checkpoints.items() for v in found]


@dataclass
class _Step:
    """The case and screen after one step of the person's script."""

    case: dict[str, Any]
    screen: dict[str, Any] | None
    previous: dict[str, Any] | None
    notes: list[str]
    frames: dict[str, pd.DataFrame]


def _get_fixture_frame(fixture_name: str) -> pd.DataFrame:
    """Generate deterministic synthetic DataFrame for the scenario fixture."""
    if fixture_name == "order_log":
        return generate_order_log()
    if fixture_name == "churn_list":
        return generate_churn_list()
    if fixture_name == "bank_marketing":
        return generate_bank_marketing()
    if fixture_name == "hillstrom":
        return generate_hillstrom_campaign()
    if fixture_name == "two_currencies":
        return generate_two_currencies()
    if fixture_name == "wide_monthly":
        return generate_wide_monthly()
    if fixture_name == "customers":
        cust, _ = generate_customers_and_orders()
        return cust
    if fixture_name == "orders":
        _, ords = generate_customers_and_orders()
        return ords
    raise ValueError(f"Unknown fixture name: {fixture_name}")


def _csv(frame: pd.DataFrame) -> bytes:
    buf = io.StringIO()
    frame.to_csv(buf, index=False)
    return buf.getvalue().encode()


class _Person:
    """The person: plays the script of `person.json` against the app, one action at a time."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.case_id = ""
        self.case: dict[str, Any] = {}
        self.frames: dict[str, pd.DataFrame] = {}
        self.notes: list[str] = []

    def _upload(self, name: str, frame: pd.DataFrame, use_case: str) -> str:
        self.frames[f"{name}.csv"] = frame
        res = self.client.post(
            "/uploads",
            files={"file": (f"{name}.csv", _csv(frame), "text/csv")},
            data={"use_case": use_case, "mode": "train"},
        )
        assert res.status_code == 201, res.text
        return str(res.json()["upload_id"])

    def upload(self, action: dict[str, Any]) -> None:
        file_key, use_case = str(action.get("file")), str(action.get("use_case", ""))
        if file_key == "customers_and_orders":
            customers, orders = generate_customers_and_orders()
            ids = [self._upload("customers", customers, use_case), self._upload("orders", orders, use_case)]
        else:
            ids = [self._upload(file_key, _get_fixture_frame(file_key), use_case)]
        res = self.client.post("/cases", json={"upload_ids": ids})
        assert res.status_code == 201, res.text
        self.case = res.json()
        self.case_id = str(self.case["case_id"])

    def consent(self, _action: dict[str, Any]) -> None:
        res = self.client.post(f"/cases/{self.case_id}/ai-consent", json={})
        assert res.status_code == 200, res.text
        self.case = self.client.get(f"/cases/{self.case_id}").json()

    def visit(self, _action: dict[str, Any]) -> None:
        res = self.client.post(f"/cases/{self.case_id}/visit", json={})
        assert res.status_code == 200, res.text
        self.case = res.json()

    def goal(self, action: dict[str, Any]) -> None:
        res = self.client.post(
            f"/cases/{self.case_id}/goal",
            json={"goal_id": str(action.get("goal_id")), "version": self.case["version"]},
        )
        assert res.status_code == 200, res.text
        self.case = res.json()

    def answer(self, action: dict[str, Any]) -> None:
        match = str(action.get("match", "")).lower()
        option = str(action.get("option", "recommended"))
        question = next(
            (
                q
                for q in self.case.get("questions", [])
                if q.get("answer") is None
                and (
                    not match
                    or match in str(q.get("text", "")).lower()
                    or match in str(q.get("code", "")).lower()
                )
            ),
            None,
        )
        if question is None:
            self.notes.append(f"the person found no open question matching '{match}'")
            return
        option_id = question.get("recommended") if option == "recommended" else None
        for o in question.get("options", []):
            if option_id is None and (
                option in str(o.get("label", "")).lower() or option == o.get("option_id")
            ):
                option_id = o.get("option_id")
        if option_id is None:
            self.notes.append(f"question '{question.get('code')}' has no option '{option}'")
            return
        res = self.client.post(
            f"/cases/{self.case_id}/answers",
            json={
                "version": self.case["version"],
                "question_id": question["question_id"],
                "option_id": option_id,
                "value": action.get("value"),
            },
        )
        assert res.status_code == 200, res.text
        self.case = res.json()

    def decide(self, action: dict[str, Any]) -> None:
        """Say yes (or no) to the first change waiting for the person whose title or payload holds `match` (a word,
        or a list of words of which any will do)."""
        raw = action.get("match", "")
        match = [str(m).lower() for m in (raw if isinstance(raw, list) else [raw])]
        item = next(
            (
                i
                for i in self.case.get("items", [])
                if i.get("state") == "pending"
                and any(
                    m in json.dumps([i.get("title"), i.get("payload")], default=str).lower() for m in match
                )
            ),
            None,
        )
        if item is None:
            self.notes.append(f"the person found no change waiting for a yes that matches {match}")
            return
        res = self.client.post(
            f"/cases/{self.case_id}/decisions",
            json={
                "version": self.case["version"],
                "item_id": item["item_id"],
                "accept": bool(action.get("accept", True)),
                "park_for": action.get("park_for"),
            },
        )
        if res.status_code != 200:
            self.notes.append(f"the app refused the person's decision on {match}: {res.text[:200]}")
            return
        self.case = res.json()
        self._wait_for_full_check()  # the screen's poll does: the person reads the count of the whole file

    def chat(self, action: dict[str, Any]) -> None:
        res = self.client.post(
            f"/cases/{self.case_id}/chat",
            json={"version": self.case["version"], "message": str(action.get("message", ""))},
        )
        if res.status_code != 200:
            self.notes.append(f"the chat was refused: {res.text[:200]}")
            return
        self.case = res.json()

    def _wait_for_full_check(self, seconds: float = 120.0) -> None:
        """Approve is refused while the count of the whole file runs beside the request: wait, as the screen's poll
        does (each tick asks the visit to continue, which takes a finished count into the case)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            res = self.client.post(f"/cases/{self.case_id}/visit", json={})
            assert res.status_code == 200, res.text
            self.case = res.json()
            shown = (self.client.get(f"/cases/{self.case_id}/screen").json().get("view") or {}).get(
                "full_check"
            )
            if shown is None or shown["state"] in {"done", "failed"}:
                return
            time.sleep(0.05)
        self.notes.append("the check of the whole file did not finish")

    def approve(self, _action: dict[str, Any]) -> None:
        """Say yes to the one approval on the screen, once the screen has one and the app accepts it."""
        deadline = time.monotonic() + 120
        while True:
            self._wait_for_full_check()
            view = (self.client.get(f"/cases/{self.case_id}/screen").json().get("view")) or {}
            approval = view.get("approval")
            if not approval or not approval.get("sentence") or not approval.get("digest"):
                if (view.get("verdict") or {}).get("key") == "working" and time.monotonic() < deadline:
                    time.sleep(0.2)
                    continue
                self.notes.append("the screen offers nothing to approve")
                return
            yeses = [
                {k: y.get(k) for k in ("kind", "key", "item_id", "sentence")}
                | {"reason": y.get("reason", "")}
                for y in approval.get("yeses", [])
            ]
            res = self.client.post(
                f"/cases/{self.case_id}/approval",
                json={
                    "version": self.case["version"],
                    "sentence": approval["sentence"],
                    "digest": approval["digest"],
                    "yeses": yeses,
                },
            )
            if res.status_code == 409 and "FULL_CHECK_RUNNING" in res.text and time.monotonic() < deadline:
                time.sleep(0.2)
                continue
            if res.status_code != 200:
                self.notes.append(f"the app refused the approval: {res.text[:200]}")
                return
            self.case = res.json()
            return


def _replies_for(scenario_path: Path) -> tuple[list[str], str]:
    """The recorded replies: `replies.jsonl` once an AI player has recorded them, else the baseline of today."""
    for name in ("replies.jsonl", "baseline_replies.jsonl"):
        file = scenario_path / name
        if file.exists():
            return [
                line.strip() for line in file.read_text(encoding="utf-8").splitlines() if line.strip()
            ], name
    return [], ""


def run_scenario(
    scenario_path: Path,
    *,
    with_ai: bool = True,
    config_root: Path = Path("configs"),
) -> ScenarioResult:
    """Run a scenario end-to-end, checking the invariants after every step and the expectations at their steps."""
    name = scenario_path.name
    person_file = scenario_path / "person.json"
    expect_file = scenario_path / "expect.json"
    if not person_file.exists():
        raise FileNotFoundError(f"Missing person.json in {scenario_path}")

    script: list[dict[str, Any]] = json.loads(person_file.read_text(encoding="utf-8"))
    expectations: dict[str, Any] = (
        json.loads(expect_file.read_text(encoding="utf-8")) if expect_file.exists() else {}
    )
    replies, replies_from = _replies_for(scenario_path)

    usage_tracker = UsageTracker()
    replay_client = (
        ReplayLLMClient(replies, scenario_path / "next_prompt.txt", usage_tracker) if with_ai else None
    )
    temp_dir = tempfile.mkdtemp()
    old_fake_env = os.environ.get("MARKETING_AI_ALLOW_FAKE_AI")
    os.environ["MARKETING_AI_ALLOW_FAKE_AI"] = "true" if with_ai else "false"
    old_build_client = ai_service.build_client
    ai_service.build_client = lambda *_args, **_kwargs: replay_client  # type: ignore[assignment]
    # An upload's id is part of the id of every change made from it, and so of the name the AI reads for it: with
    # the ids drawn at random a recorded reply that names a change would not replay. A counter makes them the same
    # in every run of the scenario.
    drawn = iter(range(1, 10_000))

    def next_upload_id() -> str:
        return "u_" + hashlib.sha256(f"{name}:{next(drawn)}".encode()).hexdigest()[:12]

    patches = contextlib.ExitStack()
    for module in (upload_routes, case_combine):
        patches.enter_context(mock.patch.object(module, "new_upload_id", next_upload_id))

    invariants: dict[str, list[str]] = {n: [] for n in INVARIANTS}
    steps: dict[str, _Step] = {}
    chat_results: dict[str, str] = {}
    kind_before_chat: tuple[Any, ...] | None = None
    last: _Step | None = None
    app: Any = None
    memo: dict[Any, Any] = {}
    prompts_seen = 0
    all_notes: list[str] = []

    try:
        app = create_app(config_root=config_root, data_dir=Path(temp_dir))
        with TestClient(app) as client:
            person = _Person(client)
            for number, action in enumerate(script):
                kind = str(action.get("action"))
                if kind == "chat" and kind_before_chat is None:
                    kind_before_chat = plan_kind(person.case)
                if kind == "chat" and not with_ai:
                    break  # nobody to answer: the chat is the AI's, and what follows it builds on its answers
                previous = person.case
                person.notes = []
                getattr(person, kind)(action)

                screen_res = client.get(f"/cases/{person.case_id}/screen")
                screen = screen_res.json() if screen_res.status_code == 200 else None
                prompts = replay_client.prompts[prompts_seen:] if replay_client is not None else []
                prompts_seen += len(prompts)
                found = check_invariants(
                    person.case,
                    screen,
                    previous=previous,
                    instruction=action if kind == "chat" else None,
                    prompts=prompts,
                    frames=person.frames,
                    memo=memo,
                )
                for invariant, violations in found.items():
                    # a violation that lasts over many steps is reported once, at the step it first showed
                    invariants[invariant] += [
                        f"step {number} ({kind}): {v}"
                        for v in violations
                        if not any(e.endswith(f": {v}") for e in invariants[invariant])
                    ]
                all_notes += [f"step {number} ({kind}): {n}" for n in person.notes]
                last = _Step(person.case, screen, previous, list(person.notes), person.frames)
                if action.get("name"):
                    steps[str(action["name"])] = last
                if kind == "chat" and action.get("instruction_id"):
                    reply = person.case.get("chat", [{}])[-1].get("text", "")
                    chat_results[str(action["instruction_id"])] = reply
    finally:
        jobs_runner = getattr(getattr(app, "state", None), "jobs", None)
        if jobs_runner is not None and hasattr(jobs_runner, "shutdown"):
            with contextlib.suppress(Exception):
                jobs_runner.shutdown(wait=True)
        ai_service.build_client = old_build_client
        patches.close()
        if old_fake_env is None:
            os.environ.pop("MARKETING_AI_ALLOW_FAKE_AI", None)
        else:
            os.environ["MARKETING_AI_ALLOW_FAKE_AI"] = old_fake_env
        shutil.rmtree(temp_dir, ignore_errors=True)

    checkpoints: dict[str, list[str]] = {}
    # `after` maps a step's name to what must hold once it is done; "name#label" adds a second, separately
    # reported group for the same step; "final" is the last step of the script.
    for checkpoint, checks in {
        **expectations.get("after", {}),
        "final": expectations.get("final", {}),
    }.items():
        step_name = checkpoint.split("#")[0]
        step = last if step_name == "final" else steps.get(step_name)
        if step is None:
            checkpoints[checkpoint] = [f"no step named '{step_name}' ran"]
            continue
        checkpoints[checkpoint] = evaluate(
            checks,
            step.case,
            step.screen,
            previous=step.previous,
            notes=(
                [] if checkpoint.endswith("#safety") else step.notes
            ),  # a "#safety" group asks for safety, not for the step to have done anything
            frames=step.frames,
            earlier={n: s.case for n, s in steps.items()},
        )

    failed = any(checkpoints.values()) or any(invariants.values())
    return ScenarioResult(
        name=name,
        status="FAIL" if failed else "PASS",
        cost_line=usage_tracker.cost_line(),
        invariants=invariants,
        checkpoints=checkpoints,
        chat_results=chat_results,
        plan_kind=kind_before_chat if kind_before_chat is not None else plan_kind(last.case if last else {}),
        replies_from=replies_from,
        notes=all_notes,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Data Doctor scenarios harness.")
    parser.add_argument("--scenario", default="all", help="Specific scenario folder name or 'all'")
    parser.add_argument("--no-ai", action="store_true", help="Run with default author (no AI)")
    parser.add_argument("--baseline", action="store_true", help="Generate and print baseline report")
    args = parser.parse_args()

    scenarios = sorted([p for p in SCENARIOS_DIR.iterdir() if p.is_dir()])
    if args.scenario != "all":
        scenarios = [p for p in scenarios if args.scenario in p.name]

    print(f"Running {len(scenarios)} scenario(s) (with_ai={not args.no_ai})...\n")
    results: list[ScenarioResult] = []

    for sc_path in scenarios:
        print(f"=== Scenario: {sc_path.name} ===")
        try:
            res = run_scenario(sc_path, with_ai=not args.no_ai)
            results.append(res)
            print(f"Status: {res.status}")
            print(res.cost_line, f"(replies: {res.replies_from or 'none'})")
            if res.invariant_violations:
                print(f"Invariant violations ({len(res.invariant_violations)}):")
                for v in res.invariant_violations:
                    print(f"  - {v}")
            if res.expectation_failures:
                print(f"Expectation failures ({len(res.expectation_failures)}):")
                for f in res.expectation_failures:
                    print(f"  - {f}")
        except OutOfRepliesError as e:
            print(f"STOPPED: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"ERROR: {e}")
            results.append(
                ScenarioResult(name=sc_path.name, status="ERROR", cost_line="COST: 0", details=str(e))
            )
        print()

    print("\n================== SUMMARY ==================")
    for r in results:
        print(f"{r.name:30} | {r.status:6} | {r.cost_line}")


if __name__ == "__main__":
    main()
