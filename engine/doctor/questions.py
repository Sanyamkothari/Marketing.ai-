r"""The questions Data Doctor may ask, as data: one catalogue, one way to build the ask.

The rules and the AI agent ask the same questions in the same way. Each entry says what is asked (a plain-English
template), the answers offered (ids `c0`, `c1`, ... as the visit numbers them), which one is recommended, whether the
question blocks the preview, and whether it is about meaning (the outcome, consent, which column is which): a
meaning-bearing question recommends nothing and is never answered by the rules.

:func:`build_ask` turns an entry plus what the data showed (values for the placeholders, the rows each answer
changes) into a protocol :class:`~engine.doctor.protocol.Ask`, so `settle` can decide to ask or not.

**"I don't know".** Every question ends with it. It is the safe choice, and it leads to :func:`who_would_know`: the
roles that would know, a message ready to copy and send, the step parked as "waiting for ...", and the safe choice
used meanwhile. Nothing here reads a file or calls an AI service.
"""

from __future__ import annotations

import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import JsonValue

from engine.doctor.decisions import Sets
from engine.doctor.protocol import DONT_KNOW, Ask, AskOption
from engine.doctor.visit_contracts import OptionEffect

__all__ = [
    "CATALOGUE",
    "CRM_ADMIN",
    "OptionSpec",
    "QuestionError",
    "QuestionSpec",
    "WhoWouldKnow",
    "build_ask",
    "catalogue_lines",
    "question",
    "ready_message",
    "render_options",
    "required_fields",
    "who_would_know",
]

CRM_ADMIN: Final[str] = "CRM admin"
DATABASE_ADMIN: Final[str] = "database administrator"
COMPLIANCE: Final[str] = "compliance officer"
FINANCE: Final[str] = "finance team"
CAMPAIGN_MANAGER: Final[str] = "campaign manager"
EXPORTER: Final[str] = "person who exported the file"


class QuestionError(ValueError):
    """The catalogue was used wrongly (an unknown id, a missing value); a mistake in the caller, not the client's."""


@dataclass(frozen=True)
class OptionSpec:
    """One answer. `label` is a template; `repeat` fills one option per detected choice; `optional` drops it
    when a value it needs is not known."""

    label: str
    repeat: bool = False
    optional: bool = False
    effect: str = ""
    """What choosing it does, in a plain sentence (no counts: code measures those). Shown only where the option
    carries a step; a repeated option has none (its label is already a sentence)."""
    value_kind: Literal["date", "number", "text"] | None = None
    """Set when choosing it needs the person to type a value of this kind with the answer ("I will type a date")."""
    sets: Sets | None = None
    """The decision choosing it sets (`engine.doctor.decisions`): the as-of date, the outcome or the key. `note` says
    on purpose that it sets nothing; None = not declared (an option that adds a step makes its own item)."""


@dataclass(frozen=True)
class QuestionSpec:
    qid: str
    code: str
    """The ask's code (stable; with the subject it makes the question's id)."""
    topic: str
    """What it is about, in a few plain words (the agent's list of questions)."""
    text: str
    why: str
    options: tuple[OptionSpec, ...]
    """The answers, without "I don't know" (it is always added last)."""
    recommended: int | None = None
    """Index of the recommended fixed option, if the catalogue recommends one."""
    recommend_from_data: bool = False
    """The recommended answer is one the data points to: the caller names it (`recommended_choice`)."""
    blocking: bool = False
    meaning_bearing: bool = False
    """About meaning: never answered by the rules, nothing recommended."""
    chat_only: bool = False
    """Asked only when the person says it in the chat (the review's prompt does not list it)."""
    roles: tuple[str, ...] = (CRM_ADMIN,)
    """Who would know, when the person does not."""
    safe_default: str = ""
    """What is used meanwhile, in plain words, after "will"."""
    from_code: bool = False
    """Asked only by code that carries the change each answer makes (the review's unpivot): the model cannot ask it."""

    @property
    def recommends(self) -> bool:
        return not self.meaning_bearing and (self.recommended is not None or self.recommend_from_data)


CATALOGUE: Final[Mapping[str, QuestionSpec]] = {
    spec.qid: spec
    for spec in (
        QuestionSpec(
            qid="Q1",
            code="Q01_MAIN_ID",
            topic="main customer ID",
            text="Which column is the main customer ID in {file}? Pick the one your campaign contacts: a "
            "person, an account or a household.",
            why="More than one column looks like an ID, and they may name different things.",
            options=(
                OptionSpec(
                    "{choice}",
                    repeat=True,
                ),
            ),
            recommend_from_data=True,
            blocking=True,
            roles=(CRM_ADMIN, CAMPAIGN_MANAGER),
            safe_default="keep the files apart and not join them",
        ),
        QuestionSpec(
            qid="Q2",
            code="Q02_SAME_PERSON",
            topic="merge look-alike customers",
            text="{count} customers in {file} look like the same person. Merge them?",
            why="Merging joins their history; keeping them apart may count one person twice.",
            options=(
                OptionSpec(
                    "Merge when the phone number is the same",
                    effect="Customers with the same phone number become one customer.",
                ),
                OptionSpec(
                    "Merge when the phone number and date of birth are the same",
                    effect="Customers with the same phone number and date of birth become one customer.",
                ),
                OptionSpec(
                    "Keep them separate", effect="Nothing is merged; look-alikes stay separate customers."
                ),
            ),
            recommended=1,
            roles=(CRM_ADMIN,),
            safe_default="keep them separate",
        ),
        QuestionSpec(
            qid="Q3",
            code="Q03_ID_LENGTH",
            topic="ID length and letter case",
            text="How many digits should the ID “{column}” have, and do upper and lower case letters differ?",
            why="The IDs in {column} come in more than one length, so zeros may have been cut off.",
            options=(OptionSpec("{choice}", repeat=True),),
            roles=(CRM_ADMIN, DATABASE_ADMIN),
            safe_default="read IDs as text exactly as they are, without adding zeros",
        ),
        QuestionSpec(
            qid="Q4",
            code="Q04_BLOCK_OR_SHEET",
            topic="which sheet or block",
            text="{file} has several {kind}. Which one holds the customer table?",
            why="Each one has its own header row, so the wrong pick reads the wrong columns.",
            options=(OptionSpec("{choice}", repeat=True),),
            roles=(EXPORTER,),
            safe_default="leave the extra sheets out and say which ones",
        ),
        QuestionSpec(
            qid="Q5",
            code="Q05_BLANK_MEANING",
            topic="what a blank means",
            text="What does a blank mean in {columns}?",
            why="{count} customers have a blank here; zero and unknown give different results.",
            options=(
                OptionSpec("Nothing happened (count it as 0)", effect="Blanks count as 0."),
                OptionSpec("Not known", effect="Blanks stay empty, as unknown."),
                OptionSpec(
                    "Not applicable", effect="Blanks stay empty; they do not apply to these customers."
                ),
                OptionSpec(
                    "Not yet (it fills in later)", effect="Blanks stay empty until the value fills in later."
                ),
            ),
            recommended=1,
            roles=(CRM_ADMIN, DATABASE_ADMIN),
            safe_default="treat blanks as not known and leave them empty",
        ),
        QuestionSpec(
            qid="Q6",
            code="Q06_UNIT_SCALE",
            topic="unit or scale",
            text="What unit is {column} in? Its values look like {hint}.",
            why="Mixed units make amounts differ by a factor, for example 100 between rupees and paise.",
            options=(OptionSpec("{choice}", repeat=True),),
            recommend_from_data=True,
            roles=(FINANCE, CRM_ADMIN),
            safe_default="leave the amounts as they are and convert nothing",
        ),
        QuestionSpec(
            qid="Q7",
            code="Q07_KNOWN_DATE",
            topic="which date says when it was known",
            text="Which date in {file} says when this was known, or when each customer's clock starts?",
            why="The wrong date lets the model see what happened after the as-of date.",
            options=(OptionSpec("{choice}", repeat=True),),
            meaning_bearing=True,
            roles=(CRM_ADMIN,),
            safe_default="leave this column out of the model",
        ),
        QuestionSpec(
            qid="Q8",
            code="Q08_AS_OF",
            topic="as-of date of the data",
            text="What date is this data true as of?",
            why="Everything is cut at this date; a wrong one leaks the future or drops recent customers.",
            options=(
                OptionSpec(
                    "The latest date in the file: {latest_date}",
                    effect="The data is cut at the latest date in the file.",
                    sets="as_of",
                ),
                OptionSpec(
                    "The date in the file name: {name_date}",
                    optional=True,
                    effect="The data is cut at the date in the file name.",
                    sets="as_of",
                ),
                OptionSpec(
                    "Counted back from the days-since column: {back_date}",
                    optional=True,
                    effect="The data is cut at the date counted back from the days-since column.",
                    sets="as_of",
                ),
                OptionSpec(
                    "I will type a date",
                    effect="Your date is used as the cut-off for the steps that follow.",
                    value_kind="date",
                    sets="as_of",
                ),
            ),
            blocking=True,
            meaning_bearing=True,
            roles=(EXPORTER, CRM_ADMIN),
            safe_default="use the last complete month in the file",
        ),
        QuestionSpec(
            qid="Q9",
            code="Q09_TAKEN_WHEN",
            topic="was the file taken today",
            text="Was {file} taken today, or on the as-of date {as_of}?",
            why="A file taken today holds statuses as they are now, which the model could not have known then.",
            options=(
                OptionSpec("Today", effect="Columns that only describe today are left out of the model."),
                OptionSpec(
                    "On the as-of date", effect="The file's statuses are used as they were on the as-of date."
                ),
                OptionSpec(
                    "A history table exists",
                    effect="The history table is used for statuses; add it when you have it.",
                ),
            ),
            meaning_bearing=True,
            roles=(EXPORTER, CRM_ADMIN),
            safe_default="treat it as taken today and leave out columns that only describe today",
        ),
        QuestionSpec(
            qid="Q10",
            code="Q10_WHO_COUNTS",
            topic="who counts as a customer",
            text="Who counts as a customer on {as_of}?",
            why="The answer changes how many customers are scored and the share of yes outcomes.",
            options=(
                OptionSpec(
                    "Bought in the last 12 months",
                    effect="Customers who bought in the last 12 months are scored; the rest are left out.",
                ),
                OptionSpec("Any order ever", effect="Anyone with an order ever is scored."),
                OptionSpec("Everyone in the master file", effect="Everyone in the master file is scored."),
            ),
            recommended=0,
            blocking=True,
            roles=(CAMPAIGN_MANAGER,),
            safe_default="use customers who bought in the last 12 months and show how many were left out",
        ),
        QuestionSpec(
            qid="Q11",
            code="Q11_WHICH_WINS",
            topic="which copy wins",
            text="{count} customers appear more than once with different values in {column}. Which copy wins?",
            why="Two copies disagree, and the one kept becomes the customer's value.",
            options=(
                OptionSpec("The later one", effect="Keeps the later copy of each repeated customer."),
                OptionSpec("The earlier one", effect="Keeps the earlier copy of each repeated customer."),
                OptionSpec(
                    "Yes if any copy says yes", effect="A customer counts as yes if any copy says yes."
                ),
                OptionSpec(
                    "The latest by {date_column}",
                    optional=True,
                    effect="Keeps the copy with the latest date.",
                ),
                OptionSpec(
                    "Leave these customers out",
                    effect="These customers are left out of training and testing.",
                ),
            ),
            meaning_bearing=True,
            roles=(CRM_ADMIN,),
            safe_default="leave these customers out",
        ),
        QuestionSpec(
            qid="Q12",
            code="Q12_VALUE_MEANING",
            topic="what a value means (yes, no or leave out)",
            text="In {column}, what does “{value}” mean? It appears in {count} rows.",
            why="The model learns from what you call a yes and a no.",
            options=(
                OptionSpec("A yes (positive)", effect="These rows count as yes."),
                OptionSpec("A no (negative)", effect="These rows count as no."),
                OptionSpec("Leave these rows out", effect="These rows are left out of training and testing."),
            ),
            blocking=True,
            meaning_bearing=True,
            roles=(CAMPAIGN_MANAGER, CRM_ADMIN),
            safe_default="leave these rows out of training",
        ),
        QuestionSpec(
            qid="Q13",
            code="Q13_HOLDOUT",
            topic="random holdout in a past campaign",
            text="In the past campaign {campaign}, was a random group deliberately not contacted?",
            why="Only a random group left out can show extra sales caused by the campaign.",
            options=(
                OptionSpec(
                    "Yes, a column marks them",
                    effect="A column in the file marks the group that was not contacted.",
                ),
                OptionSpec(
                    "Yes, there is a separate list",
                    effect="A separate list names the group that was not contacted.",
                ),
                OptionSpec(
                    "No",
                    effect="The campaign counts as having no random holdout; no extra sales are claimed.",
                ),
            ),
            recommended=2,
            roles=(CAMPAIGN_MANAGER,),
            safe_default="assume no random holdout and not claim extra sales caused by the campaign",
        ),
        QuestionSpec(
            qid="Q14",
            code="Q14_CONSENT_MEANING",
            topic="what consent values mean",
            text="In {column}, what does “{value}” mean? What was the consent given for, and how old may it be?",
            why="This decides who may be contacted.",
            options=(
                OptionSpec(
                    "They agreed to be contacted", effect="Customers with this value may be contacted."
                ),
                OptionSpec(
                    "They asked NOT to be contacted",
                    effect="Customers with this value are kept off the contact list.",
                ),
                OptionSpec(
                    "Marketing messages only", effect="Customers with this value get marketing messages only."
                ),
                OptionSpec(
                    "Service messages only", effect="Customers with this value get service messages only."
                ),
            ),
            blocking=True,
            meaning_bearing=True,
            roles=(COMPLIANCE, CRM_ADMIN),
            safe_default="treat blank and unclear values as not agreed, so these customers stay off the "
            "contact list",
        ),
        QuestionSpec(
            qid="Q15",
            code="Q15_SENSITIVE_USE",
            topic="sensitive column",
            text="{column} looks like {kind}. How may it be used?",
            why="Using it to choose who is contacted can treat groups unfairly.",
            options=(
                OptionSpec(
                    "In the fairness report only",
                    effect="Used in the fairness report only, never to choose who is contacted.",
                ),
                OptionSpec("Remove it", effect="The column is removed; the model never sees it."),
                OptionSpec("Use it", effect="The model may use it to choose who is contacted."),
            ),
            recommended=0,
            roles=(COMPLIANCE,),
            safe_default="use it in the fairness report only, never for targeting",
        ),
        QuestionSpec(
            qid="Q16",
            code="Q16_WHO_WOULD_KNOW",
            topic="who would know (follows any I don't know)",
            text="Who would know the answer to “{question}”?",
            why="A ready message is shown for whoever you pick, and the safe choice is used meanwhile.",
            options=(OptionSpec("{choice}", repeat=True),),
            roles=(CRM_ADMIN,),
            safe_default="keep the safe choice and wait for the answer",
        ),
        QuestionSpec(
            qid="Q17",
            code="Q17_BUSINESS_RULES",
            topic="business rules that always hold",
            text="Which business rules always hold in {file}? Each shows how many rows it would remove.",
            why="A row that breaks a rule is probably a data mistake.",
            options=(OptionSpec("{choice}", repeat=True),),
            meaning_bearing=True,
            roles=(CAMPAIGN_MANAGER, FINANCE),
            safe_default="apply no extra rules",
        ),
        QuestionSpec(
            qid="Q18",
            code="Q18_LOCALE",
            topic="time zone, financial year, date order",
            text="Which time zone, financial-year start and date order does {client} use?",
            why="Asked once per client; it fixes day boundaries, financial years and dates like 03/04.",
            options=(
                OptionSpec(
                    "India time (Asia/Kolkata), year starts in April, day first",
                    effect="Uses India time, a year starting in April and day-first dates.",
                ),
                OptionSpec(
                    "Something else: I will set them",
                    effect="You set the time zone, year start and date order yourself.",
                ),
            ),
            recommended=0,
            roles=(CRM_ADMIN,),
            safe_default="use India time, a year starting in April and day-first dates",
        ),
        QuestionSpec(
            qid="Q19",
            code="Q19_COLOURS_HIDDEN",
            topic="colour-coded or hidden rows",
            text="Rows in {file} are colour-coded or hidden. What do the colours mean, and should hidden rows "
            "be included?",
            why="A colour often marks a status that exists nowhere else in the file.",
            options=(
                OptionSpec("{choice}", repeat=True),
                OptionSpec(
                    "The colours mean nothing; include the hidden rows",
                    effect="The colours are ignored and the hidden rows are included.",
                ),
            ),
            roles=(EXPORTER,),
            safe_default="leave hidden rows out and ignore the colours",
        ),
        QuestionSpec(
            qid="Q20",
            code="Q20_ALL_OR_EXTRACT",
            topic="all customers or an extract",
            text="Is {file} all your customers, or an extract?",
            why="Totals and rates from an extract do not describe the whole business.",
            options=(
                OptionSpec("All customers", effect="The file counts as all your customers."),
                OptionSpec(
                    "A sample",
                    effect="The file counts as a sample, so its totals do not describe the whole business.",
                ),
                OptionSpec(
                    "One segment only",
                    effect="The file counts as one segment, so its totals are not compared with the whole business.",
                ),
            ),
            meaning_bearing=True,
            roles=(EXPORTER, CRM_ADMIN),
            safe_default="treat the file as a possible extract and not compare totals with the whole business",
        ),
        QuestionSpec(
            qid="Q21",
            code="Q21_ONE_ROW",
            topic="what one row is",
            text="Each “{id}” in {file} has several rows. {detail} What is one row?",
            why="Adding up or counting repeated rows counts the same order, ticket or customer more than once.",
            options=(OptionSpec("{choice}", repeat=True),),
            recommend_from_data=True,
            roles=(CRM_ADMIN, FINANCE),
            safe_default="leave the rows as they are and warn that totals may count the same thing twice",
        ),
        QuestionSpec(
            qid="Q22",
            code="Q22_REAL_MONEY",
            topic="which statuses are real money",
            text="Which values of “{column}” in {file} are real money? {detail}",
            why="A failed, declined or refunded row is not money received, but adding the amounts up counts it.",
            options=(OptionSpec("{choice}", repeat=True),),
            recommend_from_data=True,
            roles=(FINANCE, CRM_ADMIN),
            safe_default="count every row and warn that the total includes failed or refunded money",
        ),
        QuestionSpec(
            qid="Q23",
            code="Q23_WHO_CAN_RESPOND",
            topic="who can have the outcome",
            text="{count} customers in {file} {reason}. {positives} of them are marked as a yes in “{outcome}”. "
            "Leave them out of the outcome?",
            why="Customers who cannot have the outcome make the yes rate look different from the real one.",
            options=(
                OptionSpec(
                    "Yes, leave them out of the outcome",
                    effect="These customers are left out of the outcome.",
                ),
                OptionSpec(
                    "No, keep them as they are", effect="These customers stay in the outcome as they are."
                ),
            ),
            recommended=0,
            roles=(CAMPAIGN_MANAGER,),
            safe_default="keep them and show how many of them there are",
        ),
        QuestionSpec(
            qid="Q24",
            code="Q24_WHAT_COUNTS",
            topic="what counts as a yes",
            text="What should count as a yes in {file}?",
            why="I note it for your analyst. It does not change what the model learns yet: that still comes from "
            "the outcome the file has.",
            options=(OptionSpec("{choice}", repeat=True),),
            blocking=False,
            meaning_bearing=True,
            chat_only=True,
            roles=(CAMPAIGN_MANAGER,),
            safe_default="leave the outcome as the file has it",
        ),
        QuestionSpec(
            qid="Q25",
            code="Q25_UNPIVOT",
            topic="period columns into rows",
            text="{file} has one column per {kind} for {measures}. Turn them into rows, one per {entity} per "
            "{kind}? The first {rows_in} rows become {rows_out}.",
            why="As rows they can be combined back into one row per {entity}; as columns each {kind} is an "
            "unrelated column.",
            options=(
                OptionSpec(
                    "Yes, turn them into rows",
                    effect="One row per {entity} per {kind}; the rows can then be combined into one row per "
                    "{entity}.",
                ),
                OptionSpec("No, keep them as columns", effect="Each {kind} stays a separate column."),
            ),
            blocking=True,
            roles=(CRM_ADMIN,),
            safe_default="keep the columns as they are",
            from_code=True,
        ),
        QuestionSpec(
            qid="Q26",
            code="Q26_UNPIVOT_YEAR",
            topic="year of period columns",
            text="The {kind} columns of {measures} in {file} name no year. Which year do they cover? As rows, "
            "one per {entity} per {kind}, the first {rows_in} rows become {rows_out}.",
            why="Without a year the rows cannot be put in date order. The year you pick is written into the step.",
            options=(
                OptionSpec("{choice}", repeat=True),
                OptionSpec(
                    "None of these: keep them as columns", effect="Each {kind} stays a separate column."
                ),
            ),
            blocking=True,
            roles=(CRM_ADMIN,),
            safe_default="keep the columns as they are",
            from_code=True,
        ),
    )
}
"""Q1 to Q20 of the scenario study, and Q21 to Q23 (what one row is, which statuses are real money, who can have the
outcome) that its themes 3 and 5 need, and Q24 (what counts as a yes) that the chat asks when the person says it in
their own words. Q24 is a note for the analyst, not blocking: no outcome definition is built from it yet (the
outcome-definition items of `engine.doctor.outcome_definition` are not part of the visit), so it must not hold the
visit or say the rule is applied. "I don't know" is added to each at build time."""

_FORMATTER: Final[string.Formatter] = string.Formatter()


def question(qid: str) -> QuestionSpec:
    try:
        return CATALOGUE[qid]
    except KeyError:
        raise QuestionError(f"unknown question {qid!r}; the catalogue has {', '.join(CATALOGUE)}") from None


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in _FORMATTER.parse(template) if name}


def required_fields(spec: QuestionSpec) -> frozenset[str]:
    """The placeholders a caller must give: those of the text and why, and of every option that is not optional
    or repeated (a repeated option takes `choices`)."""
    found = _fields(spec.text) | _fields(spec.why)
    for option in spec.options:
        if not option.optional and not option.repeat:
            found |= _fields(option.label)
    return frozenset(found)


def _fill(template: str, values: Mapping[str, object]) -> str:
    missing = sorted(_fields(template) - set(values))
    if missing:
        raise QuestionError(f"no value for {', '.join(missing)} in “{template}”")
    return template.format_map({key: str(value) for key, value in values.items()})


def _rendered(
    spec: QuestionSpec, values: Mapping[str, object], choices: Sequence[str]
) -> list[tuple[OptionSpec, str, int | None]]:
    """Each answer offered: its entry, its label, and its place among `choices` when it came from one."""
    shown: list[tuple[OptionSpec, str, int | None]] = []
    for option in spec.options:
        if option.repeat:
            shown.extend(
                (option, _fill(option.label, {**values, "choice": choice}), index)
                for index, choice in enumerate(choices)
            )
        elif option.optional and not _fields(option.label) <= set(values):
            continue
        else:
            shown.append((option, _fill(option.label, values), None))
    if not shown:
        raise QuestionError(f"{spec.qid} has no answers to offer: it needs `choices`")
    return shown


def render_options(
    spec: QuestionSpec, values: Mapping[str, object], choices: Sequence[str] = ()
) -> tuple[str, ...]:
    """The answers' labels, in order, without "I don't know": repeated entries expand to `choices`, optional
    ones whose values are missing are dropped."""
    return tuple(label for _, label, _ in _rendered(spec, values, choices))


def build_ask(
    qid: str,
    values: Mapping[str, object],
    *,
    rows: int,
    choices: Sequence[str] = (),
    recommended_choice: int | None = None,
    rows_changed: Sequence[int | None] | None = None,
    label_rates: Sequence[float | None] | None = None,
    effects: Sequence[OptionEffect | None] | None = None,
    choice_says: Sequence[str] = (),
    upload_id: str | None = None,
    column: str | None = None,
    goal_id: str | None = None,
    rows_disagree: int | None = None,
    value_signature: tuple[str, ...] = (),
    evidence_ids: tuple[str, ...] = (),
    proven: str | None = None,
    by_code: bool = False,
) -> Ask:
    """A protocol `Ask` from a catalogue entry (`by_code`: the caller carries the change of each answer, which an
    entry marked `from_code` requires).

    `values` fill the placeholders. `rows_changed`, `label_rates` and `effects` are aligned with the answers as
    rendered (see :func:`render_options`), without "I don't know"; `effects` carry what an answer adds, and their
    option ids are replaced by `c0`, `c1`, ... `choice_says` are checked sentences of what each of `choices` does,
    one per choice, each replacing the catalogue's own where not empty. `recommended_choice` is the index of the data's own pick among
    `choices` (only for an entry that takes its recommendation from the data). "I don't know" is last, `c<n>`,
    and is the safe choice.
    """
    spec = question(qid)
    if spec.from_code and not by_code:
        raise QuestionError(f"{qid} is asked by Data Doctor itself; choose another question")
    if recommended_choice is not None and not 0 <= recommended_choice < len(choices):
        raise QuestionError(
            f"{qid}: recommended_choice {recommended_choice} is not one of the {len(choices)} choices"
        )
    shown = _rendered(spec, values, choices)
    labels = [label for _, label, _ in shown]
    count = len(labels)
    for name, given in (("rows_changed", rows_changed), ("label_rates", label_rates), ("effects", effects)):
        if given is not None and len(given) != count:
            raise QuestionError(f"{qid}: {name} has {len(given)} entries for {count} answers")
    if len(choice_says) != len(choices):
        choice_says = ()  # sentences that do not line up with the choices would sit on the wrong ones
    options = [
        AskOption(
            label=label,
            effect=_effect(effects[index] if effects else None, f"c{index}"),
            rows_changed=rows_changed[index] if rows_changed else None,
            label_rate=label_rates[index] if label_rates else None,
            says=_says(shown[index], values, choice_says),
            value_kind=shown[index][0].value_kind,
            sets=shown[index][0].sets,
        )
        for index, label in enumerate(labels)
    ]
    options.append(AskOption(label=DONT_KNOW, effect=OptionEffect(option_id=f"c{count}"), safe=True))
    if rows_disagree is None and rows_changed and all(changed is not None for changed in rows_changed):
        # "I don't know" is unmeasured; the spread the impact rule reads is that of the real answers.
        known = [int(changed) for changed in rows_changed if changed is not None]
        rows_disagree = max(known) - min(known)
    recommended = _recommended(spec, shown, recommended_choice)
    subject: dict[str, JsonValue] = {"question": qid, "upload": upload_id, "column": column}
    if "value" in values:
        subject["value"] = str(values["value"])
    return Ask(
        code=spec.code,
        subject=subject,
        text=_fill(spec.text, values),
        why=_fill(spec.why, values),
        options=tuple(options),
        rows=rows,
        rows_disagree=rows_disagree,
        upload_id=upload_id,
        column=column,
        goal_id=goal_id,
        blocking=spec.blocking,
        proven=proven,
        recommended=recommended,
        meaning_bearing=spec.meaning_bearing,
        value_signature=value_signature,
        evidence_ids=evidence_ids,
    )


def _says(
    shown: tuple[OptionSpec, str, int | None],
    values: Mapping[str, object],
    choice_says: Sequence[str],
) -> str:
    """One answer's sentence: the checked one for its choice, else the catalogue's own ("" when it has none)."""
    option, _, choice = shown
    if choice is not None and choice_says and choice_says[choice]:
        return choice_says[choice]
    if not option.effect:
        return ""
    return _fill(option.effect, values)


def _effect(given: OptionEffect | None, option_id: str) -> OptionEffect:
    if given is None:
        return OptionEffect(option_id=option_id)
    return given.model_copy(update={"option_id": option_id})


def _recommended(
    spec: QuestionSpec, shown: Sequence[tuple[OptionSpec, str, int | None]], recommended_choice: int | None
) -> str | None:
    """The recommended answer's id, or None (meaning-bearing, or the catalogue recommends nothing)."""
    if not spec.recommends:
        return None
    for index, (option, _, choice) in enumerate(shown):
        if spec.recommended is not None and option is spec.options[spec.recommended]:
            return f"c{index}"
        if spec.recommended is None and choice is not None and choice == recommended_choice:
            return f"c{index}"
    return None


# ---------------------------------------------------------------------------
# "I don't know": who would know, and a message ready to send (Q16)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WhoWouldKnow:
    """What follows "I don't know": who could answer, what to send them, what is used meanwhile."""

    qid: str
    roles: tuple[str, ...]
    message: str
    """Plain text, ready to copy and send."""
    safe_default: str
    """What is used until the answer comes, in a sentence."""
    waiting_for: str
    """The step's state while it waits: "Waiting for the CRM admin"."""


def who_would_know(
    qid: str,
    values: Mapping[str, object],
    *,
    choices: Sequence[str] = (),
    file_name: str | None = None,
    role: str | None = None,
) -> WhoWouldKnow:
    """The roles, the ready message and the safe default for a question the person cannot answer.

    `role` names the person it is addressed to (default: the first role). Nothing is sent from here.
    """
    spec = question(qid)
    chosen = role if role in spec.roles else spec.roles[0]
    return WhoWouldKnow(
        qid=qid,
        roles=spec.roles,
        message=ready_message(qid, values, choices=choices, file_name=file_name, role=chosen),
        safe_default=f"Until then I will {spec.safe_default}.",
        waiting_for=f"Waiting for the {chosen}",
    )


def ready_message(
    qid: str,
    values: Mapping[str, object],
    *,
    choices: Sequence[str] = (),
    file_name: str | None = None,
    role: str | None = None,
) -> str:
    """A short message to the person who would know, with the question and the possible answers."""
    spec = question(qid)
    labels = render_options(spec, values, choices)
    who = role or spec.roles[0]
    subject = f" for {file_name}" if file_name else ""
    answers = "\n".join(f"  - {label}" for label in labels)
    return (
        f"Hello,\n\n"
        f"We are preparing your customer data{subject} for a marketing campaign, and we need one answer "
        f"that only the {who} can give.\n\n"
        f"Question: {_fill(spec.text, values)}\n"
        f"Why it matters: {_fill(spec.why, values)}\n\n"
        f"The possible answers:\n{answers}\n\n"
        f"Could you reply with the one that is right? If you are unsure, tell us who is.\n\n"
        f"Thank you."
    )


def catalogue_lines(*, chat: bool = False) -> tuple[str, ...]:
    """One line per question for the agent's prompt: its id, topic, whether it blocks, and the values it needs.
    The chat's prompt (`chat`) also lists the questions only the chat asks."""
    lines = []
    for spec in CATALOGUE.values():
        if (spec.chat_only and not chat) or spec.from_code:
            continue
        need = ", ".join(
            sorted(required_fields(spec) | ({"choices"} if any(o.repeat for o in spec.options) else set()))
        )
        mark = " (blocking)" if spec.blocking else ""
        lines.append(f"{spec.qid} {spec.topic}{mark}: {need or 'no values'}")
    return tuple(lines)
