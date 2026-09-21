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
"""

from __future__ import annotations

import logging
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
from engine.utils.logging import (
    WITHHELD,
    RedactingFormatter,
    configure_logging,
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


# ---------------------------------------------------------------------------
# Capturing and searching
# ---------------------------------------------------------------------------
class _Capture(logging.Handler):
    """Keeps every record it is handed, at any level."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

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


def renderings(record: logging.LogRecord) -> list[str]:
    """Everything this record could put in front of a reader.

    The interpolated message, the line the engine's handler writes (which is the message plus any
    traceback), and each argument on its own - an argument the current format string happens not to
    use is still a value handed to the logger.
    """
    texts: list[str] = []
    for render in (record.getMessage, lambda: _FORMATTER.format(record)):
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
def test_the_formatter_keeps_the_frames_and_withholds_the_message(captured: _Capture) -> None:
    try:
        raise ValueError(f"could not convert string to float: '{SENTINEL}-key-0001'")
    except ValueError:
        logging.getLogger("engine.audit").exception("stage=prepare failed")

    (record,) = captured.records
    rendered = _FORMATTER.format(record)
    assert SENTINEL not in rendered
    assert "ValueError" in rendered
    assert WITHHELD in rendered
    assert "test_logging_audit.py" in rendered, "the frames are the diagnostic; only the message goes"
    assert_no_values_logged(captured.records)


def test_the_formatter_withholds_the_message_of_every_exception_in_a_chain(captured: _Capture) -> None:
    try:
        try:
            raise ValueError(f"bad cell {SENTINEL}-key-0001")
        except ValueError as inner:
            raise RuntimeError(f"while preparing {SENTINEL}-key-0001") from inner
    except RuntimeError:
        logging.getLogger("engine.audit").exception("stage=prepare failed")

    rendered = _FORMATTER.format(captured.records[0])
    assert SENTINEL not in rendered
    assert rendered.count(WITHHELD) == 2, "both links of the chain carry a message"
    assert "ValueError" in rendered
    assert "RuntimeError" in rendered
    assert "the direct cause" in rendered


def test_the_formatter_ignores_a_rendering_another_formatter_cached(captured: _Capture) -> None:
    try:
        raise ValueError(f"could not convert string to float: '{SENTINEL}-key-0001'")
    except ValueError:
        logging.getLogger("engine.audit").exception("stage=prepare failed")

    record = captured.records[0]
    logging.Formatter().format(record)
    assert SENTINEL in (record.exc_text or ""), "the stock formatter caches the full traceback"

    assert SENTINEL not in _FORMATTER.format(record)


def test_configure_logging_installs_the_redacting_formatter() -> None:
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        configure_logging("INFO")
        assert any(
            isinstance(handler.formatter, RedactingFormatter) for handler in root.handlers
        ), "the console handler must be the one that withholds exception messages"
    finally:
        root.handlers = handlers
        root.setLevel(level)


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
