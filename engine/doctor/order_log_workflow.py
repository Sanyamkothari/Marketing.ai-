"""The Data Doctor's order-log workflow as one pure entry point (v3 phase 0, the part A3 of the AI-first design wraps).

`docs/plans/DATA_DOCTOR_AI_FIRST.md` makes the order-log path one of the AI's workflows, not a rule path inside the
visit: "order log -> one row per customer, returns netted, guests out, outcome ordered again within 90 days". This
module is that workflow without any visit state, plan item or screen. Two functions:

* :func:`propose_order_log` - given the log, the column a person named as the shopper and their answer about the lines
  with no shopper, the outcome proposal (its exact sentence, with the counts) and the build plan (its exact sentence
  and hash). Nothing is built or changed. A person reads both sentences.
* :func:`build_from_order_log` - the same inputs plus the person's two approvals and a port: it re-derives both
  (deterministic), refuses an approval that is not for what it now reads, runs the plan through the builder
  (`drive_builder`) and then the readiness check on the built table. It returns the built dataset's id, the counts and
  the readiness report.

Everything is behind `data_doctor.order_log` (with `unified`): off, both raise `ORDER_LOG_OFF` and nothing else in the
product moves. The customer column and the outcome are chosen by a person, never ticked; the loss guard, the minimum
rows and positives (`min_rows`, `min_positive`) and the leak probe are the engine's own and unchanged: this module
lowers nothing. Who may approve (the Analyst role, `data.define_outcome` and `data.build_dataset`) is the caller's
policy layer; here an approval must name the hash and the exact sentence it reads now.

The caller supplies the whole log (`LoadedFile.file_rows` unset): a sample cannot be rolled up honestly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

import pandas as pd
from pydantic import Field

from engine.config import StrictBase, UseCaseConfig, get_roles, load_use_case
from engine.config import config_root as resolve_root
from engine.doctor.build_driver import (
    BuildApproval,
    BuildDriveError,
    BuilderPort,
    BuildPlan,
    OrderLogInput,
    drive_builder,
    plan_build,
)
from engine.doctor.contracts import Diagnosis, FileDiagnosis
from engine.doctor.diagnose import LoadedFile, diagnose_file
from engine.doctor.order_log import (
    DerivedCustomers,
    OrderLogReading,
    blank_customer_question,
    derive_customer_list,
    derived_entity,
    mixed_orders_question,
    order_log_enabled,
    order_log_reading,
)
from engine.doctor.order_roll_up import find_log_columns
from engine.doctor.outcome_definition import (
    EventOutcomeProposal,
    OutcomeApproval,
    OutcomeError,
    propose_event_outcome,
)
from engine.doctor.outcomes import OutcomeFile, load_outcome_catalogue
from engine.doctor.readiness_contracts import DataReadinessReport

if TYPE_CHECKING:
    from engine.doctor.builder_local import BuiltDataset

__all__ = [
    "GuestsAnswer",
    "OrderLogApprovals",
    "OrderLogBuild",
    "OrderLogCounts",
    "OrderLogPort",
    "OrderLogProposal",
    "OrderLogWorkflowError",
    "build_from_order_log",
    "propose_order_log",
]


class OrderLogWorkflowError(Exception):
    """The workflow cannot go on; `code` is its own (`ORDER_LOG_*`) or the code of the step that refused (an outcome,
    a build or a builder route), kept as that step gave it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class OrderLogPort(BuilderPort, Protocol):
    """A `BuilderPort` that can also hand back the finished build (`engine.doctor.builder_local.LocalBuilderPort`)."""

    def read_dataset(self, dataset_id: str) -> BuiltDataset: ...


class GuestsAnswer(StrictBase):
    """A person's answer to Q2, the lines with no shopper (`order_log.blank_customer_question`).

    `option_id` is `leave_out` or `stop`. When leaving them out is over the loss guard's limit, `finding_key` is the
    key the question's option carried: the yes is tied to that finding with that count and is refused when the count
    has changed (`ORDER_LOG_GUESTS_STALE`)."""

    option_id: Literal["leave_out", "stop"]
    finding_key: str | None = None


class OrderLogApprovals(StrictBase):
    """The person's two yeses: the outcome's exact sentence and the build plan's. Each names the hash and the
    sentence it read; the policy check of who may give them (Analyst) is the caller's."""

    outcome: OutcomeApproval
    build: BuildApproval


class OrderLogCounts(StrictBase):
    """What the log and the built table hold, counted."""

    log: dict[str, int] = Field(
        description="The roll-up's counts (`lines`, `lines_without_shopper`, `orders`, `shoppers`, ...), which add up "
        "to the log's lines."
    )
    rows: int = Field(description="Rows of the built table: one per audience shopper per month-end.")
    yes: int
    no: int
    snapshot_dates: tuple[date, ...]


class OrderLogBuild(StrictBase):
    """A built dataset and the verdict on it."""

    dataset_id: str
    spec_id: str = Field(
        description="The saved recipe; `port.load_record(spec_id)` is what next month's replay reads."
    )
    plan_hash: str
    outcome_hash: str
    counts: OrderLogCounts
    readiness: DataReadinessReport


@dataclass(frozen=True)
class OrderLogProposal:
    """What a person is asked to approve: the outcome (its definition, sentence and counts) and the build plan."""

    outcome: EventOutcomeProposal
    plan: BuildPlan
    upload_id: str

    @property
    def outcome_sentence(self) -> str:
        assert self.outcome.sentence is not None  # `propose_order_log` never returns an undecided outcome
        return self.outcome.sentence

    @property
    def outcome_hash(self) -> str:
        assert self.outcome.definition is not None
        return self.outcome.definition.definition_hash


def _fail(code: str, message: str) -> OrderLogWorkflowError:
    return OrderLogWorkflowError(code, message)


def propose_order_log(
    file: LoadedFile,
    customer_column: str,
    guests_answer: GuestsAnswer | None,
    use_case: str,
    *,
    config_root: Path | None = None,
    mixed_answer: str | None = None,
    diagnosis: FileDiagnosis | None = None,
) -> OrderLogProposal:
    """The outcome and the build plan for the order log `file`, whose shopper is `customer_column` (a person's choice
    among the reading's candidates). Builds nothing.

    `guests_answer` is needed only when some lines have no shopper (Q2); `mixed_answer` (`keep_named`) only when some
    order has lines with and lines without one. Raises :class:`OrderLogWorkflowError` when the path is off, the use
    case does not offer it, the file does not read as a whole order log, an answer is missing, stale or a stop, or the
    outcome or the plan cannot be made.

    `diagnosis` is the file's diagnosis when the caller has it (`diagnose_file`); the function makes it otherwise. Both
    read every date of the file, which is most of the time this takes on a large log."""
    config = load_use_case(use_case, config_root)
    if not order_log_enabled(config):
        raise _fail(
            "ORDER_LOG_OFF", "The order-log path is off (data_doctor.order_log needs data_doctor.unified)."
        )
    label = config.order_log_label
    roles = get_roles(config_root)
    catalogue = load_outcome_catalogue(resolve_root(config_root))
    template = catalogue.template_for(config)
    if label is None or template is None or not template.order_log:
        raise _fail("ORDER_LOG_USE_CASE", f"{config.id} does not offer the order-log path.")
    if label.window_days is None or label.type.value != "event_presence":
        raise _fail(
            "ORDER_LOG_USE_CASE", f"{config.id} names no outcome window the order-log path can measure."
        )
    frame = file.frame
    if file.file_rows is not None and file.file_rows > len(frame):
        raise _fail(
            "ORDER_LOG_SAMPLED",
            f"Only {len(frame):,} of the {file.file_rows:,} lines of {file.file_name} were read; the path rolls up "
            "the whole log.",
        )
    diagnosis = diagnosis or diagnose_file(file, config, roles)
    reading = order_log_reading(file, config, date_columns=diagnosis.date_columns)
    if reading is None:
        raise _fail("ORDER_LOG_NOT_A_LOG", f"{file.file_name} does not read as an order log.")
    if customer_column not in {c.column for c in reading.customer_candidates}:
        raise _fail(
            "ORDER_LOG_CUSTOMER_UNKNOWN",
            f"'{customer_column}' is not one of the columns that could name the shopper.",
        )
    order = reading.order_number
    if order is None:
        raise _fail(
            "ORDER_LOG_NO_ORDER_NUMBER", f"{file.file_name} has no column that looks like an order number."
        )
    columns = find_log_columns(frame, customer=customer_column, order=order.column, date=reading.date_column)
    derived = derive_customer_list(
        frame,
        customer_column,
        reading.date_column,
        order_column=order.column,
        quantity_column=columns.quantity,
    )
    _check_guests(reading, derived, guests_answer)
    _check_mixed(reading, derived, mixed_answer)
    entity, events = derived_entity(
        OutcomeFile(diagnosis, frame), derived, roles, reading, quantity_column=columns.quantity
    )
    try:
        outcome = propose_event_outcome(
            [events],
            config,
            roles,
            catalogue,
            kind="event_presence",
            derived_entity=entity,
            days=label.window_days,
            audience=config.audience,
        )
    except OutcomeError as exc:
        raise OrderLogWorkflowError(exc.code, exc.message) from exc
    if outcome.definition is None or outcome.sentence is None or outcome.blocking:
        asked = outcome.questions[0].text if outcome.questions else "the outcome is not decided"
        raise _fail("ORDER_LOG_OUTCOME_UNDECIDED", asked)
    try:
        plan = plan_build(
            Diagnosis(files=(diagnosis,), use_cases=(), links=(), stacks=()),
            {file.upload_id: frame},
            use_case=config.id,
            label_spec=label,
            order_log=OrderLogInput(file.upload_id, columns),
            suggested=config.order_log_features,
            audience=config.audience,
        )
    except BuildDriveError as exc:
        raise OrderLogWorkflowError(exc.code, exc.message) from exc
    if plan.blocking or plan.questions:
        raise _fail(
            "BUILD_PLAN_INCOMPLETE", "The plan cannot run yet: " + ", ".join(plan.blocking or ("a question",))
        )
    return OrderLogProposal(outcome=outcome, plan=plan, upload_id=file.upload_id)


def _check_guests(reading: OrderLogReading, derived: DerivedCustomers, answer: GuestsAnswer | None) -> None:
    question = blank_customer_question(reading, derived)
    if question is None:
        return
    _, _, finding = question
    if answer is None:
        raise _fail("ORDER_LOG_GUESTS_UNANSWERED", question[0].text)
    if answer.option_id == "stop":
        raise _fail("ORDER_LOG_STOPPED", "The person stopped at the lines with no shopper; nothing is built.")
    if finding is not None and answer.finding_key != finding.key:
        raise _fail(
            "ORDER_LOG_GUESTS_STALE",
            "The yes to leaving out the lines with no shopper was for another count; ask again.",
        )


def _check_mixed(reading: OrderLogReading, derived: DerivedCustomers, answer: str | None) -> None:
    asked = mixed_orders_question(reading, derived)
    if asked is None:
        return
    if answer != "keep_named":
        raise _fail("ORDER_LOG_MIXED_ORDERS", asked[0].text)


def build_from_order_log(
    file: LoadedFile,
    customer_column: str,
    guests_answer: GuestsAnswer | None,
    use_case: str,
    approvals: OrderLogApprovals,
    port: OrderLogPort,
    *,
    config_root: Path | None = None,
    mixed_answer: str | None = None,
    diagnosis: FileDiagnosis | None = None,
) -> OrderLogBuild:
    """Build the lapsed-shopper table from the order log `file`, once both approvals hold, and check it is ready.

    Re-derives the proposal (:func:`propose_order_log`; the same inputs give the same hashes and sentences), refuses an
    outcome approval that is not for this definition (`OUTCOME_NOT_APPROVED`) or this sentence
    (`APPROVAL_SENTENCE_CHANGED`), runs the approved plan through the builder (`drive_builder` refuses a plan hash or
    sentence that is not the one approved), reads the built table back and runs the readiness check on it, with the
    use case's `built_split` (time-based, on `snapshot_date`). A build that finds a blocking problem raises with that
    check's code; a table that is built but not ready is returned with its report, never hidden."""
    proposal = propose_order_log(
        file,
        customer_column,
        guests_answer,
        use_case,
        config_root=config_root,
        mixed_answer=mixed_answer,
        diagnosis=diagnosis,
    )
    outcome = approvals.outcome
    if outcome.definition_hash != proposal.outcome_hash:
        raise _fail(
            "OUTCOME_NOT_APPROVED", "This approval is not for this outcome; read it again and approve it."
        )
    if outcome.sentence != proposal.outcome_sentence:
        raise _fail(
            "APPROVAL_SENTENCE_CHANGED",
            "The outcome now reads differently from what was approved; read it again before approving.",
        )
    try:
        driven = drive_builder(
            proposal.plan, {file.upload_id: file.frame}, port, approvals.build, mode="train"
        )
        if driven.dataset_id is None:
            raise _fail("BUILD_STEP_FAILED", "The builder made no dataset.")
        built = port.read_dataset(driven.dataset_id)
    except BuildDriveError as exc:
        raise OrderLogWorkflowError(exc.code, exc.message) from exc
    config = load_use_case(use_case, config_root)
    return OrderLogBuild(
        dataset_id=driven.dataset_id,
        spec_id=driven.spec_id,
        plan_hash=proposal.plan.plan_hash,
        outcome_hash=proposal.outcome_hash,
        counts=_counts(proposal.plan, built.frame, built.manifest.target, built.manifest.snapshot_dates),
        readiness=_readiness(config, built),
    )


def _counts(
    plan: BuildPlan, frame: pd.DataFrame, target: str | None, dates: tuple[date, ...]
) -> OrderLogCounts:
    assert plan.order_log is not None and target is not None
    yes = int((frame[target] == 1).sum())
    return OrderLogCounts(
        log=plan.order_log.counts(),
        rows=len(frame),
        yes=yes,
        no=len(frame) - yes,
        snapshot_dates=tuple(dates),
    )


def _readiness(config: UseCaseConfig, built: BuiltDataset) -> DataReadinessReport:
    """`check_readiness` on the built table as a run reads it: the manifest's key and outcome, the use case's
    `built_split` in place of its `split` (several month-ends per shopper cannot be split at random), the rows capped
    as a run caps them. Nothing in the check is loosened."""
    from engine.config import RunMode
    from engine.doctor.readiness import ReadinessInputs, check_readiness
    from engine.keys import normalise_key
    from engine.stages import ingest

    if config.built_split is not None:
        config = config.model_copy(update={"split": config.built_split})
    cap = ingest.profile_row_cap(config)
    return check_readiness(
        ReadinessInputs(
            frame=built.frame.head(cap),
            config=config,
            mode=RunMode.TRAIN,
            primary_key=normalise_key(list(built.manifest.primary_key)),
            target=built.manifest.target,
            upload_id=built.manifest.dataset_id,
            row_count=len(built.frame),
            goal=config.id,
        )
    )
