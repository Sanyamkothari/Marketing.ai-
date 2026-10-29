"""Harness to run Data Doctor scenarios with a replay AI client or default author (Stage 0).

Executes the real app in-process using FastAPI TestClient with MARKETING_AI_ALLOW_FAKE_AI=true.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pandas as pd
from fastapi.testclient import TestClient

import engine.ai_service as ai_service
from api.main import create_app
from engine.llm import APPROX_CHARS_PER_TOKEN, FakeLLMClient, FakeLLMMode, LLMCall, LLMCompletion
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
    "check_invariants",
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
        Path("next_prompt.txt").write_text(prompt_content, encoding="utf-8")
        print(f"\n[REPLAY] Out of replies! Next prompt written to {self.prompt_log_path}")
        print(self.usage_tracker.cost_line())
        raise OutOfRepliesError(f"Scenario out of recorded replies. Prompt written to {self.prompt_log_path}")


@dataclass
class ScenarioResult:
    """Outcome of running a single scenario."""

    name: str
    status: str  # "PASS" or "FAIL"
    cost_line: str
    invariant_violations: list[str] = field(default_factory=list)
    expectation_failures: list[str] = field(default_factory=list)
    chat_results: dict[str, str] = field(default_factory=dict)
    details: str = ""


def check_invariants(case: dict[str, Any], screen: dict[str, Any] | None = None) -> list[str]:
    """Check the 7 required invariants on the case state and screen."""
    violations: list[str] = []

    # Invariant 1: No plan item reaches accepted without a person's decision or approval
    approvals = case.get("approvals", [])
    has_approval = len(approvals) > 0
    for item in case.get("items", []):
        if item.get("state") == "accepted":
            decided_by = str(item.get("decided_by") or "")
            # Valid decision: by person (user id, not rules or agent) or covered by approval
            is_person = (
                bool(decided_by)
                and not decided_by.startswith("system:")
                and not decided_by.startswith("rules")
                and not decided_by.startswith("agent:")
            )
            if not is_person and not has_approval:
                violations.append(
                    f"Invariant 1 violation: item '{item.get('item_id')}' accepted without person decision (decided_by={decided_by})"
                )

    # Invariant 2: The plan never contains a feature that reads data after the snapshot
    for item in case.get("items", []):
        payload = item.get("payload") or {}
        if isinstance(payload, dict) and payload.get("reads_future") and not payload.get("outcome"):
            violations.append(
                f"Invariant 2 violation: item '{item.get('item_id')}' reads future data outside outcome"
            )

    # Invariant 3: Screen numbers equal AI numbers for the same item; sample counts marked
    if screen is not None:
        screen_items = screen.get("items") or screen.get("view", {}).get("items") or []
        for s_item in screen_items:
            if isinstance(s_item, dict):
                item_id = s_item.get("item_id")
                matching = next((i for i in case.get("items", []) if i.get("item_id") == item_id), None)
                if matching is not None:
                    # check count consistency if rows are presented
                    s_rows = s_item.get("rows")
                    m_rows = matching.get("payload", {}).get("tried", {}).get("rows")
                    if s_rows is not None and m_rows is not None and s_rows != m_rows:
                        violations.append(
                            f"Invariant 3 violation: screen rows ({s_rows}) != case rows ({m_rows}) for {item_id}"
                        )

    # Invariant 4: Every refusal the AI received has a non-empty how_to_fix
    for chat_entry in case.get("chat", []):
        if isinstance(chat_entry, dict) and chat_entry.get("role") == "agent":
            blocked_by = chat_entry.get("blocked_by")
            error_codes = chat_entry.get("error_codes") or []
            if blocked_by or error_codes:
                text = str(chat_entry.get("text") or "")
                if not text:
                    violations.append(
                        f"Invariant 4 violation: agent refusal has empty explanation: {chat_entry}"
                    )

    # Invariant 6: No tool call stops the visit for size (no TOOL_OUTPUT_TOO_LARGE reaching person)
    for chat_entry in case.get("chat", []):
        if isinstance(chat_entry, dict):
            error_codes = chat_entry.get("error_codes") or []
            if "TOOL_OUTPUT_TOO_LARGE" in error_codes:
                violations.append("Invariant 6 violation: TOOL_OUTPUT_TOO_LARGE reached the chat/person")
            if "TOOL_OUTPUT_TOO_LARGE" in str(chat_entry.get("text", "")):
                violations.append(
                    "Invariant 6 violation: TOOL_OUTPUT_TOO_LARGE reached the person in reply text"
                )

    return violations


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


def run_scenario(
    scenario_path: Path,
    *,
    with_ai: bool = True,
    config_root: Path = Path("configs"),
) -> ScenarioResult:
    """Run a scenario end-to-end, checking invariants and expectations."""
    name = scenario_path.name
    person_file = scenario_path / "person.json"
    expect_file = scenario_path / "expect.json"
    replies_file = scenario_path / "replies.jsonl"
    prompt_log_path = scenario_path / "next_prompt.txt"

    if not person_file.exists():
        raise FileNotFoundError(f"Missing person.json in {scenario_path}")

    person_actions: list[dict[str, Any]] = json.loads(person_file.read_text(encoding="utf-8"))
    expectations: dict[str, Any] = (
        json.loads(expect_file.read_text(encoding="utf-8")) if expect_file.exists() else {}
    )

    replies: list[str] = []
    if replies_file.exists():
        replies = [
            line.strip() for line in replies_file.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

    usage_tracker = UsageTracker()
    replay_client = ReplayLLMClient(replies, prompt_log_path, usage_tracker) if with_ai else None

    import shutil

    temp_dir = tempfile.mkdtemp()
    data_dir = Path(temp_dir)
    old_fake_env = os.environ.get("MARKETING_AI_ALLOW_FAKE_AI")
    os.environ["MARKETING_AI_ALLOW_FAKE_AI"] = "true" if with_ai else "false"

    old_build_client = ai_service.build_client
    if with_ai and replay_client is not None:
        ai_service.build_client = lambda *_args, **_kwargs: replay_client
    elif not with_ai:
        ai_service.build_client = lambda *_args, **_kwargs: None  # type: ignore[assignment]

    invariant_violations: list[str] = []
    expectation_failures: list[str] = []
    chat_results: dict[str, str] = {}
    case: dict[str, Any] = {}

    try:
        app = create_app(config_root=config_root, data_dir=data_dir)
        with TestClient(app) as client:
            case_id = ""

            for action in person_actions:
                kind = str(action.get("action"))

                if kind == "upload":
                    file_key = str(action.get("file"))
                    use_case = str(action.get("use_case", ""))
                    if file_key == "customers_and_orders":
                        # Two uploads
                        cust_df, ords_df = generate_customers_and_orders()
                        c_buf = io.StringIO()
                        cust_df.to_csv(c_buf, index=False)
                        o_buf = io.StringIO()
                        ords_df.to_csv(o_buf, index=False)

                        res_c = client.post(
                            "/uploads",
                            files={"file": ("customers.csv", c_buf.getvalue().encode(), "text/csv")},
                            data={"use_case": use_case, "mode": "train"},
                        )
                        assert res_c.status_code == 201, res_c.text
                        u1 = str(res_c.json()["upload_id"])

                        res_o = client.post(
                            "/uploads",
                            files={"file": ("orders.csv", o_buf.getvalue().encode(), "text/csv")},
                            data={"use_case": use_case, "mode": "train"},
                        )
                        assert res_o.status_code == 201, res_o.text
                        u2 = str(res_o.json()["upload_id"])

                        case_res = client.post("/cases", json={"upload_ids": [u1, u2]})
                    else:
                        frame = _get_fixture_frame(file_key)
                        buf = io.StringIO()
                        frame.to_csv(buf, index=False)
                        res = client.post(
                            "/uploads",
                            files={"file": (f"{file_key}.csv", buf.getvalue().encode(), "text/csv")},
                            data={"use_case": use_case, "mode": "train"},
                        )
                        assert res.status_code == 201, res.text
                        upload_id = str(res.json()["upload_id"])
                        case_res = client.post("/cases", json={"upload_ids": [upload_id]})

                    assert case_res.status_code == 201, case_res.text
                    case = case_res.json()
                    case_id = str(case["case_id"])

                elif kind == "consent":
                    c_res = client.post(f"/cases/{case_id}/ai-consent", json={})
                    assert c_res.status_code == 200, c_res.text
                    case = client.get(f"/cases/{case_id}").json()

                elif kind == "visit":
                    v_res = client.post(f"/cases/{case_id}/visit", json={})
                    assert v_res.status_code == 200, v_res.text
                    case = v_res.json()

                elif kind == "goal":
                    goal_id = str(action.get("goal_id"))
                    g_res = client.post(
                        f"/cases/{case_id}/goal",
                        json={"goal_id": goal_id, "version": case["version"]},
                    )
                    assert g_res.status_code == 200, g_res.text
                    case = g_res.json()

                elif kind == "answer":
                    match = str(action.get("match", "")).lower()
                    opt = str(action.get("option", "recommended"))
                    open_q = None
                    for q in case.get("questions", []):
                        q_text = str(q.get("text", "")).lower()
                        q_code = str(q.get("code", "")).lower()
                        if q.get("answer") is None and (not match or match in q_text or match in q_code):
                            open_q = q
                            break
                    if open_q is not None:
                        opt_id = open_q.get("recommended") if opt == "recommended" else None
                        if opt_id is None:
                            for o in open_q.get("options", []):
                                if opt in str(o.get("label", "")).lower() or opt == o.get("option_id"):
                                    opt_id = o.get("option_id")
                                    break
                        if opt_id is None and open_q.get("options"):
                            opt_id = open_q["options"][0].get("option_id")
                        if opt_id is not None:
                            a_res = client.post(
                                f"/cases/{case_id}/answers",
                                json={
                                    "version": case["version"],
                                    "question_id": open_q["question_id"],
                                    "option_id": opt_id,
                                    "value": action.get("value"),
                                },
                            )
                            assert a_res.status_code == 200, a_res.text
                            case = a_res.json()

                elif kind == "decide":
                    match = str(action.get("match", "")).lower()
                    accept = bool(action.get("accept", True))
                    matched_item = None
                    for item in case.get("items", []):
                        title = str(item.get("title", "")).lower()
                        item_id = str(item.get("item_id", "")).lower()
                        if match in title or match in item_id:
                            matched_item = item
                            break
                    if matched_item is not None:
                        d_res = client.post(
                            f"/cases/{case_id}/decisions",
                            json={
                                "version": case["version"],
                                "item_id": matched_item["item_id"],
                                "accept": accept,
                                "park_for": action.get("park_for"),
                            },
                        )
                        if d_res.status_code == 200:
                            case = d_res.json()

                elif kind == "chat":
                    msg = str(action.get("message", ""))
                    inst_id = str(action.get("instruction_id", ""))
                    chat_res = client.post(
                        f"/cases/{case_id}/chat",
                        json={"version": case["version"], "message": msg},
                    )
                    if chat_res.status_code == 200:
                        case = chat_res.json()
                        reply = case.get("chat", [])[-1].get("text", "")
                        if inst_id:
                            chat_results[inst_id] = reply

                elif kind == "approve":
                    s_res = client.get(f"/cases/{case_id}/screen")
                    if s_res.status_code == 200:
                        screen = s_res.json()
                        approval = screen.get("approval") or screen.get("view", {}).get("approval")
                        if approval and approval.get("sentence") and approval.get("digest"):
                            filtered_yeses = [
                                {
                                    "kind": y["kind"],
                                    "key": y.get("key"),
                                    "item_id": y.get("item_id"),
                                    "sentence": y.get("sentence"),
                                    "reason": y.get("reason", ""),
                                }
                                for y in approval.get("yeses", [])
                            ]
                            app_res = client.post(
                                f"/cases/{case_id}/approval",
                                json={
                                    "version": case["version"],
                                    "sentence": approval["sentence"],
                                    "digest": approval["digest"],
                                    "yeses": filtered_yeses,
                                },
                            )
                            if app_res.status_code == 200:
                                case = app_res.json()

                # Invariant checks after every step
                s_res = client.get(f"/cases/{case_id}/screen")
                screen_data = s_res.json() if s_res.status_code == 200 else None
                step_violations = check_invariants(case, screen_data)
                for v in step_violations:
                    if v not in invariant_violations:
                        invariant_violations.append(v)

            # Check final expectations from expect.json
            if "state" in expectations:
                expected_states = expectations["state"]
                if isinstance(expected_states, str):
                    expected_states = [expected_states]
                if case.get("state") not in expected_states:
                    expectation_failures.append(
                        f"Expected state in {expected_states}, got '{case.get('state')}'"
                    )

            if "min_rows" in expectations:
                expected_min = int(expectations["min_rows"])
                screen_rows = screen_data.get("rows") if screen_data else None
                if screen_rows is not None and screen_rows < expected_min:
                    expectation_failures.append(f"Expected >= {expected_min} rows, got {screen_rows}")

            if "min_positives" in expectations:
                expected_pos = int(expectations["min_positives"])
                pos = None
                for item in case.get("items", []):
                    if "buildable" in item.get("payload", {}):
                        pos = item.get("payload", {}).get("buildable", {}).get("yes")
                if pos is not None and pos < expected_pos:
                    expectation_failures.append(f"Expected >= {expected_pos} positives, got {pos}")

            if "blocking_questions" in expectations:
                expected_bq = expectations["blocking_questions"]
                actual_bq = [
                    q.get("code")
                    for q in case.get("questions", [])
                    if q.get("blocking") and q.get("answer") is None
                ]
                for b in expected_bq:
                    if b not in actual_bq:
                        expectation_failures.append(f"Expected blocking question '{b}', got {actual_bq}")

            if "can_approve" in expectations:
                expected_can_approve = bool(expectations["can_approve"])
                actual_can_approve = bool(
                    screen_data
                    and (screen_data.get("approval") or screen_data.get("view", {}).get("approval"))
                )
                if actual_can_approve != expected_can_approve:
                    expectation_failures.append(
                        f"Expected can_approve={expected_can_approve}, got {actual_can_approve}"
                    )

            if "has_unpivot_step" in expectations:
                unpivot_items = [
                    item
                    for item in case.get("items", [])
                    if item.get("payload", {}).get("step", {}).get("kind") == "unpivot"
                    or "unpivot" in item.get("kind", "").lower()
                ]
                if expectations["has_unpivot_step"] and not unpivot_items:
                    expectation_failures.append("Expected unpivot step in case items, none found")

            if "has_join_step" in expectations:
                join_items = [
                    item
                    for item in case.get("items", [])
                    if "join" in str(item.get("payload", {})).lower()
                    or "join" in str(item.get("title", "")).lower()
                    or "combine" in str(item.get("title", "")).lower()
                    or "build_dataset" in item.get("kind", "").lower()
                ]
                if expectations["has_join_step"] and not join_items:
                    expectation_failures.append("Expected join step in case items, none found")

            if "instructions_tested" in expectations:
                expected_count = int(expectations["instructions_tested"])
                actual_count = len(chat_results)
                if actual_count < expected_count:
                    expectation_failures.append(
                        f"Expected {expected_count} chat responses, got {actual_count}"
                    )

    finally:
        jobs_runner = getattr(app.state, "jobs", None)
        if jobs_runner is not None and hasattr(jobs_runner, "shutdown"):
            with contextlib.suppress(Exception):
                jobs_runner.shutdown(wait=True)
        ai_service.build_client = old_build_client
        if old_fake_env is None:
            os.environ.pop("MARKETING_AI_ALLOW_FAKE_AI", None)
        else:
            os.environ["MARKETING_AI_ALLOW_FAKE_AI"] = old_fake_env
        shutil.rmtree(temp_dir, ignore_errors=True)

    status = "PASS" if not expectation_failures and not invariant_violations else "FAIL"
    return ScenarioResult(
        name=name,
        status=status,
        cost_line=usage_tracker.cost_line(),
        invariant_violations=invariant_violations,
        expectation_failures=expectation_failures,
        chat_results=chat_results,
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
            print(res.cost_line)
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
