"""The logging audit (plan section 13.7): stage timings and row counts at INFO, data values never.

This is a privacy property, and a property is worth only as much as the thing that enforces it. The
enforcement here is mechanical rather than by inspection: every stage that logs is driven over a
frame whose **cell values are all sentinels**, every log record the drive produces is captured, and
each one is searched - its interpolated message, its raw arguments and the line the engine's own
handler would write - for any trace of a sentinel.

What is and is not a data value, for the purpose of this file:

* **cell values are data.** No log line may carry one, on any path.
* **column names are not.** `stage=replay transform=clip column=monthly_charges skipped=not_numeric`
  is a line the engine needs and is allowed to write, so the sentinels live in the values only and
  the column names here are ordinary ones. A test that forbade column names would be testing a rule
  the plan does not state and the stages could not honour.

The failure paths matter more than the happy ones, because an exception message is where a value
leaks without anyone deciding to log it: pandas answers a bad cast with
``could not convert string to float: 'ACME-042'``, and ``logger.exception(...)`` writes that
verbatim. So each stage is driven into failure as well as through success, and
:class:`~engine.utils.logging.RedactingFormatter` - which `configure_logging` installs - is asserted
directly: it keeps a traceback's frames, which are this repository's code, and withholds every
exception message, which is not.

Phase 4a adds a second rendering, and the rule for this file is that **it earns no exemption**.
`RedactingJsonFormatter` is a subclass of `RedactingFormatter`, so :func:`renderings` formats every
captured record with *both* and every `assert_no_values_logged` in this file therefore proves the
same thing about the JSON path that it proves about the text one - by the same assertions, not by a
parallel set that could drift. The three formatter tests are parametrised over both for the same
reason. Two further properties are asserted directly, because they are what makes the guarantee
structural rather than a habit: that the subclass does not override ``formatException`` (so there is
still exactly one code path that turns an exception into text), and that its payload comes from a
closed allow-list (so an attribute attached to a record by anybody cannot be serialised).

The run identity a record carries is audited here too. It reaches the record through a `ContextVar`
and a `logging.Filter`, and a `ContextVar` is **not** inherited by a `ThreadPoolExecutor` worker, so
the tests at the end of this file drive a real `ThreadJobRunner` to prove both halves of that: a
binding made inside the job body reaches the log, a binding wrapped around the submit does not, and
two jobs running at the same time never see each other's run.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

from engine.config import ColumnType, ProblemType, RunMode, SplitType, UseCaseConfig, load_use_case_document
from engine.contracts import FeatureSchema, FeatureSchemaColumn, Severity
from engine.jobs import CancelToken, ThreadJobRunner
from engine.stages import prepare as prepare_stage
from engine.stages import validate as validate_stage
from engine.stages.actions import apply_actions
from engine.stages.ingest import profile_dataset, read_upload
from engine.storage import LocalStorage
from engine.utils import logging as engine_logging
from engine.utils.logging import (
    CONTEXT_FIELDS,
    JSON_FIELDS,
    WITHHELD,
    RedactingFormatter,
    RedactingJsonFormatter,
    bind_log_context,
    configure_logging,
    install_context_filter,
    log_failure,
    log_stage,
)
from engine.utils.time import utc_now

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

#: Every cell value in the driven frame carries this marker.
SENTINEL = "QZLEAK"
#: A numeric cell value, which cannot carry the marker; no count or duration can collide with it.
NUMBER_SENTINEL = 86753099

RUN_ID = "r_20260901_abcdef01"
UPLOAD_ID = "u_20260901_abcdef01"

_FORMATTER = RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
_JSON_FORMATTER = RedactingJsonFormatter()

#: Both renderings the engine can install. Every formatter assertion below runs against each.
FORMATTERS = pytest.mark.parametrize("formatter", [_FORMATTER, _JSON_FORMATTER], ids=["text", "json"])

#: The logger the job-runner tests write on, so their records are easy to pick out of the capture.
JOB_LOGGER = "engine.audit.jobs"


# ---------------------------------------------------------------------------
# Capturing and searching
# ---------------------------------------------------------------------------
class _Capture(logging.Handler):
    """Keeps every record it is handed, at any level."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        # The same filter `configure_logging` puts on the real handler, because a capture that did
        # not stamp the context would be auditing a record shape the engine never actually writes.
        install_context_filter(self)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def captured() -> Iterator[_Capture]:
    """Capture every record reaching the root logger while the test runs."""
    handler = _Capture()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


@pytest.fixture
def pristine_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[logging.Logger]:
    """A root logger `configure_logging` has never touched, restored afterwards.

    `configure_logging` is idempotent by remembering the one handler it owns in a module global.
    A test that swaps the root logger's handler list out from under it would leave that global
    pointing at a handler nobody can reach, so the global is reset here as well - otherwise the
    first such test would quietly break every later one.
    """
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    monkeypatch.setattr(engine_logging, "_handler", None)
    try:
        yield root
    finally:
        root.handlers = handlers
        root.setLevel(level)


def renderings(record: logging.LogRecord) -> list[str]:
    """Everything this record could put in front of a reader.

    The interpolated message, the line the engine's handler writes (which is the message plus any
    traceback), **both** renderings that handler can be wearing, and each argument on its own - an
    argument the current format string happens not to use is still a value handed to the logger.

    The JSON rendering is in this list rather than in tests of its own, so that every
    `assert_no_values_logged` in this file audits it too: whatever the text path is proved not to
    leak, the JSON path is proved not to leak by the very same assertion.
    """
    texts: list[str] = []
    for render in (
        record.getMessage,
        lambda: _FORMATTER.format(record),
        lambda: _JSON_FORMATTER.format(record),
    ):
        try:
            texts.append(render())
        except (TypeError, ValueError):
            # A record whose arguments do not match its format string is a bug in its own right;
            # the raw arguments below are still searched, so a leak cannot hide behind one.
            texts.append(str(record.msg))
    args: Any = record.args
    if isinstance(args, tuple):
        texts.extend(repr(item) for item in args)
    elif isinstance(args, dict):
        texts.extend(repr(item) for item in args.values())
    elif args is not None:
        texts.append(repr(args))
    return texts


def assert_no_values_logged(records: Sequence[logging.LogRecord]) -> None:
    """Fail naming the offending record, so the leak's owner can be sent its file and line."""
    offenders = [
        f"{record.name} ({record.pathname}:{record.lineno}): {text}"
        for record in records
        for text in renderings(record)
        if SENTINEL in text or str(NUMBER_SENTINEL) in text
    ]
    assert offenders == [], "a data value reached the log (plan 13.7):\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# The frame every stage is driven over
# ---------------------------------------------------------------------------
def sentinel_frame(rows: int = 40) -> pd.DataFrame:
    """Ordinary column names; every cell a sentinel.

    The shape is chosen to light up as much of the validation table as one frame can: a constant
    column, a mostly-empty one, an identifier-shaped one, a column that looks like personal data and
    a date column that does not parse.
    """
    return pd.DataFrame(
        {
            "customer_id": [f"{SENTINEL}-key-{index:04d}" for index in range(rows)],
            "plan_type": [f"{SENTINEL}-plan-{'a' if index % 2 else 'b'}" for index in range(rows)],
            "contact_email": [f"{SENTINEL.lower()}{index:04d}@example.invalid" for index in range(rows)],
            "signup_date": [f"{SENTINEL}-not-a-date" for _ in range(rows)],
            "notes": [f"{SENTINEL}-note-{index}" if index % 3 == 0 else None for index in range(rows)],
            "region": [f"{SENTINEL}-constant" for _ in range(rows)],
            "amount": [float(NUMBER_SENTINEL + index) for index in range(rows)],
            "converted": [index % 2 for index in range(rows)],
        }
    )


def config_for(**patch: dict[str, Any]) -> UseCaseConfig:
    """A validated use case with no template, so only the block under test differs from the defaults."""
    document = load_use_case_document("targeted-advertisement")
    document["template"] = {"columns": []}
    document["target"] = {"column": "converted", "positive_label": 1}
    document["split"] = {**document["split"], "type": "random_stratified", "time_column": None}
    for block, value in patch.items():
        current = document.get(block)
        document[block] = {**current, **value} if isinstance(current, dict) else value
    return UseCaseConfig.model_validate(document)


# ---------------------------------------------------------------------------
# The two helpers, which are the only shapes a stage is meant to log
# ---------------------------------------------------------------------------
def test_log_stage_writes_only_the_stage_the_rows_and_the_seconds(captured: _Capture) -> None:
    """The known-good line, which is also this file's own machinery checked against one."""
    log_stage(logging.getLogger("engine.audit"), "prepare", rows=40, seconds=1.5)

    (record,) = captured.records
    assert record.getMessage() == "stage=prepare rows=40 seconds=1.500"
    assert record.levelno == logging.INFO
    assert_no_values_logged(captured.records)


def test_log_failure_names_the_exception_class_and_never_its_message(captured: _Capture) -> None:
    exc = ValueError(f"could not convert string to float: '{SENTINEL}-key-0001'")

    log_failure(logging.getLogger("engine.audit"), "stage=prepare step=cast", exc)

    (record,) = captured.records
    assert record.getMessage() == "stage=prepare step=cast error=ValueError"
    assert_no_values_logged(captured.records)


# ---------------------------------------------------------------------------
# The sink: what the engine's handler writes when an exception is logged
# ---------------------------------------------------------------------------
@FORMATTERS
def test_the_formatter_keeps_the_frames_and_withholds_the_message(
    captured: _Capture, formatter: logging.Formatter
) -> None:
    try:
        raise ValueError(f"could not convert string to float: '{SENTINEL}-key-0001'")
    except ValueError:
        logging.getLogger("engine.audit").exception("stage=prepare failed")

    (record,) = captured.records
    rendered = formatter.format(record)
    assert SENTINEL not in rendered
    assert "ValueError" in rendered
    assert WITHHELD in rendered
    assert "test_logging_audit.py" in rendered, "the frames are the diagnostic; only the message goes"
    assert_no_values_logged(captured.records)


@FORMATTERS
def test_the_formatter_withholds_the_message_of_every_exception_in_a_chain(
    captured: _Capture, formatter: logging.Formatter
) -> None:
    try:
        try:
            raise ValueError(f"bad cell {SENTINEL}-key-0001")
        except ValueError as inner:
            raise RuntimeError(f"while preparing {SENTINEL}-key-0001") from inner
    except RuntimeError:
        logging.getLogger("engine.audit").exception("stage=prepare failed")

    rendered = formatter.format(captured.records[0])
    assert SENTINEL not in rendered
    assert rendered.count(WITHHELD) == 2, "both links of the chain carry a message"
    assert "ValueError" in rendered
    assert "RuntimeError" in rendered
    assert "the direct cause" in rendered


@FORMATTERS
def test_the_formatter_ignores_a_rendering_another_formatter_cached(
    captured: _Capture, formatter: logging.Formatter
) -> None:
    try:
        raise ValueError(f"could not convert string to float: '{SENTINEL}-key-0001'")
    except ValueError:
        logging.getLogger("engine.audit").exception("stage=prepare failed")

    record = captured.records[0]
    logging.Formatter().format(record)
    assert SENTINEL in (record.exc_text or ""), "the stock formatter caches the full traceback"

    assert SENTINEL not in formatter.format(record)


# ---------------------------------------------------------------------------
# The JSON rendering: the same guarantee, made structural rather than repeated
# ---------------------------------------------------------------------------
def test_the_json_formatter_inherits_the_one_path_that_redacts_an_exception() -> None:
    """The guarantee is a class, not a convention.

    If `RedactingJsonFormatter` ever defines its own `formatException` - "just to put the traceback
    in a field of its own" - then this repository has two ways of turning an exception into text and
    only one of them is audited. `vars()` rather than `getattr`, because an inherited attribute
    would make the second assertion pass no matter what.
    """
    assert issubclass(RedactingJsonFormatter, RedactingFormatter)
    assert "formatException" not in vars(RedactingJsonFormatter), (
        "the JSON formatter must NOT override formatException: the inherited one is the only code "
        "path in this repository that turns an exception into text, and it withholds the message"
    )
    assert RedactingJsonFormatter.formatException is RedactingFormatter.formatException


def test_the_json_formatter_serialises_no_attribute_anyone_attached_to_the_record(
    captured: _Capture,
) -> None:
    """A closed allow-list, proved by attaching to a record exactly what a leak would look like."""
    logging.getLogger("engine.audit").info(
        "stage=prepare rows=40 seconds=1.500",
        extra={"offending_cell": f"{SENTINEL}-key-0001", "sample": {"email": SENTINEL}},
    )

    (record,) = captured.records
    assert record.offending_cell.startswith(SENTINEL), "the value really is on the record"

    payload = json.loads(_JSON_FORMATTER.format(record))
    assert set(payload) <= set(JSON_FIELDS), f"an unexpected key was serialised: {sorted(payload)}"
    assert "offending_cell" not in payload
    assert_no_values_logged(captured.records)


def test_the_json_formatter_writes_the_bound_identity_and_the_same_message(captured: _Capture) -> None:
    with bind_log_context(run_id=RUN_ID, stage="prepare", client_id="acme"):
        log_stage(logging.getLogger("engine.audit"), "prepare", rows=40, seconds=1.5)

    (record,) = captured.records
    payload = json.loads(_JSON_FORMATTER.format(record))
    assert payload["message"] == record.getMessage(), "both renderings say the same thing"
    assert payload["run_id"] == RUN_ID
    assert payload["stage"] == "prepare"
    assert payload["client_id"] == "acme"
    assert "exception" not in payload, "a line with no exception carries no empty exception key"
    assert_no_values_logged(captured.records)


def test_configure_logging_installs_the_redacting_formatter(pristine_root: logging.Logger) -> None:
    configure_logging("INFO")
    assert any(
        isinstance(handler.formatter, RedactingFormatter) for handler in pristine_root.handlers
    ), "the console handler must be the one that withholds exception messages"


def test_configure_logging_json_installs_the_json_formatter_without_a_second_handler(
    pristine_root: logging.Logger,
) -> None:
    """The format is a choice about one handler, not a reason to grow a second one."""
    configure_logging("INFO")
    before = len(pristine_root.handlers)

    configure_logging("INFO", log_format="json")

    assert len(pristine_root.handlers) == before
    # Only the handler this module owns is asserted on: pytest puts handlers of its own on the root
    # logger, and this test is about the engine's, not about everyone else's.
    assert isinstance(engine_logging._handler.formatter, RedactingJsonFormatter)
    assert engine_logging._handler in pristine_root.handlers


def test_configure_logging_installs_exactly_one_context_filter(pristine_root: logging.Logger) -> None:
    configure_logging("INFO")
    configure_logging("DEBUG", log_format="json")
    configure_logging("INFO")

    stamping = [
        filter_
        for handler in pristine_root.handlers
        for filter_ in handler.filters
        if isinstance(filter_, engine_logging.ContextFilter)
    ]
    assert len(stamping) == 1, "repeated calls must not leave a filter each, stamping the same record"


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------
def test_ingest_logs_nothing_from_the_file(captured: _Capture, tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = f"uploads/{UPLOAD_ID}/data.csv"
    storage.write_bytes(key, sentinel_frame().to_csv(index=False).encode("utf-8"))

    result = read_upload(storage, key)
    profile = profile_dataset(
        result.frame,
        config_for(),
        upload_id=UPLOAD_ID,
        file_name="data.csv",
        file_format=result.file_format,
        file_size_bytes=storage.size_bytes(key),
        delimiter=result.delimiter,
        encoding=result.encoding,
        row_count=result.row_count,
        fingerprint=result.fingerprint,
    )

    assert profile.row_count == 40
    assert captured.records, "ingest logs its stage line, so there is something to audit"
    assert_no_values_logged(captured.records)


def test_ingest_logs_an_encoding_retry_by_encoding_name_only(captured: _Capture, tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = f"uploads/{UPLOAD_ID}/latin.csv"
    frame = sentinel_frame(rows=4)
    frame["plan_type"] = [f"{SENTINEL}-plan-café" for _ in range(len(frame))]
    storage.write_bytes(key, frame.to_csv(index=False).encode("latin-1"))

    read_upload(storage, key)

    assert_no_values_logged(captured.records)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def train_params() -> validate_stage.CheckParams:
    return validate_stage.CheckParams(
        primary_key="customer_id",
        target="converted",
        time_column="signup_date",
        consent_column="consent_flag",
        opt_out_column="opted_out",
        split_type=SplitType.TIME_BASED,
    )


def test_validate_logs_nothing_from_the_frame(captured: _Capture) -> None:
    report = validate_stage.validate_frame(
        sentinel_frame(), train_params(), mode=RunMode.TRAIN, upload_id=UPLOAD_ID
    )

    codes = {check.code for check in report.checks}
    assert {
        "ROWS_TOO_FEW",
        "TARGET_TOO_FEW_POSITIVES",
        "TIME_COLUMN_UNPARSEABLE",
        "HIGH_NULL_COLUMN",
        "CONSTANT_COLUMN",
        "PII_DETECTED",
        "CONSENT_COLUMN_MISSING",
        "SUPPRESSION_COLUMN_MISSING",
    } <= codes, f"the frame must exercise the table; it raised {sorted(codes)}"
    assert_no_values_logged(captured.records)


def test_validate_against_a_schema_logs_nothing_from_the_frame(captured: _Capture) -> None:
    schema = FeatureSchema(
        use_case_id="targeted-advertisement",
        model_version_id="m_20260901_abcdef01",
        primary_key="customer_id",
        target="converted",
        problem_type=ProblemType.BINARY_CLASSIFICATION,
        columns=(
            FeatureSchemaColumn(name="tenure_months", inferred_type=ColumnType.INTEGER),
            FeatureSchemaColumn(name="amount", inferred_type=ColumnType.STRING),
        ),
        row_count_at_fit=1000,
        created_at=utc_now(),
    )

    report = validate_stage.validate_frame(
        sentinel_frame(),
        validate_stage.CheckParams(primary_key="customer_id", schema=schema),
        mode=RunMode.SCORE,
        upload_id=UPLOAD_ID,
    )

    assert "SCHEMA_MISMATCH" in {check.code for check in report.checks}
    assert_no_values_logged(captured.records)


def test_a_check_that_raises_is_logged_as_its_class_and_not_its_message(
    captured: _Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(frame: pd.DataFrame, params: validate_stage.CheckParams) -> validate_stage.CheckResult:
        raise ValueError(f"could not convert string to float: '{SENTINEL}-key-0001'")

    spec = validate_stage.CheckSpec(
        code="PK_MISSING",
        fn=explode,
        severity=Severity.ERROR,
        modes=frozenset({RunMode.TRAIN}),
        order=0,
        acknowledgeable=False,
        needs_target=False,
    )
    monkeypatch.setattr(validate_stage, "CHECK_REGISTRY", (spec,))

    checks = validate_stage.run_checks(
        sentinel_frame(), validate_stage.CheckParams(primary_key="customer_id"), mode=RunMode.TRAIN
    )

    assert checks == (), "a check that raises contributes no finding"
    (record,) = [item for item in captured.records if item.name.endswith("validate")]
    assert record.getMessage() == "validation.check_failed code=PK_MISSING error=ValueError"
    assert_no_values_logged(captured.records)


# ---------------------------------------------------------------------------
# prepare, split and replay
# ---------------------------------------------------------------------------
def test_prepare_and_split_log_nothing_from_the_frame(captured: _Capture) -> None:
    config = config_for()
    rows, plan = prepare_stage.prepare_rows(
        sentinel_frame(), config, primary_key="customer_id", target="converted"
    )
    parts, split = prepare_stage.split_dataset(rows, config, run_id=RUN_ID, target="converted")
    frame, _report = prepare_stage.fit_transforms(
        rows, config, plan, run_id=RUN_ID, fit_index=parts["train"].index
    )

    assert len(frame.index) == len(rows.index)
    assert split.type is SplitType.RANDOM_STRATIFIED
    assert captured.records, "prepare and split log their stage lines"
    assert_no_values_logged(captured.records)


def test_replay_skipping_a_transform_logs_the_column_and_no_value(captured: _Capture) -> None:
    config = config_for(prepare={"pii_handling": "redact", "outliers": "clip"})
    frame = sentinel_frame()
    _rows, report = prepare_stage.prepare(
        frame, config, run_id=RUN_ID, primary_key="customer_id", target="converted"
    )

    scoring = frame.drop(columns=["contact_email"])
    scoring["amount"] = [f"{SENTINEL}-not-a-number" for _ in range(len(scoring))]
    prepare_stage.replay(scoring, report)

    messages = [record.getMessage() for record in captured.records if "stage=replay" in record.getMessage()]
    assert messages, "replay must say which transform it skipped and on which column"
    assert_no_values_logged(captured.records)


def test_a_split_that_cannot_be_made_logs_no_value(captured: _Capture) -> None:
    """The time-based split raises when its column is absent; the column name is all it may say."""
    config = config_for(split={"type": "time_based", "time_column": "signup_date"})
    frame = sentinel_frame().drop(columns=["signup_date"])

    with pytest.raises(ValueError, match="signup_date"):
        prepare_stage.split_dataset(frame, config, run_id=RUN_ID, target="converted")

    assert_no_values_logged(captured.records)


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------
def test_actions_logs_counts_and_rule_codes_only(captured: _Capture) -> None:
    config = config_for()
    frame = sentinel_frame()
    frame[config.actions.score_field] = [index / 100 for index in range(len(frame))]

    result = apply_actions(frame, config, run_id=RUN_ID, primary_key="customer_id")

    assert len(result.index) == len(frame.index)
    assert captured.records, "actions logs its rule counts and its stage line"
    assert_no_values_logged(captured.records)


def test_actions_refusing_a_frame_logs_no_value(captured: _Capture) -> None:
    with pytest.raises(ValueError, match="missing"):
        apply_actions(sentinel_frame(), config_for(), run_id=RUN_ID, primary_key="customer_id")

    assert_no_values_logged(captured.records)


# ---------------------------------------------------------------------------
# The job runner, which logs whatever escapes a run
# ---------------------------------------------------------------------------
def test_a_failing_job_writes_no_value_to_the_log(captured: _Capture) -> None:
    runner = ThreadJobRunner(max_workers=1)

    def explode(token: CancelToken) -> None:
        raise ValueError(f"could not convert string to float: '{SENTINEL}-key-0001'")

    try:
        runner.submit("job_audit_1", explode)
        info = runner.wait("job_audit_1", timeout=10)
    finally:
        runner.shutdown()

    assert info.state.value == "failed"
    assert_no_values_logged(captured.records)


# ---------------------------------------------------------------------------
# A run's identity, and the thread boundary it does not cross by itself
# ---------------------------------------------------------------------------
def _job_that_logs(run_id: str | None, barrier: threading.Barrier | None = None) -> Any:
    """A job body that logs one stage line, optionally binding a run inside itself first."""

    def run(token: CancelToken) -> None:
        token.raise_if_cancelled()
        if run_id is None:
            if barrier is not None:
                barrier.wait(timeout=10)
            log_stage(logging.getLogger(JOB_LOGGER), "train", rows=None, seconds=0.0)
            return
        with bind_log_context(run_id=run_id, stage="train"):
            if barrier is not None:
                # Both jobs are inside their own binding at the same instant, which is the only
                # arrangement under which one leaking into the other would be visible.
                barrier.wait(timeout=10)
            log_stage(logging.getLogger(JOB_LOGGER), "train", rows=None, seconds=0.0)

    return run


def _job_records(captured: _Capture) -> list[logging.LogRecord]:
    return [record for record in captured.records if record.name == JOB_LOGGER]


def test_a_binding_made_inside_the_job_body_reaches_the_log(captured: _Capture) -> None:
    runner = ThreadJobRunner(max_workers=1)
    try:
        runner.submit("job_ctx_inside", _job_that_logs(RUN_ID))
        info = runner.wait("job_ctx_inside", timeout=10)
    finally:
        runner.shutdown()

    assert info.state.value == "done"
    (record,) = _job_records(captured)
    assert record.run_id == RUN_ID
    assert record.stage == "train"
    assert record.client_id is None, "an unbound field is None, never a stale value"
    assert_no_values_logged(captured.records)


def test_a_binding_wrapped_around_the_submit_does_not_reach_the_worker(captured: _Capture) -> None:
    """The reason `bind_log_context` has to be the job body's first statement.

    `ThreadPoolExecutor` does not copy the submitting context into its workers, so this is not a
    style preference: a binding around the submit is silently lost, and the run whose log lines it
    was supposed to identify writes them anonymously.
    """
    runner = ThreadJobRunner(max_workers=1)
    try:
        with bind_log_context(run_id=RUN_ID, client_id="acme"):
            runner.submit("job_ctx_outside", _job_that_logs(None))
        runner.wait("job_ctx_outside", timeout=10)
    finally:
        runner.shutdown()

    (record,) = _job_records(captured)
    assert record.run_id is None, (
        "if this ever passes with the run id set, ThreadPoolExecutor has started copying "
        "contextvars and the binding may move back around the submit - until then it may not"
    )
    assert_no_values_logged(captured.records)


def test_two_concurrent_jobs_never_see_each_others_run_id(captured: _Capture) -> None:
    runner = ThreadJobRunner(max_workers=2)
    barrier = threading.Barrier(2)
    first, second = "r_20260901_aaaaaaaa", "r_20260901_bbbbbbbb"
    try:
        runner.submit("job_ctx_first", _job_that_logs(first, barrier))
        runner.submit("job_ctx_second", _job_that_logs(second, barrier))
        states = {
            runner.wait("job_ctx_first", timeout=10).state.value,
            runner.wait("job_ctx_second", timeout=10).state.value,
        }
    finally:
        runner.shutdown()

    assert states == {"done"}, "both jobs must have reached the barrier and logged"
    records = _job_records(captured)
    assert len(records) == 2
    assert {record.run_id for record in records} == {first, second}
    assert all(record.stage == "train" for record in records)
    assert_no_values_logged(captured.records)


def test_the_binding_is_restored_when_a_nested_block_leaves() -> None:
    """Nesting merges and unwinds; a stage cannot clear the run it belongs to by not naming it."""
    assert engine_logging.current_log_context() == {}
    with bind_log_context(run_id=RUN_ID, client_id="acme"):
        with bind_log_context(stage="prepare"):
            assert engine_logging.current_log_context() == {
                "run_id": RUN_ID,
                "client_id": "acme",
                "stage": "prepare",
            }
        assert engine_logging.current_log_context() == {"run_id": RUN_ID, "client_id": "acme"}
    assert engine_logging.current_log_context() == {}


def test_the_context_filter_overwrites_an_identity_the_caller_attached(captured: _Capture) -> None:
    """One mechanism, not two: the ContextVar is the only source of a run's identity."""
    logging.getLogger("engine.audit").info("stage=prepare rows=40 seconds=1.500", extra={"run_id": "forged"})

    (record,) = captured.records
    assert record.run_id is None
    assert set(CONTEXT_FIELDS) <= set(vars(record)), "every field is stamped, present or None"
