"""The Value Proof Pack: a finance-ready account of one campaign, every number traced (Plan J M104, DEC-1314).

A finance head asks four questions of a campaign: what did it really change, what would have happened
anyway, what did it cost, and can we trust the answer. :func:`build_proof` answers them for one campaign in
ten sections, read only from the campaign's own measured artefacts:

1. the plan as registered against what ran (`test_plan.json` against the report);
2. whether the list went out (`contact_readout.json`, when a contact file was added);
3. the incremental outcomes with their 95% ranges (the report; per offer when there are several);
4. gross against incremental: every outcome among contacted customers, what the held-back group says would
   have happened anyway, and the difference;
5. naive credit against measured credit: a tool that credits every response counts every outcome among
   the contacted customers; the measured credit is only the difference the control group shows;
6. offer money spent on sure things and sleeping dogs (lists chosen by the campaign-effect model, read from
   `segment_effects.json`);
7. what the control group and the explore slice cost;
8. backfire: groups whose range lies wholly on the harmful side, with a "leave them out next cycle"
   suggestion an Analyst approves (:func:`approve_suppression`; nothing is ever applied automatically);
9. net value in rupees, as a range (the client's value inputs, the measured interval and the costs; not
   measured for a programme readout, which records nobody as contacted or as taking an offer);
10. method and limits, including what the numbers may claim.

**Every number is traced.** A number in a :class:`ProofView` is a :class:`Figure`: its value, the text the
pack prints, the artefact and field it was read from and, for an arithmetic result, the formula over those
fields (`s0 * s1 - s2`, names only, no constant). :func:`verify_provenance` reads every source again from the
store, recomputes every formula and every printed text, and :func:`build_proof` refuses to return a view in
which any figure does not resolve (`ProvenanceError`, `PROOF_NOT_TRACEABLE`). Text read from an artefact that
may carry digits (the campaign's name, a group's name, the outcome column) is a figure too, so every digit
the pack prints comes from a figure. A missing artefact is never a default: the section, or the line, says
"not measured" and why.

**What the numbers may claim (section 10).** A campaign whose control group the engine drew at random, or
whose random assignment it verified, is *proven*. One random only by the person's statement is shown under
that condition ("If the groups were chosen at random as you said"), never as proven. A descriptive-only one
shows its counts and rates and credits nothing to the campaign: no measured credit, no net value, no
backfire.

**Refusals.** A campaign whose data was generated (`RunRecord.synthetic`, a synthetic outcomes upload, an
audit, programme or contact file marked synthetic) is refused with `PROOF_SYNTHETIC_DATA`: a planted effect
is never presented to finance as value. A campaign not yet measured, measured before its outcomes were all in, or read
as an early look is refused with `PROOF_NOT_MATURE` and the day it can be read.

**No customer rows.** The pack reads only aggregate files (the report, the plan, the readouts, the group
effects, the value inputs, run and upload records); it never opens `assignment.parquet`, `outcomes.parquet`
or `contact.parquet`, and it lists the artefacts it read (`ProofView.artefacts`).
"""

from __future__ import annotations

import ast
import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import AwareDatetime, BaseModel, Field

from engine.config import StrictBase
from engine.contracts import Artefact
from engine.pilot.document import (
    AnyBlock,
    Bullets,
    Callout,
    Heading,
    KeyValues,
    Paragraph,
    ReportDocument,
    Table,
)
from engine.pilot.roi import ROI_INPUTS_FILENAME, RoiInputs, format_inr

if TYPE_CHECKING:
    from engine.storage import Storage

__all__ = [
    "PROOF_CODES",
    "PROOF_NOT_MATURE",
    "PROOF_NOT_TRACEABLE",
    "PROOF_SUPPRESSION_INVALID",
    "PROOF_SYNTHETIC_DATA",
    "SUPPRESSIONS_FILENAME",
    "Figure",
    "FigureRange",
    "ProofLine",
    "ProofNotFoundError",
    "ProofRefusedError",
    "ProofSection",
    "ProofTable",
    "ProofView",
    "ProvenanceError",
    "Source",
    "SuppressionApproval",
    "SuppressionLedger",
    "SuppressionProposal",
    "approve_suppression",
    "build_proof",
    "figures_of",
    "format_value",
    "proof_document",
    "save_campaign_value_inputs",
    "verify_provenance",
]

# ---------------------------------------------------------------------------
# Codes (DEC-1314): each joins `engine.decide.codes.PLAN_J_CODES` with a `configs/pilot/help.yaml` entry.
# ---------------------------------------------------------------------------
PROOF_SYNTHETIC_DATA: Final[str] = "PROOF_SYNTHETIC_DATA"
"""409: the campaign read generated data (a planted effect), so no proof is drawn from it."""
PROOF_NOT_MATURE: Final[str] = "PROOF_NOT_MATURE"
"""409: the campaign has no final result yet (not measured, outcomes still coming in, or an early look)."""
PROOF_NOT_TRACEABLE: Final[str] = "PROOF_NOT_TRACEABLE"
"""500: a number of the pack could not be traced back to the artefact it was read from; nothing is shown."""
PROOF_SUPPRESSION_INVALID: Final[str] = "PROOF_SUPPRESSION_INVALID"
"""409: the group named for leaving out next cycle is not one the pack flags as backfiring."""
PROOF_CODES: Final[frozenset[str]] = frozenset(
    {PROOF_SYNTHETIC_DATA, PROOF_NOT_MATURE, PROOF_NOT_TRACEABLE, PROOF_SUPPRESSION_INVALID}
)

SUPPRESSIONS_FILENAME: Final[str] = "suppression_proposals.json"
"""`campaigns/<id>/suppression_proposals.json`: the suggestions an Analyst approved. Never applied by the engine."""

_SAFE_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.-]{1,128}")
"""The characters of a campaign, run or upload id: one path segment."""


def _safe(identifier: object) -> bool:
    """True for an id that is one path segment: never `..`, a slash or empty."""
    return isinstance(identifier, str) and bool(_SAFE_ID.fullmatch(identifier)) and ".." not in identifier


_CAMPAIGN: Final[str] = "campaign.json"
_REPORT: Final[str] = "incrementality_report.json"
_PLAN: Final[str] = "test_plan.json"
_CONTACTS: Final[str] = "contact_readout.json"
_AUDIT: Final[str] = "audit.json"
_PROGRAMME: Final[str] = "programme.json"
_SEGMENTS: Final[str] = "segment_effects.json"
_RECOMMENDATION: Final[str] = "policy_recommendation.json"

SectionKey = Literal[
    "plan",
    "delivery",
    "incremental",
    "gross",
    "credit",
    "offer_money",
    "test_cost",
    "backfire",
    "net_value",
    "method",
]
Claim = Literal["proven", "stated_random", "descriptive"]
FigureFormat = Literal[
    "count",
    "signed_count",
    "share",
    "points",
    "planned_points",
    "amount",
    "signed_amount",
    "inr",
    "inr_unit",
    "date",
    "days",
    "times",
    "text",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ProofNotFoundError(LookupError):
    """No campaign record (`campaigns/<id>/campaign.json`) with that id."""


class ProofRefusedError(Exception):
    """The pack is refused with a code: `PROOF_SYNTHETIC_DATA`, `PROOF_NOT_MATURE` or `PROOF_SUPPRESSION_INVALID`."""

    def __init__(self, code: str, message: str, *, results_available_on: date | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.results_available_on = results_available_on


class ProvenanceError(Exception):
    """Some figure of a built view does not resolve to its sources (`PROOF_NOT_TRACEABLE`)."""

    def __init__(self, failures: tuple[str, ...]) -> None:
        super().__init__(f"{len(failures)} figure(s) of the pack could not be traced: " + "; ".join(failures))
        self.failures = failures


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------
class Source(StrictBase):
    """Where a value was read: an artefact's storage key and a dotted field path inside its JSON."""

    artefact: str = Field(description="Storage key, for example `campaigns/<id>/incrementality_report.json`.")
    field: str = Field(
        description="Dotted path in that JSON; a list index is a number, `arms.0.effect.value`."
    )


class Figure(StrictBase):
    """One number (or one piece of artefact text) the pack prints, with where it came from."""

    value: int | float | str = Field(description="The value: the source's own, or the formula's result.")
    text: str = Field(description="What the pack prints: `format_value(format, value)`.")
    format: FigureFormat = Field(description="How `text` is made from `value`.")
    sources: tuple[Source, ...] = Field(min_length=1, description="The fields the value is read from.")
    formula: str | None = Field(
        default=None,
        description="Arithmetic over the sources, named s0, s1, ... (+ - * / and brackets only); null: s0 itself.",
    )


class FigureRange(StrictBase):
    """A range printed in one table cell: `low` to `high`."""

    low: Figure
    high: Figure


Cell = Figure | FigureRange | str


class ProofLine(StrictBase):
    """One line of a section: a label, a value and, for a range, its two ends; or why it is not measured."""

    label: str
    value: Figure | None = None
    low: Figure | None = None
    high: Figure | None = None
    missing: str | None = Field(default=None, description="Why the line is not measured; null when it is.")


class ProofTable(StrictBase):
    columns: tuple[str, ...]
    rows: tuple[tuple[Cell, ...], ...]


class ProofSection(StrictBase):
    key: SectionKey
    title: str
    status: Literal["measured", "not_measured"]
    reason: str | None = Field(default=None, description="Why the section is not measured; null when it is.")
    lines: tuple[ProofLine, ...] = ()
    table: ProofTable | None = None
    notes: tuple[str, ...] = ()


class SuppressionProposal(StrictBase):
    """A group the pack flags as backfiring: the suggestion to leave it out of the next cycle."""

    dimension: Literal["band", "segment", "offer"]
    segment: Figure = Field(description="The group's name, read from `segment_effects.json`.")
    worst_case: Figure = Field(
        description="The least harmful end of the group's range after allowing for every group checked: still harm."
    )
    status: Literal["proposed", "approved"]
    approved_by: Figure | None = None
    approved_at: Figure | None = None


class ProofView(StrictBase):
    """`GET /pilot/proof/{campaign_id}?format=json`: the pack, every number a traced :class:`Figure`."""

    schema_version: int = 1
    campaign_id: str
    kind: Literal["scored", "external", "programme"]
    campaign_name: Figure
    use_case_id: str | None = None
    outcome: Figure = Field(description="The outcome column measured.")
    outcome_kind: Literal["binary", "continuous"]
    outcome_is_good: bool = Field(description="False when the campaign aims to make the outcome rarer.")
    claim: Claim
    claim_label: str
    headline: str = Field(description="One sentence; every digit in it is a figure's text.")
    measured_on: Figure = Field(description="The day the result was read (`as_of` of the report).")
    sections: tuple[ProofSection, ...]
    proposals: tuple[SuppressionProposal, ...] = ()
    artefacts: tuple[str, ...] = Field(description="Every artefact the pack read: aggregate files only.")
    built_at: AwareDatetime


class SuppressionApproval(Artefact):
    """One approved suggestion: leave this group out of the next cycle. Recorded, never applied by the engine."""

    dimension: Literal["band", "segment", "offer"]
    segment: str
    report_computed_at: AwareDatetime = Field(description="The measurement the suggestion was made from.")
    approved_by: str
    approved_at: AwareDatetime


class SuppressionLedger(Artefact):
    """`campaigns/<id>/suppression_proposals.json`."""

    campaign_id: str
    approvals: tuple[SuppressionApproval, ...] = ()


# ---------------------------------------------------------------------------
# Formatting: the one way a value becomes text (verified by `verify_provenance`)
# ---------------------------------------------------------------------------
def _signed(text: str) -> str:
    return text if text.startswith("-") else f"+{text}"


def _plain(value: float, decimals: int) -> str:
    rounded = round(value, decimals)
    if rounded == 0:
        rounded = 0.0
    return f"{rounded:,.{decimals}f}"


def _day(value: Any) -> str:
    moment = value if isinstance(value, date) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return f"{moment.day} {moment:%b %Y}"


def _percent(value: float) -> str:
    share = round(value * 100.0, 1)
    if share == 0:
        share = 0.0
    return f"{share:.0f}%" if share == int(share) else f"{share:.1f}%"


def _unit_inr(value: float) -> str:
    """A rupee amount per unit (a value or a cost as entered): paise kept, so ₹0.30 never prints as ₹0."""
    if float(value).is_integer():
        return format_inr(value)
    decimals = 2
    while decimals < 6 and abs(value) < 10.0 ** (1 - decimals):
        decimals += 1
    return format_inr(value, decimals=decimals)


def format_value(fmt: FigureFormat, value: int | float | str) -> str:
    """The text the pack prints for `value` in format `fmt`."""
    if fmt == "text":
        return str(value)
    if fmt == "date":
        return _day(value)
    number = float(value)
    if fmt == "count":
        return _plain(number, 0)
    if fmt == "signed_count":
        return _signed(_plain(number, 0))
    if fmt == "share":
        return _percent(number)
    if fmt == "points":
        return f"{_signed(_plain(number * 100.0, 1))} points"
    if fmt == "planned_points":
        return f"{_plain(number, 1)} points"
    if fmt == "amount":
        return _plain(number, 2)
    if fmt == "signed_amount":
        return _signed(_plain(number, 2))
    if fmt == "inr":
        return format_inr(number)
    if fmt == "inr_unit":
        return _unit_inr(number)
    if fmt == "days":
        return f"{_plain(number, 0)} days"
    return f"{_plain(number, 1)}x"  # times


# ---------------------------------------------------------------------------
# Formulas: names and arithmetic only
# ---------------------------------------------------------------------------
_NAME: Final[re.Pattern[str]] = re.compile(r"s(\d+)")


def _evaluate(formula: str, values: list[float]) -> float:
    """`formula` over `values` (s0, s1, ...): + - * / and brackets, nothing else. Raises ValueError."""

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Sub | ast.Mult | ast.Div):
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                raise ValueError("division by zero")
            return left / right
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
            operand = walk(node.operand)
            return -operand if isinstance(node.op, ast.USub) else operand
        if isinstance(node, ast.Name):
            match = _NAME.fullmatch(node.id)
            if match is None or int(match.group(1)) >= len(values):
                raise ValueError(f"unknown name {node.id!r}")
            return values[int(match.group(1))]
        raise ValueError(f"{type(node).__name__} is not allowed in a formula")

    return walk(ast.parse(formula, mode="eval"))


# ---------------------------------------------------------------------------
# Reading artefacts
# ---------------------------------------------------------------------------
class _MissingError(LookupError):
    pass


def _walk_field(document: Any, path: str) -> Any:
    current = document
    for part in path.split("."):
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise _MissingError(path)
    return current


class _Reader:
    """JSON artefacts read once each, and figures made from their fields."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._docs: dict[str, Any] = {}

    @property
    def read(self) -> tuple[str, ...]:
        return tuple(key for key, doc in self._docs.items() if doc is not None)

    def doc(self, key: str) -> Any:
        from engine.storage import StorageError

        if key not in self._docs:
            try:
                self._docs[key] = (
                    json.loads(self._storage.read_bytes(key)) if self._storage.exists(key) else None
                )
            except (StorageError, ValueError):
                self._docs[key] = None
        return self._docs[key]

    def raw(self, key: str, path: str) -> Any:
        document = self.doc(key)
        if document is None:
            raise _MissingError(key)
        return _walk_field(document, path)

    def get(self, key: str, path: str) -> Any:
        try:
            return self.raw(key, path)
        except _MissingError:
            return None

    def fig(self, key: str, path: str, fmt: FigureFormat) -> Figure | None:
        value = self.get(key, path)
        if value is None or isinstance(value, bool) or not isinstance(value, int | float | str):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return Figure(
            value=value,
            text=format_value(fmt, value),
            format=fmt,
            sources=(Source(artefact=key, field=path),),
        )


def derived(parts: list[Figure | None], formula: str, fmt: FigureFormat) -> Figure | None:
    """A figure computed from direct figures (each read from one field) by `formula`; None when any part is."""
    present = [part for part in parts if part is not None]
    if len(present) != len(parts) or any(part.formula is not None for part in present):
        return None
    if any(isinstance(part.value, str) for part in present):
        return None
    try:
        value = _evaluate(formula, [float(part.value) for part in present])
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return Figure(
        value=value,
        text=format_value(fmt, value),
        format=fmt,
        sources=tuple(part.sources[0] for part in present),
        formula=formula,
    )


def _as(figure: Figure | None, fmt: FigureFormat) -> Figure | None:
    """The same direct figure printed in another format."""
    if figure is None:
        return None
    return figure.model_copy(update={"format": fmt, "text": format_value(fmt, figure.value)})


def figures_of(item: Any) -> Iterator[Figure]:
    """Every :class:`Figure` inside a view, a section or any part of one, depth first."""
    if isinstance(item, Figure):
        yield item
    elif isinstance(item, BaseModel):
        for name in type(item).model_fields:
            yield from figures_of(getattr(item, name))
    elif isinstance(item, tuple | list):
        for element in item:
            yield from figures_of(element)


_DIGITS: Final[re.Pattern[str]] = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?")
"""A number as the pack prints it (`1,234`, `2.6`): the unit the free-text check compares."""

_UNPRINTED: Final[frozenset[str]] = frozenset({"campaign_id", "use_case_id", "artefacts", "schema_version"})
"""Fields of the view that are identifiers, never printed as a number of the pack."""


def _free_text(item: Any) -> Iterator[str]:
    """Every string of the view outside a figure: labels, notes, reasons, the headline, string table cells."""
    if isinstance(item, Figure):
        return
    if isinstance(item, str):
        yield item
    elif isinstance(item, BaseModel):
        for name in type(item).model_fields:
            if isinstance(item, ProofView) and name in _UNPRINTED:
                continue
            yield from _free_text(getattr(item, name))
    elif isinstance(item, tuple | list):
        for element in item:
            yield from _free_text(element)


def verify_provenance(view: ProofView, storage: Storage) -> None:
    """Read every figure's sources again and recompute its value and text; raise `ProvenanceError` if any differ.

    Every digit in the view's own words (a label, a note, a reason, the headline) must also be one a figure
    prints, so a number written into a sentence cannot reach the page untraced.
    """
    reader = _Reader(storage)
    failures: list[str] = []
    printed = {token for figure in figures_of(view) for token in _DIGITS.findall(figure.text)}
    for text in _free_text(view):
        stray = sorted(set(_DIGITS.findall(text)) - printed)
        if stray:
            failures.append(f"the words {text!r} print {', '.join(stray)}, which no figure holds")
    for figure in figures_of(view):
        where = ", ".join(f"{s.artefact}:{s.field}" for s in figure.sources)
        try:
            values = [reader.raw(source.artefact, source.field) for source in figure.sources]
        except _MissingError:
            failures.append(f"{where} cannot be read")
            continue
        try:
            if figure.formula is None:
                expected: Any = values[0]
            else:
                expected = _evaluate(figure.formula, [float(value) for value in values])
        except (TypeError, ValueError):
            failures.append(f"{where}: {figure.formula} cannot be computed")
            continue
        if isinstance(expected, str) or isinstance(figure.value, str):
            same = expected == figure.value
        else:
            same = math.isclose(float(expected), float(figure.value), rel_tol=1e-9, abs_tol=1e-9)
        if not same:
            failures.append(f"{where}: {figure.value!r} is not {expected!r}")
        elif figure.text != format_value(figure.format, figure.value):
            failures.append(f"{where}: the text {figure.text!r} does not print {figure.value!r}")
    if failures:
        raise ProvenanceError(tuple(failures))


# ---------------------------------------------------------------------------
# Building the view
# ---------------------------------------------------------------------------
@dataclass
class _Context:
    reader: _Reader
    campaign_id: str
    campaign: dict[str, Any]
    report: dict[str, Any]
    kind: Literal["scored", "external", "programme"]
    claim: Claim
    good: bool
    continuous: bool
    plan_key: str | None
    contacts_key: str | None
    segments_key: str | None
    segments_reason: str | None
    inputs_key: str | None
    costs_key: str | None
    costs_source: str
    approvals: dict[tuple[str, str], int] = field(default_factory=dict)
    ledger_key: str | None = None

    def key(self, name: str) -> str:
        return f"campaigns/{self.campaign_id}/{name}"

    @property
    def report_key(self) -> str:
        return self.key(_REPORT)

    def r(self, path: str, fmt: FigureFormat) -> Figure | None:
        return self.reader.fig(self.report_key, path, fmt)


_KINDS: Final[dict[str, Literal["scored", "external", "programme"]]] = {
    "scored": "scored",
    "external": "external",
    "programme": "programme",
}


def build_proof(
    storage: Storage, campaign_id: str, *, root: Path | None = None, now: datetime | None = None
) -> ProofView:
    """The Value Proof Pack of `campaign_id`, every figure verified (see the module docstring).

    Raises `ProofNotFoundError` for no campaign, `ProofRefusedError` (`PROOF_SYNTHETIC_DATA`,
    `PROOF_NOT_MATURE`) when no proof may be drawn, and `ProvenanceError` when a figure does not resolve.
    """
    from engine.utils.time import utc_now

    if not _safe(campaign_id):
        raise ProofNotFoundError(campaign_id)
    reader = _Reader(storage)
    campaign_key = f"campaigns/{campaign_id}/{_CAMPAIGN}"
    campaign = reader.doc(campaign_key)
    if not isinstance(campaign, dict):
        raise ProofNotFoundError(campaign_id)
    kind: Literal["scored", "external", "programme"] = _KINDS.get(str(campaign.get("kind")), "scored")
    audit = reader.doc(f"campaigns/{campaign_id}/{_AUDIT}") if kind == "external" else None
    programme = reader.doc(f"campaigns/{campaign_id}/{_PROGRAMME}") if kind == "programme" else None
    contacts = reader.doc(f"campaigns/{campaign_id}/{_CONTACTS}")
    _refuse_synthetic(reader, campaign, audit, programme, contacts)
    report_key = f"campaigns/{campaign_id}/{_REPORT}"
    report = reader.doc(report_key)
    plan_key = f"campaigns/{campaign_id}/{_PLAN}"
    plan = reader.doc(plan_key)
    _refuse_immature(campaign, report, plan)
    assert isinstance(report, dict)
    continuous = report.get("outcome_kind") == "continuous"
    claim = _claim(campaign, report)
    inputs_key = _inputs_key(reader, campaign_id, campaign)
    good = _outcome_is_good(reader, campaign, report, audit, programme, inputs_key, root)
    costs_key, costs_source = _costs(reader, campaign, inputs_key)
    segments_key, segments_reason = _segments(reader, f"campaigns/{campaign_id}/{_SEGMENTS}", report)
    contacts_key = f"campaigns/{campaign_id}/{_CONTACTS}"
    context = _Context(
        reader=reader,
        campaign_id=campaign_id,
        campaign=campaign,
        report=report,
        kind=kind,
        claim=claim,
        good=good,
        continuous=continuous,
        plan_key=plan_key if isinstance(plan, dict) else None,
        contacts_key=contacts_key if isinstance(reader.doc(contacts_key), dict) else None,
        segments_key=segments_key,
        segments_reason=segments_reason,
        inputs_key=inputs_key,
        costs_key=costs_key,
        costs_source=costs_source,
    )
    _load_approvals(context)
    incremental = _incremental(context)
    net = _net_value(context)
    backfire, proposals = _backfire(context)
    sections = (
        _plan(context),
        _delivery(context),
        incremental,
        _gross(context),
        _credit(context),
        _offer_money(context),
        _test_cost(context),
        backfire,
        net,
        _method(context),
    )
    name = reader.fig(campaign_key, "name", "text")
    outcome = context.r("outcome_column", "text")
    measured_on = context.r("as_of", "date")
    assert name is not None and outcome is not None and measured_on is not None  # required fields
    view = ProofView(
        campaign_id=campaign_id,
        kind=kind,
        campaign_name=name,
        use_case_id=campaign.get("use_case_id"),
        outcome=outcome,
        outcome_kind="continuous" if continuous else "binary",
        outcome_is_good=good,
        claim=claim,
        claim_label=_CLAIM_LABEL[claim],
        headline=_headline(context, incremental, net),
        measured_on=measured_on,
        sections=sections,
        proposals=proposals,
        artefacts=tuple(sorted(reader.read)),
        built_at=now or utc_now(),
    )
    verify_provenance(view, storage)
    return view


# --- refusals ------------------------------------------------------------------------------------------
def _upload_synthetic(reader: _Reader, campaign: dict[str, Any]) -> bool:
    outcomes = campaign.get("outcomes") or {}
    upload_id = outcomes.get("upload_id") if isinstance(outcomes, dict) else None
    if not _safe(upload_id):
        return False
    return reader.get(f"uploads/{upload_id}/upload.json", "synthetic") is True


def _refuse_synthetic(
    reader: _Reader, campaign: dict[str, Any], audit: Any, programme: Any, contacts: Any
) -> None:
    synthetic = _upload_synthetic(reader, campaign)
    for run_id in campaign.get("run_ids") or ():
        if _safe(run_id) and reader.get(f"runs/{run_id}/run.json", "synthetic") is True:
            synthetic = True
    for readout in (audit, programme, contacts):
        if isinstance(readout, dict) and readout.get("synthetic") is True:
            synthetic = True
    contact_upload = contacts.get("contact_upload_id") if isinstance(contacts, dict) else None
    if _safe(contact_upload) and reader.get(f"uploads/{contact_upload}/upload.json", "synthetic") is True:
        synthetic = True
    if synthetic:
        raise ProofRefusedError(
            PROOF_SYNTHETIC_DATA,
            "This campaign was measured on generated data, where the effect was planted. A Value Proof Pack is "
            "drawn only from a client's own data, so none is made for it.",
        )


def _refuse_immature(campaign: dict[str, Any], report: Any, plan: Any) -> None:
    from datetime import timedelta

    if not isinstance(report, dict):
        window = campaign.get("outcome_window_days")
        when = None
        start = campaign.get("treatment_start")
        if isinstance(window, int) and isinstance(start, str):
            when = (datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(days=window)).date()
        raise ProofRefusedError(
            PROOF_NOT_MATURE,
            "This campaign has not been measured yet. Add its outcomes and measure it once every customer's "
            "outcome period has ended; the Value Proof Pack is drawn from that final result.",
            results_available_on=when,
        )
    if report.get("early_look") is True:
        analysis = plan.get("analysis_date") if isinstance(plan, dict) else None
        raise ProofRefusedError(
            PROOF_NOT_MATURE,
            "The stored result is an early look, read before the date the test plan fixed for the final "
            "reading. Measure the campaign again on or after that date.",
            results_available_on=date.fromisoformat(analysis) if isinstance(analysis, str) else None,
        )
    if report.get("status") != "mature" or (report.get("rows_immature") or 0) > 0:
        available = report.get("results_available_on")
        raise ProofRefusedError(
            PROOF_NOT_MATURE,
            "Some customers' outcome period had not ended when the campaign was measured, so its result is not "
            "final. Measure it again once it has.",
            results_available_on=date.fromisoformat(available) if isinstance(available, str) else None,
        )


# --- the inputs ----------------------------------------------------------------------------------------
_CLAIM_LABEL: Final[dict[Claim, str]] = {
    "proven": "Causal: the customers held back were chosen at random",
    "stated_random": "Random by your statement, not verified",
    "descriptive": "Descriptive only",
}


def _claim(campaign: dict[str, Any], report: dict[str, Any]) -> Claim:
    basis = campaign.get("causal_basis")
    if basis == "declared_random":
        return "stated_random"
    if (
        basis in ("engine_random", "verified_random")
        and campaign.get("causal") is True
        and report.get("causal")
    ):
        return "proven"
    return "descriptive"


def _inputs_key(reader: _Reader, campaign_id: str, campaign: dict[str, Any]) -> str | None:
    """The value inputs: the campaign's own, else its scoring run's (`pilot_roi_inputs.json`)."""
    own = f"campaigns/{campaign_id}/{ROI_INPUTS_FILENAME}"
    if isinstance(reader.doc(own), dict):
        return own
    for run_id in (campaign.get("run_ids") or ())[:1]:
        key = f"runs/{run_id}/{ROI_INPUTS_FILENAME}"
        if _safe(run_id) and isinstance(reader.doc(key), dict):
            return key
    return None


def _outcome_is_good(
    reader: _Reader,
    campaign: dict[str, Any],
    report: dict[str, Any],
    audit: Any,
    programme: Any,
    inputs_key: str | None,
    root: Path | None,
) -> bool:
    """Which way round the outcome counts: the client's value inputs, the readout's, else the use case's rule."""
    from engine.config import load_use_case
    from engine.pilot.roi import outcome_is_good_by_default

    if inputs_key is not None and isinstance(reader.get(inputs_key, "outcome_is_good"), bool):
        return bool(reader.get(inputs_key, "outcome_is_good"))
    for readout in (audit, programme):
        if isinstance(readout, dict) and isinstance(readout.get("outcome_is_good"), bool):
            return bool(readout["outcome_is_good"])
    use_case_id = campaign.get("use_case_id")
    if not isinstance(use_case_id, str):
        return True
    outcomes = campaign.get("outcomes") or {}
    column = str(report.get("outcome_column") or "")
    if not (isinstance(outcomes, dict) and outcomes.get("outcome_named")):
        try:
            config = load_use_case(use_case_id, root)
            column = config.target.column or "outcome"
        except (
            Exception
        ):  # the use case has gone: judge the column by its own name, as the campaign page does
            column = str(report.get("outcome_column") or "")
    return outcome_is_good_by_default(use_case_id, column, root)


def _inputs_source(inputs_key: str) -> str:
    """Whose value inputs these are, in words: the campaign's own or its scoring run's."""
    if inputs_key.startswith("runs/"):
        return "the value inputs entered for the scoring run"
    return "the value inputs entered for this campaign"


def _costs(reader: _Reader, campaign: dict[str, Any], inputs_key: str | None) -> tuple[str | None, str]:
    """Where the contact and offer costs come from: the value inputs, else the costs the run recorded (M97)."""
    if inputs_key is not None:
        return inputs_key, _inputs_source(inputs_key)
    for run_id in (campaign.get("run_ids") or ())[:1]:
        key = f"runs/{run_id}/{_RECOMMENDATION}"
        if (
            _safe(run_id)
            and isinstance(reader.get(key, "contact_cost"), int | float)
            and isinstance(reader.get(key, "offer_cost"), int | float)
        ):
            return key, "the costs the scoring run ranked its list with"
    return None, ""


def _segments(reader: _Reader, key: str, report: dict[str, Any]) -> tuple[str | None, str | None]:
    """`segment_effects.json` when it belongs to the stored report, else None and why."""
    document = reader.doc(key)
    if not isinstance(document, dict):
        return None, (
            "This campaign was measured before results were kept for each group of customers. Measure it again "
            "to see them."
        )
    if document.get("report_computed_at") != report.get("computed_at"):
        return None, (
            "The results kept for each group belong to an earlier measurement of this campaign. Measure it again "
            "to bring them up to date."
        )
    return key, None


def _load_approvals(context: _Context) -> None:
    key = context.key(SUPPRESSIONS_FILENAME)
    document = context.reader.doc(key)
    if not isinstance(document, dict):
        return
    context.ledger_key = key
    for index, approval in enumerate(document.get("approvals") or ()):
        if isinstance(approval, dict) and approval.get("report_computed_at") == context.report.get(
            "computed_at"
        ):
            context.approvals[(str(approval.get("dimension")), str(approval.get("segment")))] = index


# --- small helpers -------------------------------------------------------------------------------------
def _line(
    label: str, value: Figure | None, low: Figure | None = None, high: Figure | None = None, *, missing: str
) -> ProofLine:
    """A line with its figures, or with `missing` when its value could not be read."""
    if value is None:
        return ProofLine(label=label, missing=missing)
    return ProofLine(label=label, value=value, low=low, high=high)


def _not_measured(key: SectionKey, title: str, reason: str) -> ProofSection:
    return ProofSection(key=key, title=title, status="not_measured", reason=reason)


def _oriented(
    context: _Context, base: str, *, multipliers: tuple[Figure | None, ...] = (), fmt: FigureFormat
) -> tuple[Figure | None, Figure | None, Figure | None]:
    """`(value, low, high)` of the report's interval at `base`, times `multipliers`, turned so up is good.

    For an outcome the campaign means to prevent, the gain is the fall: the value is negated and the two
    ends swap, so `low` is still the less favourable end.
    """
    names = ("value", "ci_low", "ci_high") if context.good else ("value", "ci_high", "ci_low")
    sign = "" if context.good else "-"
    out: list[Figure | None] = []
    for name in names:
        parts = [context.r(f"{base}.{name}", fmt), *multipliers]
        if not multipliers and context.good:
            out.append(_as(parts[0], fmt))
            continue
        formula = sign + "*".join(f"s{i}" for i in range(len(parts)))
        out.append(derived(parts, formula, fmt))
    return out[0], out[1], out[2]


def _estimate_base(context: _Context) -> str | None:
    """The report field of the per-customer difference an amount is valued from (adjusted when registered)."""
    if not context.continuous:
        return "incremental_conversions"
    if isinstance(context.reader.get(context.report_key, "adjusted_interval"), dict):
        return "adjusted_interval"
    if isinstance(context.reader.get(context.report_key, "mean_difference_ci"), dict):
        return "mean_difference_ci"
    return None


def _benefit(
    context: _Context, *, extra: tuple[Figure | None, ...] = (), fmt: FigureFormat
) -> tuple[Figure | None, Figure | None, Figure | None]:
    """The campaign's gain in outcomes (a rate) or in the amount's total (an amount), turned so up is good."""
    base = _estimate_base(context)
    if base is None:
        return None, None, None
    rows = (context.r("treated_rows", "count"),) if context.continuous else ()
    return _oriented(context, base, multipliers=(*rows, *extra), fmt=fmt)


def _benefit_label(context: _Context) -> str:
    if context.continuous:
        return "Extra amount because of the campaign" if context.good else "Amount reduced by the campaign"
    return "Extra outcomes because of the campaign" if context.good else "Outcomes prevented by the campaign"


def _treated_words(context: _Context) -> str:
    """Who the treated arm is: for a programme everyone outside the control group, contacted or not."""
    return "customers outside the control group" if context.kind == "programme" else "contacted customers"


def _held_words(context: _Context) -> str:
    return "customers in the control group" if context.kind == "programme" else "held-back customers"


def _cap(text: str) -> str:
    return text[0].upper() + text[1:]


def _one_treated(context: _Context) -> str:
    return "customer outside the control group" if context.kind == "programme" else "contacted customer"


def _conditional(context: _Context, label: str) -> str:
    return (
        f"If the groups were random as you said: {label[0].lower()}{label[1:]}"
        if context.claim == "stated_random"
        else label
    )


_DESCRIPTIVE: Final[str] = (
    "The groups were not chosen at random, so no part of this difference can be credited to the campaign."
)


def _value_inputs(context: _Context) -> tuple[Figure | None, str]:
    if context.inputs_key is None:
        return None, (
            "No value inputs were entered for this campaign. Enter what one extra outcome is worth, and what "
            "contacts and offers cost, to see it in rupees."
        )
    figure = context.reader.fig(context.inputs_key, "value_per_outcome", "inr_unit")
    return figure, "The value inputs could not be read, so nothing is shown in rupees."


def _cost(context: _Context, name: Literal["contact_cost", "offer_cost"]) -> Figure | None:
    if context.costs_key is None:
        return None
    return context.reader.fig(context.costs_key, name, "inr")


def _takers(context: _Context) -> tuple[list[Figure | None], str]:
    """The figures and formula fragment of how many contacted customers took the offer (its cost is paid)."""
    if context.good or context.continuous:
        return [context.r("treated_conversions", "count")], "{0}"
    return [context.r("treated_rows", "count"), context.r("treated_conversions", "count")], "({0}-{1})"


# --- section 1: the plan against what ran ---------------------------------------------------------------
def _plan(context: _Context) -> ProofSection:
    title = "The plan as registered, against what ran"
    if context.plan_key is None:
        reason = {
            "external": "An audit reads a campaign after it ran, so no test plan was registered before its outcomes.",
            "programme": "A programme readout covers a period that has ended, so no test plan was registered for it.",
        }.get(
            context.kind,
            "No test plan was registered before the outcomes were read, so there is nothing to compare what ran "
            "against. Register one with the next campaign.",
        )
        return _not_measured("plan", title, reason)
    reader, key = context.reader, context.plan_key

    def planned(path: str, fmt: FigureFormat) -> Cell:
        figure = reader.fig(key, path, fmt)
        return figure if figure is not None else "not set"

    def ran(figure: Figure | None) -> Cell:
        return figure if figure is not None else "not recorded"

    treated, control = context.r("treated_rows", "count"), context.r("control_rows", "count")
    rows: list[tuple[Cell, ...]] = [
        ("Customers contacted", planned("n_treat", "count"), ran(treated)),
        ("Customers held back", planned("n_holdout", "count"), ran(control)),
        (
            "Share held back",
            planned("holdout_fraction", "share"),
            ran(derived([control, treated], "s0/(s0+s1)", "share")),
        ),
        (
            "Outcome counted over",
            planned("outcome_window_days", "days"),
            ran(context.r("outcome_window_days", "days")),
        ),
        ("Result read on", planned("analysis_date", "date"), ran(context.r("as_of", "date"))),
    ]
    if context.continuous:
        rows.append(
            ("Smallest change the test was sized for", planned("mde_value", "amount"), "measured above")
        )
    else:
        rows.append(
            ("Smallest change the test was sized for", planned("mde_pp", "planned_points"), "measured above")
        )
    rows.append(("Chance of seeing that change", planned("achieved_power", "share"), "not applicable"))
    rows.append(("Plan version", planned("version", "count"), _plan_matched(context)))
    notes: list[str] = []
    if "PLAN_UNDERPOWERED" in (reader.get(key, "warnings") or ()):
        notes.append(
            "The plan was registered knowing the test might be too small to see the change it was sized for."
        )
    return ProofSection(
        key="plan",
        title=title,
        status="measured",
        table=ProofTable(columns=("", "Planned", "As measured"), rows=tuple(rows)),
        notes=tuple(notes),
    )


def _plan_matched(context: _Context) -> str:
    plan_hash = context.reader.get(context.plan_key or "", "plan_hash")
    return (
        "read against this plan"
        if plan_hash is not None and plan_hash == context.report.get("test_plan_hash")
        else "not read against this plan"
    )


# --- section 2: did the list go out --------------------------------------------------------------------
def _delivery(context: _Context) -> ProofSection:
    title = "Did the list go out as planned?"
    if context.contacts_key is None:
        return _not_measured(
            "delivery",
            title,
            "No contact file was added, so whether the customers on the list were really contacted, and whether "
            "any held-back customer was contacted anyway, is not measured. Add one on the campaign's page.",
        )
    reader, key = context.reader, context.contacts_key
    return ProofSection(
        key="delivery",
        title=title,
        status="measured",
        lines=(
            _line(
                "Customers meant to be contacted whose contact is known",
                reader.fig(key, "treated_listed", "count"),
                missing="Not in the contact file.",
            ),
            _line(
                "Of them, contacted",
                reader.fig(key, "contact_rate", "share"),
                missing="Not measured: the contact file lists none of the customers meant to be contacted.",
            ),
            _line(
                "Held-back customers whose contact is known",
                reader.fig(key, "holdout_listed", "count"),
                missing="Not in the contact file.",
            ),
            _line(
                "Of them, contacted anyway",
                reader.fig(key, "contamination", "share"),
                missing="Not measured: the contact file lists none of the held-back customers.",
            ),
        ),
        notes=(
            "Contacted customers missing from the list, or held-back customers contacted anyway, make the "
            "measured difference smaller than the campaign's real effect on the people it reached.",
        ),
    )


# --- section 3: incremental outcomes -------------------------------------------------------------------
def _incremental(context: _Context) -> ProofSection:
    title = "What the campaign changed, with ranges"
    level = context.r(f"{_estimate_base(context) or 'absolute_lift'}.confidence_level", "share")
    lines: list[ProofLine] = [
        _line("Confidence of each range below", level, missing="Not measured: no range could be computed.")
    ]
    treated, held = _treated_words(context), _held_words(context)
    if context.continuous:
        lines += [
            _line(
                f"Average among {treated}",
                context.r("treated_mean", "amount"),
                missing=f"Not measured: the result has no average for the {treated}.",
            ),
            _line(
                f"Average among {held}",
                context.r("control_mean", "amount"),
                missing=f"Not measured: the result has no average for the {held}.",
            ),
        ]
        base = _estimate_base(context)
        if base is not None:
            lines.append(
                _line(
                    (
                        "Difference per customer outside the control group"
                        if context.kind == "programme"
                        else "Difference per contacted customer"
                    )
                    + (
                        " (adjusted for the earlier amount registered in the plan)"
                        if base == "adjusted_interval"
                        else ""
                    ),
                    context.r(f"{base}.value", "signed_amount"),
                    context.r(f"{base}.ci_low", "signed_amount"),
                    context.r(f"{base}.ci_high", "signed_amount"),
                    missing="Not measured: the result has no difference between the two groups' averages.",
                )
            )
    else:
        lines += [
            _line(
                f"{_cap(treated)} with the outcome",
                context.r("treated_rate", "share"),
                missing=f"Not measured: the result has no rate for the {treated}.",
            ),
            _line(
                f"{_cap(held)} with the outcome",
                context.r("control_rate", "share"),
                missing=f"Not measured: the result has no rate for the {held}.",
            ),
            _line(
                "Difference",
                context.r("absolute_lift.value", "points"),
                context.r("absolute_lift.ci_low", "points"),
                context.r("absolute_lift.ci_high", "points"),
                missing="Not measured: one of the two groups had no customer with an outcome.",
            ),
        ]
    if context.claim == "descriptive":
        lines.append(ProofLine(label=_benefit_label(context), missing=_DESCRIPTIVE))
    else:
        value, low, high = _benefit(context, fmt="signed_amount" if context.continuous else "signed_count")
        lines.append(
            _line(
                _conditional(context, _benefit_label(context)),
                value,
                low,
                high,
                missing="Not measured: one of the two groups is too small to compare.",
            )
        )
    table = _offer_table(context)
    notes = [
        "Each range is a confidence interval at that level: the true number very probably lies inside it."
    ]
    if context.claim == "stated_random":
        notes.append(
            "If the groups were not in fact chosen at random, these differences may not be the campaign's doing."
        )
    if context.claim == "descriptive":
        notes.append(_DESCRIPTIVE)
    return ProofSection(
        key="incremental", title=title, status="measured", lines=tuple(lines), table=table, notes=tuple(notes)
    )


def _offer_table(context: _Context) -> ProofTable | None:
    """Per offer: the report's own arms (an audited campaign), else the offer groups of `segment_effects.json`."""
    reader = context.reader
    arms = reader.get(context.report_key, "arms")
    rows: list[tuple[Cell, ...]] = []
    if isinstance(arms, list) and arms:
        for index in range(len(arms)):
            base = f"arms.{index}"
            name = reader.fig(context.report_key, f"{base}.arm", "text")
            effect = reader.fig(context.report_key, f"{base}.effect.value", "points")
            low = reader.fig(context.report_key, f"{base}.effect.ci_low", "points")
            high = reader.fig(context.report_key, f"{base}.effect.ci_high", "points")
            treated = reader.fig(context.report_key, f"{base}.treated_rows", "count")
            if name is None or effect is None or low is None or high is None or treated is None:
                continue
            rows.append((name, treated, effect, FigureRange(low=low, high=high)))
    elif context.segments_key is not None:
        for index, _cell in _cells(context, "offer"):
            base = f"cells.{index}"
            fmt: FigureFormat = "signed_amount" if context.continuous else "points"
            name = reader.fig(context.segments_key, f"{base}.segment", "text")
            effect = reader.fig(context.segments_key, f"{base}.effect.value", fmt)
            low = reader.fig(context.segments_key, f"{base}.effect.ci_low", fmt)
            high = reader.fig(context.segments_key, f"{base}.effect.ci_high", fmt)
            treated = reader.fig(context.segments_key, f"{base}.treated_rows", "count")
            if name is None or treated is None:
                continue
            if effect is None or low is None or high is None:
                rows.append((name, treated, "not measured", "not measured"))
            else:
                rows.append((name, treated, effect, FigureRange(low=low, high=high)))
    if not rows:
        return None
    return ProofTable(columns=("Offer", "Contacted, measured", "Difference", "Range"), rows=tuple(rows))


def _cells(context: _Context, dimension: str) -> list[tuple[int, dict[str, Any]]]:
    if context.segments_key is None:
        return []
    cells = context.reader.get(context.segments_key, "cells") or []
    return [
        (index, cell)
        for index, cell in enumerate(cells)
        if isinstance(cell, dict) and cell.get("dimension") == dimension
    ]


# --- section 4: gross against incremental --------------------------------------------------------------
def _gross(context: _Context) -> ProofSection:
    title = "Gross against incremental"
    if context.continuous:
        rows = context.r("treated_rows", "count")
        gross = derived([context.r("treated_mean", "amount"), rows], "s0*s1", "amount")
        expected = derived([context.r("control_mean", "amount"), rows], "s0*s1", "amount")
        base = _estimate_base(context)
        incremental: tuple[Figure | None, ...] = (None, None, None)
        if base is not None:
            incremental = tuple(
                derived([context.r(f"{base}.{n}", "amount"), rows], "s0*s1", "signed_amount")
                for n in ("value", "ci_low", "ci_high")
            )
        gross_label = f"Total amount of the {_treated_words(context)} (gross)"
        expected_label = f"What the average of the {_held_words(context)} says they would have had anyway"
    else:
        gross = context.r("treated_conversions", "count")
        expected = derived(
            [context.r("control_rate", "share"), context.r("treated_rows", "count")], "s0*s1", "count"
        )
        incremental = (
            context.r("incremental_conversions.value", "signed_count"),
            context.r("incremental_conversions.ci_low", "signed_count"),
            context.r("incremental_conversions.ci_high", "signed_count"),
        )
        gross_label = f"{_cap(_treated_words(context))} with the outcome (gross)"
        expected_label = f"How many would have had it anyway, at the rate of the {_held_words(context)}"
    lines = [
        _line(
            gross_label,
            gross,
            missing=f"Not measured: the result does not say what the {_treated_words(context)} had in all.",
        ),
        _line(
            expected_label,
            expected,
            missing=f"Not measured: none of the {_held_words(context)} was measured.",
        ),
    ]
    if context.claim == "descriptive":
        lines.append(ProofLine(label="The difference: incremental", missing=_DESCRIPTIVE))
    else:
        lines.append(
            _line(
                _conditional(context, "The difference: incremental"),
                incremental[0],
                incremental[1],
                incremental[2],
                missing="Not measured: one of the two groups is too small to compare.",
            )
        )
        share = (
            derived([context.r("incremental_conversions.value", "count"), gross], "s0/s1", "share")
            if not context.continuous
            else None
        )
        if share is not None:
            lines.append(
                ProofLine(label=_conditional(context, "Share of the gross that is incremental"), value=share)
            )
    return ProofSection(
        key="gross",
        title=title,
        status="measured",
        lines=tuple(lines),
        notes=(
            (
                "Gross counts everything the customers outside the control group did, whether or not they were "
                "contacted. Incremental is only what they did beyond the control group's members, chosen at random "
                "and kept out of the programme: the part the programme caused."
                if context.kind == "programme"
                else "Gross counts everything the contacted customers did. Incremental is only what they did "
                "beyond the held-back customers, chosen the same way and not contacted: the part the campaign "
                "caused."
            ),
        ),
    )


# --- section 5: naive credit against measured credit ---------------------------------------------------
def _credit(context: _Context) -> ProofSection:
    title = "Naive credit against measured credit"
    value, value_reason = _value_inputs(context)
    if context.continuous:
        if not context.good:
            return _not_measured(
                "credit",
                title,
                "Naive credit counts every amount contacted customers had as the campaign's; for an amount the "
                "campaign means to reduce there is no such count to compare with.",
            )
        rows = context.r("treated_rows", "count")
        naive = derived([context.r("treated_mean", "amount"), rows], "s0*s1", "amount")
        naive_money = derived([context.r("treated_mean", "amount"), rows, value], "s0*s1*s2", "inr")
        naive_label = f"Naive credit: the whole amount of every {_one_treated(context)}"
    elif context.good:
        naive = context.r("treated_conversions", "count")
        naive_money = derived([naive, value], "s0*s1", "inr")
        naive_label = f"Naive credit: every {_one_treated(context)} who had the outcome"
    else:
        rows = context.r("treated_rows", "count")
        outcomes = context.r("treated_conversions", "count")
        naive = derived([rows, outcomes], "s0-s1", "count")
        naive_money = derived([rows, outcomes, value], "(s0-s1)*s2", "inr")
        naive_label = f"Naive credit: every {_one_treated(context)} who did not have the outcome"
    lines = [
        _line(
            naive_label,
            naive,
            missing=f"Not measured: the result does not say what the {_treated_words(context)} had in all.",
        )
    ]
    if context.claim == "descriptive":
        lines.append(ProofLine(label="Measured credit", missing=_DESCRIPTIVE))
    else:
        measured = _benefit(context, fmt="signed_amount" if context.continuous else "signed_count")
        lines.append(
            _line(
                _conditional(context, "Measured credit: only what the campaign changed"),
                *measured,
                missing="Not measured: one of the two groups is too small to compare.",
            )
        )
        ratio = (
            derived([naive, measured[0]], "s0/s1", "times")
            if naive is not None and measured[0] is not None
            else None
        )
        if ratio is not None and measured[0] is not None and float(measured[0].value) > 0:
            lines.append(ProofLine(label="Naive credit is this many times the measured credit", value=ratio))
    if value is None:
        lines.append(ProofLine(label="In rupees", missing=value_reason))
    else:
        lines.append(
            _line(
                "Naive credit in rupees",
                naive_money,
                missing=f"Not measured: the result does not say what the {_treated_words(context)} had in all.",
            )
        )
        if context.claim != "descriptive":
            money = _benefit(context, extra=(value,), fmt="inr")
            lines.append(
                _line(
                    _conditional(context, "Measured credit in rupees"),
                    *money,
                    missing="Not measured: one of the two groups is too small to compare.",
                )
            )
    return ProofSection(
        key="credit",
        title=title,
        status="measured",
        lines=tuple(lines),
        notes=(
            "Naive credit is what a tool that credits every response to the campaign would report: every outcome "
            f"among the {_treated_words(context)}, including the ones who would have had it anyway.",
        ),
    )


_NO_SEGMENT_FILE: Final[str] = "This campaign has no results kept for each group of customers."

_NOT_READ: Final[dict[str, str]] = {
    "band": "Not read band by band",
    "segment": "Not read predicted group by predicted group",
    "offer": "Not read offer by offer",
}


def _dimension_notes(
    context: _Context, dimensions: tuple[str, ...] = ("band", "segment", "offer")
) -> list[ProofLine]:
    """Why a kind of group was not split, as `segment_effects.json` recorded it (a text figure: it may hold digits)."""
    key = context.segments_key
    if key is None:
        return []
    lines: list[ProofLine] = []
    for index, note in enumerate(context.reader.get(key, "not_measured") or ()):
        dimension = str(note.get("dimension")) if isinstance(note, dict) else ""
        if dimension not in dimensions:
            continue
        reason = context.reader.fig(key, f"not_measured.{index}.reason", "text")
        if reason is not None:
            lines.append(ProofLine(label=_NOT_READ.get(dimension, "Not read group by group"), value=reason))
    return lines


# --- section 6: offer money on sure things and sleeping dogs -------------------------------------------
_SEGMENT_NAMES: Final[dict[str, str]] = {
    "persuadable": "Persuadables",
    "sure_thing": "Sure things",
    "lost_cause": "Lost causes",
    "sleeping_dog": "Sleeping dogs",
}


def _offer_money(context: _Context) -> ProofSection:
    title = "Offer money spent on sure things and sleeping dogs"
    if context.claim == "descriptive":
        return _not_measured("offer_money", title, _DESCRIPTIVE)
    if context.segments_key is None:
        return _not_measured("offer_money", title, context.segments_reason or _NO_SEGMENT_FILE)
    cells = _cells(context, "segment")
    if not cells:
        skipped = _dimension_notes(context, ("segment",))
        if skipped:
            return ProofSection(
                key="offer_money",
                title=title,
                status="not_measured",
                reason="The customers could not be read by predicted group; the line below says why.",
                lines=tuple(skipped),
            )
        return _not_measured(
            "offer_money",
            title,
            (
                "A programme readout compares everyone with the universal control group; its customers were not "
                "sorted into persuadables, sure things, lost causes and sleeping dogs."
                if context.kind == "programme"
                else "This list was not chosen by the campaign-effect model, so its customers were not sorted "
                "into persuadables, sure things, lost causes and sleeping dogs."
            ),
        )
    reader, key = context.reader, context.segments_key
    offer_cost = _cost(context, "offer_cost")
    rows: list[tuple[Cell, ...]] = []
    wasted_parts: list[Figure | None] = []
    wasted_terms: list[str] = []
    for index, cell in cells:
        base = f"cells.{index}"
        name = _SEGMENT_NAMES.get(str(cell.get("segment")), None)
        label: Cell = (
            name if name is not None else (reader.fig(key, f"{base}.segment", "text") or "not named")
        )
        contacted = reader.fig(key, f"{base}.treated_rows", "count")
        conversions = reader.fig(key, f"{base}.treated_conversions", "count")
        if context.good or context.continuous:
            takers = _as(conversions, "count")
            money = derived([conversions, offer_cost], "s0*s1", "inr")
            take_parts, take_term = [conversions], "s{0}"
        else:
            takers = derived([contacted, conversions], "s0-s1", "count")
            money = derived([contacted, conversions, offer_cost], "(s0-s1)*s2", "inr")
            take_parts, take_term = [contacted, conversions], "(s{0}-s{1})"
        effect = reader.fig(key, f"{base}.effect.value", "signed_amount" if context.continuous else "points")
        rows.append(
            (
                label,
                contacted if contacted is not None else "not measured",
                takers if takers is not None else "not measured",
                money if money is not None else "no offer cost entered",
                effect if effect is not None else "not measured",
            )
        )
        if cell.get("segment") in ("sure_thing", "sleeping_dog"):
            start = len(wasted_parts)
            wasted_parts += take_parts
            wasted_terms.append(take_term.format(*(start + i for i in range(len(take_parts)))))
    lines: list[ProofLine] = []
    if not wasted_terms:
        lines.append(
            ProofLine(
                label="Offer money spent on sure things and sleeping dogs",
                missing="No customer predicted to be a sure thing or a sleeping dog was in the measured list.",
            )
        )
    elif offer_cost is None:
        lines.append(
            ProofLine(
                label="Offer money spent on sure things and sleeping dogs",
                missing="No offer cost was entered, so the money is not measured; the counts are in the table.",
            )
        )
    else:
        cost_name = f"s{len(wasted_parts)}"
        formula = "(" + "+".join(wasted_terms) + ")*" + cost_name
        lines.append(
            _line(
                "Offer money spent on sure things and sleeping dogs",
                derived([*wasted_parts, offer_cost], formula, "inr"),
                missing="Not measured: a count in the table for those groups could not be read.",
            )
        )
    return ProofSection(
        key="offer_money",
        title=title,
        status="measured",
        lines=tuple(lines),
        table=ProofTable(
            columns=(
                "Predicted group",
                "Contacted, measured",
                "Took the offer",
                "Offer money",
                "Measured difference",
            ),
            rows=tuple(rows),
        ),
        notes=(
            "Sure things would have had the outcome anyway, so an offer they take is money spent for nothing. "
            "Sleeping dogs react badly to being contacted. The offer cost is "
            + (context.costs_source or "not entered")
            + ".",
            *(
                (
                    "Each group's measured difference holds only if the groups were chosen at random as you said.",
                )
                if context.claim == "stated_random"
                else ()
            ),
        ),
    )


# --- section 7: the cost of the control group and the explore slice ------------------------------------
def _test_cost(context: _Context) -> ProofSection:
    title = "What the control group and the explore slice cost"
    reader = context.reader
    campaign_key = context.key(_CAMPAIGN)
    held = context.r("control_rows", "count")
    lines: list[ProofLine] = [
        _line(
            f"{_cap(_held_words(context))} measured",
            held,
            missing=f"Not measured: the result does not say how many {_held_words(context)} it rests on.",
        )
    ]
    if context.claim == "descriptive":
        lines.append(ProofLine(label="What holding them back cost", missing=_DESCRIPTIVE))
    else:
        if context.continuous:
            base = _estimate_base(context)
            forgone = (
                _oriented(context, base, multipliers=(held,), fmt="signed_amount")
                if base is not None
                else (None, None, None)
            )
        else:
            forgone = _oriented(context, "absolute_lift", multipliers=(held,), fmt="signed_count")
        lines.append(
            _line(
                _conditional(
                    context,
                    (
                        "What they would have added had they been in the programme"
                        if context.kind == "programme"
                        else "What they would have added had they been contacted"
                    ),
                ),
                *forgone,
                missing="Not measured: one of the two groups is too small to compare.",
            )
        )
        value, reason = _value_inputs(context)
        if value is None:
            lines.append(ProofLine(label="In rupees", missing=reason))
        else:
            base = _estimate_base(context) if context.continuous else "absolute_lift"
            money = (
                _oriented(context, base, multipliers=(held, value), fmt="inr")
                if base is not None
                else (None, None, None)
            )
            lines.append(
                _line(
                    _conditional(context, "What that is worth in rupees, before costs"),
                    *money,
                    missing="Not measured: one of the two groups is too small to compare.",
                )
            )
    if context.kind == "programme":  # a programme has no list, so no explore slice beside one
        return ProofSection(
            key="test_cost",
            title="What the control group costs",
            status="measured",
            lines=tuple(lines),
            notes=("The control group is the price of knowing: without it nothing above could be measured.",),
        )
    explore = reader.fig(campaign_key, "counts.explore", "count")
    lines.append(
        _line(
            "Customers contacted at random outside the list (the explore slice)",
            explore,
            missing="Not recorded: the campaign record has no count of an explore slice.",
        )
    )
    if explore is not None and float(explore.value) > 0:
        contact, offer = _cost(context, "contact_cost"), _cost(context, "offer_cost")
        if contact is None or offer is None:
            lines.append(
                ProofLine(
                    label="What the explore slice cost",
                    missing="Not measured: no contact or offer cost was entered.",
                )
            )
        else:
            lines.append(
                ProofLine(
                    label="What the explore slice cost, from every contact paid to every offer also taken",
                    value=derived([explore, contact], "s0*s1", "inr"),
                    low=derived([explore, contact], "s0*s1", "inr"),
                    high=derived([explore, contact, offer], "s0*(s1+s2)", "inr"),
                )
            )
    return ProofSection(
        key="test_cost",
        title=title,
        status="measured",
        lines=tuple(lines),
        notes=(
            "The held-back customers are the price of knowing: without them nothing above could be measured. "
            "The explore slice pays for what the next model learns about customers outside the list.",
        ),
    )


# --- section 8: backfire -------------------------------------------------------------------------------
_DIMENSION_WORDS: Final[dict[str, str]] = {"band": "Band", "segment": "Predicted group", "offer": "Offer"}


def _backfire(context: _Context) -> tuple[ProofSection, tuple[SuppressionProposal, ...]]:
    title = "Groups where the campaign backfired"
    if context.claim == "descriptive":
        return _not_measured("backfire", title, _DESCRIPTIVE), ()
    if context.segments_key is None:
        return _not_measured("backfire", title, context.segments_reason or _NO_SEGMENT_FILE), ()
    reader, key = context.reader, context.segments_key
    cells = [
        (index, cell) for index, cell in enumerate(reader.get(key, "cells") or ()) if isinstance(cell, dict)
    ]
    skipped = _dimension_notes(context)
    if not cells:
        if context.kind == "programme":
            reason = (
                "A programme readout compares everyone with the universal control group; it has no groups of "
                "customers to read one by one."
            )
        elif skipped:
            reason = "No group of the campaign's customers could be read one by one; the lines below say why."
        else:
            reason = "The campaign's customers carry no band, predicted group or offer to read one by one."
        section = ProofSection(
            key="backfire", title=title, status="not_measured", reason=reason, lines=tuple(skipped)
        )
        return section, ()
    fmt: FigureFormat = "signed_amount" if context.continuous else "points"
    sign = "" if context.good else "-"
    ends = ("ci_low", "ci_high") if context.good else ("ci_high", "ci_low")
    rows: list[tuple[Cell, ...]] = []
    proposals: list[SuppressionProposal] = []
    flagged_any = False
    for index, cell in cells:
        base = f"cells.{index}"

        def turned(path: str) -> Figure | None:
            figure = reader.fig(key, path, fmt)
            return _as(figure, fmt) if context.good else derived([figure], f"{sign}s0", fmt)

        name = reader.fig(key, f"{base}.segment", "text")
        if name is None:
            continue
        dimension = str(cell.get("dimension"))
        contacted = reader.fig(key, f"{base}.treated_rows", "count")
        held = reader.fig(key, f"{base}.control_rows", "count")
        effect = turned(f"{base}.effect.value")
        low95, high95 = turned(f"{base}.effect.{ends[0]}"), turned(f"{base}.effect.{ends[1]}")
        family_low, family_high = turned(f"{base}.family_interval.{ends[0]}"), turned(
            f"{base}.family_interval.{ends[1]}"
        )
        judged = cell.get("judged") is True and family_high is not None and family_low is not None
        flagged = judged and family_high is not None and float(family_high.value) < 0
        if not judged:
            verdict = "too few customers to judge"
        elif flagged:
            verdict = _conditional(context, "backfired")
        else:
            verdict = "no backfire shown"
        rows.append(
            (
                _DIMENSION_WORDS.get(dimension, dimension),
                (_SEGMENT_NAMES.get(str(cell.get("segment")), name) if dimension == "segment" else name),
                contacted if contacted is not None else "not measured",
                held if held is not None else "not measured",
                effect if effect is not None else "not measured",
                (
                    FigureRange(low=low95, high=high95)
                    if low95 is not None and high95 is not None
                    else "not measured"
                ),
                (
                    FigureRange(low=family_low, high=family_high)
                    if judged and family_low and family_high
                    else "not judged"
                ),
                verdict,
            )
        )
        if flagged and family_high is not None and dimension in ("band", "segment", "offer"):
            flagged_any = True
            proposals.append(_proposal(context, dimension, str(cell.get("segment")), name, family_high))
    lines = (
        _line(
            "Groups judged",
            reader.fig(key, "family_size", "count"),
            missing="Not recorded: the group results do not say how many groups were judged.",
        ),
        _line(
            "Confidence of each judged range, allowing for that many checks",
            reader.fig(key, "family_confidence", "share"),
            missing="Not measured: no group had enough customers in both groups to be judged.",
        ),
        _line(
            "Smallest number of customers a judged group has, contacted and held back alike",
            reader.fig(key, "min_rows_per_arm", "count"),
            missing="Not recorded: the group results do not say how small a judged group may be.",
        ),
        *skipped,
    )
    notes = [
        "A group is flagged only when its whole range, widened to allow for the number of groups checked, lies "
        "on the harmful side of zero. Reading many groups at the usual confidence would flag some by chance; "
        "this rule keeps the chance of any false flag in a campaign with no harm anywhere to one in twenty.",
        "A flagged group comes with a suggestion: leave it out of the next cycle. An Analyst approves it; "
        "nothing changes in any list until then, and the engine never applies it by itself.",
    ]
    if not flagged_any:
        notes.insert(0, "No group backfired.")
    if context.claim == "stated_random":
        notes.append("These group results hold only if the groups were chosen at random as you said.")
    section = ProofSection(
        key="backfire",
        title=title,
        status="measured",
        lines=lines,
        table=ProofTable(
            columns=(
                "Kind",
                "Group",
                "Contacted",
                "Held back",
                "Difference",
                "Range",
                "Range allowing for every group checked",
                "Result",
            ),
            rows=tuple(rows),
        ),
        notes=tuple(notes),
    )
    return section, tuple(proposals)


def _proposal(
    context: _Context, dimension: str, segment: str, name: Figure, worst: Figure
) -> SuppressionProposal:
    index = context.approvals.get((dimension, segment))
    status: Literal["proposed", "approved"] = "approved" if index is not None else "proposed"
    by = at = None
    if index is not None and context.ledger_key is not None:
        by = context.reader.fig(context.ledger_key, f"approvals.{index}.approved_by", "text")
        at = context.reader.fig(context.ledger_key, f"approvals.{index}.approved_at", "date")
    return SuppressionProposal(
        dimension=dimension,  # type: ignore[arg-type]  # checked by the caller
        segment=name,
        worst_case=worst,
        status=status,
        approved_by=by,
        approved_at=at,
    )


# --- section 9: net value ------------------------------------------------------------------------------
_NET_LINE: Final[int] = 3
"""The position of the net value's line in its section (the headline quotes it)."""


def _contacts_paid(context: _Context) -> tuple[Figure | None, str, str | None]:
    """How many contacts were paid for, its line's label and, when only the measured ones are costed, why.

    Every customer meant to be contacted in the measured population was paid for, including those later left
    out of the measurement for having no outcome in the file: the campaign record's `counts.intended_treated`.
    A record without that count falls back to the measured contacted customers, and the pack says so.
    """
    paid = context.reader.fig(context.key(_CAMPAIGN), "counts.intended_treated", "count")
    if paid is not None:
        return paid, "Cost of contacts, for every customer meant to be contacted", None
    return (
        context.r("treated_rows", "count"),
        "Cost of contacts, for the contacted customers measured",
        "The campaign record does not say how many customers were meant to be contacted, so only the "
        "contacted customers measured are costed; any left out for having no outcome are not, and the cost "
        "is that much too low.",
    )


def _net_value(context: _Context) -> ProofSection:
    title = "Net value in rupees, as a range"
    if context.claim == "descriptive":
        return _not_measured("net_value", title, _DESCRIPTIVE)
    value, reason = _value_inputs(context)
    if context.kind == "programme":
        return _not_measured(
            "net_value",
            title,
            "A programme readout does not record who outside the control group was contacted or who took an "
            "offer, so what the programme cost, and its net value, are not measured. "
            + (
                "What it changed, valued at the inputs entered, is in the credit section."
                if value is not None
                else "Enter what one extra outcome is worth to see what it changed in rupees, in the credit "
                "section."
            ),
        )
    if value is None:
        return _not_measured("net_value", title, reason)
    contact, offer = _cost(context, "contact_cost"), _cost(context, "offer_cost")
    rows = context.r("treated_rows", "count")
    paid, contacts_label, contacts_note = _contacts_paid(context)
    base = _estimate_base(context)
    if base is None or contact is None or offer is None or rows is None or paid is None:
        return _not_measured(
            "net_value",
            title,
            "The difference between the groups, or the costs, could not be read, so no net value is shown.",
        )
    take_parts, take_term = _takers(context)
    names = ("value", "ci_low", "ci_high") if context.good else ("value", "ci_high", "ci_low")
    sign = "" if context.good else "-"
    nets: list[Figure | None] = []
    gains: list[Figure | None] = []
    for name in names:
        estimate = context.r(f"{base}.{name}", "count")
        gain_parts: list[Figure | None] = [estimate, *((rows,) if context.continuous else ()), value]
        gain = sign + "*".join(f"s{i}" for i in range(len(gain_parts)))
        gains.append(derived(gain_parts, gain, "inr"))
        parts = [*gain_parts, paid, contact, *take_parts, offer]
        at = len(gain_parts)
        takers = take_term.format(*(f"s{at + 2 + i}" for i in range(len(take_parts))))
        formula = f"{gain} - s{at}*s{at + 1} - {takers}*s{at + 2 + len(take_parts)}"
        nets.append(derived(parts, formula, "inr"))
    contacts_total = derived([paid, contact], "s0*s1", "inr")
    offer_parts = [*take_parts, offer]
    offers_total = derived(
        offer_parts,
        take_term.format(*(f"s{i}" for i in range(len(take_parts)))) + f"*s{len(take_parts)}",
        "inr",
    )
    unreadable = "Not measured: a count or a cost it is computed from could not be read."
    lines = (
        _line(
            _conditional(context, "Value of what the campaign changed"),
            gains[0],
            gains[1],
            gains[2],
            missing="Not measured: one of the two groups is too small to compare.",
        ),
        _line(contacts_label, contacts_total, missing=unreadable),
        _line(
            (
                "Cost of offers taken, by the contacted customers measured with an amount above zero"
                if context.continuous
                else "Cost of offers taken, by the contacted customers measured"
            ),
            offers_total,
            missing=unreadable,
        ),
        _line(_conditional(context, "Net value"), nets[0], nets[1], nets[2], missing=unreadable),  # _NET_LINE
        _line(
            (
                "Value of one unit of the amount, as entered"
                if context.continuous
                else "Value of one extra outcome, as entered"
            ),
            value,
            missing="Not entered.",
        ),
        _line("Cost of one contact, as entered", _as(contact, "inr_unit"), missing="Not entered."),
        _line("Cost of one offer taken, as entered", _as(offer, "inr_unit"), missing="Not entered."),
    )
    notes = [
        "The range comes from the measured range of what the campaign changed; the rupee values per outcome "
        "and the costs are "
        + (context.costs_source or "the value inputs")
        + ", estimates that move the rupees but not the measured count.",
        "Offers are costed for the contacted customers measured; an offer taken by a customer with no outcome "
        "in the file is not known, so it is not counted.",
    ]
    if contacts_note is not None:
        notes.append(contacts_note)
    return ProofSection(
        key="net_value",
        title=title,
        status="measured",
        lines=lines,
        notes=tuple(notes),
    )


# --- section 10: method and limits ---------------------------------------------------------------------
_BASIS_TEXT: Final[dict[str, str]] = {
    "engine_random": "The engine chose who was held back, at random, before the campaign went out.",
    "verified_random": (
        "You said the groups were chosen at random, and the engine could not tell them apart from the customer "
        "details in the file, so the claim is verified."
    ),
    "declared_random": (
        "You said the groups were chosen at random. The file had nothing to check that with, so every result "
        "holds only under that statement."
    ),
    "not_random": "The groups were not chosen at random, so their difference describes them and proves nothing.",
}


def _method(context: _Context) -> ProofSection:
    basis = str(context.campaign.get("causal_basis") or "not_random")
    lines: list[ProofLine] = [
        _line("Result read on", context.r("as_of", "date"), missing="Not recorded: the result has no date."),
        _line(
            "Outcome counted over",
            context.r("outcome_window_days", "days"),
            missing="Every outcome in the file was counted.",
        ),
        _line(
            "Customers left out for having no outcome in the file",
            context.r("rows_without_outcome", "count"),
            missing="Not recorded: the result does not say how many customers had no outcome.",
        ),
    ]
    if _estimate_base(context) == "adjusted_interval":
        lines += [
            _line(
                "Adjusted for each customer's earlier amount in",
                context.r("covariate_column", "text"),
                missing="Not recorded: the result does not name the earlier amount.",
            ),
            _line(
                "Share of the noise the adjustment removed",
                context.r("variance_reduction", "share"),
                missing="Not recorded: the result does not say how much noise the adjustment removed.",
            ),
        ]
    notes = [
        _BASIS_TEXT.get(basis, _BASIS_TEXT["not_random"]),
        {
            "proven": "So the results are proven: the difference between the groups is what the campaign caused.",
            "stated_random": "So the results are shown under your statement, not as proven value.",
            "descriptive": "So nothing in this pack is credited to the campaign as value it caused.",
        }[context.claim],
        "Outcomes are counted for everyone the campaign was meant to reach, whether or not the message arrived, "
        "so the result is the effect of running the campaign.",
        (
            f"Rupee values use {_inputs_source(context.inputs_key)}; they are estimates, and the pack says where "
            "each comes from."
            if context.inputs_key is not None
            else "No value inputs were entered, so no value is shown in rupees."
        ),
        "Results for each group are read without any adjustment by an earlier amount, and judged by the rule in "
        "the backfire section.",
    ]
    if context.kind == "programme":
        notes.append(
            "A programme readout compares everyone outside the universal control group with its members, over a "
            "period: the effect of the whole programme, not of one message."
        )
    return ProofSection(
        key="method", title="Method and limits", status="measured", lines=tuple(lines), notes=tuple(notes)
    )


# --- the headline --------------------------------------------------------------------------------------
def _range_text(line: ProofLine) -> str:
    if line.value is None:
        return ""
    if line.low is not None and line.high is not None:
        return f"{line.value.text} (likely {line.low.text} to {line.high.text})"
    return line.value.text


def _headline(context: _Context, incremental: ProofSection, net: ProofSection) -> str:
    if context.claim == "descriptive":
        return (
            "Descriptive only: the groups were not chosen at random, so this pack shows what each group did and "
            "credits no value to the campaign."
        )
    gain = incremental.lines[-1] if incremental.lines else None  # the gain is the section's last line
    text = _range_text(gain) if gain is not None else ""
    if not text:
        return _conditional(context, "The groups were too small to compare.")
    sentence = f"{_conditional(context, _benefit_label(context))}: {text}."
    net_line = net.lines[_NET_LINE] if net.status == "measured" and len(net.lines) > _NET_LINE else None
    if (
        net_line is not None
        and net_line.value is not None
        and net_line.low is not None
        and net_line.high is not None
    ):
        sentence += f" Net value {net_line.low.text} to {net_line.high.text}."
    return sentence


# ---------------------------------------------------------------------------
# Approving a suggestion; the campaign's own value inputs
# ---------------------------------------------------------------------------
def approve_suppression(
    storage: Storage,
    campaign_id: str,
    *,
    dimension: Literal["band", "segment", "offer"],
    segment: str,
    approved_by: str,
    now: datetime,
    root: Path | None = None,
) -> SuppressionApproval:
    """Record that an Analyst approved leaving a flagged group out of the next cycle; nothing else changes.

    Raises `ProofRefusedError` (`PROOF_SUPPRESSION_INVALID`) when the pack does not flag that group, and
    whatever :func:`build_proof` raises. Approving the same group of the same measurement again returns the
    approval already recorded.
    """
    view = build_proof(storage, campaign_id, root=root, now=now)
    flagged = {(p.dimension, str(p.segment.value)) for p in view.proposals}
    if (dimension, segment) not in flagged:
        raise ProofRefusedError(
            PROOF_SUPPRESSION_INVALID,
            "That group is not flagged as backfiring in this campaign's Value Proof Pack, so there is no "
            "suggestion to approve for it.",
        )
    report = storage.read_model(f"campaigns/{campaign_id}/{_REPORT}", _ReportStamp)
    key = f"campaigns/{campaign_id}/{SUPPRESSIONS_FILENAME}"
    ledger = (
        storage.read_model(key, SuppressionLedger)
        if storage.exists(key)
        else SuppressionLedger(campaign_id=campaign_id)
    )
    for approval in ledger.approvals:
        if (
            approval.dimension == dimension
            and approval.segment == segment
            and approval.report_computed_at == report.computed_at
        ):
            return approval
    approval = SuppressionApproval(
        dimension=dimension,
        segment=segment,
        report_computed_at=report.computed_at,
        approved_by=approved_by,
        approved_at=now,
    )
    storage.write_model(key, ledger.model_copy(update={"approvals": (*ledger.approvals, approval)}))
    return approval


class _ReportStamp(BaseModel):
    """Only the moment a stored report was computed (the rest of the report is not needed here)."""

    computed_at: AwareDatetime


def save_campaign_value_inputs(storage: Storage, campaign_id: str, inputs: RoiInputs) -> RoiInputs:
    """Store the value inputs of a campaign (`campaigns/<id>/pilot_roi_inputs.json`); they win over the run's."""
    from engine.utils.time import utc_now

    stamped = inputs if inputs.entered_at is not None else inputs.model_copy(update={"entered_at": utc_now()})
    storage.write_model(f"campaigns/{campaign_id}/{ROI_INPUTS_FILENAME}", stamped)
    return stamped


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
def _line_text(line: ProofLine) -> str:
    if line.value is None:
        missing = line.missing or ""
        return missing if missing.startswith("Not ") else f"Not measured. {missing}".strip()
    if line.low is not None and line.high is not None:
        return f"{line.value.text}, likely {line.low.text} to {line.high.text}"
    return line.value.text


def _cell_text(cell: Cell) -> str:
    if isinstance(cell, Figure):
        return cell.text
    if isinstance(cell, FigureRange):
        return f"{cell.low.text} to {cell.high.text}"
    return cell


def _tone(view: ProofView) -> Literal["info", "warning", "error", "success"]:
    """The headline box's colour: green only for proven value whose whole range is a gain."""
    if view.claim != "proven":
        return "info"
    gain = next((s for s in view.sections if s.key == "incremental"), None)
    line = gain.lines[-1] if gain is not None and gain.lines else None
    if line is None or line.low is None or line.high is None:
        return "warning"
    if float(line.high.value) < 0:
        return "error"
    return "success" if float(line.low.value) > 0 else "warning"


def proof_document(view: ProofView, *, client_name: str = "") -> ReportDocument:
    """The pack as a report document: the same blocks for the HTML page and the PDF."""
    blocks: list[AnyBlock] = [Callout(title=view.claim_label, text=view.headline, tone=_tone(view))]
    for section in view.sections:
        blocks.append(Heading(text=section.title))
        if section.status == "not_measured":
            blocks.append(Callout(title="Not measured", text=section.reason or "", tone="info"))
            if section.lines:
                blocks.append(KeyValues(rows=tuple((line.label, _line_text(line)) for line in section.lines)))
            continue
        if section.lines:
            blocks.append(KeyValues(rows=tuple((line.label, _line_text(line)) for line in section.lines)))
        if section.table is not None:
            blocks.append(
                Table(
                    columns=section.table.columns,
                    rows=tuple(tuple(_cell_text(cell) for cell in row) for row in section.table.rows),
                )
            )
        if section.key == "backfire":
            for proposal in view.proposals:
                if proposal.status == "approved" and proposal.approved_by and proposal.approved_at:
                    text = (
                        f"Leaving {proposal.segment.text} out of the next cycle was approved by "
                        f"{proposal.approved_by.text} on {proposal.approved_at.text}. Apply it in the next "
                        f"cycle's settings; the engine does not change any list by itself."
                    )
                    blocks.append(Callout(title="Suggestion approved", text=text, tone="success"))
                else:
                    text = (
                        f"Leave {proposal.segment.text} out of the next cycle: even the least harmful end of its "
                        f"range is {proposal.worst_case.text}. An Analyst approves this suggestion; nothing "
                        f"changes until then."
                    )
                    if view.claim == "stated_random":
                        text = (
                            "If the groups were random as you said, l"
                            + text[1:]
                            + " If they were not, this group's result may not be the campaign's doing."
                        )
                    blocks.append(Callout(title="Suggestion for the next cycle", text=text, tone="warning"))
        if section.notes:
            blocks.append(Bullets(items=section.notes))
    blocks.append(
        Paragraph(
            text=(
                "Every number in this pack is read from the campaign's own measured records, and the pack is "
                "refused if any of them cannot be traced back."
            ),
            muted=True,
        )
    )
    return ReportDocument(
        kind="proof",
        title="Value Proof Pack",
        subtitle=view.campaign_name.text,
        client_name=client_name,
        generated_at=view.built_at,
        facts=(
            ("Campaign", view.campaign_name.text),
            ("Outcome", view.outcome.text),
            ("Result read on", view.measured_on.text),
            ("What the numbers can claim", view.claim_label),
        ),
        blocks=tuple(blocks),
        footer="Measured numbers come from the campaign's records; rupee values use the inputs the pack names.",
    )
