"""What a run will cost, said before it starts, and the cap that stops it (Plan J M108, DEC-1318).

Until now cost was written down only after a run (`engine.aws.prices.cost_estimate`, from the seconds
AWS billed). We run on the client's cloud bill, so a surprise bill is the way to lose their trust:
this module says the number first, beside the Run button, and lets an Admin put a ceiling on any one
run.

**The estimate is a ceiling at a list price, in US dollars, and says so.** A cloud job is billed for
the time it takes, which is not known until it ends, but the most it can take is: the deployment's
time limit for a job (`sagemaker_max_runtime_seconds`). So the figure is
`time limit x instances x the AWS published hourly rate`, the same arithmetic as the after-the-run
figure (`engine.aws.prices.price_compute_time` serves both, and the running cost below). A list price is
not a bill: it ignores discounts, free allowances and tax, and `basis` says so in every response.

**AI text is shown, not counted.** A run started by `POST /runs`, a schedule or the uplift route never
calls the AI text service (the predictive stages do not read the `generative` block, DEC-200; text jobs
are their own requests under their own budget, `generative.budget.max_cost_usd_per_run`). So that
ceiling is listed as an informational line (`in_total=False`) when the use case has a billed text
service, and is neither added to the estimate nor counted against the cap: the cap stops only what it
can see and stop, the compute time of a cloud job (DEC-1318 (j)).

**Nothing is made up.** No price list, an instance type the list does not carry, no time limit, or a
text model without a price: the amount is `None`, with a sentence saying which, and the total is
`None` too rather than the sum of what happens to be known (`known_usd` carries that). A deployment
that runs on its own machine is not billed by anyone, so it has no estimate (also `None`, also with
the reason), never a zero. Indian rupees appear only beside an Admin-saved exchange rate, its source
and its date (`FxRate`); the product never guesses a rate.

**The cap** is `governance.max_run_cost_usd` (null = none, today's behaviour). A run whose estimate is
above it - or whose estimate cannot be worked out on a deployment that bills - needs the person to
confirm (`POST /runs` answers `RUN_COST_NEEDS_CONFIRMATION` until the request carries
`confirm_cost: true`), and a run, confirmed or not, is stopped through `cancel_run` when the cost it has
run up so far (`running_cost_usd`: the time since it started x instances x the same rate) passes the
cap. Two things watch for that, whichever sees it first: a small thread started with the run
(`start_cost_watch`) and the Running screen's own poll (`GET /runs/{id}` calls `enforce_cost_cap`). A
run that cannot be priced cannot be stopped for its cost, and the confirmation says so.

No boto3, no call to any pricing API: the price list is a file (`configs/aws_prices.yaml`).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field

from engine.aws.prices import (
    PRICES_FILENAME,
    PROCESSING_COMPONENT,
    TRAINING_COMPONENT,
    PricedTime,
    PriceTable,
    load_price_table,
    price_compute_time,
)
from engine.config import (
    LlmBackend,
    ResolvedConfig,
    RunMode,
    StrictBase,
    UseCaseConfig,
)
from engine.contracts import RunError, RunRecord, RunState
from engine.generative.budget import PriceTable as LlmPriceTable
from engine.runs import RUN_CONFIG_FILENAME, RUN_FILENAME, cancel_run
from engine.storage import Storage, StorageError, run_key
from engine.utils.logging import get_logger, log_failure
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from engine.jobs import JobRunner
    from engine.settings import Settings

__all__ = [
    "COST_WATCH_INTERVAL_S",
    "RUN_COST_CAP_REACHED",
    "RUN_COST_CODES",
    "RUN_COST_NEEDS_CONFIRMATION",
    "CostLine",
    "CostWatch",
    "FxRate",
    "InrAmount",
    "RunCostEstimate",
    "cached_price_table",
    "confirmation_message",
    "enforce_cost_cap",
    "estimate_run_cost",
    "format_inr",
    "format_usd",
    "inr_amount",
    "running_cost_usd",
    "start_cost_watch",
]

_LOGGER = get_logger(__name__)

RUN_COST_NEEDS_CONFIRMATION: Final[str] = "RUN_COST_NEEDS_CONFIRMATION"
"""`POST /runs` (409): the run is over the cost cap, or cannot be checked against it, and was not confirmed."""
RUN_COST_CAP_REACHED: Final[str] = "RUN_COST_CAP_REACHED"
"""`RunRecord.error.code` of a run that was stopped because its running cost passed the cap."""
RUN_COST_CODES: Final[frozenset[str]] = frozenset({RUN_COST_NEEDS_CONFIRMATION, RUN_COST_CAP_REACHED})
"""M108's user-facing codes. `engine.decide.codes.PLAN_J_CODES` is to import this set (one definition).

That line and the two `configs/pilot/help.yaml` entries belong to the integrator (DEC-1300 (d)); the
text is given in M108's result.
"""

COST_WATCH_INTERVAL_S: Final[float] = 30.0
"""How often a started run's cost is looked at. Read at each wait, so a test can change it."""

_DEFAULT_WATCH_LIFETIME_S: Final[float] = 7 * 24 * 3600.0
"""How long a watcher outlives a run that never ends when the deployment sets no time limit."""

BASIS: Final[str] = (
    "An estimate at AWS's published list price, in US dollars: the most the cloud could charge for this "
    "run if it used its whole time limit, on the machine it runs on. It is not a bill: it leaves out "
    "discounts, free allowances and tax."
)
NO_PRICE_LIST: Final[str] = (
    "No AWS price list is installed on this deployment, so the cost cannot be estimated. "
    "An administrator can refresh the price list."
)
NO_MACHINE: Final[str] = (
    "The cloud machine this job runs on is not named in the deployment's settings, so it cannot be priced."
)
NO_PRICE: Final[str] = (
    "The installed AWS price list has no price for {instance_type} in {region}, so the cost cannot be estimated."
)
NO_TIME_LIMIT: Final[str] = (
    "No time limit is set for cloud jobs, so the most a run could cost is not known. "
    "An administrator can set one."
)
OWN_MACHINE: Final[str] = (
    "This deployment runs on its own machine, so the cloud does not bill for the run and "
    "there is nothing to estimate."
)
NO_AI_PRICE: Final[str] = (
    "The AI text price list has no price for {models}, so the most the AI text may cost cannot be stated."
)
NO_EXCHANGE_RATE: Final[str] = (
    "No exchange rate has been set by an administrator, so the amount is shown in US dollars only."
)
NOTHING_BILLED: Final[str] = "Nothing in this run is billed by the cloud, so there is no cost to estimate."

LineKind = Literal["training", "scoring", "ai_text"]


# ---------------------------------------------------------------------------
# Words and numbers a person reads
# ---------------------------------------------------------------------------
def format_usd(amount: float) -> str:
    """`USD 12.34`; a positive amount under a cent says so rather than reading `USD 0.00`."""
    if 0.0 < amount < 0.01:
        return "under USD 0.01"
    return f"USD {amount:,.2f}"


def format_inr(amount: float) -> str:
    return f"INR {amount:,.2f}"


def _lower_first(sentence: str | None) -> str:
    """The sentence without its full stop and with only its first letter lower-cased ("AWS" stays "AWS")."""
    text = (sentence or "").rstrip(".")
    return text[:1].lower() + text[1:]


def _duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour" + ("" if hours == 1 else "s")
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    return f"{seconds} seconds"


# ---------------------------------------------------------------------------
# The models a screen and a test read
# ---------------------------------------------------------------------------
class FxRate(StrictBase):
    """The exchange rate an Admin saved, with where it came from. Rupees exist only beside one."""

    inr_per_usd: float = Field(gt=0.0, allow_inf_nan=False, description="Indian rupees for one US dollar.")
    source: str = Field(
        min_length=1, max_length=200, description="Where the rate was read, in the Admin's words."
    )
    as_of: date = Field(description="The day the rate was read.")


class InrAmount(StrictBase):
    """A rupee amount, carrying the rate, source and date it was worked out with."""

    amount: float = Field(description="US dollars multiplied by the saved rate.")
    inr_per_usd: float = Field(description="The saved exchange rate.")
    source: str = Field(description="Where the rate was read.")
    as_of: date = Field(description="The day the rate was read.")


def inr_amount(usd: float | None, fx: FxRate | None) -> InrAmount | None:
    """Rupees for `usd` at `fx`, or `None` when either is missing - never a guess."""
    if usd is None or fx is None:
        return None
    return InrAmount(
        amount=round(usd * fx.inr_per_usd, 2), inr_per_usd=fx.inr_per_usd, source=fx.source, as_of=fx.as_of
    )


class CostLine(StrictBase):
    """One part of the estimate. `usd` is `None` with a `reason` when it cannot be stated."""

    kind: LineKind = Field(description="training, scoring or ai_text.")
    label: str = Field(description="What this part is, in plain words.")
    usd: float | None = Field(description="The most this part could cost at list price; null when unknown.")
    detail: str = Field(description="How the figure was worked out; empty when it was not.")
    reason: str | None = Field(default=None, description="Why `usd` is null; null when it is a number.")
    in_total: bool = Field(description="Whether this part is counted in the estimate and the cap.")


class RunCostEstimate(StrictBase):
    """`GET /use-cases/{id}/cost-estimate`: the list-price ceiling of one run, and the cap on it."""

    use_case_id: str
    mode: RunMode
    backend: Literal["sagemaker", "local"] = Field(
        description="Whether the cloud bills this deployment's jobs."
    )
    currency: Literal["USD"] = "USD"
    estimated_usd: float | None = Field(
        description="The most one run could cost at list price; null (with `reason`) when any counted part is unknown."
    )
    known_usd: float | None = Field(
        description="What the known counted parts add up to; null when none is known. Never a total."
    )
    reason: str | None = Field(description="Why `estimated_usd` is null; null when it is a number.")
    basis: str = Field(description="What kind of figure this is, in plain words.")
    lines: tuple[CostLine, ...]
    inr: InrAmount | None = Field(description="The estimate in rupees; null without a saved exchange rate.")
    inr_reason: str | None = Field(description="Why `inr` is null when a dollar figure exists.")
    cap_usd: float | None = Field(description="governance.max_run_cost_usd; null when no cap is set.")
    over_cap: bool | None = Field(
        description="Whether the estimate is above the cap; null when unknown or no cap."
    )
    needs_confirmation: bool = Field(description="Whether starting this run needs the person's confirmation.")
    confirmation_reason: str | None = Field(description="Why confirmation is needed, in plain words.")


# ---------------------------------------------------------------------------
# The price list, read once per change of the file
# ---------------------------------------------------------------------------
_TABLE_CACHE: dict[Path, tuple[int, PriceTable | None]] = {}
_TABLE_LOCK: Final[threading.Lock] = threading.Lock()


def cached_price_table(root: Path) -> PriceTable | None:
    """`load_price_table` for `root`, parsed again only when the file changes (a poll must be cheap)."""
    path = root / PRICES_FILENAME
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    with _TABLE_LOCK:
        cached = _TABLE_CACHE.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    loaded = load_price_table(path)
    with _TABLE_LOCK:
        _TABLE_CACHE[path] = (stamp, loaded)
    return loaded


# ---------------------------------------------------------------------------
# The estimate
# ---------------------------------------------------------------------------
def _machine(settings: Settings, mode: RunMode) -> tuple[str, str | None]:
    """The price-list component a run of `mode` is billed under and the instance type it runs on."""
    if mode is RunMode.TRAIN:
        return TRAINING_COMPONENT, settings.sagemaker_instance_type
    return (
        PROCESSING_COMPONENT,
        settings.sagemaker_processing_instance_type or settings.sagemaker_instance_type,
    )


def _missing_reason(priced: PricedTime, instance_type: str | None, region: str | None) -> str:
    if priced.missing == "table":
        return NO_PRICE_LIST
    if priced.missing == "instance":
        return NO_MACHINE
    return NO_PRICE.format(instance_type=instance_type, region=region)


def _compute_line(
    kind: Literal["training", "scoring"],
    mode: RunMode,
    settings: Settings,
    table: PriceTable | None,
    *,
    in_total: bool,
) -> CostLine:
    label = "Training the model" if kind == "training" else "Scoring new data"
    if kind == "scoring" and mode is RunMode.TRAIN:
        label = "Scoring new data with this model later (a separate run)"
    component, instance_type = _machine(settings, RunMode.TRAIN if kind == "training" else RunMode.SCORE)
    limit = settings.sagemaker_max_runtime_seconds
    if limit is None:
        return CostLine(kind=kind, label=label, usd=None, detail="", reason=NO_TIME_LIMIT, in_total=in_total)
    priced = price_compute_time(
        component=component,
        instance_type=instance_type,
        instance_count=settings.sagemaker_instance_count,
        region=settings.aws_region,
        seconds=float(limit),
        table=table,
    )
    if priced.usd is None or priced.rate is None:
        reason = _missing_reason(priced, instance_type, settings.aws_region)
        return CostLine(kind=kind, label=label, usd=None, detail="", reason=reason, in_total=in_total)
    detail = (
        f"Up to {_duration(limit)} on {settings.sagemaker_instance_count} x {priced.rate.instance_type} "
        f"at USD {priced.rate.usd_per_hour:g} per hour (AWS list price, published {priced.rate.publication_date[:10]})."
    )
    return CostLine(kind=kind, label=label, usd=round(priced.usd, 4), detail=detail, in_total=in_total)


def _text_line(config: UseCaseConfig, prices: LlmPriceTable) -> CostLine | None:
    """The AI text ceiling as an informational line, when the use case calls a billed text service.

    Never counted: a run does not call the text service (see the module docstring), so adding the ceiling
    would charge a run for money it cannot spend, and the cap could not stop it.
    """
    generative = config.generative
    if not generative.enabled or generative.llm.backend is not LlmBackend.BEDROCK:
        return None
    label = "AI-written text is started separately and is not part of this run"
    models = [
        model
        for model in (
            generative.llm.generation_model_id,
            generative.llm.judge_model_id,
            generative.llm.embedding_model_id,
        )
        if model
    ]
    unpriced = [model for model in models if prices.get(model) is None]
    if unpriced:
        reason = NO_AI_PRICE.format(models=", ".join(unpriced))
        return CostLine(kind="ai_text", label=label, usd=None, detail="", reason=reason, in_total=False)
    ceiling = generative.budget.max_cost_usd_per_run
    detail = (
        f"Each AI text job may cost up to {format_usd(ceiling)}, the ceiling set for it. "
        "It is not added to this run's estimate or counted against its limit."
    )
    return CostLine(kind="ai_text", label=label, usd=round(ceiling, 4), detail=detail, in_total=False)


def estimate_run_cost(
    config: UseCaseConfig,
    mode: RunMode,
    *,
    settings: Settings,
    table: PriceTable | None,
    llm_prices: LlmPriceTable,
    fx: FxRate | None,
) -> RunCostEstimate:
    """The most one run of `config` in `mode` could cost at list price, and what the cap says about it."""
    billed = settings.job_backend == "sagemaker"
    lines: list[CostLine] = []
    own: Literal["training", "scoring"] = "training" if mode is RunMode.TRAIN else "scoring"
    if billed:
        lines.append(_compute_line(own, mode, settings, table, in_total=True))
        if mode is RunMode.TRAIN:
            lines.append(_compute_line("scoring", mode, settings, table, in_total=False))
    else:
        label = "Training the model" if own == "training" else "Scoring new data"
        lines.append(CostLine(kind=own, label=label, usd=None, detail="", reason=OWN_MACHINE, in_total=False))
    text = _text_line(config, llm_prices)
    if text is not None:
        lines.append(text)
    counted = [line for line in lines if line.in_total]
    priced_lines = [line.usd for line in counted if line.usd is not None]
    known = round(sum(priced_lines), 4) if priced_lines else None
    unknown = [line for line in counted if line.usd is None]
    total = known if counted and not unknown else None
    reason: str | None = None
    if total is None:
        reason = unknown[0].reason if unknown else (lines[0].reason or NOTHING_BILLED)
    inr = inr_amount(total, fx)
    inr_reason = NO_EXCHANGE_RATE if total is not None and fx is None else None

    cap = config.governance.max_run_cost_usd
    over_cap: bool | None = None
    needs = False
    why: str | None = None
    if cap is not None and counted:
        over = known is not None and known > cap
        if over:
            over_cap, needs = True, True
            why = (
                f"This run could cost up to {format_usd(known or 0.0)} at list price, more than the "
                f"{format_usd(cap)} limit set for a single run. If you confirm, it starts and is stopped "
                "if its running cost reaches that limit."
            )
        elif total is None:
            needs = True
            why = (
                f"The most this run could cost cannot be worked out ({_lower_first(reason)}), "
                f"so it cannot be checked against the {format_usd(cap)} limit set for a single run. "
                "A run that cannot be priced cannot be stopped for its cost; confirm only if you accept that."
            )
        else:
            over_cap = False
    return RunCostEstimate(
        use_case_id=config.id,
        mode=mode,
        backend="sagemaker" if billed else "local",
        estimated_usd=total,
        known_usd=known,
        reason=reason,
        basis=BASIS,
        lines=tuple(lines),
        inr=inr,
        inr_reason=inr_reason,
        cap_usd=cap,
        over_cap=over_cap,
        needs_confirmation=needs,
        confirmation_reason=why,
    )


def confirmation_message(estimate: RunCostEstimate) -> str:
    """The 409's sentence: what the run could cost and what confirming means."""
    return f"This run was not started. {estimate.confirmation_reason}"


# ---------------------------------------------------------------------------
# The cost so far, and the stop
# ---------------------------------------------------------------------------
def running_cost_usd(
    mode: RunMode, *, elapsed_seconds: float, settings: Settings, table: PriceTable | None
) -> PricedTime:
    """What `elapsed_seconds` of a run of `mode` has cost at list price - the estimate's own arithmetic."""
    if settings.job_backend != "sagemaker":
        return PricedTime(usd=None, missing="local", rate=None)
    component, instance_type = _machine(settings, mode)
    return price_compute_time(
        component=component,
        instance_type=instance_type,
        instance_count=settings.sagemaker_instance_count,
        region=settings.aws_region,
        seconds=max(elapsed_seconds, 0.0),
        table=table,
    )


def _stop_error(spent: float, cap: float) -> RunError:
    return RunError(
        code=RUN_COST_CAP_REACHED,
        message=(
            f"This run was stopped because its running cost reached {format_usd(cap)}, the limit set for a "
            f"single run. So far it has run up about {format_usd(spent)} at list price (an estimate, not a "
            "bill). To run it again, ask an administrator to raise the limit."
        ),
        stage=None,
    )


def enforce_cost_cap(
    storage: Storage,
    jobs: JobRunner,
    run_id: str,
    *,
    settings: Settings,
    table: PriceTable | None,
    now: datetime | None = None,
) -> RunRecord | None:
    """Stop the run if its running cost has passed the cap; the stopped record, or `None` if nothing was done.

    Idempotent and cheap to call on every poll: a finished, unstarted, uncapped or unpriceable run is
    left exactly as it is. The cap is read from the run's own `run_config.json` - the configuration it
    started with - so it cannot be moved while the run goes.
    """
    if settings.job_backend != "sagemaker":
        return None
    try:
        record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
        if record.state not in (RunState.PENDING, RunState.RUNNING) or record.started_at is None:
            return None
        cap = storage.read_model(
            run_key(run_id, RUN_CONFIG_FILENAME), ResolvedConfig
        ).config.governance.max_run_cost_usd
    except (StorageError, ValueError):
        return None
    if cap is None:
        return None
    moment = now or utc_now()
    elapsed = (moment - record.started_at).total_seconds()
    spent = running_cost_usd(record.mode, elapsed_seconds=elapsed, settings=settings, table=table).usd
    if spent is None or spent <= cap:
        return None
    # Read again just before writing: a run that finished while this was priced is not cancelled.
    try:
        latest = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
    except (StorageError, ValueError):
        return None
    if latest.state not in (RunState.PENDING, RunState.RUNNING):
        return None
    if not jobs.cancel(run_id):
        # The stop was not delivered (throttled, a missing permission, an unknown job): the job is still
        # billing, so the record must not say it was stopped. The next look tries again; a job that has
        # already ended reaches its terminal state through reconcile.
        _LOGGER.warning("runs.cost_cap_stop_failed run=%s", run_id)
        return None
    stopped = cancel_run(storage, run_id, now=moment, error=_stop_error(spent, cap))
    _LOGGER.warning("runs.cost_cap_stop run=%s", run_id)
    return stopped


@dataclass(slots=True)
class CostWatch:
    """A running watcher: `stop()` ends it. One per capped run, started with the run."""

    run_id: str
    _stop: threading.Event
    thread: threading.Thread

    def stop(self) -> None:
        self._stop.set()


def start_cost_watch(
    storage: Storage,
    jobs: JobRunner,
    run_id: str,
    *,
    settings: Settings,
    table_for: Callable[[], PriceTable | None],
) -> CostWatch:
    """Look at one run's running cost every `COST_WATCH_INTERVAL_S` until it ends or is stopped.

    A daemon thread that dies with the process; if the process restarts mid-run the Running screen's
    poll still enforces the cap (`GET /runs/{id}`), and SageMaker's own time limit bounds the job
    either way. It ends when the run is no longer pending or running, or after the time limit plus an
    hour.
    """
    stop = threading.Event()
    limit = settings.sagemaker_max_runtime_seconds
    lifetime = (float(limit) if limit else _DEFAULT_WATCH_LIFETIME_S) + 3600.0

    def watch() -> None:
        deadline = time.monotonic() + lifetime
        while not stop.wait(COST_WATCH_INTERVAL_S) and time.monotonic() < deadline:
            try:
                record = storage.read_model(run_key(run_id, RUN_FILENAME), RunRecord)
                if record.state not in (RunState.PENDING, RunState.RUNNING):
                    return
                if enforce_cost_cap(storage, jobs, run_id, settings=settings, table=table_for()) is not None:
                    return
            except Exception as exc:  # a watcher that dies silently is a cap that is not there
                log_failure(_LOGGER, "runs.cost_watch", exc)

    thread = threading.Thread(target=watch, name=f"cost-watch-{run_id}", daemon=True)
    thread.start()
    return CostWatch(run_id=run_id, _stop=stop, thread=thread)
