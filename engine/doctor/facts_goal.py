"""The goal's rules as fact functions (Stage 2, the design §1.3): what the data says about the goal, and what the
rules would do about it.

Each function reads the case and the files and returns `Facts`: what it found (names and counts only), the
`Suggestion`s it would make (an item, a question, a doubt, a request) and the tool results they cite. None of them
writes the case: the author (the AI or the default author) applies the suggestions it takes.

* `goal_fit_facts`: what the data can power, and what is missing for the other goals (no goal chosen).
* `outcome_facts`: the goal's outcome column, or the build that makes it (`suggest_build`).
* `time_facts`, `consent_facts`: the date each row describes, and who agreed to be contacted.
* `goal_facts`: the three in the order the rules ran them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from engine.agent.contracts import AgentConfidence, ProposalSource, ProposalState
from engine.doctor.case import Case, CaseDoubt, CaseItem
from engine.doctor.decisions import decisions
from engine.doctor.goal_settings import load_goal_settings, open_setting_questions
from engine.doctor.protocol import DataRequest
from engine.doctor.registry import DataToolError
from engine.doctor.tools_facts import (
    DONT_KNOW,
    MAX_OFFERED,
    OUTPUT_NAME_CHARS,
    FactRun,
    Facts,
    Found,
)
from engine.doctor.visit_contracts import OptionEffect
from engine.utils.logging import get_logger

if TYPE_CHECKING:
    from engine.doctor.buildable import BuildableOutcome, BuildAudience
    from engine.doctor.current_tables import CurrentTables
    from engine.doctor.diagnose import LoadedFile
    from engine.doctor.goal_brief import GoalBrief

__all__ = [
    "consent_facts",
    "goal_facts",
    "goal_fit_facts",
    "outcome_facts",
    "outcome_is_gone",
    "suggest_build",
    "time_facts",
]

_LOG = get_logger(__name__)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _named(columns: Sequence[str], names: Sequence[str], *, parts: bool) -> list[str]:
    """Columns whose name is one of `names` (case and punctuation aside); with `parts`, also a `_`-part of it."""
    wanted = {_norm(name) for name in names if name}
    found: list[str] = []
    for column in columns:
        norm = _norm(column)
        if norm in wanted or (parts and wanted & set(norm.split("_"))):
            found.append(column)
    return found


def _section(case: Case) -> tuple[CaseItem, ...]:
    from engine.doctor.visit import active_section

    return active_section(case)[0]


# ---------------------------------------------------------------------------
# No goal: what the data can power
# ---------------------------------------------------------------------------
def goal_fit_facts(run: FactRun, case: Case) -> Facts:
    """No goal: what the data can power, and what is missing for the other goals (the design §3.1).

    The tables are the files as the plan leaves them and the tables its queries build, and a campaign test the
    person confirmed (`treatment_column`) is read as they said, so a built table that holds the outcome is what the
    fit and the floors (rows, 'yes' examples) are counted on. The verdicts are paged (`left_out`) and are a fact,
    not an item: the caller keeps them as the case's diagnosis. Nothing is suggested; a goal that is missing
    something comes with the request for the data, which the caller records."""
    found = Found()
    if not run.deps.offers:
        return found.facts({})
    ctx = run.context(case, files=run.current(case).tables, case_items=_section(case))
    power = run.call("what_can_this_power", {}, ctx)
    verdicts = list(power.output["use_cases"])
    missing = tuple(
        str(v["use_case_id"])
        for v in verdicts
        if isinstance(v, dict) and v.get("status") in {"missing", "too_small"}
    )
    facts: dict[str, Any] = {"use_cases": verdicts, "missing_for": list(missing), "data_request": None}
    if missing:
        request = run.call("data_request", {"use_case_ids": list(missing[:4])}, ctx)
        found.evidence.append(request)
        facts["data_request"] = request.evidence_id
    return found.facts(facts, left_out=int(power.output.get("left_out") or 0))


# ---------------------------------------------------------------------------
# The outcome
# ---------------------------------------------------------------------------
_AFTER_OUTCOME: Final[tuple[str, ...]] = (
    "as_of_column",
    "AS_OF_COLUMN",
    "d.time_missing.",
    "consent_column",
    "d.consent_missing.",
)
"""What the goal's rules look at only once the outcome is decided (its table is where they look)."""

_STEM: Final[int] = 5
_STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "the",
        "and",
        "that",
        "after",
        "before",
        "with",
        "from",
        "this",
        "day",
        "days",
        "date",
        "snapshot",
        "placed",
    }
)
"""Words that say nothing about which column is the outcome (every window is in days, after a snapshot)."""


def _stems(text: str) -> set[str]:
    """The word-starts of a name or a sentence (`ordered_again_90d` and "placed another order in the 90 days" share
    `order` and `90`): letters cut to five, numbers whole."""
    words = re.findall(r"[a-z]+|\d+", text.lower())
    return {w[:_STEM] for w in words if w not in _STOP_WORDS and (len(w) >= 3 or w.isdigit())}


def _query_tables(
    case: Case, files: Sequence[LoadedFile]
) -> list[tuple[CaseItem, LoadedFile, dict[str, Any], list[str]]]:
    """The query steps of the active goal's section that make a table (accepted, or proposed by the AI and not yet
    answered): `(item, the file the query reads, the step, the names of the columns it makes)`. A step that runs
    after the build, with no file of its own, is left out when there are several files: the prescription carries it
    only for a single file."""
    from engine.doctor.prescription import slot_name

    found: list[tuple[CaseItem, LoadedFile, dict[str, Any], list[str]]] = []
    for item in _section(case):
        step = item.payload.get("step")
        columns = item.payload.get("columns")
        if (
            not isinstance(step, dict)
            or step.get("kind") != "sql_query"
            or not isinstance(columns, list)
            or not (item.state is ProposalState.ACCEPTED or item.source is ProposalSource.AI)
            or item.state is ProposalState.REJECTED
            or (item.upload_id is None and len(files) > 1 and item.payload.get("stage") == "post_build")
        ):
            continue
        given = step.get("params")
        params: dict[str, Any] = given if isinstance(given, dict) else {}
        inputs = params.get("inputs")
        reads = [t for t in (inputs if isinstance(inputs, list) else []) if isinstance(t, str)]
        origin = next(
            (
                lf
                for lf in files
                if (item.upload_id is not None and lf.upload_id == item.upload_id)
                or (item.upload_id is None and slot_name(Path(lf.file_name or "").stem) in reads)
            ),
            files[0] if files and item.upload_id is None else None,
        )
        if origin is not None:
            found.append((item, origin, step, [str(c) for c in columns]))
    return found


def _built_outcomes(
    current: CurrentTables, brief: GoalBrief, case: Case | None = None
) -> list[tuple[LoadedFile, str]]:
    """The columns of the tables the case's queries really build that read as the goal's outcome, best first: the
    same ranking as :func:`_prepared_outcomes` (a name like the outcome's or a synonym, then a name sharing two of its
    words), but on the built tables' own columns, and each must be two-valued on the rows built (a yes/no), so an order
    count or a date is never offered. Candidates for the person's question only. Each is a `(file, column)` pair, the
    file being the upload the query reads."""
    named = {_norm(n) for n in (brief.outcome.column, *brief.outcome.synonyms) if n}
    words = _stems(f"{brief.outcome.definition} {brief.outcome.column or ''}")
    ranked: list[tuple[int, int, LoadedFile, str]] = []
    for table in current.built:
        maker = (
            next((i for i in case.items if i.item_id == table.made_by), None) if case is not None else None
        )
        step = maker.payload.get("step") if maker is not None else None
        given = step.get("params") if isinstance(step, dict) else None
        said = _stems(
            f"{step.get('reason') or '' if isinstance(step, dict) else ''} "
            f"{given.get('description') or '' if isinstance(given, dict) else ''}"
        )
        for column in table.columns:
            mine = _stems(column)
            if _norm(column) in named or set(_named([column], [*named], parts=True)):
                tier = 0
            elif len(mine & words) >= 2:
                tier = 1
            elif len(mine & words) >= 1 and len(said & words) >= 2:
                tier = 2  # the AI names its columns freely: the step's own words say what it is
            else:
                continue
            if tier and column not in table.two_valued:
                continue
            ranked.append((tier, len(ranked), table.origin, column))
    ranked.sort(key=lambda r: (r[0], r[1]))
    return [(origin, column) for _t, _i, origin, column in ranked]


def _built_by_approved(case: Case, current: CurrentTables, column: str) -> bool:
    """The table that holds `column` is made by a query step the person approved (not one only proposed)."""
    table = current.built_with(column)
    maker = next((i for i in case.items if table is not None and i.item_id == table.made_by), None)
    return maker is not None and maker.state is ProposalState.ACCEPTED


def _prepared_outcomes(
    case: Case, brief: GoalBrief, files: Sequence[LoadedFile], current: CurrentTables | None = None
) -> list[tuple[LoadedFile, str]]:
    """The columns the case's own query steps make that read as the goal's outcome, best first: a column named like
    it or a synonym, then one whose name shares two of the outcome's words, then one sharing one word while the
    step's description shares two (the AI names its columns freely). Except for the first kind, the column must be
    two-valued on the rows the step was tried on (a yes/no), so an order count or a date is never offered. Only
    candidates for the person's question, never a decision. Each is a `(file, column)` pair.

    This reads a step's recorded column names, so it is the fallback for a query that built no table (one that could
    not run on the rows read): a table that was built is read by :func:`_built_outcomes`."""
    named = {_norm(n) for n in (brief.outcome.column, *brief.outcome.synonyms) if n}
    words = _stems(f"{brief.outcome.definition} {brief.outcome.column or ''}")
    ranked: list[tuple[int, int, LoadedFile, str]] = []
    built = {t.made_by for t in current.built} if current is not None else set()
    for item, origin, step, columns in _query_tables(case, files):
        if item.item_id in built:
            continue
        given = step.get("params")
        params: dict[str, Any] = given if isinstance(given, dict) else {}
        said = _stems(f"{step.get('reason') or ''} {params.get('description') or ''}")
        binary = item.payload.get("two_valued")
        for column in columns:
            mine = _stems(column)
            if _norm(column) in named or set(_named([column], [*named], parts=True)):
                tier = 0
            elif len(mine & words) >= 2:
                tier = 1
            elif len(mine & words) >= 1 and len(said & words) >= 2:
                tier = 2
            else:
                continue
            if tier and isinstance(binary, list) and column not in binary:
                continue
            ranked.append((tier, len(ranked), origin, column))
    ranked.sort(key=lambda r: (r[0], r[1]))
    return [(origin, column) for _t, _i, origin, column in ranked]


def outcome_is_gone(
    case: Case, files: Sequence[LoadedFile], decided: tuple[str, str], current: CurrentTables | None = None
) -> bool:
    """True when the decided outcome column is in no file, in no table the plan builds and in no query step still in
    the plan (the person left the step out, or the AI proposed it again with other names)."""
    upload_id, column = decided
    raw = next((lf for lf in files if lf.upload_id == upload_id), None)
    if raw is None or column in {str(c) for c in raw.frame.columns}:
        return False
    if current is not None and current.built_with(column) is not None:
        return False
    return not any(column in made for _i, _o, _s, made in _query_tables(case, files))


def outcome_facts(run: FactRun, case: Case, brief: GoalBrief) -> Facts:
    """The goal's outcome column, and the build that makes it when no file has one.

    Facts: the decided (or, for a column named exactly as the goal names it, suggested) outcome as
    `facts["outcome"] = [upload_id, column]`, whether a decided outcome is gone (`gone`), and what the rules did not
    look at because the outcome is not decided (`unevaluated`). Suggestions: `Create outcome_column` for the exact
    name; `AskPerson OUTCOME_COLUMN` over the likely columns (the column a plan's build makes is the recommended
    one); the build (`suggest_build`) when no file has the column and an event table can define it; else a doubt
    and a request for the column."""
    found = Found()
    goal = brief.goal_id
    current = run.current(
        case
    )  # the tables as the plan leaves them: the files, and the tables its queries build
    files = current.files
    # The outcome: the configured column where it is; otherwise one question over the current tables' likely columns.
    if any(
        i.goal_id == goal
        and i.kind == "recipe_step"
        and i.state is not ProposalState.REJECTED
        and isinstance(i.payload.get("buildable"), dict)
        for i in _section(case)
    ):
        # The build this plan carries makes the outcome column (it is among the tables the rules read below), so it is
        # proposed again every time, else the store takes it back: "no longer proposed".
        offer_build(run, case, brief, files, found)
    outcome = decisions(case).pair("outcome", goal)
    if outcome is not None and outcome_is_gone(case, files, outcome, current):
        found.doubt(
            CaseDoubt(
                doubt_id=f"d.outcome_gone.{goal}"[:128],
                because=f"the outcome “{outcome[1]}” is in no file and in no table the plan still makes",
                would_do="ask again which column says it once the plan makes a table that has one",
                if_not=f"{brief.goal_name} cannot be checked",
            ),
            "The column the person settled as the outcome is in no table the plan makes now.",
        )
        return found.facts({"outcome": None, "gone": True, "unevaluated": list(_AFTER_OUTCOME)})
    if outcome is None or not decisions(case).by_person("outcome", goal_id=goal):
        # Nobody decided it, or only the author has (in the plan, undecided): a column named exactly as the goal names
        # it is suggested again each time, else the store would take it back as "no longer proposed".
        exact = [
            (loaded, column)
            for loaded in files
            for column in _named(
                [str(c) for c in loaded.frame.columns], [brief.outcome.column or ""], parts=False
            )
        ]
        if len(exact) == 1:
            loaded, column = exact[0]
            found.create(
                "outcome_column",
                {"upload_id": loaded.upload_id, "column": column},
                title=f"“{column}” in {loaded.file_name or 'this file'} is the outcome: {brief.outcome.definition}",
                upload_id=loaded.upload_id,
                confidence=AgentConfidence.SURE,
                evidence=(),
                goal_id=goal,
                reason=f"“{column}” is named as {brief.goal_name} names its outcome column.",
            )
            outcome = (loaded.upload_id, column)
        elif outcome is None:
            likely = exact or [
                (loaded, column)
                for loaded in files
                for column in _named(
                    [str(c) for c in loaded.frame.columns], brief.outcome.synonyms, parts=True
                )
            ]
            built_likely: list[tuple[LoadedFile, str]] = []
            offered = False
            if not likely:
                built_likely = _built_outcomes(current, brief, case)
                likely = built_likely + [
                    pair
                    for pair in _prepared_outcomes(case, brief, files, current)
                    if pair not in built_likely
                ]
            if not likely:
                # No build in the plan yet: the goal's build is proposed. It declares its own outcome column, so no
                # question is asked for it: the person's yes to the build covers the outcome.
                offered = offer_build(run, case, brief, files, found)
            if likely:
                choices = [
                    (
                        f"{column} ({loaded.file_name})" if len(files) > 1 else column,
                        OptionEffect(
                            option_id=f"c{index}",
                            kind="outcome_column",
                            title=f"“{column}” is the outcome: {brief.outcome.definition}",
                            payload={"upload_id": loaded.upload_id, "column": column},
                        ),
                    )
                    for index, (loaded, column) in enumerate(likely[:MAX_OFFERED])
                ]
                choices.append(("None of these", OptionEffect(option_id="none")))
                what = brief.outcome.definition.rstrip(".") or f"the outcome of {brief.goal_name}"
                # A column a step the person approved makes is a fact, not a guess: it is the one recommended.
                made = {(b.upload_id, c) for b, c in built_likely if _built_by_approved(case, current, c)}
                approved = next(
                    (
                        f"c{index}"
                        for index, (lf, column) in enumerate(likely[:MAX_OFFERED])
                        if (lf.upload_id, column) in made
                    ),
                    None,
                )
                found.ask(
                    "OUTCOME_COLUMN",
                    {"files": [f.upload_id for f in files]},
                    text=f"Which column says whether: {what}?",
                    choices=choices,
                    upload_id=(
                        likely[0][0].upload_id if len({lf.upload_id for lf, _c in likely}) == 1 else None
                    ),
                    goal_id=goal,
                    recommended=approved,
                    why=(
                        "The training table in this plan builds it, and your yes to the plan covers that."
                        if approved is not None
                        else ""
                    ),
                    safe="none",
                    reason=f"{len(likely)} column(s) could say whether: {brief.outcome.definition}",
                )
            elif not offered:
                found.doubt(
                    CaseDoubt(
                        doubt_id=f"d.outcome_missing.{goal}"[:128],
                        because=f"none of the files has a column saying whether: {brief.outcome.definition}",
                        would_do="ask the team that has it for that column, one row per customer (the data request)",
                        if_not=f"{brief.goal_name} cannot be trained on these files",
                    ),
                    "No file has a column for the outcome and no table can define it.",
                )
                # Plan O M246 (DEC-1816; the design §6.8): ask for the data, not just stop.
                found.request(
                    DataRequest(
                        kind="more_data",
                        what=f"a column saying whether: {brief.outcome.definition.rstrip('.')}",
                        why=f"{brief.goal_name} learns from it.",
                        columns=tuple(c for c in (brief.outcome.column,) if c),
                        goal_id=goal,
                    ),
                    f"{brief.goal_name} learns from a column saying whether: {brief.outcome.definition}",
                )
    return found.facts(
        {
            "outcome": list(outcome) if outcome is not None else None,
            "gone": False,
            "unevaluated": list(_AFTER_OUTCOME) if outcome is None else [],
        }
    )


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------
def suggest_build(run: FactRun, case: Case, brief: GoalBrief) -> Facts:
    """No file has the goal's outcome, but an event table can define it (`engine.doctor.buildable`): the build, as the
    ordinary point-in-time `sql_query` step through `propose_sql_step`, with the receipt of its trial."""
    found = Found()
    made = offer_build(run, case, brief, run.current(case).files, found)
    return found.facts({"offered": made})


def offer_build(
    run: FactRun, case: Case, brief: GoalBrief, files: Sequence[LoadedFile], found: Found
) -> bool:
    """No file has the goal's outcome, but an event table can define it (`engine.doctor.buildable`): suggest the build
    - one row per customer at a snapshot, behaviour measured on or before it and the outcome read from the window
    after it, through the ordinary point-in-time `sql_query` step - instead of asking for a column. The goal needs it,
    so it is in the plan and it needs the person's own yes, which the one Approve gives (never silently left out);
    its own date is the training snapshot, and the case's as-of date (the scoring date) is checked against it as for
    any query. True when a suggestion was made; False leaves the request to ask for the column (the goal does not
    allow building, the table is too small, the trial refused the query, or the person already declined this goal's
    build: it is not suggested again, and the column is asked for instead)."""
    from engine.agent.sql_step import table_alias
    from engine.config import load_use_case
    from engine.doctor.agent_apply import person_owns
    from engine.doctor.buildable import buildable_outcome, compile_build
    from engine.doctor.materialize import table_names
    from engine.doctor.tools_sql import SqlStepProposal

    try:
        config = load_use_case(brief.goal_id, run.deps.config_root)
    except Exception:  # a goal whose configuration cannot be read is asked for the column, as before
        return False
    declined = any(  # the person's no; a build the author swapped out for a newer one is not a decline
        i.kind == "recipe_step"
        and i.goal_id == brief.goal_id
        and i.state is ProposalState.REJECTED
        and "buildable" in i.payload
        and person_owns(case, i)
        for i in _section(case)
    )
    if declined:
        return False
    settings = load_goal_settings(brief.goal_id, run.deps.config_root)
    decided = decisions(case)
    window = settings.value("window_days", decided) if "window_days" in settings.names else None
    built = buildable_outcome(
        files, config, run.deps.config_root, window_days=window if isinstance(window, int) else None
    )
    if built is None or not built.enough or built.snapshot is None:
        return False
    loaded = next((f for f in files if f.upload_id == built.upload_id), None)
    if loaded is None:
        return False
    # What the goal's configuration has the person say first (the audience of a goal about customers who have gone
    # quiet, say): who the table is about is a meaning, so they pick it from both readings' counts (the AI may point
    # at one in the chat, with its reason). Asked again on every pass while it is open: a question the author stops
    # suggesting is taken back, and the plan would go to Approve (and back) as other answers come in.
    open_questions = open_setting_questions(settings, case, built)
    if open_questions:
        if any(
            q.code == "BUILD_AUDIENCE" and q.goal_id == brief.goal_id and q.answer == "none"
            for q in case.questions
        ):
            return False  # an older case's "neither": the column is asked for instead
        for ask in open_questions:
            found.put(ask)
        return True
    audience = None
    if built.audiences:
        wanted = settings.value("audience", decided)
        audience = next((a for a in built.audiences if a.audience_id == wanted and a.enough), None)
        if audience is None:  # a reading too small to learn from: the column is asked for instead
            return False
    # The plan already carries this build (it is proposed again every time): it is tried as if it were not there, else
    # its own table is among the ones it reads from.
    others = [
        i
        for i in _section(case)
        if not (i.kind == "recipe_step" and i.goal_id == brief.goal_id and "buildable" in i.payload)
    ]
    ctx = run.context(case, files=files, case_items=others)
    table = table_names(ctx.files)[loaded.upload_id]
    try:
        sql, description = compile_build(built, config, table_alias(table), audience)
        result = run.call(
            "propose_sql_step",
            {
                "description": description,
                "sql": sql,
                "output_name": f"training_{_norm(brief.goal_id)}"[:OUTPUT_NAME_CHARS],
                "upload_ids": [loaded.upload_id],
                "as_of": built.snapshot.isoformat(),
                "reads_future": True,
                "outcome_window_days": built.window_days,
            },
            ctx,
        )
    except DataToolError as exc:
        _LOG.info("doctor.build_offer_refused goal=%s code=%s", brief.goal_id, exc.code)
        return False
    found.evidence.append(result)
    made = SqlStepProposal.model_validate(result.output)
    payload = made.payload()
    payload["needs_yes"] = True
    payload["buildable"] = built.model_dump(mode="json")
    if audience is not None:
        payload["audience"] = audience.model_dump(mode="json")
    _say_build_counts(
        payload, built, config.entity or "customer", audience, decisions(case).training_as_of_date
    )
    # The goal needs it, so it is in the plan: but nobody has decided it yet. The person's yes to its sentence (the
    # customers the audience leaves out are said in it, by group and reason) comes with Approve, and that yes is what
    # records who decided it and the count they agreed to (`confirm`).
    found.create(
        "recipe_step",
        payload,
        title=made.title,
        upload_id=loaded.upload_id,
        confidence=AgentConfidence.CHECK,
        evidence=(result.evidence_id,),
        goal_id=brief.goal_id,
        reason=f"No file has the outcome of {brief.goal_name}; the event table can define it at {built.snapshot}.",
    )
    return True


def _say_build_counts(
    payload: dict[str, Any],
    built: BuildableOutcome,
    entity: str,
    audience: BuildAudience | None,
    decided: date | None,
) -> None:
    """The build's sentence says what the sample could only estimate (the table has one row per customer in the
    audience, counted on every row of the file) and, in plain words, who the audience leaves out."""
    from engine.doctor.plan_check import replace_estimate

    sentence = str(payload.get("sentence") or "")
    rows = audience.customers if audience is not None else built.active_before
    estimate = payload.get("sentence_estimate")
    if built.exact and isinstance(estimate, dict):
        # The customers clause is a sample's too; the sentence below says who the audience leaves out.
        sentence = replace_estimate(sentence, estimate, rows)
        payload.pop("sentence_estimate")
    if built.snapshot is not None:
        plural = entity if entity.endswith("s") else f"{entity}s"
        # Each group left out has its own reason: not known yet at the snapshot (no activity on or before it), and, for
        # the gone-quiet audience, still active in the days before it.
        not_yet = max(0, built.ids - built.active_before)
        quiet = audience if audience is not None and audience.audience_id == "quiet" else None
        not_quiet = max(0, built.active_before - quiet.customers) if quiet is not None else 0
        reasons = []
        if not_yet:
            reasons.append(
                f"{not_yet:,} have no activity on or before {built.snapshot:%d %b %Y}, so they were not "
                "known by then"
            )
        if quiet is not None and not_quiet:
            reasons.append(
                f"{not_quiet:,} were active in the {quiet.quiet_days} days before "
                f"{built.snapshot:%d %b %Y}, so they have not gone quiet"
            )
        if reasons:
            sentence += (
                f" Left out, of the file's {built.ids:,} {plural}: "
                + "; ".join(reasons)
                + (
                    ". Who is kept is the audience you chose."
                    if audience is not None
                    else ". The table is about those active by then."
                )
            )
    if (
        decided is not None
        and built.snapshot is not None
        and built.snapshot != decided
        and built.snapshot + timedelta(days=built.window_days) <= decided
    ):
        # The two dates of a training table: said here, so the difference is not a stop to say yes to.
        sentence += (
            f" It is cut at {built.snapshot:%d %b %Y}: the {built.window_days} days after it give the outcome, "
            f"and {decided:%d %b %Y} is the date your case is read at."
        )
    payload["sentence"] = sentence


# ---------------------------------------------------------------------------
# The date each row describes, and consent
# ---------------------------------------------------------------------------
def time_facts(
    run: FactRun,
    case: Case,
    brief: GoalBrief,
    upload_id: str,
    *,
    look: LoadedFile | None = None,
    current: CurrentTables | None = None,
) -> Facts:
    """The date each row describes, for a goal whose split or outcome window needs one.

    A column named like one the goal lists is a `Create as_of_column`; otherwise, for a goal that requires one, the
    columns that read as dates are the question `AS_OF_COLUMN`, or a doubt when none does.

    `look` is the table the date is looked for in when it is not the file itself (the table a query built, which holds
    the outcome); the item is still recorded against `upload_id`, the file it is made from."""
    found = Found()
    need = brief.column("time")
    if need is None or decisions(case).by_person("as_of_column", goal_id=brief.goal_id):
        return found.facts({"looked": False})
    pool = current.files if current is not None else run.files(case)
    loaded = look or next((lf for lf in pool if lf.upload_id == upload_id), None)
    if loaded is None:
        return found.facts({"looked": False})
    named = _named([str(c) for c in loaded.frame.columns], need.names, parts=False)
    if named:
        found.create(
            "as_of_column",
            {"upload_id": upload_id, "column": named[0]},
            title=f"“{named[0]}” is the date each row describes.",
            upload_id=upload_id,
            confidence=AgentConfidence.SURE,
            evidence=(),
            goal_id=brief.goal_id,
            reason=f"“{named[0]}” is named like the date {brief.goal_name} splits by.",
        )
        return found.facts({"looked": True, "columns": named})
    if not need.required:
        return found.facts({"looked": True, "columns": []})
    result = run.call(
        "find_time_columns",
        {"upload_id": loaded.upload_id},
        run.context(case, files=(*pool, loaded) if look is not None else pool),
    )
    found.evidence.append(result)
    candidates = [str(c["column"]) for c in result.output.get("columns", []) if isinstance(c, dict)]
    if not candidates:
        found.doubt(
            CaseDoubt(
                doubt_id=f"d.time_missing.{brief.goal_id}"[:128],
                because="no column of the file reads as a date",
                would_do="ask for the date each row describes",
                if_not=f"{brief.goal_name} splits by date and cannot be tested without one",
            ),
            "No column of the file reads as a date.",
        )
        return found.facts({"looked": True, "columns": []})
    choices = [
        (
            column,
            OptionEffect(
                option_id=f"c{index}",
                kind="as_of_column",
                title=f"“{column}” is the date each row describes.",
                payload={"upload_id": upload_id, "column": column},
            ),
        )
        for index, column in enumerate(candidates[:MAX_OFFERED])
    ]
    choices.append((DONT_KNOW, OptionEffect(option_id="none")))
    found.ask(
        "AS_OF_COLUMN",
        {"upload_id": upload_id},
        text="Which date does each row describe? Testing uses the newest dates.",
        choices=choices,
        upload_id=upload_id,
        goal_id=brief.goal_id,
        reason=f"{len(candidates)} column(s) read as dates and none is named like the one {brief.goal_name} needs.",
    )
    return found.facts({"looked": True, "columns": candidates[:MAX_OFFERED]})


def consent_facts(
    case: Case,
    brief: GoalBrief,
    upload_id: str,
    files: Sequence[LoadedFile],
    *,
    look: LoadedFile | None = None,
) -> Facts:
    """A contacting goal's consent column: suggested, never in the plan on its own (it decides who may be contacted).
    A required column that is not there is a doubt. `look` is the table a query built that holds the outcome, when
    the column is looked for there."""
    found = Found()
    need = brief.column("consent")
    if need is None or decisions(case).by_person("consent", goal_id=brief.goal_id):
        return found.facts({"looked": False})
    loaded = look or next((lf for lf in files if lf.upload_id == upload_id), None)
    if loaded is None:
        return found.facts({"looked": False})
    named = _named([str(c) for c in loaded.frame.columns], need.names, parts=False)
    if named:
        found.create(
            "consent_column",
            {"upload_id": upload_id, "column": named[0]},
            title=f"“{named[0]}” says who agreed to be contacted.",
            upload_id=upload_id,
            confidence=AgentConfidence.CHECK,
            evidence=(),
            goal_id=brief.goal_id,
            reason=f"“{named[0]}” is named like the column that says who agreed to be contacted.",
        )
    elif need.required:
        found.doubt(
            CaseDoubt(
                doubt_id=f"d.consent_missing.{brief.goal_id}"[:128],
                because="no column says who agreed to be contacted",
                would_do="leave everyone without a recorded agreement off the list",
                if_not="people who did not agree could be contacted",
            ),
            "No column says who agreed to be contacted.",
        )
    return found.facts({"looked": True, "columns": named})


def goal_facts(run: FactRun, case: Case, brief: GoalBrief) -> tuple[Facts, ...]:
    """The goal's own checks in the order the rules ran them: the outcome column, then the date each row describes
    and consent, which are looked for in the outcome's table and so wait until the outcome is known."""
    from engine.doctor.combine_run import current_upload

    outcome = outcome_facts(run, case, brief)
    pair = outcome.facts["outcome"]
    if pair is None:
        return (outcome,)
    current = run.current(case)
    # The table an approved join made from the outcome's file is where the date and consent are looked for now.
    table = current_upload(case, pair[0])
    # ... and a table a query built is where they are looked for first, when it holds the outcome.
    built = current.built_with(pair[1])
    look = built.loaded if built is not None and built.origin.upload_id == table else None
    return (
        outcome,
        time_facts(run, case, brief, table, look=look, current=current),
        consent_facts(case, brief, table, current.files, look=look),
    )
