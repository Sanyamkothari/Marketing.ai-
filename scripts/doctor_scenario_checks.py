"""What a Data Doctor scenario must show: the seven invariants and the expectations of `expect.json`.

Pure functions over the JSON the app serves (`/cases/{id}` and `/cases/{id}/screen`), so each check can be tested on
its own with a small made-up case. `scripts.doctor_scenarios` plays the person and calls them after every step.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any, Final

import pandas as pd

from engine.agent.sql_step import SqlStepError, resolve_as_of_rule, run_sql, sql_params
from engine.stages.validate import CheckParams, check_leakage_suspected

__all__ = [
    "INVARIANTS",
    "check_invariants",
    "evaluate",
    "plan_fingerprint",
    "plan_kind",
    "same_plan_without_ai",
]

INVARIANTS: Final[tuple[str, ...]] = (
    "accepted_not_by_ai",
    "accepted_needs_person",
    "no_leak",
    "screen_matches_ai",
    "refusals_explain",
    "no_silent_ignore",
    "no_size_stop",
    "same_plan_without_ai",
)
"""The seven invariants of the brief (Stage 0, item 5), in its order. The first is two checks: nothing is accepted by
an AI (must hold today) and nothing is accepted by the rules without a person (holds from Stage 2)."""

_AI: Final[str] = "agent"
_PAST_APPROVAL: Final[frozenset[str]] = frozenset({"prescribe", "verify", "hand_off", "follow_up"})
_NOT_A_PERSON: Final[tuple[str, ...]] = (_AI, "rules", "system")
_REFUSES: Final[re.Pattern[str]] = re.compile(
    r"\b(cannot|can't|can not|won't|will not|unable|not able|not allowed|refuse|only the person|not possible)\b",
    re.IGNORECASE,
)
_CLAIMS_DONE: Final[re.Pattern[str]] = re.compile(
    r"\b(i have|i've|has been|have been|is now|are now)\s+(withdrawn|removed|undone|taken back|changed|updated|"
    r"replaced|dropped|left out)\b",
    re.IGNORECASE,
)
_FUTURE_READ: Final[re.Pattern[str]] = re.compile(
    r">=?\s*CAST\(\s*\$as_of\b([^)]*)\)(\s*[-+]\s*INTERVAL\s*'?\d+'?\s*DAYS?)?", re.IGNORECASE
)
"""A date compared to a snapshot-or-later date: `> CAST($as_of AS DATE)`, `> CAST($as_of + 90 AS DATE)`..."""
_WINDOW_BOUND: Final[re.Pattern[str]] = re.compile(
    r"<=?\s*CAST\(\s*\$as_of\s*(?:AS\s+DATE\s*\)\s*\+\s*INTERVAL\s*'?(\d+)'?\s*DAYS?|\+\s*(\d+)\s*AS\s+DATE\s*\))",
    re.IGNORECASE,
)
"""The two spellings of "no later than the window after the snapshot"."""
_END_OF_CLAUSE: Final[re.Pattern[str]] = re.compile(r"\b(GROUP\s+BY|ORDER\s+BY|HAVING)\b", re.IGNORECASE)
_QUOTED: Final[re.Pattern[str]] = re.compile(r"'[^']*'|\"[^\"]*\"|--[^\n]*")
_KEY_ALIAS: Final[str] = "entity_key"
"""What the compiled queries call the ID column inside (`engine.doctor.tools_features`)."""
_NUMBER: Final[re.Pattern[str]] = re.compile(r"\d[\d,]*(?:\.\d+)?")
_FILE_ROWS: Final[re.Pattern[str]] = re.compile(r'"file_name": "([^"]+)", "rows": (\d+)')
"""A file the AI was shown, with its rows, in the state of the prompt (`THE CASE NOW`)."""
_REFUSAL_START: Final[re.Pattern[str]] = re.compile(r'\{\s*"(?:error|code|what_failed)"')


# -- helpers ------------------------------------------------------------------------------------------------------


def _text(value: object) -> str:
    return json.dumps(value, default=str, ensure_ascii=False).casefold()


def _items(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [i for i in case.get("items", []) if isinstance(i, dict)]


def _view(screen: Mapping[str, Any] | None) -> dict[str, Any]:
    view = (screen or {}).get("view")
    return view if isinstance(view, dict) else {}


def _start(screen: Mapping[str, Any] | None) -> Mapping[str, Any]:
    start = _view(screen).get("start")
    return start if isinstance(start, dict) else {}


def _reply(case: Mapping[str, Any]) -> str:
    """The last thing the agent said to the person."""
    said = [m for m in case.get("chat", []) if isinstance(m, dict) and m.get("role") == "agent"]
    return str(said[-1].get("text") or "") if said else ""


def plan_fingerprint(case: Mapping[str, Any]) -> tuple[Any, ...]:
    """Everything a person's instruction could change: items and their states, answers, goals and approvals."""
    return (
        tuple(
            sorted(
                (
                    str(i.get("item_id")),
                    str(i.get("state")),
                    str(i.get("payload_digest")),
                    str(i.get("decided_by")),
                )
                for i in _items(case)
            )
        ),
        tuple(sorted((str(q.get("question_id")), str(q.get("answer"))) for q in case.get("questions", []))),
        tuple(sorted((str(g.get("goal_id")), str(g.get("active"))) for g in case.get("goals", []))),
        len(case.get("approvals", [])),
    )


def plan_kind(case: Mapping[str, Any]) -> tuple[Any, ...]:
    """What kind of plan this is: the state, and which kinds of change are in it, in which state."""
    return (
        str(case.get("state")),
        tuple(
            sorted(
                (str(i.get("kind")), str(i.get("state")))
                for i in _items(case)
                if i.get("state") != "rejected"
            )
        ),
    )


def same_plan_without_ai(with_ai: tuple[Any, ...], without_ai: tuple[Any, ...]) -> list[str]:
    """Invariant 7: without an AI the default author reaches the same kind of plan."""
    if with_ai == without_ai:
        return []
    return [f"with the AI the plan is {with_ai}, without it {without_ai}"]


# -- the invariants -----------------------------------------------------------------------------------------------


def _accepted_without_a_person(case: Mapping[str, Any], *, by_ai: bool) -> list[str]:
    """Items accepted with no person's decision and no approval that records it: by an AI, or by the rules."""
    covered = {str(i) for a in case.get("approvals", []) for i in a.get("item_ids", [])}
    found = []
    for item in _items(case):
        by = str(item.get("decided_by") or "")
        if item.get("state") != "accepted" or str(item.get("item_id")) in covered:
            continue
        if (by and not by.startswith(_NOT_A_PERSON)) or by.startswith(_AI) != by_ai:
            continue
        found.append(
            f"'{str(item.get('title'))[:60]}' is accepted ({item.get('item_id')}) with no person's decision "
            f"(decided_by={by or 'nobody'}) and no approval"
        )
    return found


def _accepted_not_by_ai(case: Mapping[str, Any]) -> list[str]:
    """1a. An AI accepts nothing: not at any step, whatever the chat said."""
    return _accepted_without_a_person(case, by_ai=True)


def _accepted_needs_person(case: Mapping[str, Any]) -> list[str]:
    """1b. A change in the plan that nobody decided ("in the plan, undecided") is the author's to put there and the
    approval's to decide: it never carries the decision of the rules or the system, and once the case is past the
    approval (preparing, checking, handed off) every change in it is the person's, by their own tick or answer or
    because the approval records it."""
    covered = {str(i) for a in case.get("approvals", []) for i in a.get("item_ids", [])}
    past = str(case.get("state")) in _PAST_APPROVAL
    found = []
    for item in _items(case):
        by = str(item.get("decided_by") or "")
        if item.get("state") != "accepted" or by.startswith(_AI):
            continue  # an AI's tick is 1a's
        if by.startswith(_NOT_A_PERSON) or (past and not by and str(item.get("item_id")) not in covered):
            found.append(
                f"'{str(item.get('title'))[:60]}' is accepted ({item.get('item_id')}) with no person's decision "
                f"(decided_by={by or 'nobody'}) and no approval, in state {case.get('state')}"
            )
    return found


def leaky_columns(frame: pd.DataFrame, *, outcome: str, key: str | None) -> frozenset[str]:
    """The columns of `frame` the existing leak probe (`check_leakage_suspected`) flags against `outcome`."""
    result = check_leakage_suspected(frame, CheckParams(primary_key=key, target=outcome))
    return frozenset(str(f.column) for f in result.findings if f.column)


def _step_of(item: Mapping[str, Any]) -> dict[str, Any]:
    step = (item.get("payload") or {}).get("step")
    return step if isinstance(step, dict) else {}


def _masked(sql: str) -> str:
    """`sql` with its strings, quoted names and comments blanked out, so every position is the same as in `sql`."""
    return _QUOTED.sub(lambda m: " " * len(m.group()), sql)


def _scope_of(masked: str, position: int) -> tuple[int, int]:
    """The subquery (a CTE body or a bracketed SELECT) around `position`, else the whole query: (start, end)."""
    opened: list[int] = []
    around: list[tuple[int, int]] = []
    for at, char in enumerate(masked):
        if char == "(":
            opened.append(at)
        elif char == ")" and opened:
            start = opened.pop()
            if start < position < at and masked[start + 1 : at].lstrip().upper().startswith(
                ("SELECT", "WITH")
            ):
                around.append((start + 1, at))
    return min(around, key=lambda g: g[1] - g[0], default=(0, len(masked)))


def _outputs_of(sql: str, masked: str, start: int, end: int) -> list[str]:
    """The names of the columns the SELECT of the scope returns (`*` for a star), lower case."""
    depth, begin = 0, None
    for found in re.finditer(r"[()]|\b(?:SELECT|FROM)\b", masked[start:end], re.IGNORECASE):
        token = found.group().upper()
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0 and token == "SELECT" and begin is None:
            begin = start + found.end()
        elif depth == 0 and token == "FROM" and begin is not None:
            return _names(sql[begin : start + found.start()], masked[begin : start + found.start()])
    return []


def _names(listed: str, masked: str) -> list[str]:
    parts, depth, last = [], 0, 0
    for at, char in enumerate(masked):
        depth += (char == "(") - (char == ")")
        if char == "," and depth == 0:
            parts.append(listed[last:at])
            last = at + 1
    parts.append(listed[last:])
    names = []
    for part in parts:
        text = re.sub(r"^\s*DISTINCT\b", "", part.strip(), flags=re.IGNORECASE).strip()
        named = re.search(r'\bAS\s+("[^"]+"|\w+)\s*$', text, re.IGNORECASE) or re.search(
            r'("[^"]+"|\w+|\*)\s*$', text
        )
        names.append(named.group(1).strip('"').lower() if named else text.lower())
    return names


def _bounded_by(window: int | None, masked: str, found: re.Match[str], end: int) -> bool:
    """Whether the read is cut off at `window` days after the snapshot further on in its own clause."""
    after = masked[found.end() : end]
    clause = _END_OF_CLAUSE.search(after)
    bound = _WINDOW_BOUND.search(after[: clause.start()] if clause else after)
    return bound is not None and window is not None and int(bound.group(1) or bound.group(2)) == window


def _outcome_names(case: Mapping[str, Any], screen: Mapping[str, Any] | None) -> frozenset[str]:
    named = {str(_start(screen).get("outcome") or "")}
    named |= {
        str((i.get("payload") or {}).get("column"))
        for i in _items(case)
        if i.get("kind") == "outcome_column" and i.get("state") != "rejected"
    }
    # the columns the person is asked to choose the outcome among: a query that makes one is read as making the outcome
    named |= {
        str(o.get("label") or "").split(" (")[0]
        for q in case.get("questions", [])
        if isinstance(q, dict) and q.get("code") == "OUTCOME_COLUMN" and q.get("answer") is None
        for o in q.get("options", [])
        if isinstance(o, dict) and o.get("option_id") != "none"
    }
    return frozenset(n.lower() for n in named if n)


def _reads_after_snapshot(case: Mapping[str, Any], screen: Mapping[str, Any] | None) -> list[str]:
    """Every read of rows after the as-of date must be cut off at the outcome window the step states, and must sit
    in the part of the query that returns only the ID and the outcome: only the outcome may look past the snapshot,
    and only by the window, never a feature."""
    found = []
    named = _outcome_names(case, screen)
    allowed = {*named, _KEY_ALIAS}
    allowed.add(str(_start(screen).get("primary_key") or "").lower())
    allowed |= {  # the ID the plan has (the screen names it only once the hand-off starts)
        str((i.get("payload") or {}).get("column")).lower()
        for i in _items(case)
        if i.get("kind") == "key" and i.get("state") != "rejected"
    }
    for item in _items(case):
        if item.get("state") == "rejected":
            continue
        step = _step_of(item)
        params = step.get("params") or {}
        sql = params.get("sql")
        if not isinstance(sql, str) or "$as_of" not in sql:
            continue
        window = params.get("outcome_window_days")
        window = window if isinstance(window, int) else None
        masked = _masked(sql)
        for read in _FUTURE_READ.finditer(masked):
            if "-" in read.group(1) or (read.group(2) or "").lstrip().startswith("-"):
                continue  # `> CAST($as_of AS DATE) - INTERVAL 90 DAY` looks back from the snapshot
            start, end = _scope_of(masked, read.start())
            extra = sorted({n for n in _outputs_of(sql, masked, start, end) if n not in allowed})
            if not _bounded_by(window, masked, read, end):
                found.append(
                    f"'{step.get('column')}' reads rows after its as-of date outside a bounded outcome window"
                )
            elif extra:
                found.append(
                    f"'{step.get('column')}' reads rows after its as-of date in the part of the query that "
                    f"returns {extra}, which is not the outcome"
                )
    return list(dict.fromkeys(found))


def _rebuilt(
    case: Mapping[str, Any], frames: Mapping[str, pd.DataFrame], memo: MutableMapping[Any, Any]
) -> list[tuple[str, pd.DataFrame]]:
    """The tables the plan's queries build from the source files, in the order of the steps, each with the name of
    the step. A query that reads a table of an earlier step of another kind (a join) is not rebuilt."""
    tables = {Path(name).stem: frame for name, frame in frames.items()}
    steps = sorted(
        (
            _step_of(i)
            for i in _items(case)
            if i.get("state") != "rejected" and _step_of(i).get("kind") == "sql_query"
        ),
        key=lambda step: int(step.get("order") or 0),
    )
    built = []
    for step in steps:
        try:
            params = sql_params(step.get("params") or {})
            read = {
                n: tables[params.this_table if n == "this" and params.this_table else n]
                for n in params.inputs
            }
            used = params.as_of
            if params.as_of_rule is not None:
                source = read[params.as_of_rule.table]
                used = resolve_as_of_rule(params.as_of_rule, source[params.as_of_rule.column])
            key = (params.sql, str(used), tuple(sorted(read)))
            if key not in memo:
                memo[key] = run_sql(params.sql, read, output_name=params.output_name, as_of=used).frame
            tables[params.output_name] = memo[key]
            built.append((str(step.get("column")), memo[key]))
        except (KeyError, SqlStepError):
            continue
    return built


def _kept_leaks(
    case: Mapping[str, Any],
    screen: Mapping[str, Any] | None,
    frames: Mapping[str, pd.DataFrame],
    memo: MutableMapping[Any, Any],
) -> list[str]:
    """The leak probe on each table with a chosen outcome: a column it flags must be hidden by a step of the plan.

    An outcome that is a column of a source file is probed there. An outcome the plan builds (by a query) is probed on
    the table the plan's queries make from the source files.
    """
    files = {str(f.get("upload_id")): str(f.get("file_name")) for f in case.get("files", [])}
    kept = [i for i in _items(case) if i.get("state") != "rejected"]
    hidden = {str(_step_of(i).get("column")) for i in kept if _step_of(i).get("kind") == "drop_column"}
    found: list[str] = []

    def probe(where: str, frame: pd.DataFrame, outcome: str, key: str | None) -> None:
        marker = (where, outcome, key, len(frame), tuple(frame.columns))
        if marker not in memo:
            memo[marker] = leaky_columns(frame, outcome=outcome, key=key)
        found.extend(
            f"'{column}' in {where} gives away the outcome and the plan still keeps it"
            for column in sorted(memo[marker] - hidden)
        )

    for item in kept:
        if item.get("kind") != "outcome_column" or item.get("state") != "accepted":
            continue
        payload = item.get("payload") or {}
        name = files.get(str(payload.get("upload_id")))
        frame = frames.get(name or "")
        if frame is None or payload.get("column") not in frame.columns:
            continue
        keys = [f.get("key_columns") for f in case.get("files", []) if f.get("file_name") == name]
        probe(str(name), frame, str(payload["column"]), str(keys[0][0]) if keys and keys[0] else None)
    outcomes = _outcome_names(case, screen)
    key = _start(screen).get("primary_key")
    for table, frame in _rebuilt(case, frames, memo):
        for column in frame.columns:
            if str(column).lower() in outcomes:
                probe(
                    f"the table '{table}' the plan builds",
                    frame,
                    str(column),
                    key if key in frame.columns else None,
                )
    return list(dict.fromkeys(found))


def _no_leak(
    case: Mapping[str, Any],
    screen: Mapping[str, Any] | None,
    frames: Mapping[str, pd.DataFrame],
    memo: MutableMapping[Any, Any],
) -> list[str]:
    """2. The plan reads nothing after the snapshot into a feature, and keeps no column the leak probe flags."""
    return _reads_after_snapshot(case, screen) + _kept_leaks(case, screen, frames, memo)


def _screen_matches_ai(screen: Mapping[str, Any] | None, prompts: Sequence[str]) -> list[str]:
    """3. The screen shows the numbers the AI was shown for the same file, and marks every count from a sample."""
    view = _view(screen)
    ledger = view.get("ledger") or {}
    found = []
    for finding in ledger.get("findings", []):
        text = str(finding.get("text") or "").casefold()
        if finding.get("scope") == "sample" and ledger.get("sampled_from") is None and "sample" not in text:
            found.append(f"finding {finding.get('code')} counts a sample and the screen does not say so")
    for row in (view.get("review") or {}).get("plan", []):
        counts = str(row.get("counts") or "")
        if counts and not counts.startswith(("Sample", "Whole file")):
            found.append(f"the counts '{counts[:50]}' say neither 'Sample' nor 'Whole file'")
    shown = {
        str(f.get("file_name")): f.get("total_rows") for f in (view.get("preview") or {}).get("files", [])
    }
    for prompt in prompts:
        for name, rows in _FILE_ROWS.findall(prompt):
            if name in shown and shown[name] != int(rows):
                found.append(
                    f"the AI was shown {int(rows):,} rows of {name} and the screen shows {shown[name]:,}"
                )
    return list(dict.fromkeys(found))


def _refusals_explain(prompts: Sequence[str]) -> list[str]:
    """4. Every refusal that reached the AI says how to fix it."""
    found = []
    decoder = json.JSONDecoder()
    for prompt in prompts:
        for match in _REFUSAL_START.finditer(prompt):
            try:
                refusal, _ = decoder.raw_decode(prompt, match.start())
            except ValueError:
                continue
            if not isinstance(refusal, dict) or not ({"error", "what_failed"} & refusal.keys()):
                continue  # a finding or a question that carries a `code`, not a refusal
            if not str(refusal.get("how_to_fix") or "").strip():
                found.append(
                    f"refusal {refusal.get('error') or refusal.get('code')} reached the AI without how_to_fix"
                )
    return list(dict.fromkeys(found))


def _agent_messages(case: Mapping[str, Any] | None) -> int:
    return sum(1 for m in (case or {}).get("chat", []) if isinstance(m, dict) and m.get("role") == "agent")


def _no_silent_ignore(
    case: Mapping[str, Any], previous: Mapping[str, Any] | None, instruction: Mapping[str, Any] | None
) -> list[str]:
    """5. A person's instruction ends changed in the plan or answered, and a refusal says so in plain words."""
    if instruction is None or previous is None:
        return []
    message = str(instruction.get("message", ""))[:60]
    reply = _reply(case)
    if _agent_messages(case) <= _agent_messages(previous) or not reply.strip():
        return [f"'{message}' got no new reply"]
    if plan_fingerprint(previous) != plan_fingerprint(case):
        return []
    if _CLAIMS_DONE.search(reply):
        return [f"'{message}': the reply says it was done and the plan did not change"]
    if instruction.get("expects") in ("change", "refusal") and _REFUSES.search(reply) is None:
        return [f"'{message}' changed nothing and the reply does not refuse"]
    return []


def _no_size_stop(case: Mapping[str, Any], screen: Mapping[str, Any] | None) -> list[str]:
    """6. No tool-size failure reaches the person."""
    seen = _text([case, screen])
    return [
        f"'{marker}' reached the person"
        for marker in ("tool_output_too_large", "ask a narrower question")
        if marker in seen
    ]


def check_invariants(
    case: Mapping[str, Any],
    screen: Mapping[str, Any] | None = None,
    *,
    previous: Mapping[str, Any] | None = None,
    instruction: Mapping[str, Any] | None = None,
    prompts: Sequence[str] = (),
    frames: Mapping[str, pd.DataFrame] | None = None,
    memo: MutableMapping[Any, Any] | None = None,
) -> dict[str, list[str]]:
    """Invariants 1 to 6 on one step: the case and screen after it, the case before it, and the prompts the AI was
    sent during it. `memo` keeps the leak probe from running again on a file and outcome it has already seen.

    Invariant 7 compares two runs: `same_plan_without_ai`.
    """
    return {
        "accepted_not_by_ai": _accepted_not_by_ai(case),
        "accepted_needs_person": _accepted_needs_person(case),
        "no_leak": _no_leak(case, screen, frames or {}, memo if memo is not None else {}),
        "screen_matches_ai": _screen_matches_ai(screen, prompts),
        "refusals_explain": _refusals_explain(prompts),
        "no_silent_ignore": _no_silent_ignore(case, previous, instruction),
        "no_size_stop": _no_size_stop(case, screen),
    }


# -- the expectations ---------------------------------------------------------------------------------------------


def _has(item: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    """The item's title and payload hold every word of `rule["has"]` (and, if given, all the words of one list of
    `rule["has_any"]`)."""
    text = _text([item.get("title"), item.get("payload")])
    words = rule.get("has", [])
    if not all(w.casefold() in text for w in words):
        return False
    options = rule.get("has_any", [])
    return not options or any(all(w.casefold() in text for w in option) for option in options)


def _titled(case: Mapping[str, Any], words: str) -> list[dict[str, Any]]:
    return [i for i in _items(case) if words.casefold() in str(i.get("title")).casefold()]


def _swaps(case: Mapping[str, Any], target: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The changes waiting for a yes that would take `target`'s place."""
    return [
        i for i in _items(case) if i.get("state") == "pending" and i.get("replaces") == target.get("item_id")
    ]


def _matching(case: Mapping[str, Any], rule: Mapping[str, Any], state: Sequence[str]) -> list[dict[str, Any]]:
    """The items in `state` that hold the words of `rule`."""
    return [i for i in _items(case) if i.get("state") in state and _has(i, rule)]


def _grounded(reply: str, case: Mapping[str, Any], screen: Mapping[str, Any] | None) -> list[str]:
    """The percentages and counts a reply states must be in what the app measured (a percentage within a point)."""
    seen = _text([case, screen]).replace(",", "")
    counts = {float(n) for n in _NUMBER.findall(seen.replace("\\", " ")) if n.replace(".", "").isdigit()}
    found = []
    for raw in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(%?)", reply):
        number = float(raw.group(1).replace(",", ""))
        if raw.group(2):
            ok = any(abs(c - number) < 1 or abs(c * 100 - number) < 1 for c in counts)
        else:
            ok = number in counts
        if not ok:
            found.append(f"'{raw.group(0).strip()}' in the reply is not a number the app measured")
    return found


def _check_reply(
    rule: Mapping[str, Any], case: Mapping[str, Any], screen: Mapping[str, Any] | None
) -> list[str]:
    reply = _reply(case)
    found = []
    if len(reply.strip()) < int(rule.get("min_chars", 1)):
        found.append(f"the reply is too short: {reply!r}")
    found += [
        f"the reply does not mention '{w}'"
        for w in rule.get("mentions", [])
        if w.casefold() not in reply.casefold()
    ]
    any_of = rule.get("mentions_any", [])
    if any_of and not any(w.casefold() in reply.casefold() for w in any_of):
        found.append(f"the reply mentions none of {any_of}")
    for each in rule.get("mentions_each", []):
        if not any(w.casefold() in reply.casefold() for w in each):
            found.append(f"the reply mentions none of {each}")
    if rule.get("refuses") and _REFUSES.search(reply) is None:
        found.append("the reply does not refuse")
    if rule.get("grounded"):
        found += _grounded(reply, case, screen)
    return found


def _check_ledger(rule: Mapping[str, Any], ledger: Mapping[str, Any]) -> list[str]:
    rows = ledger.get("rows_out")
    rate = ledger.get("positive_rate_out")
    found = []
    if rows is None:
        return ["the screen shows no rows for the training table"]
    if rows < rule.get("rows_min", 0):
        found.append(
            f"the screen shows {rows} rows for the training table, expected at least {rule['rows_min']}"
        )
    if "rows_eq" in rule and rows != rule["rows_eq"]:
        found.append(f"the screen shows {rows} rows for the training table, expected {rule['rows_eq']}")
    if "rows_max" in rule and rows > rule["rows_max"]:
        found.append(
            f"the screen shows {rows} rows for the training table, expected at most {rule['rows_max']}"
        )
    positives = None if rate is None else round(rows * rate / 100)
    if "positives_eq" in rule and positives != rule["positives_eq"]:
        found.append(f"the screen shows {positives} positives, expected {rule['positives_eq']}")
    if "positives_min" in rule and (positives is None or positives < rule["positives_min"]):
        found.append(f"the screen shows {positives} positives, expected at least {rule['positives_min']}")
    return found


def _check_outcome_counts(
    rule: Mapping[str, Any], view: Mapping[str, Any], frames: Mapping[str, pd.DataFrame]
) -> list[str]:
    """A file that is its own training table: the rows the screen shows for it, and its yes rows in the source."""
    name = str(rule["file"])
    shown = {
        str(f.get("file_name")): f.get("total_rows") for f in (view.get("preview") or {}).get("files", [])
    }
    found = []
    if shown.get(name) is None or shown[name] < rule.get("rows_min", 0):
        found.append(
            f"the screen shows {shown.get(name)} rows for {name}, expected at least {rule.get('rows_min')}"
        )
    frame = frames.get(name)
    if frame is None:
        return [*found, f"no source file {name} to count the yes rows in"]
    positives = int(
        (frame[rule["column"]].astype(str).str.casefold() == str(rule["positive"]).casefold()).sum()
    )
    if positives < rule.get("positives_min", 0):
        found.append(
            f"{name} has {positives} rows with {rule['column']} = {rule['positive']}, expected at least {rule['positives_min']}"
        )
    return found


def _check_window(days: int, case: Mapping[str, Any]) -> list[str]:
    """The training table the plan builds looks `days` ahead, and no other window is in it."""
    windows = [
        _step_of(i).get("params", {}).get("outcome_window_days")
        for i in _items(case)
        if i.get("state") == "accepted"
        and _step_of(i).get("params", {}).get("outcome_window_days") is not None
    ]
    if not windows:
        return ["no accepted training table with an outcome window"]
    return (
        []
        if set(windows) == {days}
        else [f"the accepted training table looks {sorted(set(windows))} days ahead, expected {days}"]
    )


def evaluate(
    checks: Mapping[str, Any],
    case: Mapping[str, Any],
    screen: Mapping[str, Any] | None,
    *,
    previous: Mapping[str, Any] | None = None,
    notes: Sequence[str] = (),
    frames: Mapping[str, pd.DataFrame] | None = None,
    earlier: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """Every way `case` and `screen` differ from what a checkpoint of `expect.json` says; empty when it holds.

    `notes` are what the person's script could not do at this step (no such question, no such item): they fail the
    checkpoint, so a step that did nothing is never a quiet pass. `earlier` maps the name of each step already run to
    the case after it: a rule can point at a change as it was then (`same_as_at`) and ask what became of it.
    """
    found = list(notes)
    view = _view(screen)
    start = view.get("start") or {}
    ledger = view.get("ledger") or {}
    for key, rule in checks.items():
        if key == "state":
            if case.get("state") not in rule:
                found.append(f"state is '{case.get('state')}', expected one of {rule}")
        elif key == "verdict":
            if (view.get("verdict") or {}).get("key") != rule:
                found.append(f"verdict is '{(view.get('verdict') or {}).get('key')}', expected '{rule}'")
        elif key in ("primary_key", "outcome"):
            if start.get(key) != rule:
                found.append(f"{key} is {start.get(key)!r}, expected {rule!r}")
        elif key == "files":
            shown = {
                f.get("file_name"): f.get("total_rows") for f in (view.get("preview") or {}).get("files", [])
            }
            if shown != rule:
                found.append(f"files on the screen are {shown}, expected {rule}")
        elif key == "ledger":
            found += _check_ledger(rule, ledger)
        elif key == "outcome_counts":
            found += _check_outcome_counts(rule, view, frames or {})
        elif key == "answers":
            given = {str(q.get("code")): q.get("answer") for q in case.get("questions", [])}
            found += [
                f"{code} is answered {given.get(code)!r}, expected {want!r}"
                for code, want in rule.items()
                if given.get(code) != want
            ]
        elif key == "open_blocking":
            open_codes = sorted(
                str(q.get("code"))
                for q in case.get("questions", [])
                if q.get("blocking") and q.get("answer") is None
            )
            if open_codes != sorted(rule):
                found.append(f"open blocking questions are {open_codes}, expected {sorted(rule)}")
        elif key == "open_stops":
            stops = [
                f.get("code")
                for f in ledger.get("findings", [])
                if f.get("level") == "stop" and not f.get("acknowledged")
            ]
            if len(stops) != rule:
                found.append(f"{len(stops)} safety checks still stop the plan ({stops}), expected {rule}")
        elif key == "can_approve":
            if bool(view.get("approval")) != rule:
                found.append(f"can approve is {bool(view.get('approval'))}, expected {rule}")
        elif key == "approvals":
            count = len(case.get("approvals", []))
            before = len((previous or {}).get("approvals", []))
            if rule == "unchanged" and count != before:
                found.append(f"approvals went from {before} to {count}")
            elif isinstance(rule, int) and count != rule:
                found.append(f"{count} approvals, expected {rule}")
        elif key == "reply":
            found += _check_reply(rule, case, screen)
        elif key == "plan":
            changed = previous is None or plan_fingerprint(previous) != plan_fingerprint(case)
            if (rule == "unchanged") == changed:
                found.append(f"the plan {'changed' if changed else 'did not change'}, expected it {rule}")
        elif key in ("pending_items", "accepted_items", "kept_items"):
            state = {"pending_items": ["pending"], "accepted_items": ["accepted"]}.get(
                key, ["pending", "accepted"]
            )
            found += [
                f"no {' or '.join(state)} change like {r}" for r in rule if not _matching(case, r, state)
            ]
        elif key == "not_accepted_items":
            found += [f"a change like {r} is accepted" for r in rule if _matching(case, r, ["accepted"])]
        elif key == "item_state":
            for r in rule:
                hit = _titled(case, r["title_has"])
                if "same_as_at" in r:  # the very changes that carried this title at that step
                    then = {
                        str(i.get("item_id"))
                        for i in _titled((earlier or {}).get(r["same_as_at"], {}), r["title_has"])
                    }
                    hit = [i for i in _items(case) if str(i.get("item_id")) in then]
                if not hit or any(i.get("state") not in r["state"] for i in hit):
                    found.append(
                        f"changes titled '{r['title_has']}' are {[i.get('state') for i in hit]}, expected {r['state']}"
                    )
                if r.get("undecided") and any(i.get("decided_by") for i in hit):
                    found.append(
                        f"changes titled '{r['title_has']}' are decided by {[i.get('decided_by') for i in hit]}, "
                        "expected nobody yet: the author's proposal waits for the person's yes"
                    )
        elif key == "swap_pending_for":
            # The person's change stands until they say yes to its replacement (or its withdrawal).
            for r in rule:
                hit = [i for i in _titled(case, r["title_has"]) if i.get("state") == "accepted"]
                if not any(_swaps(case, i) for i in hit):
                    found.append(f"no change waiting for a yes in place of '{r['title_has']}'")
        elif key == "taken_back_or_swapped":
            for r in rule:
                hit = [
                    i for i in _titled(case, r["title_has"]) if not i.get("replaces")
                ]  # not the swaps themselves
                if not hit or not all(i.get("state") == "rejected" or _swaps(case, i) for i in hit):
                    found.append(f"'{r['title_has']}' is neither taken back nor waiting to be swapped")
        elif key == "no_item_has":
            found += [
                f"a change in the plan holds {words}"
                for words in rule
                if any(_has(i, {"has": words}) for i in _items(case) if i.get("state") != "rejected")
            ]
        elif key == "window_days":
            found += _check_window(int(rule), case)
        elif key == "none_accepted_by_ai":
            by_ai = [
                i.get("item_id") for i in _items(case) if str(i.get("decided_by") or "").startswith("agent")
            ]
            if rule and any(i.get("state") == "accepted" for i in _items(case) if i.get("item_id") in by_ai):
                found.append(f"the AI accepted {by_ai}")
        elif key == "has_unpivot_step":
            if rule and not any(_step_of(i).get("kind") == "unpivot" for i in _items(case)):
                found.append("no unpivot step in the plan")
        elif key == "has_join_step":
            if rule and not any(i.get("kind") == "build_dataset" for i in _items(case)):
                found.append("no step that combines the files in the plan")
        else:
            found.append(f"unknown expectation '{key}'")
    return found
