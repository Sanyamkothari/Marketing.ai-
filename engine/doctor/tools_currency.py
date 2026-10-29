"""The currency tools' registry entries (Plan O M242; the design §5.5 G #64 and §8.2), standalone.

Shaped like M241's `engine.doctor.tools_locale` (and so like M232's `DataTool` entry): name, kind, mode, input and
output models (`extra="forbid"`), exposure, role, the function wrapped, deterministic, a `run(args, ctx)` handler,
and `register(registry)` for the integrator.

| Tool | Kind | Exposure | Role | Who may call it | Wraps |
|---|---|---|---|---|---|
| `check_currency` | READ | session-only | viewer | rules, AI | `engine.doctor.currency.check_currency` |
| `report_in_currency` | PROPOSE | session-only | analyst | rules, the person's answer | `engine.doctor.currency.report_in` |
| `convert_currency` | PROPOSE (APPROVE) | session-only | analyst | **the person's answer only** | `engine.doctor.currency.propose_conversion` |

All three are rules only and deterministic; none needs an AI service and **none fetches a rate**. `check_currency`
returns counts only. `convert_currency`'s rate, date and source are the person's words: the spec says
`person_only=True`, so M232's registry never lists it to a model and M233's policy rule set refuses an AI-sourced
`convert_currency` proposal (default-deny, DEC-1812 (k)). Its proposal is APPROVE-class (Analyst, exact sentence):
`engine.doctor.currency.approve_conversion` records the approval; no tool applies anything.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from engine.access.roles import Role
from engine.doctor.currency import (
    CurrencyError,
    check_currency,
    propose_conversion,
    report_in,
)
from engine.doctor.tool_spec import DoctorToolSpec

if TYPE_CHECKING:
    import pandas as pd

    from engine.doctor.locale import LocaleProfile

__all__ = [
    "CURRENCY_TOOLS",
    "CheckCurrencyInput",
    "CheckCurrencyOutput",
    "ConvertCurrencyInput",
    "CurrencyProposalOutput",
    "CurrencyToolContext",
    "CurrencyToolSpec",
    "ReportInCurrencyInput",
    "register",
]

In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CurrencyToolContext(Protocol):
    """The smallest part of M232's `DataToolContext` these tools read: the file, its ID and outcome, the profile, and
    the workspace currency (`engine.doctor.currency.workspace_currency_for`)."""

    @property
    def frame(self) -> pd.DataFrame: ...

    @property
    def primary_key(self) -> str | None: ...

    @property
    def target(self) -> str | None: ...

    @property
    def locale_profile(self) -> LocaleProfile | None: ...

    @property
    def workspace_currency(self) -> str: ...


class CheckCurrencyInput(_Strict):
    columns: list[str] | None = Field(default=None, max_length=200, description="Every column when left out.")


class CheckCurrencyOutput(_Strict):
    workspace_currency: str
    units: dict[str, str] = Field(
        default_factory=dict, description="Money columns whose one currency is known."
    )
    findings: list[dict[str, Any]] = Field(
        default_factory=list,
        description="CURRENCY_FOREIGN (ask), CURRENCY_MIXED (stop) and CURRENCY_UNSTATED (ask), with the question.",
    )
    stopped: bool = False


class ReportInCurrencyInput(_Strict):
    column: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class ConvertCurrencyInput(_Strict):
    """The person's own rate: never filled in by a model, never looked up."""

    column: str
    from_currency: str = Field(pattern=r"^[A-Z]{3}$")
    to_currency: str = Field(pattern=r"^[A-Z]{3}$")
    rate: float = Field(gt=0)
    rate_date: date
    source: str = Field(min_length=1, max_length=200)
    rows: Literal["all", "marked"] | dict[str, Any] = "all"


class CurrencyProposalOutput(_Strict):
    steps: list[dict[str, Any]] = Field(
        default_factory=list, description="Pending steps; nothing is applied."
    )
    sentence: str | None = Field(
        default=None, description="The exact sentence an Analyst approves (a conversion)."
    )
    refused: dict[str, str] | None = Field(
        default=None, description="`{code, message}` when it cannot be proposed."
    )


def _confirmed_units(profile: LocaleProfile | None) -> dict[str, str]:
    if profile is None or profile.money_units.source != "confirmed":
        return {}
    return dict(profile.money_units.value)


def _text_column(ctx: CurrencyToolContext, column: str) -> bool:
    import pandas as pd

    series = ctx.frame[column]
    return bool(pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series))


def _run_check(args: CheckCurrencyInput, ctx: CurrencyToolContext) -> CheckCurrencyOutput:
    found = check_currency(
        ctx.frame,
        workspace_currency=ctx.workspace_currency,
        confirmed_units=_confirmed_units(ctx.locale_profile),
        columns=args.columns,
        primary_key=ctx.primary_key,
        target=ctx.target,
    )
    return CheckCurrencyOutput(
        workspace_currency=found.workspace_currency,
        units=found.units,
        findings=[f.model_dump(mode="json") for f in found.findings],
        stopped=found.stopped,
    )


def _finding(ctx: CurrencyToolContext, column: str) -> Any:
    found = check_currency(
        ctx.frame,
        workspace_currency=ctx.workspace_currency,
        confirmed_units=_confirmed_units(ctx.locale_profile),
        columns=[column],
        primary_key=ctx.primary_key,
        target=ctx.target,
    )
    return next((f for f in found.findings if f.column == column), None)


def _run_report_in(args: ReportInCurrencyInput, ctx: CurrencyToolContext) -> CurrencyProposalOutput:
    finding = _finding(ctx, args.column)
    if finding is None:
        return CurrencyProposalOutput(
            refused={"code": "CURRENCY_FOREIGN", "message": f"'{args.column}' needs no currency answer."}
        )
    try:
        steps = report_in(finding, args.currency, text_column=_text_column(ctx, args.column))
    except CurrencyError as exc:
        return CurrencyProposalOutput(refused={"code": exc.code, "message": exc.message})
    return CurrencyProposalOutput(steps=[s.model_dump(mode="json") for s in steps])


def _run_convert(args: ConvertCurrencyInput, ctx: CurrencyToolContext) -> CurrencyProposalOutput:
    try:
        proposal = propose_conversion(
            args.column,
            source_code=args.from_currency,
            target=args.to_currency,
            rate=args.rate,
            rate_date=args.rate_date,
            source=args.source,
            rows=args.rows,
            text_column=_text_column(ctx, args.column),
        )
    except CurrencyError as exc:
        return CurrencyProposalOutput(refused={"code": exc.code, "message": exc.message})
    return CurrencyProposalOutput(
        steps=[s.model_dump(mode="json") for s in proposal.steps], sentence=proposal.sentence
    )


ToolKindName = Literal["read", "propose", "ask"]
Exposure = Literal["session-only", "aggregate"]


CurrencyToolSpec = DoctorToolSpec


CURRENCY_TOOLS: Final[tuple[CurrencyToolSpec[Any, Any], ...]] = (
    CurrencyToolSpec(
        name="check_currency",
        kind="read",
        mode="rules",
        description="Each money column's currency, and the one question a foreign, mixed or unstated one needs.",
        input_model=CheckCurrencyInput,
        output_model=CheckCurrencyOutput,
        exposure="session-only",
        role=Role.VIEWER,
        wraps="engine.doctor.currency.check_currency",
        deterministic=True,
        run=_run_check,
    ),
    CurrencyToolSpec(
        name="report_in_currency",
        kind="propose",
        mode="rules",
        description="Keep a money column in the currency the file gives, recorded as its unit; nothing is converted.",
        input_model=ReportInCurrencyInput,
        output_model=CurrencyProposalOutput,
        exposure="session-only",
        role=Role.ANALYST,
        wraps="engine.doctor.currency.report_in",
        deterministic=True,
        run=_run_report_in,
        proposes=("parse_number",),
    ),
    CurrencyToolSpec(
        name="convert_currency",
        kind="propose",
        mode="rules",
        description="Convert a money column at the rate, date and source a person gave; never a fetched rate.",
        input_model=ConvertCurrencyInput,
        output_model=CurrencyProposalOutput,
        exposure="session-only",
        role=Role.ANALYST,
        wraps="engine.doctor.currency.propose_conversion",
        deterministic=True,
        run=_run_convert,
        proposes=("convert_currency", "parse_number"),
        approve_class=True,
        person_only=True,
    ),
)


class CurrencyRegistry(Protocol):
    """What `register` needs of M232's registry (the integrator adapts the spec to its `DataTool`)."""

    def register_spec(self, spec: CurrencyToolSpec[Any, Any]) -> None: ...


def register(registry: CurrencyRegistry) -> None:
    """Add the three currency tools to `registry`, in catalogue order."""
    for spec in CURRENCY_TOOLS:
        registry.register_spec(spec)
