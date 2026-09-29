"""The canary test (Plan G §7.6): fake personal data planted in every kind of cell and header, whole
chat sessions through the real `chat_turn`, and a search of every prompt and everything stored for it.

What it proves: no value a pattern can recognise reaches any prompt, any stored `ChatMessage` or any
`sent` preview, in either mode, for every tool in the registry - and a tool nobody has written yet
is masked too. What it cannot prove: that a name inside a sentence in a column nobody marked is
hidden in `masked_data` mode (`test_masked_data_cannot_hide_a_name_in_a_sentence` says so on purpose).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pandas as pd
import pytest

from engine.agent.egress import (
    EGRESS_LATE_MASK,
    HIDDEN_BY_SETTINGS,
    MAX_SENT_ITEMS,
    MAX_SENT_SESSION_CHARS,
    SAFE_KEYS,
    SAFE_LITERALS,
    SHAPE_ALPHABET,
    STATE_TOOL,
    Egress,
    alias_table,
    unalias,
)
from engine.agent.loop import TurnResult, chat_turn
from engine.agent.session import start_session
from engine.agent.tools import TOOLS, AgentContext, NoArgs, Tool, ToolKind, call_tool
from engine.llm import FakeLLMClient, FakeLLMMode
from engine.pii import REDACTION_MARKER_PATTERN
from tests.unit.agent.egress_canary import (
    ADDRESS,
    CARD,
    CELL_WORDS,
    HARD_SECRETS,
    HEADER_EMAIL,
    HEADER_NUMBER,
    HEADER_PHONE,
    HIDE_ALL_TEXT,
    IP,
    NAME,
    ToolingClient,
    after,
    canary_context,
    every_call,
    guardrails,
    leaks,
    meter_for,
    run_session,
    stored_text,
    stray_keys,
)

PERSONAL_VALUES = (
    "Joe Bloggs",
    "Ann Lee",
    "PQRST5678U",
    "LMNOP4321Z",
    "ann.lee",
    "91234 56789",
    "99887 76655",
)
"""Values of the columns the profile marks as personal data: never shown, in any mode."""


def _codes(turn: TurnResult) -> tuple[str, ...]:
    return turn.reply.turn.error_codes if turn.reply.turn is not None else ()


@dataclass
class Run:
    ctx: AgentContext
    client: ToolingClient
    session: Any
    turns: list[TurnResult]
    planned: int

    @property
    def prompts(self) -> str:
        return "\n".join(self.client.sent_text)

    @property
    def errors(self) -> list[str]:
        return [code for turn in self.turns for code in _codes(turn)]


def _run(mode: str, always_hide: tuple[str, ...] = ()) -> Run:
    ctx = canary_context(mode=mode, always_hide=always_hide)
    columns = [str(c) for c in ctx.frame.columns]
    actions = every_call(columns)
    client = ToolingClient(actions)
    session, turns = run_session(ctx, client)
    return Run(ctx, client, session, turns, planned=len(actions))


@pytest.fixture(scope="module")
def masked() -> Run:
    return _run("masked_data")


@pytest.fixture(scope="module")
def hidden() -> Run:
    return _run("masked_data", HIDE_ALL_TEXT)


@pytest.fixture(scope="module")
def summaries() -> Run:
    return _run("summaries_only")


@pytest.fixture(params=["masked", "hidden", "summaries"])
def any_run(request: pytest.FixtureRequest) -> Run:
    run: Run = request.getfixturevalue(request.param)
    return run


# ---------------------------------------------------------------------------
# The runs themselves were real
# ---------------------------------------------------------------------------
def test_every_read_tool_was_called_on_every_column_and_answered(any_run: Run) -> None:
    called = {r.tool for r in any_run.session.tool_results}
    expected = {t.name for t in TOOLS.values() if t.kind is ToolKind.READ}
    assert expected <= called, expected - called
    assert all(turn.blocked_by is None for turn in any_run.turns), [t.blocked_by for t in any_run.turns]
    assert any_run.planned >= 90
    assert len(any_run.client.calls) > 100  # every step of every turn was a prompt to the model


def test_the_canary_frame_really_holds_the_secrets(masked: Run) -> None:
    frame = masked.ctx.frame
    text = frame.to_csv(index=False) + ",".join(map(str, frame.columns))
    for secret in (HARD_SECRETS[0], HARD_SECRETS[2], CARD, IP, NAME, ADDRESS, HEADER_EMAIL, HEADER_NUMBER):
        assert secret in text, secret
    assert HEADER_EMAIL in frame.columns and HEADER_NUMBER in frame.columns and HEADER_PHONE in frame.columns


# ---------------------------------------------------------------------------
# Nothing a pattern can recognise leaves, in any mode
# ---------------------------------------------------------------------------
def test_no_planted_secret_in_any_prompt(any_run: Run) -> None:
    assert leaks(any_run.prompts, HARD_SECRETS) == []


def test_no_planted_secret_in_any_stored_message_or_sent_preview(any_run: Run) -> None:
    stored = stored_text(any_run.session)
    assert leaks(stored, HARD_SECRETS) == []
    previews = "\n".join(
        item.preview + json.dumps(item.args) for m in any_run.session.transcript for item in m.sent
    )
    assert leaks(previews, HARD_SECRETS) == []
    assert previews  # there was something sent to check


def test_personal_data_columns_never_yield_a_value_in_any_mode(any_run: Run) -> None:
    assert leaks(any_run.prompts, PERSONAL_VALUES, exact=True) == []
    assert leaks(stored_text(any_run.session), PERSONAL_VALUES, exact=True) == []


def test_the_last_check_had_nothing_left_to_do(any_run: Run) -> None:
    """The gate did the work; `assert_clean` is only the backstop (it never fired on the canary)."""
    assert EGRESS_LATE_MASK not in any_run.errors


def test_a_header_that_is_personal_data_is_shown_as_an_alias(any_run: Run) -> None:
    columns = [str(c) for c in any_run.ctx.frame.columns]
    table = alias_table(columns)
    assert {HEADER_EMAIL, HEADER_NUMBER, HEADER_PHONE, str(any_run.ctx.frame.columns[0]).upper()} & set(
        table
    ) >= {
        HEADER_EMAIL,
        HEADER_NUMBER,
        HEADER_PHONE,
    }
    for header in (HEADER_EMAIL, HEADER_NUMBER, HEADER_PHONE):
        assert table[header] in any_run.prompts
    assert "column_" in any_run.prompts


# ---------------------------------------------------------------------------
# What only hiding a column or `summaries_only` can keep out
# ---------------------------------------------------------------------------
def test_names_and_addresses_stay_out_when_the_column_is_hidden(hidden: Run) -> None:
    assert leaks(hidden.prompts, CELL_WORDS, exact=True) == []
    assert leaks(stored_text(hidden.session), CELL_WORDS, exact=True) == []
    assert HIDDEN_BY_SETTINGS in hidden.prompts


def test_names_and_addresses_stay_out_in_summaries_only(summaries: Run) -> None:
    assert leaks(summaries.prompts, CELL_WORDS, exact=True) == []
    assert leaks(stored_text(summaries.session), CELL_WORDS, exact=True) == []


def test_masked_data_cannot_hide_a_name_in_a_sentence(masked: Run) -> None:
    """The honest limit (docs/AGENTS.md §7.7): no pattern knows a name, so `masked_data` shows it.

    This test exists so the documentation stays true: if it starts failing, masking got better and
    the note can be shortened. `always_hide_columns` and `summaries_only` are the two ways to keep it out."""
    assert NAME in masked.prompts and "Baker" in masked.prompts


def test_summaries_only_prompts_hold_only_shapes_for_cell_text(summaries: Run) -> None:
    """Every string that came from a cell is a shape; only labels under safe keys, known column
    names, aliases and fixed literals stay as they are."""
    columns = [str(c) for c in summaries.ctx.frame.columns]
    labels = set(columns) | set(alias_table(columns).values()) | SAFE_LITERALS
    wrapper = {"evidence_id", "tool", "error"}
    seen = 0

    def walk(value: Any, key: str) -> None:
        nonlocal seen
        if isinstance(value, dict):
            for inner_key, inner in value.items():
                walk(inner, inner_key)
        elif isinstance(value, list):
            for inner in value:
                walk(inner, key)
        elif isinstance(value, str):
            seen += 1
            if key in SAFE_KEYS | wrapper or value in labels or REDACTION_MARKER_PATTERN.fullmatch(value):
                return
            assert SHAPE_ALPHABET.fullmatch(value), f"{key!r}: {value!r} is not a shape"

    for call in summaries.client.calls:
        section = (
            call.prompt.split("Tool results so far in this turn:", 1)[1]
            .split("The person says:", 1)[0]
            .strip()
        )
        if section != "none":
            walk(json.loads(section), "")
    assert seen > 1_000


def test_summaries_only_keeps_counts_and_formats_the_model_needs(summaries: Run) -> None:
    inspected = next(
        r
        for r in summaries.session.tool_results
        if r.tool == "inspect_column" and r.args["column"] == "notes"
    )
    assert inspected.result["distinct"] == 6  # the raw evidence is unchanged
    prompt_side = [
        c.prompt
        for c in summaries.client.calls
        if '"column": "notes"' in c.prompt and '"examples"' in c.prompt
    ]
    assert prompt_side, "the model saw the notes column"
    assert (
        "Aaaa Aaa aaaa" in prompt_side[0] or "Aaaaa" in prompt_side[0]
    )  # the shape of the text, not the text


def test_always_hide_gives_counts_only_for_a_numeric_column() -> None:
    ctx = canary_context(always_hide=("monthly_spend",))
    client = ToolingClient([{"action": "inspect_column", "args": {"column": "monthly_spend"}}], per_turn=1)
    _, turns = run_session(ctx, client)
    seen = json.loads(
        client.calls[1]
        .prompt.split("Tool results so far in this turn:", 1)[1]
        .split("The person says:", 1)[0]
    )
    result = seen[0]["result"]
    # A hidden column is "personal" for the tool itself (`tools._personal`): no minimum, maximum, mean or
    # example is even computed, so the stored evidence holds none either.
    assert not {"minimum", "maximum", "mean"} & set(result)
    assert result["examples"] == [] and result["top_values"] == []
    assert result["distinct"] > 100 and result["rows"] == 1_500
    assert turns[0].blocked_by is None


def test_always_hide_is_case_insensitive() -> None:
    ctx = canary_context(always_hide=("NOTES",))
    client = ToolingClient([{"action": "inspect_column", "args": {"column": "notes"}}], per_turn=1)
    run_session(ctx, client)
    text = "\n".join(client.sent_text)
    assert "Please email" not in text and "Logged in from" not in text  # only the notes column holds these
    assert "hidden by your settings" in text  # the tool's own message says why nothing is listed


# ---------------------------------------------------------------------------
# The record of what was sent
# ---------------------------------------------------------------------------
def test_each_reply_records_exactly_what_went_into_its_prompts(any_run: Run) -> None:
    sent_total = 0
    for turn in any_run.turns:
        sent = turn.reply.sent
        assert 0 < len(sent) <= MAX_SENT_ITEMS
        # Every turn tells the person about the helper's own suggestions once, first: they quote cells too.
        assert sent[0].tool == STATE_TOOL and [i.tool for i in sent].count(STATE_TOOL) == 1
        sent_total += len(sent) - 1
        for item in sent:
            assert item.mode is any_run.ctx.config.agent.ai_data_access
            assert len(item.preview) <= 1_500 and item.chars >= len(item.preview)
            assert item.tool in TOOLS or item.tool == STATE_TOOL
            if item.chars <= 1_500:  # a whole payload appears verbatim in a prompt
                assert item.preview in any_run.prompts
    assert (
        sent_total == sum(len(t.tool_results) for t in any_run.turns) == any_run.planned
    )  # every chat result that went in a prompt


def test_a_session_keeps_at_most_twenty_kilobytes_of_previews(any_run: Run) -> None:
    kept = [
        i.preview for m in any_run.session.transcript for i in m.sent if not i.preview.startswith("[not kept")
    ]
    assert sum(map(len, kept)) <= MAX_SENT_SESSION_CHARS
    assert (
        sum(len(m.sent) for m in any_run.session.transcript) > len(kept) > 0
    )  # older ones were dropped, newest kept


def test_a_user_message_has_no_sent_record(masked: Run) -> None:
    users = [m for m in masked.session.transcript if m.role.value == "user"]
    assert users and all(m.sent == () for m in users)


# ---------------------------------------------------------------------------
# Default-deny: a tool nobody has written yet, and a gate that is bypassed
# ---------------------------------------------------------------------------
def _weird_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(ctx: AgentContext, args: NoArgs) -> dict[str, Any]:
        del ctx, args
        return {"weird_key": f"{NAME}, {CARD}, {IP}", "nested": [{"another": "call 9876543210"}], "rows": 3}

    tools = dict(TOOLS)
    tools["weird_tool"] = Tool("weird_tool", "A tool added later.", ToolKind.READ, NoArgs, run)
    monkeypatch.setattr("engine.agent.tools.TOOLS", tools)
    monkeypatch.setattr("engine.agent.loop.TOOLS", tools)


def _weird_run(monkeypatch: pytest.MonkeyPatch, mode: str) -> tuple[ToolingClient, list[TurnResult]]:
    _weird_tool(monkeypatch)
    ctx = canary_context(mode=mode, rows=1_200)
    client = ToolingClient([{"action": "weird_tool", "args": {}}], per_turn=1)
    _, turns = run_session(ctx, client)
    return client, turns


def test_a_new_tool_returning_a_raw_cell_under_a_new_key_is_masked_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, turns = _weird_run(monkeypatch, "masked_data")
    text = "\n".join(client.sent_text)
    assert "weird_key" in text
    assert CARD not in text and IP not in text and "9876543210" not in text
    assert "[REDACTED:card]" in text and "[REDACTED:ip]" in text
    assert EGRESS_LATE_MASK not in _codes(turns[0])  # the gate masked it, not the backstop
    assert [item.tool for item in turns[0].reply.sent] == [STATE_TOOL, "weird_tool"]


def test_a_new_tool_returning_a_raw_cell_is_a_shape_in_summaries_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _weird_run(monkeypatch, "summaries_only")
    text = "\n".join(client.sent_text)
    assert NAME not in text and "Roe" not in text and "4111" not in text
    assert "Aaaa Aaa, 9999 9999" in text


def test_no_dict_key_in_any_tool_result_is_a_cell(any_run: Run) -> None:
    """The gate reads a dict key as a label, so a tool must never key a dict by cell values: every key of
    every result of every tool, on every column, is a field name of ours (`egress.RESULT_KEYS`) or a column.
    A tool with a new field name adds it there on purpose (and gets the review that goes with it)."""
    columns = [str(c) for c in any_run.ctx.frame.columns]
    stray = {key for result in any_run.session.tool_results for key in stray_keys(result.result, columns)}
    assert stray == set(), f"a tool result has a dict key that is not a field name: {sorted(stray)}"


def test_no_dict_key_of_a_format_issue_is_a_spelling_found_in_the_file() -> None:
    frame = pd.DataFrame({"account_holder": ["zelda", "Zelda", "Zelda", "quill roe", "Quill Roe"] * 60})
    ctx = canary_context(frame=frame)
    result = call_tool(ctx, "find_format_issues", {}, evidence_id="e1").result
    assert result["issues"] and stray_keys(result, ["account_holder"]) == []


def test_a_new_tool_returning_a_dict_keyed_by_cells_is_caught_and_still_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(ctx: AgentContext, args: NoArgs) -> dict[str, Any]:
        del ctx, args
        return {"counts": {"zelda": 3, "quill roe": 2}, "rows": 5}

    tools = dict(TOOLS)
    tools["keyed_tool"] = Tool("keyed_tool", "A tool added later.", ToolKind.READ, NoArgs, run)
    monkeypatch.setattr("engine.agent.tools.TOOLS", tools)
    monkeypatch.setattr("engine.agent.loop.TOOLS", tools)
    ctx = canary_context(mode="summaries_only", rows=1_200)
    client = ToolingClient([{"action": "keyed_tool", "args": {}}], per_turn=1)
    session, _ = run_session(ctx, client)
    keyed = next(r for r in session.tool_results if r.tool == "keyed_tool")
    assert {"zelda", "quill roe"} <= set(stray_keys(keyed.result)), "the canary check does not see the cells"
    text = "\n".join(client.sent_text)
    assert "zelda" not in text and "quill" not in text, "the gate sent a key that is a cell"


def test_the_last_check_masks_what_a_broken_gate_lets_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break the gate on purpose (every string passes as a label): `assert_clean` still masks the prompt."""
    monkeypatch.setattr(Egress, "_string", lambda self, value, key, hidden, extra: value)
    client, turns = _weird_run(monkeypatch, "masked_data")
    text = "\n".join(client.sent_text)
    assert CARD not in text and IP not in text and "9876543210" not in text
    assert EGRESS_LATE_MASK in _codes(turns[0])
    assert _codes(turns[0]).count(EGRESS_LATE_MASK) >= 1


def test_the_persons_message_is_masked_with_every_scanner_before_it_is_sent() -> None:
    ctx = canary_context(rows=1_200)
    client = ToolingClient([])
    session = start_session(ctx, session_id="s1")
    message = f"My card is {CARD} and my server is {IP}, see {HEADER_EMAIL}"
    turn = chat_turn(ctx, session, message, meter=meter_for(client), guardrails=guardrails())
    text = "\n".join(client.sent_text)
    assert CARD not in text and IP not in text and HEADER_EMAIL not in text
    assert "[REDACTED:card]" in text
    assert turn.blocked_by is None


# ---------------------------------------------------------------------------
# Replies show the person the real names again
# ---------------------------------------------------------------------------
def test_a_reply_that_uses_an_alias_shows_the_real_name_only_when_it_is_clean() -> None:
    columns = [*canary_context(rows=1_200).frame.columns, "Monthly spend (INR)\u200b?", "x" * 70]
    table = alias_table([str(c) for c in columns])
    reply = f"Look at {table[HEADER_EMAIL]}, {table[HEADER_NUMBER]}, {table['Monthly spend (INR)' + chr(0x200b) + '?']}."
    shown = unalias(reply, table)
    assert HEADER_EMAIL not in shown and HEADER_NUMBER not in shown  # personal-looking names stay aliases
    assert "Monthly spend (INR)?" in shown  # a clean but unusual name comes back, cleaned


class _AliasReplyClient(FakeLLMClient):
    def __init__(self, alias: str) -> None:
        super().__init__(mode=FakeLLMMode.GROUNDED)
        self.alias = alias
        self.step = 0

    def _body(self, system: str, user: str) -> str:
        self.step += 1
        if self.step == 1:
            return json.dumps({"action": "inspect_column", "args": {"column": self.alias}})
        return json.dumps(
            {"action": "reply", "text": f"'{self.alias}' has 5 kinds of values.", "evidence_ids": ["c1-e1"]}
        )


def test_the_model_can_use_an_alias_as_an_argument_and_the_tool_gets_the_real_name() -> None:
    ctx = canary_context(rows=1_200)
    alias = alias_table([str(c) for c in ctx.frame.columns])[HEADER_NUMBER]
    client = _AliasReplyClient(alias)
    session = start_session(ctx, session_id="s1")
    turn = chat_turn(
        ctx, session, "What is in that column?", meter=meter_for(client), guardrails=guardrails()
    )
    assert turn.tool_results[0].args == {"column": HEADER_NUMBER}  # the real column, resolved from the alias
    assert turn.tool_results[0].result["column"] == HEADER_NUMBER
    assert HEADER_NUMBER not in "\n".join(c.prompt + c.system for c in client.calls)
    assert turn.blocked_by is None
    assert (
        alias in turn.reply.text and HEADER_NUMBER not in turn.reply.text
    )  # a 12-digit name is not restored


# ---------------------------------------------------------------------------
# The vanilla fake, walking columns the way a person would ask
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["masked_data", "summaries_only"])
def test_the_ordinary_fake_asking_about_each_column_leaks_nothing(mode: str) -> None:
    ctx = canary_context(mode=mode)
    client = FakeLLMClient(mode=FakeLLMMode.GROUNDED)
    session = start_session(ctx, session_id="s1")
    meter = meter_for(client)
    asked = 0
    for column in ctx.frame.columns:
        name = str(column)
        if not all(ch.isalnum() or ch in "_ .-" for ch in name) or sum(ch.isdigit() for ch in name) >= 7:
            continue  # the fake can only quote simple names, and a long number in a message is masked
        turn = chat_turn(ctx, session, f"What about '{name}'?", meter=meter, guardrails=guardrails())
        session = after(session, f"What about '{name}'?", turn)
        asked += 1
        assert turn.tool_results, name
    assert asked >= 18
    text = "\n".join(f"{c.system}\n{c.prompt}" for c in client.calls)
    assert leaks(text, HARD_SECRETS) == []
    assert leaks(stored_text(session), HARD_SECRETS) == []
    if mode == "summaries_only":
        assert leaks(text, CELL_WORDS, exact=True) == []
