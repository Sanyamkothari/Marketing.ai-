"""The Data Doctor's prompt and the code agree: every action and argument the prompt names exists, every action the
dispatch handles is in the registry, and every number the prompt states is a limit (`data_doctor.limits`)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from engine.agent.loop import DOCTOR_STEP_KINDS
from engine.doctor import actions
from engine.doctor.actions import ACTIONS, ALIASES, argument_names, prompt_variables
from engine.doctor.agent_turn import SQL_ARGS
from engine.doctor.config import AsksLimits, DataDoctorConfig, DataDoctorLimits, GateLimits, TurnLimits
from engine.doctor.limits import limits, prompt_limits, use_limits
from engine.doctor.questions import catalogue_lines
from engine.doctor.tools_sql import ProposeSqlStepArgs
from engine.generative.prompts import load_prompt, render

NAME = "data_doctor"
ROOT = Path(__file__).resolve().parents[3]
LOOK = "<look tool>"


def _render() -> str:
    values = {
        "goal": "g",
        "tools": [],
        "questions": catalogue_lines(chat=True),
        "state": "{}",
        "conversation": "c",
        "event": "THE PERSON SAYS:\nm",
        "turn_results": "none",
        **prompt_variables(),
    }
    done = render(load_prompt(NAME), values)
    return done.system


def _known() -> set[str]:
    return {a.name for a in ACTIONS} | set(ALIASES)


def test_every_action_the_prompt_names_is_in_the_registry() -> None:
    text = _render()
    named = set(re.findall(r'"action": "([^"]+)"', text))
    assert named - {LOOK} <= _known(), named - _known()
    offered = {a.name for a in ACTIONS}
    shown = {ALIASES.get(n, n) for n in named}
    assert offered <= shown, f"the prompt does not offer {offered - shown}"
    assert shown - {ALIASES.get(LOOK, LOOK)} <= {a.name for a in ACTIONS} | {LOOK}


def test_the_aliases_are_the_names_older_turns_used() -> None:
    assert ALIASES == {"ask_question": "ask", "finish": "reply"}
    assert all(target in {a.name for a in ACTIONS} for target in ALIASES.values())


def test_every_argument_the_prompt_lists_is_a_field_of_that_action() -> None:
    text = _render()
    checked = 0
    for line in text.splitlines():
        found = re.match(r'- `\{"action": "([^"]+)"', line)
        if found is None or found.group(1) == LOOK or " Args: " not in line:
            continue
        action = found.group(1)
        listed = re.findall(r"`([a-z_]+)\??`", line.split(" Args: ", 1)[1])
        assert listed and set(listed) <= argument_names(action), (
            action,
            set(listed) - argument_names(action),
        )
        checked += 1
    assert checked >= 5


def test_every_step_kind_the_prompt_lists_is_one_the_turn_may_propose() -> None:
    lines = prompt_variables()["step_kinds"]
    assert {line.split(" ")[0] for line in lines} == {k.value for k in DOCTOR_STEP_KINDS}


def _compared_names(path: Path, function: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            for compare in ast.walk(node):
                if (
                    isinstance(compare, ast.Compare)
                    and isinstance(compare.left, ast.Name)
                    and compare.left.id == "name"
                ):
                    for part in ast.walk(compare):
                        if isinstance(part, ast.Constant) and isinstance(part.value, str):
                            names.add(part.value)
    return names


def test_every_action_the_dispatch_handles_is_in_the_registry() -> None:
    handled = _compared_names(ROOT / "engine" / "doctor" / "agent_turn.py", "_act")
    assert handled, "the dispatch compares no action name; this test no longer reads it"
    assert handled <= _known() | {"find_values"}, handled - _known()


def test_the_registry_offers_what_the_dispatch_takes() -> None:
    taken = _compared_names(ROOT / "engine" / "doctor" / "agent_turn.py", "_act")
    for spec in ACTIONS:
        if spec.name == "reply":  # the loop takes it before `_act`: it ends the turn
            continue
        assert spec.name in taken, spec.name


def test_the_sql_arguments_are_the_tools_arguments() -> None:
    fields = set(actions.ProposeSqlArgs.model_fields)
    assert fields >= SQL_ARGS
    tool = set(ProposeSqlStepArgs.model_fields) - {"upload_ids", "as_of_upload_id"}
    assert tool <= fields, tool - fields
    assert {"files", "as_of_file", "expect"} <= fields


def test_the_prompt_states_no_number_of_its_own() -> None:
    raw = load_prompt(NAME).system
    # The data rules (R-numbers) quote the data's own numbers; what is checked is the words of a limit.
    pattern = (
        r"(at most|up to|no more than)\s+\d+\b|\b\d+\s+blocking|\b\d+ to \d+ options|fails twice|twice\b"
    )
    assert not re.findall(pattern, raw, flags=re.IGNORECASE)


def test_the_defaults_are_the_values_the_code_used_to_hard_code() -> None:
    found = DataDoctorLimits()
    assert (found.asks.blocking, found.asks.doubts, found.asks.requests, found.asks.quick) == (3, 5, 3, 2)
    assert (found.asks.quick_options_min, found.asks.quick_options_max, found.asks.request_columns) == (
        2,
        4,
        8,
    )
    assert (found.turn.strikes, found.turn.review_steps, found.turn.history_turns) == (2, 12, 12)
    assert (found.turn.chat_reply_chars, found.turn.guided_reply_chars) == (2000, 800)
    assert (found.turn.summary_chars, found.turn.effect_chars, found.turn.prompt_chars) == (800, 160, 60_000)
    assert (found.trial.sample_rows, found.trial.dry_run_rows) == (1_000, 100_000)
    assert (found.gates.retries, found.gates.loss_stop_pct, found.gates.rate_stop_points) == (2, 5.0, 1.0)
    assert (found.gates.approve_drop_share, found.gates.change_warn_pct, found.gates.change_refuse_pct) == (
        0.02,
        30.0,
        60.0,
    )
    assert DataDoctorConfig().ai_limits.max_chat_steps == 8


def test_the_engine_config_carries_the_defaults() -> None:
    assert limits() == DataDoctorLimits()


def test_the_dump_of_a_default_configuration_is_unchanged() -> None:
    assert "limits" not in DataDoctorConfig().model_dump(mode="json", exclude_defaults=False)


def test_the_guided_setup_replies_use_the_same_default() -> None:
    from engine.agent.loop import MAX_PROMPT_CHARS, MAX_REPLY_CHARS

    assert (TurnLimits().guided_reply_chars, TurnLimits().prompt_chars) == (MAX_REPLY_CHARS, MAX_PROMPT_CHARS)


def test_every_limit_the_prompt_states_is_the_one_in_the_limits() -> None:
    """Change a limit and the prompt follows it: the number in the prompt is read, not copied."""
    numbers = {
        "retries": 7,
        "blocking": 9,
        "strikes": 7,
        "quick": 5,
        "quick_options_min": 3,
        "quick_options_max": 6,
        "chat_reply_chars": 3333,
    }
    replacement = DataDoctorLimits(
        turn=TurnLimits(strikes=7, summary_chars=1234, chat_reply_chars=3333),
        asks=AsksLimits(blocking=9, quick=5, quick_options_min=3, quick_options_max=6),
        gates=GateLimits(retries=7),
    )
    with use_limits(replacement):
        assert {k: prompt_limits()[k] for k in numbers} == numbers
        text = _render()
    for value in numbers.values():
        assert str(value) in text, value
    default = _render()
    assert default != text


# ---------------------------------------------------------------------------
# One source for every limit; the handlers read what the registry lists
# ---------------------------------------------------------------------------
def test_the_gates_and_trials_are_engine_wide_so_a_prompt_states_what_the_gates_enforce() -> None:
    engine = limits()
    case = DataDoctorConfig(
        limits=DataDoctorLimits(
            gates=GateLimits(retries=0, loss_stop_pct=1.0), trial={"dry_run_rows": 5, "sample_rows": 7}
        ),
    )
    found = limits(case)
    assert found.gates == engine.gates and found.trial == engine.trial
    assert prompt_limits(case)["retries"] == engine.gates.retries == limits().gates.retries


def test_a_case_still_sets_what_the_model_reads_and_asks() -> None:
    case = DataDoctorConfig(limits=DataDoctorLimits(turn=TurnLimits(strikes=5), asks=AsksLimits(blocking=1)))
    assert (limits(case).turn.strikes, limits(case).asks.blocking) == (5, 1)
    assert prompt_limits(case)["strikes"] == 5


def test_a_gate_may_only_be_tightened() -> None:
    for field, loose in (
        ("loss_stop_pct", 6.0),
        ("rate_stop_points", 2.0),
        ("approve_drop_share", 0.5),
        ("change_refuse_pct", 61.0),
    ):
        with pytest.raises(ValueError):
            GateLimits(**{field: loose})
    assert GateLimits(loss_stop_pct=1.0, approve_drop_share=0.0).loss_stop_pct == 1.0


@pytest.mark.parametrize("field", ["history_turns", "state_doubts", "reloaded_looks"])
def test_what_the_model_reads_cannot_be_zero_because_zero_would_slice_everything(field: str) -> None:
    with pytest.raises(ValueError):
        TurnLimits(**{field: 0})


def _read_keys(module: str, function: str) -> set[str]:
    tree = ast.parse((ROOT / "engine" / "doctor" / f"{module}.py").read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == function):
            continue
        for part in ast.walk(node):
            call = (
                isinstance(part, ast.Call)
                and isinstance(part.func, ast.Attribute)
                and part.func.attr == "get"
            )
            target = part.func.value if call else getattr(part, "value", None)  # type: ignore[union-attr]
            if not (isinstance(target, ast.Name) and target.id == "args"):
                continue
            key = part.args[0] if call else getattr(part, "slice", None)  # type: ignore[union-attr]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
    return keys


def _fields(model: type) -> set[str]:
    return set(model.model_fields)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("module", "function", "model", "all_read"),
    [
        ("agent_turn", "_guide", actions.ReadGuideArgs, True),
        ("agent_turn", "_sql", actions.ProposeSqlArgs, False),
        ("agent_turn", "_catalogue_ask", actions.AskArgs, False),
        ("agent_turn", "_quick", actions.AskArgs, False),
        ("agent_turn", "_doubt", actions.ExpressDoubtArgs, False),
        ("agent_turn", "_request", actions.RequestDataArgs, False),
        ("agent_turn", "_suggest", actions.SuggestAnswerArgs, True),
        ("agent_turn", "_withdraw", actions.WithdrawArgs, True),
        ("agent_turn", "_setting", actions.ProposeSettingArgs, True),
        ("agent_turn", "_adopt", actions.AdoptSuggestionArgs, True),
        ("agent_turn", "_combine", actions.CombineArgs, False),
    ],
)
def test_a_handler_reads_only_keys_the_registry_lists(
    module: str, function: str, model: type, all_read: bool
) -> None:
    read = _read_keys(module, function)
    assert read, f"{module}.{function} reads no argument; this test no longer reads it"
    read -= {"reason"} if function == "_sql" else set()  # an older name of `description`, still accepted
    assert read <= _fields(model), (function, read - _fields(model))
    if all_read:
        assert read == _fields(model), (function, _fields(model) - read)


def test_the_rendered_system_text_is_identical_whatever_the_event_and_the_turn_results_are() -> None:
    """Prompt caching: instructions, the action contract and the guide index come first and do not move."""

    def system(results: str, state: str, event: str = "an event") -> str:
        values = {
            "goal": "g",
            "tools": [],
            "questions": catalogue_lines(chat=True),
            "state": state,
            "conversation": "c",
            "event": event,
            "turn_results": results,
            **prompt_variables(),
        }
        done = render(load_prompt(NAME), values)
        assert state in done.user and results in done.user and event in done.user
        return done.system

    assert system("none", "{}") == system(
        '[{"tool": "read_guide", "text": ["x"]}]', '{"files": ["F1"]}', "THE PERSON SAYS:\nhello"
    )
    assert "## Guides" in system("none", "{}") and "- features:" in system("none", "{}")


def test_the_prompt_offers_read_guide_and_names_the_guides_to_read() -> None:
    text = _render()
    assert '"action": "read_guide"' in text and "`read_guide`" in text
    assert "features" in text and "time_and_as_of" in text and "`rules`" in text
