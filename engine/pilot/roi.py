"""The value and ROI view: a measured campaign effect, in rupees, as a range (Plan E M62, DEC-908).

What a campaign *caused* is the difference between the customers it contacted and a control group
chosen at random and held back. The platform measures that twice already, and this module reads
whichever exists for a scoring run, never computing an effect of its own:

1. `incrementality_report.json` (Phase 3b, `POST /runs/{id}/campaign-results`): incremental
   outcomes with a 95% interval, or - while the outcome window is still open - the date the results
   will be ready (`results_available_on`).
2. `incrementality_input.json` (Phase 4b outcome ingestion, `POST /runs/{id}/outcomes`): the
   treated and control groups' counts. Its difference carries no interval, so the interval is taken
   with the same function Phase 3b uses (`engine.uplift.incrementality.newcombe_interval`), on the
   same counts, which makes the two sources agree when both exist.

When neither exists, the run's outcome window (`engine.scheduling.outcomes.outcome_window`) says
when outcomes can first be measured, and the view shows that date instead of a number.

The rupee figures multiply that interval by values the *client* enters - what one extra retained or
won-back customer is worth, what an offer costs when taken, what a contact costs - stored with the
run (`pilot_roi_inputs.json`) and printed on the report, which says that the value depends on them.
Every amount is a range from the interval's low end to its high end, with the point estimate
between; never a single number (Plan E M62).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from engine.pilot.document import (
    AnyBlock,
    Bullets,
    Callout,
    Heading,
    KeyValues,
    Paragraph,
    ReportDocument,
    Table,
    Verdict,
    VerdictState,
)

if TYPE_CHECKING:
    from engine.clients import ClientStore
    from engine.storage import Storage

__all__ = [
    "ROI_INPUTS_FILENAME",
    "Money",
    "RoiInputs",
    "RoiView",
    "compute_roi",
    "format_inr",
    "load_roi_inputs",
    "roi_document",
    "save_roi_inputs",
]

ROI_INPUTS_FILENAME: Final[str] = "pilot_roi_inputs.json"
"""Stored beside the run's other artefacts: `runs/<run_id>/pilot_roi_inputs.json`."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoiInputs(_Strict):
    """The client's own figures. Nothing here is measured; the report says so."""

    schema_version: Literal[1] = 1
    currency: Literal["INR"] = "INR"
    value_per_outcome: float = Field(
        ge=0, le=1e9, description="Rupees one extra retained or won-back customer is worth."
    )
    outcome_is_good: bool | None = Field(
        default=None,
        description=(
            "True when the outcome measured is one the campaign wants more of (converted, came back); "
            "false when it is one it wants less of (left), so fewer outcomes is the gain; null takes the "
            "use case's default from configs/pilot/value.yaml."
        ),
    )
    value_basis: str = Field(
        default="", max_length=300, description="What that value means, in the client's words."
    )
    offer_cost: float = Field(
        default=0.0,
        ge=0,
        le=1e9,
        description="Rupees an offer costs each time a contacted customer takes it.",
    )
    contact_cost: float = Field(
        default=0.0, ge=0, le=1e9, description="Rupees each contact costs (SMS, call, e-mail)."
    )
    entered_by: str = Field(default="", max_length=120)
    entered_at: datetime | None = None
    note: str = Field(default="", max_length=500)


class Money(_Strict):
    """An amount with its range: `low` and `high` from the interval's two ends, `value` between."""

    value: float
    low: float | None
    high: float | None


class RoiView(_Strict):
    run_id: str
    use_case_id: str
    status: Literal["measured", "not_mature", "not_measured"]
    outcome_is_good: bool = True
    """The direction used: the client's choice when they made one, else the use case's default."""
    source: Literal["incrementality_report", "outcome_ingestion"] | None
    results_available_on: date | None
    causal: bool
    outcome_name: str
    treated_rows: int = 0
    treated_outcomes: int = 0
    treated_rate: float | None = None
    control_rows: int = 0
    control_outcomes: int = 0
    control_rate: float | None = None
    confidence_level: float = 0.95
    incremental: Money | None = None
    """Extra outcomes the campaign caused, with their interval, as measured (treated minus control)."""
    benefit: Money | None = None
    """Customers the campaign gained: `incremental`, or its negative when the outcome is one to prevent."""
    benefit_label: str = "Extra customers because of the campaign"
    inputs: RoiInputs | None = None
    gross_value: Money | None = None
    contact_cost_total: float | None = None
    offer_cost_total: float | None = None
    net_value: Money | None = None
    roi: Money | None = None
    """Net value per rupee spent; null when nothing was spent."""
    summary: str


VALUE_CONFIG: Final[str] = "pilot/value.yaml"


def outcome_is_good_by_default(use_case_id: str, outcome_name: str, root: Path | None = None) -> bool:
    """False when the campaign was measured on the very outcome its use case exists to prevent."""
    from engine.config import config_root, load_use_case, load_yaml

    try:
        prevented = set(load_yaml(config_root(root) / VALUE_CONFIG).get("outcomes_to_prevent") or ())
    except Exception:  # no configuration: every outcome counts as one to have more of
        return True
    if use_case_id not in prevented:
        return True
    try:
        config = load_use_case(use_case_id, root)
    except Exception:
        return True
    own = {config.target.column}
    if config.label is not None:
        own.add(config.label.name)
    return outcome_name not in own


class _Common(TypedDict):
    run_id: str
    use_case_id: str
    inputs: RoiInputs | None


# ---------------------------------------------------------------------------
# Storage of the inputs
# ---------------------------------------------------------------------------
def save_roi_inputs(storage: Storage, run_id: str, inputs: RoiInputs) -> RoiInputs:
    from engine.storage import run_key
    from engine.utils.time import utc_now

    stamped = inputs if inputs.entered_at is not None else inputs.model_copy(update={"entered_at": utc_now()})
    storage.write_model(run_key(run_id, ROI_INPUTS_FILENAME), stamped)
    return stamped


def load_roi_inputs(storage: Storage, run_id: str) -> RoiInputs | None:
    from engine.storage import StorageError, run_key

    key = run_key(run_id, ROI_INPUTS_FILENAME)
    try:
        return storage.read_model(key, RoiInputs) if storage.exists(key) else None
    except StorageError:
        return None


# ---------------------------------------------------------------------------
# The computation: arithmetic on measured counts and the client's inputs, nothing else
# ---------------------------------------------------------------------------
def _read(storage: Storage, key: str, model: type[BaseModel]) -> BaseModel | None:
    from engine.storage import StorageError

    try:
        return storage.read_model(key, model) if storage.exists(key) else None
    except (StorageError, ValueError):
        return None


def compute_roi(
    storage: Storage,
    run_id: str,
    *,
    inputs: RoiInputs | None = None,
    client_store: ClientStore | None = None,
    root: Path | None = None,
) -> RoiView:
    """The value view of scoring run `run_id` from whichever measurement exists."""
    from engine.config import load_use_case
    from engine.contracts import RunRecord
    from engine.runs import RUN_FILENAME
    from engine.scheduling.outcomes import INCREMENTALITY_INPUT_FILENAME, IncrementalityInput
    from engine.storage import run_key
    from engine.uplift.contracts import INCREMENTALITY_FILENAME, IncrementalityReport, IncrementalityStatus
    from engine.uplift.incrementality import newcombe_interval

    record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
    chosen = inputs if inputs is not None else load_roi_inputs(storage, run_id)
    report = _read(storage, run_key(run_id, INCREMENTALITY_FILENAME), IncrementalityReport)
    ingested = _read(storage, run_key(run_id, INCREMENTALITY_INPUT_FILENAME), IncrementalityInput)

    base: _Common = {"run_id": run_id, "use_case_id": record.use_case_id, "inputs": chosen}
    # A measurement wins over one still waiting: an immature report stays on disk after outcomes were
    # ingested for the same run, and the ingested counts are then the newer, usable measurement.
    mature = isinstance(report, IncrementalityReport) and (
        report.status is IncrementalityStatus.MATURE and report.incremental_conversions is not None
    )
    if isinstance(report, IncrementalityReport) and mature:
        incremental = report.incremental_conversions
        assert incremental is not None
        return _priced(
            base,
            source="incrementality_report",
            causal=report.causal,
            outcome_name=report.outcome_column,
            treated=(report.treated_rows, report.treated_conversions, report.treated_rate),
            control=(report.control_rows, report.control_conversions, report.control_rate),
            incremental=Money(value=incremental.value, low=incremental.ci_low, high=incremental.ci_high),
            confidence_level=incremental.confidence_level,
            inputs=chosen,
            root=root,
        )
    if isinstance(ingested, IncrementalityInput) and ingested.outcome_kind == "binary":
        treated, control = ingested.treated, ingested.control
        if treated.rows and control.rows and treated.positives is not None and control.positives is not None:
            difference, low, high = newcombe_interval(
                treated.positives, treated.rows, control.positives, control.rows
            )
            return _priced(
                base,
                source="outcome_ingestion",
                causal=True,
                outcome_name=ingested.outcome_name,
                treated=(treated.rows, treated.positives, treated.outcome_rate),
                control=(control.rows, control.positives, control.outcome_rate),
                incremental=Money(
                    value=difference * treated.rows, low=low * treated.rows, high=high * treated.rows
                ),
                confidence_level=0.95,
                inputs=chosen,
                root=root,
            )
    if isinstance(report, IncrementalityReport):
        available = report.results_available_on
        return RoiView(
            **base,
            status="not_mature",
            source="incrementality_report",
            results_available_on=available,
            causal=report.causal,
            outcome_name=report.outcome_column,
            summary=(
                f"The campaign's outcome period has not finished; results will be ready on {available}."
                if available is not None
                else "The campaign's outcomes cannot be measured yet."
            ),
        )

    matures: date | None = None
    try:
        from engine.scheduling.outcomes import outcome_window

        config = load_use_case(record.use_case_id, root)
        matures = outcome_window(
            record, storage=storage, config=config, client_store=client_store
        ).matures_at.date()
    except Exception:  # a run whose window cannot be known shows no date rather than a guessed one
        matures = None
    return RoiView(
        **base,
        status="not_measured",
        source=None,
        results_available_on=matures,
        causal=False,
        outcome_name="",
        summary=(
            "No outcomes have been recorded for this campaign yet."
            + (f" They can be measured from {matures}." if matures is not None else "")
        ),
    )


def _priced(
    base: _Common,
    *,
    source: Literal["incrementality_report", "outcome_ingestion"],
    causal: bool,
    outcome_name: str,
    treated: tuple[int, int, float | None],
    control: tuple[int, int, float | None],
    incremental: Money,
    confidence_level: float,
    inputs: RoiInputs | None,
    root: Path | None = None,
) -> RoiView:
    gross = net = roi = None
    contact_total = offer_total = None
    chosen = inputs.outcome_is_good if inputs is not None else None
    good = (
        chosen if chosen is not None else outcome_is_good_by_default(base["use_case_id"], outcome_name, root)
    )
    benefit = (
        incremental
        if good
        else Money(
            value=-incremental.value,
            low=None if incremental.high is None else -incremental.high,
            high=None if incremental.low is None else -incremental.low,
        )
    )
    if inputs is not None:
        per = inputs.value_per_outcome

        def scaled(value: float | None) -> float | None:
            return None if value is None else value * per

        gross = Money(value=benefit.value * per, low=scaled(benefit.low), high=scaled(benefit.high))
        contact_total = treated[0] * inputs.contact_cost
        takers = treated[1] if good else treated[0] - treated[1]
        offer_total = takers * inputs.offer_cost
        spent = contact_total + offer_total

        def minus(value: float | None) -> float | None:
            return None if value is None else value - spent

        net = Money(value=gross.value - spent, low=minus(gross.low), high=minus(gross.high))
        if spent > 0:

            def per_rupee(value: float | None) -> float | None:
                return None if value is None else value / spent

            roi = Money(value=net.value / spent, low=per_rupee(net.low), high=per_rupee(net.high))
    low, high = benefit.low, benefit.high
    if low is not None and high is not None and low > 0:
        verdict = "The campaign worked: the whole range is above zero."
    elif low is not None and high is not None and high < 0:
        verdict = "The campaign did harm: the whole range is below zero."
    else:
        verdict = "The range includes zero, so this campaign's effect cannot yet be told apart from chance."
    return RoiView(
        **base,
        status="measured",
        outcome_is_good=good,
        source=source,
        results_available_on=None,
        causal=causal,
        outcome_name=outcome_name,
        treated_rows=treated[0],
        treated_outcomes=treated[1],
        treated_rate=treated[2],
        control_rows=control[0],
        control_outcomes=control[1],
        control_rate=control[2],
        confidence_level=confidence_level,
        incremental=incremental,
        benefit=benefit,
        benefit_label=(
            "Extra customers because of the campaign" if good else "Customers kept by the campaign"
        ),
        gross_value=gross,
        contact_cost_total=contact_total,
        offer_cost_total=offer_total,
        net_value=net,
        roi=roi,
        summary=verdict,
    )


# ---------------------------------------------------------------------------
# Formatting and the page
# ---------------------------------------------------------------------------
def format_inr(amount: float) -> str:
    """₹ with Indian digit grouping (₹12,34,567), lakh and crore shown in words beside large sums."""
    negative = amount < 0
    whole = round(abs(amount))
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    text = f"{'-' if negative else ''}₹{digits}"
    if whole >= 10_000_000:
        text += f" ({abs(amount) / 10_000_000:.2f} crore)"
    elif whole >= 100_000:
        text += f" ({abs(amount) / 100_000:.2f} lakh)"
    return text


def _range(money: Money | None, formatter: Callable[[float], str] = format_inr) -> str:
    if money is None:
        return "not available"
    if money.low is None or money.high is None:
        return f"{formatter(money.value)} (range not available)"
    return f"{formatter(money.low)} to {formatter(money.high)} (most likely {formatter(money.value)})"


def _count(value: float) -> str:
    return f"{value:,.0f}"


def _times(value: float) -> str:
    return f"{value:.1f}x"


def roi_document(
    view: RoiView,
    *,
    client_name: str = "",
    campaign_title: str = "",
    simulated_note: str = "",
    now: datetime | None = None,
) -> ReportDocument:
    """The one-page value view of a campaign."""
    from engine.utils.time import utc_now

    blocks: list[AnyBlock] = []
    level = f"{view.confidence_level:.0%}"
    if view.status != "measured" or view.benefit is None:
        blocks.append(
            Verdict(
                state="info",
                title=(
                    "Results are not ready yet"
                    if view.results_available_on is not None
                    else "No results recorded yet"
                ),
                text=view.summary,
            )
        )
    else:
        state: VerdictState = "ready"
        low = view.benefit.low
        if low is None or low <= 0:
            state = "warnings"
        if view.net_value is not None and view.net_value.high is not None and view.net_value.high < 0:
            state = "not_ready"
        net_text = (
            f" Net value: {_range(view.net_value)}."
            if view.net_value is not None
            else " Enter your values below to see it in rupees."
        )
        blocks.append(
            Verdict(
                state=state,
                title=f"{view.benefit_label}: {_range(view.benefit, _count)}",
                text=view.summary + net_text,
            )
        )
        blocks += [
            Heading(text="What was measured"),
            Table(
                columns=("Group", "Customers", "With the outcome", "Rate"),
                rows=(
                    (
                        "Contacted",
                        f"{view.treated_rows:,}",
                        f"{view.treated_outcomes:,}",
                        f"{view.treated_rate:.1%}" if view.treated_rate is not None else "",
                    ),
                    (
                        "Control group (not contacted)",
                        f"{view.control_rows:,}",
                        f"{view.control_outcomes:,}",
                        f"{view.control_rate:.1%}" if view.control_rate is not None else "",
                    ),
                ),
                caption=(
                    "The control group was chosen at random and not contacted, so the difference between the "
                    "two rates is what the campaign caused."
                    if view.causal
                    else "The groups were not chosen at random, so the difference is descriptive only."
                ),
            ),
            KeyValues(
                rows=(
                    (
                        view.benefit_label,
                        f"{_range(view.benefit, _count)}, {level} confidence interval",
                    ),
                )
            ),
        ]
        blocks.append(Heading(text="Value in rupees"))
        if view.inputs is None:
            blocks.append(
                Callout(
                    title="No values entered yet",
                    text="Enter what one extra customer is worth to you, what an offer costs and what a contact "
                    "costs. The value is then shown as a range.",
                    tone="info",
                )
            )
        else:
            inputs = view.inputs
            rows: list[tuple[str, str]] = [
                ("Value of those customers", _range(view.gross_value)),
                ("Cost of contacts", format_inr(view.contact_cost_total or 0.0)),
                ("Cost of offers taken", format_inr(view.offer_cost_total or 0.0)),
                ("Net value", _range(view.net_value)),
            ]
            if view.roi is not None:
                rows.append(("Net value per rupee spent", _range(view.roi, _times)))
            blocks.append(KeyValues(rows=tuple(rows)))
            blocks += [
                Heading(text="Your inputs", level=3),
                KeyValues(
                    rows=(
                        ("Value of one extra customer", format_inr(inputs.value_per_outcome)),
                        (
                            "The outcome measured is",
                            (
                                "one we want more of"
                                if view.outcome_is_good
                                else "one we want less of (for example a customer leaving)"
                            ),
                        ),
                        ("What that value is", inputs.value_basis or "not stated"),
                        ("Offer cost, when taken", format_inr(inputs.offer_cost)),
                        ("Cost per contact", format_inr(inputs.contact_cost)),
                        ("Entered by", inputs.entered_by or "not stated"),
                        (
                            "Entered on",
                            inputs.entered_at.strftime("%d %b %Y") if inputs.entered_at else "not recorded",
                        ),
                    )
                ),
                Callout(
                    title="The value depends on these inputs",
                    text="The number of customers is measured. The rupee amounts are that number multiplied by "
                    "the values above, which are your estimates; change them and the value changes. Offer cost is "
                    + (
                        "counted for every contacted customer who had the outcome."
                        if view.outcome_is_good
                        else "counted for every contacted customer who did not have the outcome."
                    ),
                    tone="warning",
                ),
            ]
            if inputs.note:
                blocks.append(Paragraph(text=f"Note: {inputs.note}", muted=True))
    if simulated_note:
        blocks.append(Callout(title="Demo data", text=simulated_note, tone="warning"))
    blocks.append(
        Bullets(
            items=(
                f"A {level} confidence interval is the range the true number very probably lies in; "
                "a range that does not include zero means the effect is real, not luck.",
                "The control group is customers chosen at random and deliberately not contacted.",
            )
        )
    )
    return ReportDocument(
        kind="roi",
        title="Campaign value",
        subtitle=campaign_title,
        client_name=client_name,
        generated_at=now or utc_now(),
        facts=(
            ("Client", client_name or "not recorded"),
            ("Campaign run", view.run_id),
            ("Outcome", view.outcome_name or "not recorded yet"),
            (
                "Measured from",
                {"incrementality_report": "campaign results", "outcome_ingestion": "outcomes uploaded"}.get(
                    view.source or "", "not measured yet"
                ),
            ),
        ),
        blocks=tuple(blocks),
        footer="Measured numbers come from the platform's campaign records; rupee values use the inputs shown.",
    )
