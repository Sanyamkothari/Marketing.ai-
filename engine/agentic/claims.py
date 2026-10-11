"""Wording without trusting the model: structured claims, a verifier and a renderer (Plan K M139, design §4).

`numbers_grounded` (Plan G) checks that every number in a sentence appears in the evidence. That is necessary
but not sufficient: "the e-mail beats no e-mail" can be number-grounded and still false when the interval of
the difference includes zero. So a model never writes a finished sentence here. It returns **structured
narrative** - JSON whose sentences are templates with slots, each carrying one **claim** that references
evidence by id:

```json
{"sentences": [
  {"template": "{arm_a} {cmp} {arm_b} on {metric}.",
   "claim": {"type": "comparison", "left": "ev_3f1c…:arms.treated", "right": "ev_3f1c…:arms.control",
             "metric": "reactivated_90d", "comparator": "gt"}},
  {"template": "The result is {label}.", "claim": {"type": "label", "ref": "ev_3f1c…:label", "value": "Causal"}}
]}
```

**The verifier** (:func:`verify_sentence`) checks each claim against the evidence it names, before anything is
rendered:

| Claim | Passes only when | Otherwise |
|---|---|---|
| `comparison` `gt` / `lt` | the difference's interval excludes 0 in that direction | the other direction: `CLAIM_DIRECTION` (dropped); an interval that includes 0: corrected to "is not distinguishable from" (kept, the correction recorded with `CLAIM_DIRECTION`) |
| `comparison` `eq` | the interval includes 0 (rendered "is not distinguishable from", never "equal") | `CLAIM_DIRECTION` |
| `label` | the label equals the evidence's label ("Causal" only where the tool returned Causal) | `CLAIM_LABEL` |
| `trend` | at least 3 measured points, every change in the claimed direction, and the endpoints' intervals do not overlap | `CLAIM_DIRECTION` |
| `recommendation` | it names a proposal the policy allowed | `CLAIM_UNREFERENCED` |

A reference to evidence that is not in the index, a slot the claim type does not have, or a template that
leaves out the slot stating the claim (`{cmp}`, `{label}`, `{direction}`, `{proposal}`) is `CLAIM_UNREFERENCED`.

**The renderer fills every slot from the evidence** - arm names, labels, the comparison words, and every
number - so the model cannot type a number into a sentence. **The model's own words** (the template with its
slots removed) go through Plan G's number check (any digit fails; `engine.agent.grounding.numbers_in`), a list of
claim words that would state something outside a claim - a direction or an outcome verb (`up`, `more`, `grow`,
`improve`, `surge`), a cause or a certainty (`because`, `sure`, `win`), a spelled-out number or a multiplier
(`ten`, `twice`, `times`, `a third`, `tenfold`, `lakh`) - and the deterministic guardrails
(`engine.generative.guardrails`: personal data, banned phrases, links, length): `CLAIM_FREE_TEXT`. A JSON
answer that does not parse, or a sentence that is not shaped as above, is `CLAIM_MALFORMED`.

**On failure** one failed sentence is dropped; more than one, or a narrative that does not parse, or one with
no sentence left, is replaced as a whole by the **rules-only summary** (:func:`rules_narrative`), which code
writes from the same evidence and which passes the same verifier. Failures and corrections are kept with their
codes for the eval suite (`engine.agentic.evals`, `claim_failures`).
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import Field, TypeAdapter, ValidationError

from engine.agentic.registry import EvidenceLabel
from engine.config import StrictBase

__all__ = [
    "CLAIM_CODES",
    "CLAIM_DIRECTION",
    "CLAIM_FREE_TEXT",
    "CLAIM_LABEL",
    "CLAIM_MALFORMED",
    "CLAIM_UNREFERENCED",
    "SLOTS",
    "Claim",
    "ClaimFinding",
    "ComparisonClaim",
    "ComparisonFact",
    "EvidenceIndex",
    "Interval",
    "LabelClaim",
    "Narrative",
    "RecommendationClaim",
    "RenderedSentence",
    "SentenceSpec",
    "SeriesFact",
    "StructuredNarrative",
    "TrendClaim",
    "format_value",
    "guardrails_for",
    "parse_narrative",
    "rules_narrative",
    "trend_holds",
    "verify_narrative",
    "verify_sentence",
]

CLAIM_DIRECTION: Final[str] = "CLAIM_DIRECTION"
CLAIM_LABEL: Final[str] = "CLAIM_LABEL"
CLAIM_UNREFERENCED: Final[str] = "CLAIM_UNREFERENCED"
CLAIM_FREE_TEXT: Final[str] = "CLAIM_FREE_TEXT"
CLAIM_MALFORMED: Final[str] = "CLAIM_MALFORMED"
CLAIM_CODES: Final[frozenset[str]] = frozenset(
    {CLAIM_DIRECTION, CLAIM_LABEL, CLAIM_UNREFERENCED, CLAIM_FREE_TEXT, CLAIM_MALFORMED}
)

MAX_SENTENCES: Final[int] = 12
MAX_TEMPLATE_CHARS: Final[int] = 300

Unit = Literal["share", "count", "inr"]
"""How a difference is written: a share as percentage points, a count of outcomes, or rupees."""


# ---------------------------------------------------------------------------
# What the model returns
# ---------------------------------------------------------------------------
class ComparisonClaim(StrictBase):
    type: Literal["comparison"]
    left: str = Field(max_length=120, description="`ev_…:arms.<name>`: one side of the comparison.")
    right: str = Field(max_length=120, description="`ev_…:arms.<name>`: the other side.")
    metric: str = Field(max_length=120, description="The metric the evidence compares them on.")
    comparator: Literal["gt", "lt", "eq"]


class LabelClaim(StrictBase):
    type: Literal["label"]
    ref: str = Field(max_length=120, description="`ev_…:label`.")
    value: str = Field(max_length=40, description="The label the sentence asserts.")


class TrendClaim(StrictBase):
    type: Literal["trend"]
    ref: str = Field(max_length=120, description="`ev_…:series.<name>`: measured points in order.")
    direction: Literal["up", "down"]


class RecommendationClaim(StrictBase):
    type: Literal["recommendation"]
    proposal_id: str = Field(max_length=40)


Claim = Annotated[
    ComparisonClaim | LabelClaim | TrendClaim | RecommendationClaim, Field(discriminator="type")
]


class SentenceSpec(StrictBase):
    """One sentence as the model (or the rules) writes it: a template, and at most one claim."""

    template: str = Field(max_length=MAX_TEMPLATE_CHARS)
    claim: Claim | None = None


class StructuredNarrative(StrictBase):
    sentences: tuple[SentenceSpec, ...] = Field(max_length=MAX_SENTENCES)


_SENTENCE: Final[TypeAdapter[SentenceSpec]] = TypeAdapter(SentenceSpec)


# ---------------------------------------------------------------------------
# What the evidence says (built by the planner from tool results)
# ---------------------------------------------------------------------------
class Interval(StrictBase):
    estimate: float
    low: float
    high: float
    level: float = Field(default=0.95, gt=0.0, lt=1.0)


class ComparisonFact(StrictBase):
    """`left` minus `right` on `metric`, with its interval, as a tool computed it."""

    left: str
    right: str
    metric: str
    metric_label: str
    unit: Unit
    difference: Interval


class SeriesFact(StrictBase):
    """Measured points in the order they were measured (oldest first)."""

    name: str
    metric_label: str
    unit: Unit
    points: tuple[Interval, ...]


class EvidenceIndex(StrictBase):
    """Every fact a sentence may state, keyed by `<evidence_id>:<path>`; built from tool results only."""

    arms: dict[str, str] = Field(default_factory=dict, description="ref -> display name.")
    comparisons: tuple[ComparisonFact, ...] = ()
    labels: dict[str, EvidenceLabel] = Field(default_factory=dict, description="ref -> label.")
    label_subjects: dict[str, str] = Field(default_factory=dict, description="ref -> what is labelled.")
    series: dict[str, SeriesFact] = Field(default_factory=dict)
    proposals: dict[str, str] = Field(
        default_factory=dict, description="Proposal ids the policy allowed -> what approving does."
    )

    def comparison(self, left: str, right: str, metric: str) -> tuple[ComparisonFact, bool] | None:
        """The fact comparing `left` with `right` on `metric`, and whether it had to be read reversed."""
        for fact in self.comparisons:
            if fact.metric != metric:
                continue
            if (fact.left, fact.right) == (left, right):
                return fact, False
            if (fact.left, fact.right) == (right, left):
                return fact, True
        return None


# ---------------------------------------------------------------------------
# What is shown
# ---------------------------------------------------------------------------
class RenderedSentence(StrictBase):
    text: str
    claim_type: str | None
    refs: tuple[str, ...] = Field(description="The evidence references the sentence states.")


class ClaimFinding(StrictBase):
    sentence: int = Field(description="Position of the sentence in the model's answer (0-based); -1 for all.")
    code: str
    reason: str


class Narrative(StrictBase):
    """The wording a person sees under a proposal; never needed to approve it."""

    source: Literal["model", "rules"]
    sentences: tuple[RenderedSentence, ...]
    failures: tuple[ClaimFinding, ...] = ()
    corrections: tuple[ClaimFinding, ...] = ()
    replaced: bool = Field(
        default=False, description="True when the model's narrative was replaced by the rules-only summary."
    )

    @property
    def text(self) -> str:
        return " ".join(sentence.text for sentence in self.sentences)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
SLOTS: Final[dict[str | None, frozenset[str]]] = {
    "comparison": frozenset({"arm_a", "arm_b", "cmp", "metric", "difference", "low", "high", "level"}),
    "label": frozenset({"label", "subject"}),
    "trend": frozenset({"series", "direction", "metric", "first", "last"}),
    "recommendation": frozenset({"proposal", "action"}),
    None: frozenset(),
}
"""The slots each claim type fills; a sentence with no claim has none."""

_STATING_SLOT: Final[dict[str, str]] = {
    "comparison": "cmp",
    "label": "label",
    "trend": "direction",
    "recommendation": "proposal",
}
"""The slot that states the claim: a template without it would state something nobody checked."""

_CMP_WORDS: Final[dict[str, str]] = {
    "gt": "is above",
    "lt": "is below",
    "eq": "is not distinguishable from",
}
_DIRECTION_WORDS: Final[dict[str, str]] = {"up": "has risen", "down": "has fallen"}

_CLAIM_WORDS: Final[frozenset[str]] = frozenset(
    {
        # comparison and direction
        "better",
        "worse",
        "best",
        "worst",
        "beat",
        "beats",
        "beaten",
        "outperform",
        "outperforms",
        "outperformed",
        "higher",
        "highest",
        "lower",
        "lowest",
        "greater",
        "smaller",
        "bigger",
        "larger",
        "above",
        "below",
        "exceed",
        "exceeds",
        "exceeded",
        "ahead",
        "behind",
        "more",
        "most",
        "less",
        "least",
        "fewer",
        "up",
        "down",
        "rise",
        "rises",
        "rising",
        "rose",
        "risen",
        "fall",
        "falls",
        "falling",
        "fell",
        "fallen",
        "grow",
        "grows",
        "growing",
        "grew",
        "grown",
        "growth",
        "gain",
        "gains",
        "gained",
        "jump",
        "jumps",
        "jumped",
        "climb",
        "climbs",
        "climbed",
        "drop",
        "drops",
        "dropped",
        "dip",
        "dips",
        "dipped",
        "soar",
        "soars",
        "soared",
        "plunge",
        "plunged",
        "slump",
        "slumped",
        "lift",
        "lifts",
        "lifted",
        "uplift",
        # cause, certainty and outcome
        "cause",
        "caused",
        "causes",
        "causal",
        "because",
        "proven",
        "proves",
        "proved",
        "prove",
        "significant",
        "significantly",
        "guaranteed",
        "guarantee",
        "guarantees",
        "sure",
        "surely",
        "certain",
        "certainly",
        "definitely",
        "always",
        "never",
        "win",
        "wins",
        "winning",
        "won",
        "winner",
        "lose",
        "loses",
        "losing",
        "lost",
        "loser",
        # amounts and multipliers
        "double",
        "doubled",
        "doubles",
        "triple",
        "tripled",
        "triples",
        "quadruple",
        "half",
        "halves",
        "halved",
        "twice",
        "thrice",
        "times",
        "quarter",
        "quarters",
        "third",
        "thirds",
        "fifth",
        "fifths",
        "percent",
        "percentage",
        "pct",
        "dozen",
        "dozens",
        "hundred",
        "hundreds",
        "thousand",
        "thousands",
        "lakh",
        "lakhs",
        "lac",
        "lacs",
        "crore",
        "crores",
        "million",
        "millions",
        "billion",
        "billions",
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
        "single",
        "none",
        "all",
        "every",
        "everyone",
    }
)
"""Words that state a direction, a cause, a certainty or an amount (a spelled-out number, a multiplier or a
share): in the model's own words they would be a claim no verifier checked, so they are allowed only inside a
slot the renderer fills."""

_CLAIM_STEMS: Final[tuple[str, ...]] = (
    "improv",
    "boost",
    "surg",
    "increas",
    "decreas",
    "declin",
    "reduc",
    "worsen",
    "skyrocket",
    "spike",
    "spiki",
    "outperform",
    "outsell",
    "doubl",
    "tripl",
    "quadrupl",
    "multipl",
    "maximi",
    "minimi",
    "succe",
    "fail",
    "guarant",
    "signific",
)
"""Stems of the outcome verbs (improve, improves, improved, improving, boosts, surging ...)."""

_TENS: Final[frozenset[str]] = frozenset(
    {"twenty", "thirty", "forty", "fourty", "fifty", "sixty", "seventy", "eighty", "ninety"}
)


def _claim_word(word: str) -> bool:
    """`word` (lower case) states a direction, a cause or an amount: listed, an outcome verb's stem, a
    multiplier (`tenfold`), a teen or a compound of the tens (`seventeen`, `twentyfive`)."""
    if word in _CLAIM_WORDS or word.startswith(_CLAIM_STEMS):
        return True
    return word.endswith(("fold", "teen")) or word.startswith(tuple(_TENS))


_WORD: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]+")


def format_value(value: float, unit: Unit) -> str:
    """A number as the renderer writes it: percentage points for a share, a count, or rupees."""
    if unit == "share":
        return f"{value * 100.0:+.1f} percentage points"
    if unit == "inr":
        sign = "-" if value < 0 else ""
        return f"{sign}₹{abs(value):,.0f}"
    return f"{value:+,.1f}"


def _level_text(level: float) -> str:
    return f"{level * 100.0:g}%"


def _fields(template: str) -> list[tuple[str, str | None, str | None]] | None:
    """`(literal, field, spec+conversion)` of each piece of `template`; None when it does not parse."""
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:
        return None
    return [
        (literal, field, (spec or "") + (conversion or "") if field is not None else None)
        for literal, field, spec, conversion in parsed
    ]


def _free_text(template: str) -> str:
    pieces = _fields(template) or []
    return " ".join(literal for literal, _, _ in pieces)


def _free_text_problem(free: str, guardrails: Any) -> str | None:
    """Why the model's own words cannot be shown, or None."""
    from engine.agent.grounding import numbers_in, ungrounded_numbers

    if any(character.isdigit() for character in free) or numbers_in(free):
        return "the sentence's own words carry a number; numbers come from the evidence only"
    if ungrounded_numbers(free, frozenset()):
        return "the sentence's own words carry a number nothing grounds"
    words = {word.lower() for word in _WORD.findall(free)}
    stated = sorted(word for word in words if _claim_word(word))
    if stated:
        return "the sentence's own words state a direction or a cause outside a checked claim"
    if guardrails is not None and free.strip():
        from engine.generative.guardrails import CheckContext

        result = guardrails.check(free, CheckContext(target="agent_narrative"))
        if not result.passed:
            return f"the sentence's own words fail the {result.blocked_by} guardrail"
    return None


def _capitalised(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


# ---------------------------------------------------------------------------
# The verifier
# ---------------------------------------------------------------------------
class _Verdict:
    def __init__(self) -> None:
        self.failure: tuple[str, str] | None = None
        self.correction: tuple[str, str] | None = None
        self.slots: dict[str, str] = {}
        self.refs: tuple[str, ...] = ()

    def fail(self, code: str, reason: str) -> _Verdict:
        self.failure = (code, reason)
        return self


def _truth(interval: Interval) -> Literal["gt", "lt", "eq"]:
    if interval.low > 0.0:
        return "gt"
    if interval.high < 0.0:
        return "lt"
    return "eq"


def _comparison(claim: ComparisonClaim, index: EvidenceIndex) -> _Verdict:
    verdict = _Verdict()
    for ref in (claim.left, claim.right):
        if ref not in index.arms:
            return verdict.fail(CLAIM_UNREFERENCED, f"{ref!r} names no arm in the evidence")
    found = index.comparison(claim.left, claim.right, claim.metric)
    if found is None:
        return verdict.fail(CLAIM_UNREFERENCED, "no evidence compares these two on this metric")
    fact, reversed_ = found
    raw = fact.difference
    interval = (
        Interval(estimate=-raw.estimate, low=-raw.high, high=-raw.low, level=raw.level) if reversed_ else raw
    )
    truth = _truth(interval)
    stated = claim.comparator
    if stated != truth:
        if stated == "eq":
            return verdict.fail(CLAIM_DIRECTION, "the interval excludes zero, so the two are distinguishable")
        if truth != "eq":
            return verdict.fail(CLAIM_DIRECTION, "the interval says the opposite direction")
        verdict.correction = (
            CLAIM_DIRECTION,
            "the interval includes zero, so 'is not distinguishable from' was written instead",
        )
        stated = "eq"
    verdict.slots = {
        "arm_a": index.arms[claim.left],
        "arm_b": index.arms[claim.right],
        "cmp": _CMP_WORDS[stated],
        "metric": fact.metric_label,
        "difference": format_value(interval.estimate, fact.unit),
        "low": format_value(interval.low, fact.unit),
        "high": format_value(interval.high, fact.unit),
        "level": _level_text(interval.level),
    }
    verdict.refs = (claim.left, claim.right)
    return verdict


def _label(claim: LabelClaim, index: EvidenceIndex) -> _Verdict:
    verdict = _Verdict()
    actual = index.labels.get(claim.ref)
    if actual is None:
        return verdict.fail(CLAIM_UNREFERENCED, f"{claim.ref!r} names no label in the evidence")
    if claim.value.strip().casefold() != actual.casefold():
        return verdict.fail(CLAIM_LABEL, f"the evidence is labelled {actual}, not {claim.value.strip()}")
    verdict.slots = {"label": actual, "subject": index.label_subjects.get(claim.ref, "the result")}
    verdict.refs = (claim.ref,)
    return verdict


def trend_holds(points: Sequence[Interval], direction: Literal["up", "down"]) -> bool:
    """At least 3 points, every change in `direction`, and the endpoints' intervals apart (design §4.1)."""
    if len(points) < 3:
        return False
    steps = [b.estimate - a.estimate for a, b in pairwise(points)]
    if direction == "up":
        return all(step > 0 for step in steps) and points[0].high < points[-1].low
    return all(step < 0 for step in steps) and points[0].low > points[-1].high


def _trend(claim: TrendClaim, index: EvidenceIndex) -> _Verdict:
    verdict = _Verdict()
    series = index.series.get(claim.ref)
    if series is None:
        return verdict.fail(CLAIM_UNREFERENCED, f"{claim.ref!r} names no measured series in the evidence")
    if not trend_holds(series.points, claim.direction):
        return verdict.fail(
            CLAIM_DIRECTION,
            "the measured points do not show that direction (three or more, each step the same way, "
            "the first and last intervals apart)",
        )
    verdict.slots = {
        "series": series.name,
        "direction": _DIRECTION_WORDS[claim.direction],
        "metric": series.metric_label,
        "first": format_value(series.points[0].estimate, series.unit),
        "last": format_value(series.points[-1].estimate, series.unit),
    }
    verdict.refs = (claim.ref,)
    return verdict


def _recommendation(claim: RecommendationClaim, index: EvidenceIndex) -> _Verdict:
    verdict = _Verdict()
    action = index.proposals.get(claim.proposal_id)
    if action is None:
        return verdict.fail(CLAIM_UNREFERENCED, "it names no proposal the policy allowed")
    verdict.slots = {"proposal": claim.proposal_id, "action": action}
    verdict.refs = (claim.proposal_id,)
    return verdict


def verify_sentence(
    spec: SentenceSpec, index: EvidenceIndex, *, guardrails: Any = None
) -> tuple[RenderedSentence | None, tuple[str, str] | None, tuple[str, str] | None]:
    """`(rendered, failure, correction)` for one sentence: rendered is None exactly when it failed."""
    claim = spec.claim
    kind = None if claim is None else claim.type
    pieces = _fields(spec.template)
    if pieces is None:
        return None, (CLAIM_MALFORMED, "the template's braces do not pair up"), None
    used = [field for _, field, _ in pieces if field is not None]
    allowed = SLOTS[kind]
    for _, field, extra in pieces:
        if field is None:
            continue
        if field not in allowed or extra:
            return None, (CLAIM_UNREFERENCED, f"the slot {{{field}}} is not one this claim fills"), None
    stating = _STATING_SLOT.get(kind or "")
    if stating is not None and stating not in used:
        return None, (CLAIM_UNREFERENCED, f"the template does not state the claim (no {{{stating}}})"), None
    problem = _free_text_problem(_free_text(spec.template), guardrails)
    if problem is not None:
        return None, (CLAIM_FREE_TEXT, problem), None
    if claim is None:
        verdict = _Verdict()
    elif isinstance(claim, ComparisonClaim):
        verdict = _comparison(claim, index)
    elif isinstance(claim, LabelClaim):
        verdict = _label(claim, index)
    elif isinstance(claim, TrendClaim):
        verdict = _trend(claim, index)
    else:
        verdict = _recommendation(claim, index)
    if verdict.failure is not None:
        return None, verdict.failure, None
    text = "".join(
        literal + ("" if field is None else verdict.slots[field]) for literal, field, _ in pieces
    ).strip()
    return (
        RenderedSentence(text=_capitalised(text), claim_type=kind, refs=verdict.refs),
        None,
        verdict.correction,
    )


# ---------------------------------------------------------------------------
# The whole narrative
# ---------------------------------------------------------------------------
def parse_narrative(raw: str | Mapping[str, Any]) -> tuple[list[SentenceSpec | None], str | None]:
    """The sentences of a model's answer (None for one that is not shaped as a sentence), or a reason the
    answer as a whole does not parse."""
    document: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{") :] if "{" in text else text
        try:
            document = json.loads(text)
        except ValueError:
            return [], "the answer is not JSON"
    if not isinstance(document, Mapping) or set(document) != {"sentences"}:
        return [], "the answer is not one object with a sentences list"
    items = document["sentences"]
    if not isinstance(items, list) or not items or len(items) > MAX_SENTENCES:
        return [], f"the sentences list is empty or longer than {MAX_SENTENCES}"
    out: list[SentenceSpec | None] = []
    for item in items:
        try:
            out.append(_SENTENCE.validate_python(item))
        except ValidationError:
            out.append(None)
    return out, None


def _render_rules(
    rules: StructuredNarrative, index: EvidenceIndex, guardrails: Any
) -> list[RenderedSentence]:
    rendered: list[RenderedSentence] = []
    for position, spec in enumerate(rules.sentences):
        sentence, failure, _ = verify_sentence(spec, index, guardrails=guardrails)
        if sentence is None:
            raise ValueError(f"the rules-only sentence {position} does not verify: {failure}")
        rendered.append(sentence)
    return rendered


def verify_narrative(
    raw: str | Mapping[str, Any] | None,
    index: EvidenceIndex,
    *,
    rules: StructuredNarrative,
    guardrails: Any = None,
) -> Narrative:
    """Verify and render a model's narrative; fall back to `rules` as the module docstring says.

    `raw` None means no model wrote one (no AI service): the rules-only summary, with nothing failed.
    """
    if raw is None:
        return Narrative(source="rules", sentences=tuple(_render_rules(rules, index, guardrails)))
    specs, problem = parse_narrative(raw)
    failures: list[ClaimFinding] = []
    corrections: list[ClaimFinding] = []
    rendered: list[RenderedSentence] = []
    if problem is not None:
        failures.append(ClaimFinding(sentence=-1, code=CLAIM_MALFORMED, reason=problem))
    for position, spec in enumerate(specs):
        if spec is None:
            failures.append(
                ClaimFinding(sentence=position, code=CLAIM_MALFORMED, reason="not shaped as a sentence")
            )
            continue
        sentence, failure, correction = verify_sentence(spec, index, guardrails=guardrails)
        if failure is not None:
            failures.append(ClaimFinding(sentence=position, code=failure[0], reason=failure[1]))
            continue
        if correction is not None:
            corrections.append(ClaimFinding(sentence=position, code=correction[0], reason=correction[1]))
        if sentence is not None:
            rendered.append(sentence)
    if problem is not None or len(failures) > 1 or not rendered:
        return Narrative(
            source="rules",
            sentences=tuple(_render_rules(rules, index, guardrails)),
            failures=tuple(failures),
            corrections=tuple(corrections),
            replaced=True,
        )
    return Narrative(
        source="model",
        sentences=tuple(rendered),
        failures=tuple(failures),
        corrections=tuple(corrections),
    )


def rules_narrative(index: EvidenceIndex) -> StructuredNarrative:
    """The rules-only summary: one true sentence per fact in the index, written by code (design §4.1)."""
    sentences: list[SentenceSpec] = []
    for proposal_id in sorted(index.proposals):
        sentences.append(
            SentenceSpec(
                template="Approving proposal {proposal} will {action}.",
                claim=RecommendationClaim(type="recommendation", proposal_id=proposal_id),
            )
        )
    for fact in index.comparisons:
        sentences.append(
            SentenceSpec(
                template="{arm_a} {cmp} {arm_b} on {metric}: {difference} ({level} range {low} to {high}).",
                claim=ComparisonClaim(
                    type="comparison",
                    left=fact.left,
                    right=fact.right,
                    metric=fact.metric,
                    comparator=_truth(fact.difference),
                ),
            )
        )
    for ref in sorted(index.labels):
        sentences.append(
            SentenceSpec(
                template="{subject} is labelled {label}.",
                claim=LabelClaim(type="label", ref=ref, value=index.labels[ref]),
            )
        )
    for ref in sorted(index.series):
        points = index.series[ref].points
        for direction in ("up", "down"):
            if trend_holds(points, direction):
                sentences.append(
                    SentenceSpec(
                        template="{series} {direction} on {metric}, from {first} to {last}.",
                        claim=TrendClaim(type="trend", ref=ref, direction=direction),
                    )
                )
    if not sentences:
        sentences.append(SentenceSpec(template="There is no measured result to describe yet."))
    return StructuredNarrative(sentences=tuple(sentences[:MAX_SENTENCES]))


def guardrails_for(config_root: Path | None) -> Any:
    """The deterministic guardrails in force (no judges: nothing here pays for a model to judge a template)."""
    from engine.generative.guardrails import Guardrails, load_policy

    return Guardrails(load_policy(config_root))
