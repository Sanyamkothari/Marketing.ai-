"""`explain`: Data Doctor's evidence in words, through the claims verifier (Plan O M236, DEC-1806; the design §5.5
#39, §6.2).

Plan K's claims verifier (`engine.agentic.claims`) lets a model word a campaign plan without trusting it: the model
writes templates with slots, each sentence carrying at most one claim that names evidence, and code checks the
claim and fills every slot. `explain` is the same verifier for **data** evidence, with four data claim types:

| Claim | States | Passes only when | Otherwise |
|---|---|---|---|
| `count` | a count a tool measured (`{count}`) | the fact is a measured count | `CLAIM_UNREFERENCED` |
| `share` | a share a tool measured (`{share}`) | the fact is a measured share | `CLAIM_UNREFERENCED` |
| `rate_by_group` | one group's rate in a `rate_by` result (`{group}` `{rate}` `{column}` `{rank}`) | the group is shown (not suppressed) and, for `rank`, it alone has the highest (lowest) rate | `CLAIM_UNREFERENCED`; a wrong or shared rank `CLAIM_DIRECTION` |
| `changed` | "changed N of M" (`{changed}` `{out_of}` `{share}`) | the fact holds both counts | `CLAIM_UNREFERENCED` |

**Facts come from the server, never from the model.** The facts are read from results of the case's earlier
registry calls (`DataToolContext.evidence`, each with its content-hash evidence id), walked by
:func:`data_evidence_index`: every count, every share or rate, every `rate_by` grouping and every object holding
`changed` and `out_of`. Each fact gets a short ref (`f1`, `f2`, ...) in the order the evidence was named.

**What the model sees.** Only each fact's ref, its claim type and what it counts (`about`: the key a tool gave it,
when that key is one of the tool's own field names - never a column name, a value or a number). It answers with a
structured narrative; the verifier checks every claim, the renderer fills every number and every group from the
facts, and the model's own words pass Plan G's number check, the claim-word list and the guardrails
(`claims._free_text_problem`).

**On failure** one failed sentence is dropped; more than one, or an answer that does not parse, or no sentence
left, is replaced by the **rules' narrative** (:func:`data_rules_narrative`), written by code from the same facts
and passing the same verifier. With no AI service connected the rules' narrative is the explanation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Final, Literal, TypeGuard

from pydantic import Field, TypeAdapter, ValidationError

from engine.agentic.claims import (
    CLAIM_DIRECTION,
    CLAIM_MALFORMED,
    CLAIM_UNREFERENCED,
    ClaimFinding,
    Narrative,
    RenderedSentence,
)
from engine.config import StrictBase

__all__ = [
    "DATA_SLOTS",
    "MAX_FACTS",
    "ChangedClaim",
    "CountClaim",
    "DataEvidenceIndex",
    "DataSentence",
    "ExplainRequest",
    "Narrator",
    "RateByGroupClaim",
    "ShareClaim",
    "data_evidence_index",
    "data_rules_narrative",
    "meter_narrator",
    "verify_data_narrative",
    "verify_data_sentence",
]

MAX_FACTS: Final[int] = 60
"""Facts an explanation reads at most, in the order the evidence was named (the prompt stays small)."""
MAX_SENTENCES: Final[int] = 12
MAX_TEMPLATE_CHARS: Final[int] = 300
_ABOUT: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_SHARE_WORDS: Final[tuple[str, ...]] = ("share", "rate", "pct_of", "fraction")


# ---------------------------------------------------------------------------
# What the model returns
# ---------------------------------------------------------------------------
class CountClaim(StrictBase):
    type: Literal["count"]
    ref: str = Field(max_length=12, description="A fact ref, `f1`.")


class ShareClaim(StrictBase):
    type: Literal["share"]
    ref: str = Field(max_length=12)


class RateByGroupClaim(StrictBase):
    type: Literal["rate_by_group"]
    ref: str = Field(max_length=12)
    group: int | None = Field(default=None, ge=0, le=500, description="The group's position in the result.")
    rank: Literal["highest", "lowest"] | None = None


class ChangedClaim(StrictBase):
    type: Literal["changed"]
    ref: str = Field(max_length=12)


DataClaim = Annotated[CountClaim | ShareClaim | RateByGroupClaim | ChangedClaim, Field(discriminator="type")]


class DataSentence(StrictBase):
    """One sentence as the model (or the rules) writes it: a template, and at most one data claim."""

    template: str = Field(max_length=MAX_TEMPLATE_CHARS)
    claim: DataClaim | None = None


_SENTENCE: Final[TypeAdapter[DataSentence]] = TypeAdapter(DataSentence)

DATA_SLOTS: Final[dict[str | None, frozenset[str]]] = {
    "count": frozenset({"count"}),
    "share": frozenset({"share"}),
    "rate_by_group": frozenset({"group", "rate", "column", "rank"}),
    "changed": frozenset({"changed", "out_of", "share"}),
    None: frozenset(),
}
"""The slots each data claim fills; a sentence with no claim has none."""
_STATING: Final[dict[str, str]] = {
    "count": "count",
    "share": "share",
    "rate_by_group": "rate",
    "changed": "changed",
}
_RANK_WORDS: Final[dict[str, str]] = {"highest": "the highest", "lowest": "the lowest"}


# ---------------------------------------------------------------------------
# The facts
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GroupRate:
    label: str
    rate: float | None
    suppressed: bool


@dataclass(frozen=True)
class GroupFact:
    column: str
    measure: str
    """`positive_rate` (a share), `mean` (a number) or `rows` (each group's share of the rows)."""
    groups: tuple[GroupRate, ...]


@dataclass(frozen=True)
class Fact:
    ref: str
    """`f1`, `f2`, ...: what a claim names."""
    evidence: str
    """`<evidence_id>:<path>`: where the fact was read (what a rendered sentence cites)."""
    type: Literal["count", "share", "rate_by_group", "changed"]
    about: str
    count: int | None = None
    share: float | None = None
    grouping: GroupFact | None = None
    changed: tuple[int, int] | None = None


@dataclass
class DataEvidenceIndex:
    """Every fact a sentence may state, keyed by its ref; built from registry results only."""

    facts: dict[str, Fact] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()

    def add(self, fact_type: str, evidence: str, about: str, **values: Any) -> None:
        if len(self.facts) >= MAX_FACTS:
            return
        ref = f"f{len(self.facts) + 1}"
        self.facts[ref] = Fact(ref=ref, evidence=evidence, type=fact_type, about=about, **values)  # type: ignore[arg-type]

    def lines(self) -> list[dict[str, str]]:
        """What the model is shown: refs, claim types and what each counts - never a number or a value."""
        return [{"ref": fact.ref, "type": fact.type, "about": fact.about} for fact in self.facts.values()]


def _about(key: str, columns: frozenset[str]) -> str:
    """A tool's own field name, or "a value": never a column name, a cell or a number."""
    return key if _ABOUT.match(key) and key not in columns else "a value"


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _child(evidence: str, path: str) -> str:
    """`<evidence_id>:<path>` one level down (`ev:` is the result itself)."""
    return f"{evidence}{path}" if evidence.endswith(":") else f"{evidence}.{path}"


def _grouping(value: Mapping[str, Any]) -> GroupFact | None:
    """A Plan G `rate_by` result: groups with a rate (or rows) each, small groups suppressed."""
    groups = value.get("groups")
    function = value.get("function")
    if not isinstance(groups, list) or not isinstance(function, str) or "column" not in value:
        return None
    read: list[GroupRate] = []
    for entry in groups:
        if not isinstance(entry, Mapping) or "group" not in entry:
            return None
        raw = entry.get("share") if function == "rows" else entry.get(function)
        rate = float(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else None
        read.append(GroupRate(str(entry["group"]), rate, bool(entry.get("suppressed", False))))
    measure = "rows" if function == "rows" else ("positive_rate" if function == "positive_rate" else "mean")
    return GroupFact(column=str(value["column"]), measure=measure, groups=tuple(read))


def _walk(index: DataEvidenceIndex, evidence_id: str, value: Any, path: str, columns: frozenset[str]) -> None:
    if isinstance(value, Mapping):
        here = f"{evidence_id}:{path}" if path else f"{evidence_id}:"
        grouping = _grouping(value)
        if grouping is not None:
            index.add(
                "rate_by_group", here, _about(path.rsplit(".", 1)[-1] or "groups", columns), grouping=grouping
            )
        changed, out_of = value.get("changed"), value.get("out_of")
        if _is_int(changed) and _is_int(out_of) and out_of > 0:
            index.add(
                "changed",
                here,
                _about(path.rsplit(".", 1)[-1] or "change", columns),
                changed=(changed, out_of),
            )
        for key, item in value.items():
            if grouping is not None and key == "groups":
                continue
            _walk(index, evidence_id, item, f"{path}.{key}" if path else str(key), columns)
        return
    if isinstance(value, list):
        for position, item in enumerate(value):
            _walk(index, evidence_id, item, f"{path}.{position}", columns)
        return
    key = path.rsplit(".", 1)[-1]
    if not key or key.isdigit():
        return
    where = f"{evidence_id}:{path}"
    if _is_int(value) and value >= 0:
        index.add("count", where, _about(key, columns), count=int(value))
    elif isinstance(value, float) and 0.0 <= value <= 1.0 and any(word in key for word in _SHARE_WORDS):
        index.add("share", where, _about(key, columns), share=float(value))


def data_evidence_index(
    results: Iterable[tuple[str, Mapping[str, Any]]], *, columns: Iterable[str] = ()
) -> DataEvidenceIndex:
    """The facts of `(evidence_id, output)` pairs, in order, at most :data:`MAX_FACTS`."""
    names = frozenset(str(column) for column in columns)
    index = DataEvidenceIndex()
    ids: list[str] = []
    for evidence_id, output in results:
        ids.append(evidence_id)
        _walk(index, evidence_id, output, "", names)
    index.evidence_ids = tuple(ids)
    return index


# ---------------------------------------------------------------------------
# Rendering and the verifier
# ---------------------------------------------------------------------------
def _count_text(value: int) -> str:
    return f"{value:,}"


def _share_text(value: float) -> str:
    return f"{value * 100.0:.1f}%"


def _rate_text(fact: GroupFact, rate: float) -> str:
    return _share_text(rate) if fact.measure in {"positive_rate", "rows"} else f"{rate:,.2f}"


class _Verdict:
    def __init__(self) -> None:
        self.failure: tuple[str, str] | None = None
        self.slots: dict[str, str] = {}
        self.refs: tuple[str, ...] = ()

    def fail(self, code: str, reason: str) -> _Verdict:
        self.failure = (code, reason)
        return self


def _ranked(fact: GroupFact, rank: str) -> int | None:
    """The one group with the highest (lowest) rate among those shown; None when none or a tie."""
    shown = [(i, g.rate) for i, g in enumerate(fact.groups) if not g.suppressed and g.rate is not None]
    if len(shown) < 2:
        return None
    best = max(rate for _, rate in shown) if rank == "highest" else min(rate for _, rate in shown)
    at = [i for i, rate in shown if rate == best]
    return at[0] if len(at) == 1 else None


def _check(claim: Any, index: DataEvidenceIndex) -> _Verdict:
    verdict = _Verdict()
    fact = index.facts.get(claim.ref)
    if fact is None or fact.type != claim.type:
        return verdict.fail(CLAIM_UNREFERENCED, f"{claim.ref!r} names no measured {claim.type} fact")
    verdict.refs = (fact.evidence,)
    if isinstance(claim, CountClaim) and fact.count is not None:
        verdict.slots = {"count": _count_text(fact.count)}
    elif isinstance(claim, ShareClaim) and fact.share is not None:
        verdict.slots = {"share": _share_text(fact.share)}
    elif isinstance(claim, ChangedClaim) and fact.changed is not None:
        changed, out_of = fact.changed
        verdict.slots = {
            "changed": _count_text(changed),
            "out_of": _count_text(out_of),
            "share": _share_text(changed / out_of),
        }
    elif isinstance(claim, RateByGroupClaim) and fact.grouping is not None:
        grouping = fact.grouping
        if (claim.group is None) == (claim.rank is None):
            return verdict.fail(CLAIM_UNREFERENCED, "a group's rate names either its position or its rank")
        position = claim.group
        if claim.rank is not None:
            ranked = _ranked(grouping, claim.rank)
            if ranked is None:
                return verdict.fail(CLAIM_DIRECTION, f"no single shown group has the {claim.rank} rate")
            position = ranked
        assert position is not None
        if position >= len(grouping.groups):
            return verdict.fail(CLAIM_UNREFERENCED, "no group at that position")
        group = grouping.groups[position]
        if group.suppressed or group.rate is None:
            return verdict.fail(CLAIM_UNREFERENCED, "that group is too small to show, or has no rate")
        verdict.slots = {
            "group": group.label,
            "rate": _rate_text(grouping, group.rate),
            "column": grouping.column,
            "rank": _RANK_WORDS.get(claim.rank or "", ""),
        }
        verdict.refs = (_child(fact.evidence, f"groups.{position}"),)
    else:  # pragma: no cover - a fact always holds the value of its type
        return verdict.fail(CLAIM_UNREFERENCED, "the fact holds no value")
    return verdict


def verify_data_sentence(
    spec: DataSentence, index: DataEvidenceIndex, *, guardrails: Any = None
) -> tuple[RenderedSentence | None, tuple[str, str] | None]:
    """`(rendered, failure)` for one sentence: rendered is None exactly when it failed."""
    from engine.agentic.claims import _capitalised, _fields, _free_text, _free_text_problem

    claim = spec.claim
    kind = None if claim is None else claim.type
    pieces = _fields(spec.template)
    if pieces is None:
        return None, (CLAIM_MALFORMED, "the template's braces do not pair up")
    used = {name for _, name, _ in pieces if name is not None}
    allowed = DATA_SLOTS[kind]
    for _, name, extra in pieces:
        if name is not None and (name not in allowed or extra):
            return None, (CLAIM_UNREFERENCED, f"the slot {{{name}}} is not one this claim fills")
    stating = _STATING.get(kind or "")
    if stating is not None and stating not in used:
        return None, (CLAIM_UNREFERENCED, f"the template does not state the claim (no {{{stating}}})")
    if isinstance(claim, RateByGroupClaim) and claim.rank is not None and "rank" not in used:
        return None, (CLAIM_UNREFERENCED, "a ranked claim says its rank through {rank}")
    problem = _free_text_problem(_free_text(spec.template), guardrails)
    if problem is not None:
        from engine.agentic.claims import CLAIM_FREE_TEXT

        return None, (CLAIM_FREE_TEXT, problem)
    verdict = _Verdict() if claim is None else _check(claim, index)
    if verdict.failure is not None:
        return None, verdict.failure
    text = "".join(
        literal + ("" if name is None else verdict.slots[name]) for literal, name, _ in pieces
    ).strip()
    return RenderedSentence(text=_capitalised(text), claim_type=kind, refs=verdict.refs), None


def _parse(raw: str | Mapping[str, Any]) -> tuple[list[DataSentence | None], str | None]:
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
    out: list[DataSentence | None] = []
    for item in items:
        try:
            out.append(_SENTENCE.validate_python(item))
        except ValidationError:
            out.append(None)
    return out, None


def data_rules_narrative(index: DataEvidenceIndex) -> tuple[DataSentence, ...]:
    """The rules' narrative: one true sentence per change measured, per grouping with one top group, and per
    count of rows a tool read, written by code; at most 12 sentences."""
    sentences: list[DataSentence] = []
    for fact in index.facts.values():
        if fact.type == "changed":
            sentences.append(
                DataSentence(
                    template="This would change {changed} of {out_of} values ({share}).",
                    claim=ChangedClaim(type="changed", ref=fact.ref),
                )
            )
        elif fact.type == "rate_by_group" and fact.grouping is not None:
            if _ranked(fact.grouping, "highest") is not None:
                sentences.append(
                    DataSentence(
                        template="By {column}, {group} has {rank} rate: {rate}.",
                        claim=RateByGroupClaim(type="rate_by_group", ref=fact.ref, rank="highest"),
                    )
                )
        elif fact.type == "count" and fact.about == "rows" and fact.evidence.endswith(":rows"):
            sentences.append(
                DataSentence(template="{count} rows were read.", claim=CountClaim(type="count", ref=fact.ref))
            )
    if not sentences:
        sentences.append(DataSentence(template="There is no measured result to describe yet."))
    return tuple(sentences[:MAX_SENTENCES])


def _render_rules(index: DataEvidenceIndex, guardrails: Any) -> tuple[RenderedSentence, ...]:
    rendered: list[RenderedSentence] = []
    for position, spec in enumerate(data_rules_narrative(index)):
        sentence, failure = verify_data_sentence(spec, index, guardrails=guardrails)
        if sentence is None:
            raise ValueError(f"the rules' sentence {position} does not verify: {failure}")
        rendered.append(sentence)
    return tuple(rendered)


def verify_data_narrative(
    raw: str | Mapping[str, Any] | None, index: DataEvidenceIndex, *, guardrails: Any = None
) -> Narrative:
    """Verify and render a model's narrative of `index`; the rules' narrative as the module docstring says.

    `raw` None means no model wrote one (no AI service): the rules' narrative, nothing failed.
    """
    if raw is None:
        return Narrative(source="rules", sentences=_render_rules(index, guardrails))
    specs, problem = _parse(raw)
    failures: list[ClaimFinding] = []
    rendered: list[RenderedSentence] = []
    if problem is not None:
        failures.append(ClaimFinding(sentence=-1, code=CLAIM_MALFORMED, reason=problem))
    for position, spec in enumerate(specs):
        if spec is None:
            failures.append(
                ClaimFinding(sentence=position, code=CLAIM_MALFORMED, reason="not shaped as a sentence")
            )
            continue
        sentence, failure = verify_data_sentence(spec, index, guardrails=guardrails)
        if failure is not None:
            failures.append(ClaimFinding(sentence=position, code=failure[0], reason=failure[1]))
            continue
        if sentence is not None:
            rendered.append(sentence)
    if problem is not None or len(failures) > 1 or not rendered:
        return Narrative(
            source="rules",
            sentences=_render_rules(index, guardrails),
            failures=tuple(failures),
            replaced=True,
        )
    return Narrative(source="model", sentences=tuple(rendered), failures=tuple(failures))


# ---------------------------------------------------------------------------
# The AI service's side
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExplainRequest:
    """What the narrator is given: the facts' refs, claim types and what each counts. No number, no value."""

    facts: tuple[dict[str, str], ...]


Narrator = Callable[[ExplainRequest], "str | None"]
"""Writes the structured narrative (JSON text) for a request; None when the AI service did not answer."""


def meter_narrator(meter: Any, config_root: Path | None = None) -> Narrator:
    """A narrator over the metered AI service (`DATA_EXPLAIN`): an AI service or budget error is no answer."""
    from engine.generative.contracts import GenerativePurpose
    from engine.generative.prompts import load_prompt, render

    def narrate(request: ExplainRequest) -> str | None:
        prompt = render(
            load_prompt("data_explain", config_root),
            {"facts": json.dumps(list(request.facts), sort_keys=True, ensure_ascii=False)},
        )
        try:
            return str(meter.complete(prompt, GenerativePurpose.DATA_EXPLAIN).text)
        except Exception:  # an LLM or budget error: the rules' narrative is written instead
            return None

    return narrate
